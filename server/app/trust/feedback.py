"""Alert Feedback Collection, Trust Metrics, and Suggestions (TRUST Stage 6).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import asyncpg

from app.trust.actions import record_incident_event

logger = logging.getLogger("changewatch.trust.feedback")


async def record_alert_feedback(
    conn: asyncpg.Connection,
    project_id: int,
    incident_id: int,
    label: str,
    actor: str = "anonymous",
    comment: Optional[str] = None,
) -> int:
    """Record user feedback (real, false, unclear) on an incident."""
    label = label.lower().strip()
    if label not in ("real", "false", "unclear"):
        raise ValueError(f"Invalid feedback label: '{label}'. Must be real, false, or unclear.")
    if not await conn.fetchval(
        "SELECT 1 FROM incidents WHERE id = $1 AND project_id = $2", incident_id, project_id
    ):
        raise LookupError(f"Incident {incident_id} not found")

    row = await conn.fetchrow(
        """
        INSERT INTO alert_feedback (
            incident_id, project_id, actor, label, comment, created_at
        )
        VALUES ($1, $2, $3, $4, $5, now())
        RETURNING id
        """,
        incident_id,
        project_id,
        actor,
        label,
        comment,
    )
    fb_id = int(row["id"]) if row else 0

    await record_incident_event(
        conn=conn,
        project_id=project_id,
        incident_id=incident_id,
        event_type="feedback",
        actor=actor,
        detail={"feedback_id": fb_id, "label": label, "comment": comment},
    )

    return fb_id


async def compute_trust_metrics(
    conn: asyncpg.Connection,
    project_id: int,
    window_days: int = 30,
) -> Dict[str, Any]:
    """Compute trust page metrics: precision, coverage, median TTD, median TTR, uptime."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # 1. Total incidents opened
    total_alerts = await conn.fetchval(
        "SELECT COUNT(*) FROM incidents WHERE project_id = $1 AND created_at >= $2",
        project_id,
        cutoff,
    )
    total_count = int(total_alerts or 0)

    # 2. Feedback counts
    fb_rows = await conn.fetch(
        """
        SELECT label, COUNT(*) AS cnt
        FROM alert_feedback
        WHERE project_id = $1 AND created_at >= $2
        GROUP BY label
        """,
        project_id,
        cutoff,
    )
    fb_counts = {r["label"]: int(r["cnt"]) for r in fb_rows}
    real_count = fb_counts.get("real", 0)
    false_count = fb_counts.get("false", 0)
    unclear_count = fb_counts.get("unclear", 0)
    rated_count = real_count + false_count + unclear_count

    precision: Optional[float] = None
    sample_ok = (real_count + false_count) >= 5
    if sample_ok:
        precision = round(real_count / (real_count + false_count), 3)

    coverage = round(rated_count / total_count, 3) if total_count > 0 else 0.0

    # 3. Median time to detect & resolve (seconds)
    ttd_rows = await conn.fetch(
        """
        SELECT first_alert_ms
        FROM incidents
        WHERE project_id = $1 AND created_at >= $2 AND first_alert_ms IS NOT NULL
        ORDER BY first_alert_ms ASC
        """,
        project_id,
        cutoff,
    )
    median_ttd_sec: Optional[float] = None
    if ttd_rows:
        mid = len(ttd_rows) // 2
        median_ttd_sec = round(ttd_rows[mid]["first_alert_ms"] / 1000.0, 2)

    ttr_rows = await conn.fetch(
        """
        SELECT EXTRACT(EPOCH FROM (resolved_at - created_at)) AS duration_sec
        FROM incidents
        WHERE project_id = $1 AND created_at >= $2 AND resolved_at IS NOT NULL
        ORDER BY duration_sec ASC
        """,
        project_id,
        cutoff,
    )
    median_ttr_min: Optional[float] = None
    if ttr_rows:
        mid = len(ttr_rows) // 2
        median_ttr_min = round(float(ttr_rows[mid]["duration_sec"]) / 60.0, 1)

    return {
        "period_days": window_days,
        "total_alerts": total_count,
        "rated_alerts": rated_count,
        "coverage": coverage,
        "precision": precision,
        "sample_ok": sample_ok,
        "counts": {
            "real": real_count,
            "false": false_count,
            "unclear": unclear_count,
        },
        "median_ttd_sec": median_ttd_sec,
        "median_ttr_min": median_ttr_min,
    }


async def get_feedback_suggestions(
    conn: asyncpg.Connection,
    project_id: int,
    min_false_alarms: int = 3,
) -> List[Dict[str, Any]]:
    """
    Generate proactive suggestions when a pattern receives >= 3 false alarms.
    Policy: Suggestions, never silent self-tuning. User confirms with one-click.
    """
    rows = await conn.fetch(
        """
        SELECT i.where_file, i.category, COUNT(*) AS false_count
        FROM alert_feedback af
        JOIN incidents i ON i.id = af.incident_id AND i.project_id = af.project_id
        WHERE af.project_id = $1 AND af.label = 'false'
        GROUP BY i.where_file, i.category
        HAVING COUNT(*) >= $2
        ORDER BY false_count DESC
        LIMIT 5
        """,
        project_id,
        min_false_alarms,
    )

    suggestions = []
    for r in rows:
        wf = r["where_file"] or "unknown"
        cnt = int(r["false_count"])
        suggestions.append({
            "kind": "path_prefix",
            "pattern": wf,
            "reason": f"Received {cnt} false-alarm ratings from your team.",
            "false_count": cnt,
            "suggestion_text": f"Consider muting path prefix '{wf}' ({cnt} false alarms).",
        })

    return suggestions
