"""Analytics and Operations API Router.

Specified in Analytics & Operations Dashboard Addendum (AN Stage 3).
All metric endpoints require a project Read Key and execute against read_project_transaction.
Unbuilt features return 'Not enabled' rather than zero or fake numbers.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Response

from app.analytics.metrics import (
    METRIC_DEFINITIONS,
    compute_alert_speed,
    compute_alerts_accuracy,
    compute_categories,
    compute_change_failure_rate,
    compute_hotspots,
    compute_releases,
    compute_time_to_detect,
    compute_time_to_fix,
    compute_tool_uptime,
)
from app.auth import require_read_project_id
from app.config import settings
from app.db import read_project_transaction
from app.ingest_stats import dropped_totals
from app.queue import get_redis

logger = logging.getLogger("changewatch.analytics")

router = APIRouter(prefix="/v1/analytics", tags=["Analytics"])


def parse_period_range(
    period: int = 30,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> tuple[datetime, datetime]:
    """Resolve period (days) or explicit from/to dates to datetime range."""
    now = datetime.now(timezone.utc)
    if from_date and to_date:
        try:
            start = datetime.fromisoformat(from_date)
            end = datetime.fromisoformat(to_date)
            return start, end
        except Exception:
            pass
    start = now - timedelta(days=period)
    return start, now


@router.get("/definitions")
async def get_definitions(project_id: int = Depends(require_read_project_id)):
    """Returns metric definitions catalog for UI tooltips and documentation."""
    return METRIC_DEFINITIONS


@router.get("/overview")
async def get_overview(
    period: int = Query(30, ge=1, le=365),
    env: str = Query("prod"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    project_id: int = Depends(require_read_project_id),
):
    """
    Returns the 6 primary KPI tiles:
    Releases, Change Failure Rate, Time to Detect, Time to Fix, Alerts that were Real, Tool Uptime.
    """
    start_dt, end_dt = parse_period_range(period, from_date, to_date)
    window_min = (end_dt - start_dt).total_seconds() / 60.0

    async with read_project_transaction(project_id) as conn:
        # 1. Deploys in window
        dep_rows = await conn.fetch(
            """
            SELECT d.id, d.commit_sha, d.deployed_at, d.environment,
                   a.verdict, a.first_verdict,
                   -- Ever confirmed: auto-resolve flips the status within 30 minutes of the fix
                   EXISTS(
                     SELECT 1 FROM incidents i
                     WHERE i.deploy_id = d.id AND i.project_id = d.project_id AND i.confirmed_at IS NOT NULL
                   ) AS has_confirmed_incident
            FROM deploys d
            LEFT JOIN deploy_analysis a ON a.deploy_id = d.id
            WHERE d.project_id = $1 AND d.environment = $2
              AND d.deployed_at >= $3 AND d.deployed_at <= $4
            ORDER BY d.deployed_at DESC
            """,
            project_id,
            env,
            start_dt,
            end_dt,
        )
        deploys = [dict(r) for r in dep_rows]

        # 2. Incidents in window
        inc_rows = await conn.fetch(
            """
            SELECT i.id, i.status, i.created_at, i.confirmed_at, i.resolved_at,
                   i.alerted_at, i.first_event_at, i.last_event_at, i.first_alert_ms
            FROM incidents i
            JOIN deploys d ON d.id = i.deploy_id AND d.project_id = i.project_id
            WHERE i.project_id = $1 AND d.environment = $4 AND i.created_at >= $2 AND i.created_at <= $3
            ORDER BY i.created_at DESC
            """,
            project_id,
            start_dt,
            end_dt,
            env,
        )
        incidents = [dict(r) for r in inc_rows]
        resolved = [i for i in incidents if i.get("status") == "resolved"]

        # 3. Status logs for uptime
        csl_rows = await conn.fetch(
            """
            SELECT component, status, started_at, ended_at
            FROM component_status_log
            WHERE (project_id IS NULL OR project_id = $1)
              AND started_at >= $2 AND started_at <= $3
            """,
            project_id,
            start_dt,
            end_dt,
        )
        status_logs = [dict(r) for r in csl_rows]

        # 4. Ratings of alerts (POST /api/incidents/{id}/feedback): the latest rating per incident
        feedback_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (af.incident_id) af.incident_id, af.label
            FROM alert_feedback af
            WHERE af.project_id = $1 AND af.created_at >= $2 AND af.created_at <= $3
            ORDER BY af.incident_id, af.created_at DESC
            """,
            project_id,
            start_dt,
            end_dt,
        )
        feedback = [dict(r) for r in feedback_rows]

    kpis = {
        "releases": compute_releases(deploys).to_dict(),
        "change_failure_rate": compute_change_failure_rate(deploys, end_dt).to_dict(),
        "time_to_detect": compute_time_to_detect(incidents).to_dict(),
        "time_to_fix": compute_time_to_fix(resolved).to_dict(),
        "alerts_real": compute_alerts_accuracy(feedback).to_dict(),
        "tool_uptime": compute_tool_uptime(status_logs, window_min).to_dict(),
    }

    return {
        "period_days": period,
        "environment": env,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "kpis": kpis,
    }


@router.get("/verdicts-by-week")
async def get_verdicts_by_week(
    period: int = Query(30, ge=7, le=365),
    env: str = Query("prod"),
    project_id: int = Depends(require_read_project_id),
):
    """Stacked bar data of release verdicts grouped by ISO week."""
    start_dt, end_dt = parse_period_range(period)

    async with read_project_transaction(project_id) as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('week', d.deployed_at) AS week_bucket,
                   COALESCE(a.verdict, 'no_data') AS verdict,
                   COUNT(d.id)::int AS count
            FROM deploys d
            LEFT JOIN deploy_analysis a ON a.deploy_id = d.id
            WHERE d.project_id = $1 AND d.environment = $2
              AND d.deployed_at >= $3 AND d.deployed_at <= $4
            GROUP BY week_bucket, verdict
            ORDER BY week_bucket ASC
            """,
            project_id,
            env,
            start_dt,
            end_dt,
        )

    weeks: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        wb = r["week_bucket"].strftime("%Y-%m-%d") if r["week_bucket"] else "unknown"
        if wb not in weeks:
            weeks[wb] = {"week": wb, "safe": 0, "watch": 0, "rollback": 0, "no_data": 0, "total": 0}
        v = (r["verdict"] or "no_data").lower()
        cnt = int(r["count"])
        if v in weeks[wb]:
            weeks[wb][v] += cnt
        weeks[wb]["total"] += cnt

    return list(weeks.values())


@router.get("/ttf-by-week")
async def get_ttf_by_week(
    period: int = Query(30, ge=7, le=365),
    project_id: int = Depends(require_read_project_id),
):
    """Median time to fix resolved incidents grouped by ISO week."""
    start_dt, end_dt = parse_period_range(period)

    async with read_project_transaction(project_id) as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('week', created_at) AS week_bucket,
                   created_at, last_event_at
            FROM incidents
            WHERE project_id = $1 AND status = 'resolved'
              AND created_at >= $2 AND created_at <= $3
            ORDER BY week_bucket ASC
            """,
            project_id,
            start_dt,
            end_dt,
        )

    weeks_data: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        wb = r["week_bucket"].strftime("%Y-%m-%d") if r["week_bucket"] else "unknown"
        weeks_data.setdefault(wb, []).append(dict(r))

    result = []
    for wb, incs in sorted(weeks_data.items()):
        res = compute_time_to_fix(incs)
        result.append({
            "week": wb,
            "median_minutes": res.value,
            "formatted": res.formatted,
            "sample_ok": res.sample_ok,
            "n": res.n,
        })

    return result


@router.get("/hotspots")
async def get_hotspots(
    period: int = Query(30, ge=1, le=365),
    depth: int = Query(1, ge=1, le=5),
    project_id: int = Depends(require_read_project_id),
):
    """Where problems come from (hotspot code areas, never people)."""
    start_dt, end_dt = parse_period_range(period)

    async with read_project_transaction(project_id) as conn:
        rows = await conn.fetch(
            """
            SELECT i.where_file,
                   EXISTS(SELECT 1 FROM alert_feedback af
                          WHERE af.incident_id = i.id AND af.project_id = i.project_id AND af.label = 'false')
                     AS is_false_alarm
            FROM incidents i
            WHERE i.project_id = $1 AND i.created_at >= $2 AND i.created_at <= $3
            """,
            project_id,
            start_dt,
            end_dt,
        )
    incidents = [dict(r) for r in rows]
    return compute_hotspots(incidents, depth=depth)


@router.get("/categories")
async def get_categories(
    period: int = Query(30, ge=1, le=365),
    project_id: int = Depends(require_read_project_id),
):
    """Likely causes by error category."""
    start_dt, end_dt = parse_period_range(period)

    async with read_project_transaction(project_id) as conn:
        rows = await conn.fetch(
            """
            SELECT category
            FROM incidents
            WHERE project_id = $1 AND created_at >= $2 AND created_at <= $3
            """,
            project_id,
            start_dt,
            end_dt,
        )
    incidents = [dict(r) for r in rows]
    return compute_categories(incidents)


@router.get("/operations")
async def get_operations(
    period: int = Query(30, ge=1, le=365),
    project_id: int = Depends(require_read_project_id),
):
    """Tool operations panel: alert speed, ingest stats, outbox deliveries, AI & calibration status."""
    start_dt, end_dt = parse_period_range(period)

    async with read_project_transaction(project_id) as conn:
        # Alert speed from incidents
        inc_rows = await conn.fetch(
            "SELECT first_alert_ms FROM incidents WHERE project_id = $1 AND created_at >= $2 AND created_at <= $3",
            project_id,
            start_dt,
            end_dt,
        )
        incidents = [dict(r) for r in inc_rows]
        alert_speed = compute_alert_speed(incidents).to_dict()

        # Error and slow-request events received, from the exact per-minute rollups; full rows stored
        received = await conn.fetchval(
            "SELECT COALESCE(SUM(count), 0)::bigint FROM event_rollups WHERE project_id = $1 AND bucket >= $2 AND bucket <= $3",
            project_id,
            start_dt,
            end_dt,
        )
        stored = await conn.fetchval(
            "SELECT COUNT(*)::bigint FROM events WHERE project_id = $1 AND occurred_at >= $2 AND occurred_at <= $3",
            project_id,
            start_dt,
            end_dt,
        )

        # Notifications (the outbox marks delivered rows 'sent')
        outbox_row = await conn.fetchrow(
            """
            SELECT COUNT(id) FILTER (WHERE status = 'sent')::int AS delivered,
                   COUNT(id) FILTER (WHERE status = 'failed')::int AS failed,
                   COUNT(id) FILTER (WHERE status = 'pending')::int AS pending
            FROM outbox
            WHERE project_id = $1 AND created_at >= $2 AND created_at <= $3
            """,
            project_id,
            start_dt,
            end_dt,
        )

        source_row = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'active')::int AS active,
                   COUNT(*) FILTER (WHERE status = 'silent')::int AS silent,
                   COUNT(*)::int AS total
            FROM sources WHERE project_id = $1
            """,
            project_id,
        )

        inv_row = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE status IN ('done', 'partial'))::int AS done,
                   COUNT(*) FILTER (WHERE status = 'failed')::int AS failed
            FROM investigations
            WHERE project_id = $1 AND started_at >= $2 AND started_at <= $3
            """,
            project_id,
            start_dt,
            end_dt,
        )

        from app.trust.calibration import compute_decision_calibration
        calibration = await compute_decision_calibration(conn, project_id)

    try:
        redis = get_redis()
    except Exception:
        redis = None
    dropped = await dropped_totals(redis, project_id, start_dt.date(), end_dt.date())

    deliv, fail, pending = outbox_row["delivered"], outbox_row["failed"], outbox_row["pending"]
    attempted = deliv + fail
    if attempted:
        delivery_formatted = f"{round(deliv * 100 / attempted, 1)}% ({deliv} of {attempted})"
    else:
        delivery_formatted = "No notifications yet"

    active, silent, total_sources = source_row["active"], source_row["silent"], source_row["total"]
    if total_sources == 0:
        sources_formatted = "No data received yet"
    elif silent:
        sources_formatted = f"{silent} silent"
    else:
        sources_formatted = "Data flowing"

    done, inv_failed = inv_row["done"], inv_row["failed"]
    provider = settings.LLM_PROVIDER.lower().strip()
    if done or inv_failed:
        inv_formatted = f"{done} done, {inv_failed} failed"
    else:
        inv_formatted = "No investigations yet"

    return {
        "alert_speed": alert_speed,
        "ingest_stats": {
            "received": int(received or 0),
            "stored": int(stored or 0),
            "counted_only": max(0, int(received or 0) - int(stored or 0)),
            "rate_limited": dropped["rate_limit"],
            "dropped_backpressure": dropped["backpressure"],
            "note": "Error and slow-request events; storm control stores some as counts only. Rate-limited "
                    "batches are retried by the SDK.",
        },
        "notifications": {
            "delivered": deliv,
            "failed": fail,
            "pending": pending,
            "delivery_rate_percent": round(deliv * 100 / attempted, 1) if attempted else None,
            "formatted": delivery_formatted,
        },
        "sources": {
            "active_count": active,
            "silent_count": silent,
            "total": total_sources,
            "formatted": sources_formatted,
        },
        "ai_investigations": {
            "status": "enabled",
            "provider": provider,
            "done": done,
            "failed": inv_failed,
            "formatted": inv_formatted,
        },
        "calibration": {
            "status": calibration.get("status", "ok"),
            "formatted": calibration.get("headline", ""),
        },
    }


@router.get("/export.csv")
async def export_csv(
    metric: Literal["overview", "verdicts", "ttf", "hotspots", "categories"] = Query(
        ..., description="Metric to export: overview, verdicts, ttf, hotspots or categories"
    ),
    period: int = Query(30, ge=1, le=365),
    env: str = Query("prod"),
    project_id: int = Depends(require_read_project_id),
):
    """Export analytics data in RFC-4180 CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)

    # The route functions are called directly, so pass every parameter: one left out would be
    # FastAPI's Query(...) object rather than its default value.
    if metric == "overview":
        overview = await get_overview(period=period, env=env, from_date=None, to_date=None, project_id=project_id)
        writer.writerow(["Metric", "Value", "Unit", "N", "Sample OK", "Note"])
        for k, v in overview["kpis"].items():
            writer.writerow([k, v.get("value"), v.get("unit"), v.get("n"), v.get("sample_ok"), v.get("note")])

    elif metric == "verdicts":
        weeks = await get_verdicts_by_week(period=max(period, 7), env=env, project_id=project_id)
        writer.writerow(["Week", "Safe", "Watch", "Rollback", "No data", "Total"])
        for w in weeks:
            writer.writerow([w["week"], w["safe"], w["watch"], w["rollback"], w["no_data"], w["total"]])

    elif metric == "ttf":
        weeks = await get_ttf_by_week(period=max(period, 7), project_id=project_id)
        writer.writerow(["Week", "Median minutes", "N", "Sample OK"])
        for w in weeks:
            writer.writerow([w["week"], w["median_minutes"], w["n"], w["sample_ok"]])

    elif metric == "hotspots":
        hotspots = await get_hotspots(period=period, depth=1, project_id=project_id)
        writer.writerow(["Area", "Incident Count", "Share Percent"])
        for h in hotspots:
            writer.writerow([h["area"], h["count"], h["share_percent"]])

    else:
        cats = await get_categories(period=period, project_id=project_id)
        writer.writerow(["Category", "Count", "Share Percent"])
        for c in cats:
            writer.writerow([c["category"], c["count"], c["share_percent"]])

    csv_content = output.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=changewatch_{metric}_{period}d.csv"},
    )
