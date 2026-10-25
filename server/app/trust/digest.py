"""Weekly Release Health and Calm Operations Digest (TRUST Stage 7 & AN Rule 1).

Uses pure calculation functions from app.analytics.metrics to ensure byte-for-byte
numerical parity with the analytics dashboard.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
import asyncpg

from app.analytics.metrics import (
    compute_alerts_accuracy,
    compute_change_failure_rate,
    compute_hotspots,
    compute_releases,
    compute_time_to_fix,
    compute_tool_uptime,
)

logger = logging.getLogger("changewatch.trust.digest")


async def generate_weekly_digest(
    conn: asyncpg.Connection,
    project_id: int,
    environment: str = "prod",
    now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Generate weekly digest metrics and formatted announcement text.
    Parity Guarantee: Calls identical pure metric functions used by the dashboard.
    """
    current = now_dt or datetime.now(timezone.utc)
    week_ago = current - timedelta(days=7)

    # 1. Fetch deploys in the last 7 days
    deploy_rows = await conn.fetch(
        """
        SELECT d.id, d.commit_sha, d.environment, d.deployed_at, a.first_verdict, a.final_verdict
        FROM deploys d
        LEFT JOIN deploy_analysis a ON a.deploy_id = d.id
        WHERE d.project_id = $1 AND d.environment = $2 AND d.deployed_at >= $3
        ORDER BY d.deployed_at ASC
        """,
        project_id,
        environment,
        week_ago,
    )
    deploys = [dict(r) for r in deploy_rows]

    # 2. Fetch incidents in the last 7 days
    incident_rows = await conn.fetch(
        """
        SELECT i.id, i.deploy_id, i.status, i.title, i.where_file, i.first_alert_ms,
               i.created_at, i.first_event_at, i.last_event_at, i.resolved_at, i.confirmed_at,
               EXISTS(SELECT 1 FROM alert_feedback af
                      WHERE af.incident_id = i.id AND af.project_id = i.project_id AND af.label = 'false')
                 AS is_false_alarm
        FROM incidents i
        JOIN deploys d ON d.id = i.deploy_id AND d.project_id = i.project_id
        WHERE i.project_id = $1 AND d.environment = $3 AND i.created_at >= $2
        """,
        project_id,
        week_ago,
        environment,
    )
    incidents = [dict(r) for r in incident_rows]

    # 3. Fetch alert feedback in the last 7 days
    feedback_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (af.incident_id) af.label
        FROM alert_feedback af
        JOIN incidents i ON i.id = af.incident_id AND i.project_id = af.project_id
        JOIN deploys d ON d.id = i.deploy_id AND d.project_id = i.project_id
        WHERE af.project_id = $1 AND d.environment = $3 AND af.created_at >= $2
        ORDER BY af.incident_id, af.created_at DESC
        """,
        project_id,
        week_ago,
        environment,
    )
    feedback_list = [dict(r) for r in feedback_rows]

    # 4. Fetch component status log in the last 7 days
    csl_rows = await conn.fetch(
        """
        SELECT component, status, started_at, ended_at
        FROM component_status_log
        WHERE (project_id = $1 OR project_id IS NULL)
          AND started_at >= $2
        """,
        project_id,
        week_ago,
    )
    status_logs = [dict(r) for r in csl_rows]

    # 5. Fetch mute rules expiring in the next 7 days
    expiring_mute_rows = await conn.fetch(
        """
        SELECT id, kind, pattern, reason, expires_at
        FROM ignore_rules
        WHERE project_id = $1 AND expires_at BETWEEN $2 AND $3
        ORDER BY expires_at ASC
        """,
        project_id,
        current,
        current + timedelta(days=7),
    )
    expiring_mutes = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "pattern": r["pattern"],
            "reason": r["reason"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        }
        for r in expiring_mute_rows
    ]

    # Mark deploys that had confirmed incidents or rollback verdicts
    failed_deploy_ids = {
        inc["deploy_id"] for inc in incidents
        if inc.get("confirmed_at") is not None  # ever confirmed, as on the dashboard
    }
    for d in deploys:
        if d.get("id") in failed_deploy_ids or d.get("final_verdict") == "rollback" or d.get("first_verdict") == "rollback":
            d["has_confirmed_incident"] = True
            d["verdict"] = "rollback"

    # Calculate metrics via pure functions
    rel_metric = compute_releases(deploys)
    cfr_metric = compute_change_failure_rate(deploys, now_dt=current)
    ttf_metric = compute_time_to_fix([i for i in incidents if i.get("status") == "resolved"])
    acc_metric = compute_alerts_accuracy(feedback_list)
    upt_metric = compute_tool_uptime(status_logs, window_minutes=10080.0)
    hot_metric = compute_hotspots(incidents)

    total_releases_n = len(deploys)
    total_incidents_n = len(incidents)

    # Formatted digest text
    # e.g.: "Last week: 12 releases, 1 incident, resolved in 9 min. Change failure rate 8%. Most incident-prone area: billing/. Alert precision 94%. ChangeWatch was fully operational for 99.9% of the week."
    lines = [f"📊 *ChangeWatch Weekly Digest ({environment})*"]

    releases_summary = f"• Releases: {total_releases_n}"
    if total_incidents_n == 0:
        releases_summary += ", 0 incidents 🎉"
    else:
        releases_summary += f", {total_incidents_n} incident(s)"
        if ttf_metric.value is not None:
            releases_summary += f", median fix time: {int(round(ttf_metric.value))} min"  # already minutes
    lines.append(releases_summary)

    if cfr_metric.formatted and not cfr_metric.formatted.startswith("Not enabled"):
        lines.append(f"• Change failure rate: {cfr_metric.formatted}")

    if hot_metric:
        top_area = hot_metric[0]["area"]
        lines.append(f"• Most incident-prone area: `{top_area}`")

    if acc_metric.formatted and "Not enough data" not in acc_metric.formatted:
        lines.append(f"• Alert precision: {acc_metric.formatted}")

    if upt_metric.formatted and "collecting data" not in upt_metric.formatted:
        lines.append(f"• Tool uptime: {upt_metric.formatted} of the week")

    if expiring_mutes:
        lines.append(f"• ⚠️ {len(expiring_mutes)} mute rule(s) expiring this week")

    digest_body = "\n".join(lines)

    return {
        "project_id": project_id,
        "environment": environment,
        "period_start": week_ago.isoformat(),
        "period_end": current.isoformat(),
        "metrics": {
            "releases": rel_metric.to_dict(),
            "change_failure_rate": cfr_metric.to_dict(),
            "time_to_fix": ttf_metric.to_dict(),
            "alerts_accuracy": acc_metric.to_dict(),
            "tool_uptime": upt_metric.to_dict(),
            "hotspots": hot_metric,
        },
        "expiring_mutes": expiring_mutes,
        "formatted_text": digest_body,
    }
