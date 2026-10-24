"""Operator system alerts: ChangeWatch telling its operator that ChangeWatch itself is unwell.

Raised once per component while a problem lasts and cleared with a "recovered" message,
then sent straight to SYSTEM_ALERT_WEBHOOK_URL (brief §9 "fallback channel"). Delivery does
not go through the outbox, because the outbox, the notifier or Postgres may be what failed.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg
import httpx

from app.config import settings
from app.logging_filter import redact_delivery_error

logger = logging.getLogger("changewatch.system_alerts")

DB_ALERT_REPEAT_SEC = 900.0
_last_db_alert_at = 0.0

# Services start in any order; give heartbeats time to appear before calling anything "down"
STARTUP_GRACE_SEC = 120.0
_process_started_at = time.monotonic()


async def send_fallback(text: str) -> bool:
    """Post a plain message to the operator fallback webhook. Never raises."""
    message = redact_delivery_error(text)
    url = settings.SYSTEM_ALERT_WEBHOOK_URL
    if not url:
        logger.warning("System alert (SYSTEM_ALERT_WEBHOOK_URL not set): %s", message)
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # `text` for Slack and Teams incoming webhooks, `content` for Discord
            resp = await client.post(url, json={"text": message, "content": message})
            resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Could not deliver system alert to fallback webhook: %s", redact_delivery_error(str(exc)))
        return False


def alerts_from_watchdog(status: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every component reported down, plus the watchdog's explicit system alerts, keyed by component."""
    alerts: Dict[str, Dict[str, Any]] = {}
    for name, info in (status.get("components") or {}).items():
        if info.get("status") == "down":
            alerts[name] = {
                "component": name,
                "severity": "critical",
                "title": f"ChangeWatch {name} is down",
                "detail": info.get("detail") or "",
            }
    for alert in status.get("system_alerts") or []:
        alerts.setdefault(alert["component"], alert)
    return list(alerts.values())


async def sync_system_alerts(
    conn: asyncpg.Connection,
    alerts: List[Dict[str, Any]],
    now_dt: Optional[datetime] = None,
) -> Dict[str, List[str]]:
    """Open alerts for new problems and resolve alerts whose problem cleared, notifying once each."""
    raised: List[str] = []
    resolved: List[str] = []
    current = {a["component"]: a for a in alerts}
    in_grace = time.monotonic() - _process_started_at < STARTUP_GRACE_SEC

    for component, alert in current.items():
        if in_grace:
            continue
        new_id = await conn.fetchval(
            """
            INSERT INTO system_alerts (project_id, component, severity, title, detail, status, created_at)
            VALUES (NULL, $1, $2, $3, $4, 'active', COALESCE($5, now()))
            ON CONFLICT (component) WHERE project_id IS NULL AND status = 'active' DO NOTHING
            RETURNING id
            """,
            component,
            alert.get("severity", "high"),
            alert["title"],
            redact_delivery_error(alert.get("detail") or ""),
            now_dt,
        )
        if new_id:
            raised.append(component)
            await send_fallback(f"🚨 {alert['title']}\n{alert.get('detail') or ''}".strip())

    active = await conn.fetch(
        "SELECT component, title FROM system_alerts WHERE project_id IS NULL AND status = 'active'"
    )
    for row in active:
        if row["component"] in current:
            continue
        cleared = await conn.fetchval(
            """
            UPDATE system_alerts SET status = 'resolved', resolved_at = COALESCE($2, now())
            WHERE project_id IS NULL AND status = 'active' AND component = $1
            RETURNING id
            """,
            row["component"],
            now_dt,
        )
        if cleared:
            resolved.append(row["component"])
            await send_fallback(f"✅ Recovered: {row['title']}")

    return {"raised": raised, "resolved": resolved}


async def run_watchdog_cycle(pool: asyncpg.Pool, redis: Any, ping_external: bool = False) -> None:
    """Run the watchdog and sync operator alerts. If Postgres itself is unreachable, alert directly."""
    from app.watchdog import run_watchdog_checks

    try:
        async with pool.acquire() as conn:
            status = await run_watchdog_checks(conn=conn, redis=redis, ping_external=ping_external)
            await sync_system_alerts(conn, alerts_from_watchdog(status))
    except Exception as exc:
        # Without the database there is nowhere to record (and dedupe) the alert, so tell the
        # operator directly, at most every DB_ALERT_REPEAT_SEC per process
        global _last_db_alert_at
        logger.error("Watchdog cycle failed: %s", exc)
        if time.monotonic() - _last_db_alert_at >= DB_ALERT_REPEAT_SEC or _last_db_alert_at == 0.0:
            _last_db_alert_at = time.monotonic()
            await send_fallback(f"🚨 ChangeWatch watchdog cannot reach its database\n{exc}")
