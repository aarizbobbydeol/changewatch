import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
import asyncpg
import httpx
from app.config import settings
from app.db import get_connection

logger = logging.getLogger("changewatch.outbox")


async def queue_notification(
    conn: asyncpg.Connection,
    project_id: int,
    dedupe_key: str,
    payload: Dict[str, Any],
    channel_id: Optional[int] = None,
) -> bool:
    """
    Queue an alert notification in the durable outbox within the caller's transaction.
    Returns True if newly queued, False if deduplicated.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO outbox (project_id, channel_id, dedupe_key, payload, next_attempt_at, status)
        VALUES ($1, $2, $3, $4::jsonb, now(), 'pending')
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        project_id,
        channel_id,
        dedupe_key,
        json.dumps(payload),
    )
    if row:
        logger.info(
            "Notification queued in outbox: project=%d, dedupe_key=%s, outbox_id=%d",
            project_id,
            dedupe_key,
            row["id"],
        )
        return True
    else:
        logger.debug(
            "Notification deduplicated by key %s; not re-queued",
            dedupe_key,
        )
        return False


def annotate_delayed_message(payload: Dict[str, Any], delay_sec: float) -> Dict[str, Any]:
    """Annotate message if delivery was delayed by Slack / channel outages."""
    annotated = dict(payload)
    if delay_sec >= 60.0:
        delay_min = int(delay_sec // 60)
        annotation = f"[delayed by {delay_min} min (channel was unreachable)]"

        # If payload has text or title, prepend or append annotation
        if "text" in annotated:
            annotated["text"] = f"{annotation} {annotated['text']}"
        elif "summary" in annotated:
            annotated["summary"] = f"{annotation} {annotated['summary']}"
        else:
            annotated["_delivery_delay_note"] = annotation

    return annotated


async def _default_send(project_id: int, payload: Dict[str, Any]) -> None:
    """Send payload to project's configured Slack webhook if present."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT slack_webhook_url FROM projects WHERE id = $1",
            project_id,
        )
    if not row or not row["slack_webhook_url"]:
        logger.warning("Project %d has no webhook URL configured; marking notification delivered", project_id)
        return

    url = row["slack_webhook_url"]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()


async def process_outbox_item(
    conn: asyncpg.Connection,
    item: asyncpg.Record,
    sender_func: Optional[Callable[[int, Dict[str, Any]], Any]] = None,
) -> bool:
    """Process a single outbox row with retry backoff and expiration."""
    item_id = item["id"]
    project_id = item["project_id"]
    attempts = item["attempts"]
    created_at = item["created_at"]
    payload = item["payload"] if isinstance(item["payload"], dict) else json.loads(item["payload"])

    now = datetime.now(timezone.utc)
    delay_sec = (now - created_at).total_seconds()
    max_age_sec = settings.OUTBOX_MAX_AGE_HOURS * 3600.0

    send_fn = sender_func or _default_send
    annotated_payload = annotate_delayed_message(payload, delay_sec)

    try:
        res = send_fn(project_id, annotated_payload)
        if asyncio.iscoroutine(res):
            await res

        # Mark sent
        await conn.execute(
            """
            UPDATE outbox
            SET status = 'sent', last_error = NULL
            WHERE id = $1
            """,
            item_id,
        )
        logger.info("Outbox message %d delivered successfully for project %d", item_id, project_id)
        return True

    except Exception as exc:
        new_attempts = attempts + 1
        err_msg = str(exc)[:500]
        logger.warning(
            "Outbox delivery failed for item %d (attempt %d): %s",
            item_id,
            new_attempts,
            err_msg,
        )

        if delay_sec >= max_age_sec or new_attempts >= 10:
            # Exceeded maximum age or attempt budget
            await conn.execute(
                """
                UPDATE outbox
                SET status = 'failed', attempts = $1, last_error = $2
                WHERE id = $3
                """,
                new_attempts,
                f"Expired after {int(delay_sec // 60)}m: {err_msg}",
                item_id,
            )
            logger.error("Outbox item %d permanently failed", item_id)
        else:
            # Exponential backoff: 10s, 20s, 40s, 80s ... capped at 1 hour
            backoff_sec = min(3600.0, (2 ** new_attempts) * 10.0)
            next_attempt_at = now + timedelta(seconds=backoff_sec)
            await conn.execute(
                """
                UPDATE outbox
                SET attempts = $1, next_attempt_at = $2, last_error = $3
                WHERE id = $4
                """,
                new_attempts,
                next_attempt_at,
                err_msg,
                item_id,
            )

        return False


async def deliver_outbox_batch(
    limit: int = 50,
    sender_func: Optional[Callable[[int, Dict[str, Any]], Any]] = None,
) -> int:
    """Deliver a batch of pending outbox messages."""
    delivered = 0
    async with get_connection() as conn:
        items = await conn.fetch(
            """
            SELECT id, project_id, channel_id, dedupe_key, payload, attempts, created_at
            FROM outbox
            WHERE status = 'pending' AND next_attempt_at <= now()
            ORDER BY next_attempt_at ASC
            LIMIT $1
            """,
            limit,
        )

        for item in items:
            success = await process_outbox_item(conn, item, sender_func)
            if success:
                delivered += 1

    return delivered
