"""Calm Notifications, Alert Grouping, Quiet Hours, Escalation, Mute Rules, and Auto-Resolve.
Specified in TRUST Addendum Stage 5.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import asyncpg

from app.sources import is_source_silent
from app.trust.actions import record_incident_event

logger = logging.getLogger("changewatch.trust.calm")

GROUP_AFTER = int(os.getenv("GROUP_AFTER", "3"))
MAX_ALERTS_PER_HOUR = int(os.getenv("MAX_ALERTS_PER_HOUR", "10"))
ESCALATE_AFTER_MIN = int(os.getenv("ESCALATE_AFTER_MIN", "10"))
RESOLVE_AFTER_MIN = int(os.getenv("RESOLVE_AFTER_MIN", "30"))


async def check_alert_budget(conn: asyncpg.Connection, project_id: int) -> bool:
    """Return True if project is within its MAX_ALERTS_PER_HOUR budget."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    count = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM incident_events
        WHERE project_id = $1 AND type = 'alerted' AND at >= $2
        """,
        project_id,
        cutoff,
    )
    return int(count or 0) < MAX_ALERTS_PER_HOUR


async def check_should_group_alert(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
) -> Tuple[bool, int]:
    """
    Check if multiple incidents opened for this deploy within 5 minutes.
    If >= GROUP_AFTER (3), group into one consolidated message.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    count = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM incidents
        WHERE project_id = $1 AND deploy_id = $2 AND created_at >= $3
        """,
        project_id,
        deploy_id,
        cutoff,
    )
    num = int(count or 0)
    return (num >= GROUP_AFTER, num)


def is_quiet_hours(
    quiet_hours_config: Optional[Dict[str, Any]],
    now_dt: Optional[datetime] = None,
) -> bool:
    """
    Determine if current time is within quiet hours for a project or member.
    During quiet hours, 'low' severity items are deferred; 'high' and 'critical' always notify.
    """
    if not quiet_hours_config or not quiet_hours_config.get("enabled"):
        return False

    current = now_dt or datetime.now(timezone.utc)
    start_hour = int(quiet_hours_config.get("start_hour", 22))
    end_hour = int(quiet_hours_config.get("end_hour", 7))
    hour = current.hour

    if start_hour > end_hour:  # Overnight range, e.g. 22:00 to 07:00
        return hour >= start_hour or hour < end_hour
    else:
        return start_hour <= hour < end_hour


async def is_issue_muted(
    conn: asyncpg.Connection,
    project_id: int,
    fingerprint: str,
    path: str,
    exc_type: str,
    event_rate_multiplier: float = 1.0,
    now_dt: Optional[datetime] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Check active mute rules in ignore_rules.
    Rule: If an issue's event rate grew 10x over baseline, stop muting it.
    """
    if event_rate_multiplier >= 10.0:
        return False, "Rate spike (>10x) unmuted issue automatically"

    current = now_dt or datetime.now(timezone.utc)
    rows = await conn.fetch(
        """
        SELECT id, kind, pattern, reason
        FROM ignore_rules
        WHERE project_id = $1 AND expires_at > $2
        """,
        project_id,
        current,
    )
    for r in rows:
        kind = r["kind"]
        pat = r["pattern"]
        if kind == "fingerprint" and pat == fingerprint:
            return True, f"Muted by rule #{r['id']} ({r['reason']})"
        elif kind == "path_prefix" and path.startswith(pat):
            return True, f"Muted by path prefix '{pat}'"
        elif kind == "exc_type" and pat.lower() in exc_type.lower():
            return True, f"Muted by exception type '{pat}'"

    return False, None


async def run_escalation_checks(
    conn: asyncpg.Connection,
    now_dt: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Check unacknowledged confirmed/critical incidents and escalate.
    - If escalated_count == 0 and unacknowledged for > 10m: escalate to owners once.
    - If escalated_count == 1 and unacknowledged for > 20m: escalate to secondary channel once.
    - Max 2 escalations. An acknowledgement stops escalation.
    """
    current = now_dt or datetime.now(timezone.utc)
    escalated_items = []

    cutoff = current - timedelta(minutes=ESCALATE_AFTER_MIN)
    rows = await conn.fetch(
        """
        SELECT id, project_id, status, title, escalated_count, created_at, owners
        FROM incidents
        WHERE acked_at IS NULL
          AND status IN ('confirmed', 'critical')
          AND escalated_count < 2
          AND created_at <= $1
          -- each further level waits a full ESCALATE_AFTER_MIN after the previous one
          AND (escalated_count = 0 OR last_escalated_at <= $1)
        ORDER BY created_at ASC
        LIMIT 50
        """,
        cutoff,
    )

    for r in rows:
        inc_id = int(r["id"])
        proj_id = int(r["project_id"])
        count = int(r["escalated_count"])

        target = "owners" if count == 0 else "secondary_channel"
        new_count = count + 1

        await conn.execute(
            """
            UPDATE incidents
            SET escalated_count = $1, last_escalated_at = $2
            WHERE id = $3 AND project_id = $4
            """,
            new_count,
            current,
            inc_id,
            proj_id,
        )

        await record_incident_event(
            conn=conn,
            project_id=proj_id,
            incident_id=inc_id,
            event_type="escalated",
            actor="escalation_watchdog",
            detail={"level": new_count, "target": target},
            occurred_at=current,
        )

        escalated_items.append({
            "incident_id": inc_id,
            "project_id": proj_id,
            "level": new_count,
            "target": target,
            "title": r["title"],
        })

    return escalated_items


async def run_calm_auto_resolve(
    conn: asyncpg.Connection,
    resolve_after_min: int = RESOLVE_AFTER_MIN,
    now_dt: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Auto-resolve stale incidents only when traffic is flowing (app is not silent).
    Detects subsequent deploys and potential revert commits.
    """
    current = now_dt or datetime.now(timezone.utc)
    cutoff = current - timedelta(minutes=resolve_after_min)

    # Find candidates
    rows = await conn.fetch(
        """
        SELECT i.id, i.project_id, i.deploy_id, i.status, i.last_event_at,
               d.environment, d.deployed_at, d.commit_sha
        FROM incidents i
        JOIN deploys d ON d.id = i.deploy_id
        WHERE i.status != 'resolved' AND i.last_event_at <= $1
        LIMIT 50
        """,
        cutoff,
    )

    resolved_items = []
    for r in rows:
        inc_id = int(r["id"])
        proj_id = int(r["project_id"])
        env = r["environment"] or "prod"

        # Check silence: if app is silent, silence is NEVER safe, so do not auto-resolve!
        silent = await is_source_silent(conn, proj_id, env)
        if silent:
            logger.info("Skipping auto-resolve for incident #%d: source is silent", inc_id)
            continue

        # Look for newer deploy that may have resolved it
        newer_dep = await conn.fetchrow(
            """
            SELECT id, commit_sha, author, message, deployed_at
            FROM deploys
            WHERE project_id = $1 AND environment = $2 AND deployed_at > $3
            ORDER BY deployed_at ASC
            LIMIT 1
            """,
            proj_id,
            env,
            r["deployed_at"],
        )

        fix_note = None
        is_revert = False
        if newer_dep:
            sha_short = newer_dep["commit_sha"][:8]
            auth = newer_dep["author"] or "developer"
            msg = newer_dep["message"] or ""
            is_revert = msg.lower().startswith("revert")
            if is_revert:
                fix_note = f"likely reverted by deploy {sha_short} ({msg})"
            else:
                fix_note = f"likely fixed by deploy {sha_short} by {auth}"

        # Resolve incident
        await conn.execute(
            """
            UPDATE incidents
            SET status = 'resolved', resolved_at = $1
            WHERE id = $2 AND project_id = $3
            """,
            current,
            inc_id,
            proj_id,
        )

        detail_payload = {
            "resolved_at": current.isoformat(),
            "auto_resolve": True,
            "fix_note": fix_note,
            "is_revert": is_revert,
        }
        await record_incident_event(
            conn=conn,
            project_id=proj_id,
            incident_id=inc_id,
            event_type="resolved",
            actor="auto_resolver",
            detail=detail_payload,
            occurred_at=current,
        )

        resolved_items.append({
            "incident_id": inc_id,
            "project_id": proj_id,
            "fix_note": fix_note,
            "is_revert": is_revert,
        })

    return resolved_items
