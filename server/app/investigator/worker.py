"""Background Investigator Worker Process (Milestone 5 Stage C).

Consumes investigation jobs from Redis queue 'investigations', checks budget limits,
executes read-only AI investigation, and records usage metering.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import asyncpg

from app.db import close_db_pool, init_db_pool, project_transaction
from app.investigator.agent import run_investigation
from app.investigator.schemas import InvestigationOutput
from app.metering import Meter, can_spend
from app.outbox import queue_notification
from app.queue import close_redis, init_redis

logger = logging.getLogger("changewatch.investigator.worker")

INVESTIGATIONS_QUEUE = "investigations"
INVESTIGATIONS_PROCESSING = "investigations:processing"
INVESTIGATIONS_DEAD = "investigations:dead"
MAX_INVESTIGATION_TRIES = 3
HEARTBEAT_KEY = "heartbeat:investigator"


async def execute_investigation(
    conn: asyncpg.Connection,
    project_id: int,
    incident_id: int,
    deep_mode: bool = False,
) -> Optional[InvestigationOutput]:
    """Execute an investigation job under project RLS, enforcing budget limits."""
    # 1. Budget check (Safety floor is never paused, but AI investigation is an optional extra)
    meter = Meter.AI_INVESTIGATION_DEEP if deep_mode else Meter.AI_INVESTIGATION
    allowed, reason = await can_spend(conn, project_id, meter)
    if not allowed:
        logger.info("AI investigation paused for project %d: budget hard limit reached (%s)", project_id, reason)
        # Record paused status in investigations table
        now = datetime.now(timezone.utc)
        paused_res = {
            "summary": "AI investigation paused: budget limit reached",
            "what_broke": "Investigation paused",
            "where": {"file": "unknown", "line": 1, "function": "unknown"},
            "cause": "Budget hard limit reached for this billing period.",
            "evidence": [],
            "suggested_fix": {"description": "Review budget settings or upgrade plan to resume AI investigations.", "rollback_recommended": False},
            "confidence": "low",
            "not_sure_reason": f"Budget limit reached ({reason}).",
        }
        inv_row = await conn.fetchrow(
            """
            INSERT INTO investigations (
                incident_id, project_id, status, model, started_at, finished_at,
                steps, result, input_tokens, output_tokens, not_sure_reason
            )
            VALUES ($1, $2, 'paused', 'none', $3, $3, '[]', $4, 0, 0, $5)
            RETURNING id
            """,
            incident_id,
            project_id,
            now,
            json.dumps(paused_res),
            f"Budget limit reached ({reason})",
        )
        paused_id = inv_row["id"] if inv_row else 0
        await queue_notification(
            conn,
            project_id=project_id,
            dedupe_key=f"investigation_paused:{incident_id}:{paused_id}",
            payload={
                "type": "investigation_paused",
                "incident_id": incident_id,
                "title": "AI investigation paused: budget reached",
                "body": "AI investigation for this incident was paused because your project's monthly budget hard limit has been reached. Instant alerts, verdicts, and monitoring continue as normal.",
            },
        )
        return None

    # 2. Run investigation
    return await run_investigation(
        conn=conn,
        project_id=project_id,
        incident_id=incident_id,
        deep_mode=deep_mode,
    )


async def process_next_investigation(redis_client, timeout: int = 2) -> bool:
    """Pull one job from Redis queue and process it. Returns True if a job was processed.

    Jobs are moved to a processing list and removed only after they finish, so a crash
    never loses one; failures are retried MAX_INVESTIGATION_TRIES times, then dead-lettered.
    """
    # incident_manager LPUSHes jobs, so the oldest job is on the right
    raw_payload = await redis_client.blmove(INVESTIGATIONS_QUEUE, INVESTIGATIONS_PROCESSING, timeout, "RIGHT", "LEFT")
    if not raw_payload:
        return False

    data: Dict[str, Any] = {}
    try:
        data = json.loads(raw_payload)
        project_id = int(data["project_id"])
        incident_id = int(data["incident_id"])
        deep_mode = bool(data.get("deep_mode", False))

        logger.info("Processing investigation job: project=%d incident=%d deep=%s", project_id, incident_id, deep_mode)
        async with project_transaction(project_id) as conn:
            await execute_investigation(conn, project_id, incident_id, deep_mode=deep_mode)
        await redis_client.lrem(INVESTIGATIONS_PROCESSING, 1, raw_payload)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        tries = int(data.get("_tries", 0)) + 1 if isinstance(data, dict) else MAX_INVESTIGATION_TRIES
        logger.error("Investigation job failed (attempt %d/%d): %s", tries, MAX_INVESTIGATION_TRIES, exc, exc_info=True)
        target = INVESTIGATIONS_QUEUE if tries < MAX_INVESTIGATION_TRIES else INVESTIGATIONS_DEAD
        retry_payload = json.dumps({**data, "_tries": tries}) if isinstance(data, dict) and data else raw_payload
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.lrem(INVESTIGATIONS_PROCESSING, 1, raw_payload)
            pipe.lpush(target, retry_payload)
            await pipe.execute()
        return False


async def requeue_interrupted_investigations(redis_client) -> int:
    """On startup, return jobs left in the processing list by a crashed run (single investigator)."""
    moved = 0
    while await redis_client.rpoplpush(INVESTIGATIONS_PROCESSING, INVESTIGATIONS_QUEUE):
        moved += 1
    if moved:
        logger.warning("Requeued %d interrupted investigation job(s)", moved)
    return moved


async def run_investigator_worker():
    """Main investigator worker loop with heartbeats and signal handling."""
    logger.info("Starting ChangeWatch AI SRE Investigator worker...")
    from app.llm import log_llm_privacy_notice
    log_llm_privacy_notice()
    await init_db_pool()
    redis_client = await init_redis()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await requeue_interrupted_investigations(redis_client)
        while not stop_event.is_set():
            # Update heartbeat
            try:
                await redis_client.set(HEARTBEAT_KEY, str(time.time()), ex=60)
            except Exception as hb_exc:
                logger.debug("Failed writing investigator heartbeat: %s", hb_exc)

            # Process job
            try:
                await process_next_investigation(redis_client, timeout=2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redis unavailable: back off instead of spinning
                logger.warning("Investigator queue unavailable: %s", exc)
                await asyncio.sleep(2)
            await asyncio.sleep(0.01)
    finally:
        logger.info("Shutting down investigator worker...")
        await close_redis()
        await close_db_pool()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run_investigator_worker())


if __name__ == "__main__":
    main()
