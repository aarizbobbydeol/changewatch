"""Periodic jobs run by the detector leader: escalation, calm auto-resolve, partitions,
retention and the weekly digest (brief §5: the detector owns "auto-resolve, retention, digests").

Every job is idempotent (outbox dedupe keys, IF NOT EXISTS, set-based deletes), so a
restart or a second detector taking over never sends anything twice.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import asyncpg

from app.config import settings
from app.db import project_transaction
from app.outbox import queue_notification

logger = logging.getLogger("changewatch.jobs")

MAINTENANCE_INTERVAL_SEC = 3600.0
PARTITION_WEEKS_AHEAD = 4
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_last_maintenance = 0.0


async def run_escalations(conn: asyncpg.Connection, now_dt: Optional[datetime] = None) -> int:
    """Escalate unacknowledged confirmed incidents and tell the team (brief: at most twice, 10 min apart)."""
    from app.trust.calm import ESCALATE_AFTER_MIN, run_escalation_checks

    escalated = await run_escalation_checks(conn, now_dt=now_dt)
    for item in escalated:
        minutes = ESCALATE_AFTER_MIN * item["level"]
        await queue_notification(
            conn,
            project_id=item["project_id"],
            dedupe_key=f"escalation:{item['incident_id']}:{item['level']}",
            payload={
                "type": "incident_escalated",
                "incident_id": item["incident_id"],
                "title": f"Still unacknowledged after {minutes} min: {item['title']}",
                "body": "Nobody has acknowledged this incident yet. Reply \"I'm on it\" from the alert or the dashboard.",
                "level": item["level"],
            },
        )
    return len(escalated)


async def run_auto_resolve(conn: asyncpg.Connection, now_dt: Optional[datetime] = None) -> int:
    """Resolve quiet incidents only while the app still sends traffic, and say so in the thread."""
    from app.trust.calm import run_calm_auto_resolve

    resolved = await run_calm_auto_resolve(conn, now_dt=now_dt)
    for item in resolved:
        title = await conn.fetchval("SELECT title FROM incidents WHERE id = $1", item["incident_id"]) or "Incident"
        note = item.get("fix_note") or "no new errors while traffic kept flowing"
        await queue_notification(
            conn,
            project_id=item["project_id"],
            dedupe_key=f"incident:{item['incident_id']}:resolved",
            payload={
                "type": "incident_resolved",
                "incident_id": item["incident_id"],
                "title": f"✅ Resolved: {title}",
                "body": f"Resolved automatically: {note}.",
            },
        )
    return len(resolved)


async def ensure_event_partitions(conn: asyncpg.Connection, weeks_ahead: int = PARTITION_WEEKS_AHEAD) -> List[str]:
    """Create weekly `events` partitions for the coming weeks, exactly as migration 009 does.

    Week bounds come from Postgres (date_trunc('week', now()) in the server's timezone) so
    they line up with the partitions 009 created. Only weeks starting after now: Postgres
    refuses to attach a range the default partition already holds rows for.
    """
    created = []
    for i in range(1, weeks_ahead + 1):
        row = await conn.fetchrow(
            """
            SELECT 'events_' || to_char(s, 'YYYY_MM_DD') AS name, s::text AS start_ts,
                   (s + INTERVAL '1 week')::text AS end_ts
            FROM (SELECT date_trunc('week', now()) + make_interval(weeks => $1::int) AS s) w
            """,
            i,
        )
        if await conn.fetchval("SELECT 1 FROM pg_class WHERE relname = $1", row["name"]):
            continue
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {row['name']} PARTITION OF events "
            f"FOR VALUES FROM ('{row['start_ts']}') TO ('{row['end_ts']}')"
        )
        created.append(row["name"])
    if created:
        logger.info("Created event partitions: %s", ", ".join(created))
    return created


async def run_maintenance(pool: asyncpg.Pool, force: bool = False) -> Dict[str, Any]:
    """Hourly: partitions for the coming weeks, then retention (events and code)."""
    global _last_maintenance
    if not force and time.monotonic() - _last_maintenance < MAINTENANCE_INTERVAL_SEC and _last_maintenance:
        return {}
    _last_maintenance = time.monotonic()

    from app.retention import cleanup_old_records

    result: Dict[str, Any] = {}
    async with pool.acquire() as conn:
        result["partitions_created"] = await ensure_event_partitions(conn)
    result["retention"] = await cleanup_old_records(
        pool, retention_days=settings.RETENTION_DAYS, code_retention_days=settings.CODE_RETENTION_DAYS
    )
    return result


def digest_due(now_dt: datetime) -> Optional[str]:
    """The ISO week key if the weekly digest is due now (at or after DIGEST_DAY DIGEST_HOUR), else None."""
    try:
        tz = ZoneInfo(settings.DIGEST_TIMEZONE)
    except Exception:
        tz = timezone.utc
    local = now_dt.astimezone(tz)
    day = _DAYS.index(settings.DIGEST_DAY) if settings.DIGEST_DAY in _DAYS else 0
    if local.weekday() != day or local.hour < settings.DIGEST_HOUR:
        return None
    iso = local.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


async def send_weekly_digests(pool: asyncpg.Pool, now_dt: Optional[datetime] = None) -> int:
    """Queue one digest per project and environment that deployed this week (dedupe per ISO week)."""
    from app.trust.digest import generate_weekly_digest

    current = now_dt or datetime.now(timezone.utc)
    week_key = digest_due(current)
    if not week_key:
        return 0

    async with pool.acquire() as conn:
        targets = await conn.fetch(
            "SELECT DISTINCT project_id, environment FROM deploys WHERE deployed_at >= $1",
            current - timedelta(days=7),
        )
    queued = 0
    for t in targets:
        project_id, environment = int(t["project_id"]), t["environment"]
        async with project_transaction(project_id) as conn:
            digest = await generate_weekly_digest(conn, project_id, environment=environment, now_dt=current)
            if await queue_notification(
                conn,
                project_id=project_id,
                dedupe_key=f"digest:{project_id}:{environment}:{week_key}",
                payload={"type": "weekly_digest", "text": digest["formatted_text"], "environment": environment},
            ):
                queued += 1
    return queued
