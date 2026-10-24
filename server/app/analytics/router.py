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
from typing import Any, Dict, List, Optional

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
from app.db import read_project_transaction

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
                   EXISTS(
                     SELECT 1 FROM incidents i
                     WHERE i.deploy_id = d.id AND i.status = 'confirmed'
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
            SELECT id, status, created_at, confirmed_at, resolved_at,
                   alerted_at, first_event_at, last_event_at, first_alert_ms
            FROM incidents
            WHERE project_id = $1 AND created_at >= $2 AND created_at <= $3
            ORDER BY created_at DESC
            """,
            project_id,
            start_dt,
            end_dt,
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

    kpis = {
        "releases": compute_releases(deploys).to_dict(),
        "change_failure_rate": compute_change_failure_rate(deploys, end_dt).to_dict(),
        "time_to_detect": compute_time_to_detect(incidents).to_dict(),
        "time_to_fix": compute_time_to_fix(resolved).to_dict(),
        "alerts_real": compute_alerts_accuracy([]).to_dict(),  # alert_feedback table populated in M6
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
            SELECT where_file
            FROM incidents
            WHERE project_id = $1 AND created_at >= $2 AND created_at <= $3
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

        # Ingest stats from ingest_stats_daily
        ingest_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(received), 0)::bigint AS received,
                   COALESCE(SUM(stored), 0)::bigint AS stored,
                   COALESCE(SUM(counted_only), 0)::bigint AS counted_only,
                   COALESCE(SUM(dropped_rate_limit), 0)::bigint AS dropped_rate_limit,
                   COALESCE(SUM(dropped_backpressure), 0)::bigint AS dropped_backpressure,
                   COALESCE(SUM(dropped_invalid), 0)::bigint AS dropped_invalid
            FROM ingest_stats_daily
            WHERE project_id = $1 AND day >= $2::date AND day <= $3::date
            """,
            project_id,
            start_dt.date(),
            end_dt.date(),
        )

        # Outbox notifications delivered
        outbox_row = await conn.fetchrow(
            """
            SELECT COUNT(id) FILTER (WHERE status = 'delivered')::int AS delivered,
                   COUNT(id) FILTER (WHERE status = 'failed')::int AS failed,
                   COUNT(id) FILTER (WHERE status = 'pending')::int AS pending
            FROM outbox
            WHERE project_id = $1 AND created_at >= $2 AND created_at <= $3
            """,
            project_id,
            start_dt,
            end_dt,
        )

    ingest_stats = {
        "received": ingest_row["received"] if ingest_row else 0,
        "stored": ingest_row["stored"] if ingest_row else 0,
        "counted_only": ingest_row["counted_only"] if ingest_row else 0,
        "dropped": (
            (ingest_row["dropped_rate_limit"] or 0)
            + (ingest_row["dropped_backpressure"] or 0)
            + (ingest_row["dropped_invalid"] or 0)
        ) if ingest_row else 0,
    }

    deliv = outbox_row["delivered"] if outbox_row else 0
    fail = outbox_row["failed"] if outbox_row else 0
    total_out = deliv + fail
    delivery_pct = round((deliv / total_out) * 100.0, 1) if total_out > 0 else 100.0

    return {
        "alert_speed": alert_speed,
        "ingest_stats": ingest_stats,
        "notifications": {
            "delivered": deliv,
            "failed": fail,
            "pending": outbox_row["pending"] if outbox_row else 0,
            "delivery_rate_percent": delivery_pct,
        },
        # Features not built yet show 'Not enabled' per spec rules
        "ai_investigations": {
            "status": "not_enabled",
            "formatted": "Not enabled",
            "note": "AI investigator arrives in Milestone M5.",
        },
        "calibration": {
            "status": "not_enabled",
            "formatted": "Not enabled",
            "note": "Trust calibration arrives in Milestone M6.",
        },
    }


@router.get("/export.csv")
async def export_csv(
    metric: str = Query(..., description="Metric ID to export (e.g. overview, hotspots, categories)"),
    period: int = Query(30, ge=1, le=365),
    project_id: int = Depends(require_read_project_id),
):
    """Export analytics data in RFC-4180 CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)

    if metric == "overview":
        overview = await get_overview(period=period, project_id=project_id)
        writer.writerow(["Metric", "Value", "Unit", "N", "Sample OK", "Note"])
        for k, v in overview["kpis"].items():
            writer.writerow([k, v.get("value"), v.get("unit"), v.get("n"), v.get("sample_ok"), v.get("note")])

    elif metric == "hotspots":
        hotspots = await get_hotspots(period=period, project_id=project_id)
        writer.writerow(["Area", "Incident Count", "Share Percent"])
        for h in hotspots:
            writer.writerow([h["area"], h["count"], h["share_percent"]])

    elif metric == "categories":
        cats = await get_categories(period=period, project_id=project_id)
        writer.writerow(["Category", "Count", "Share Percent"])
        for c in cats:
            writer.writerow([c["category"], c["count"], c["share_percent"]])

    else:
        writer.writerow(["Error", "Unknown metric"])

    csv_content = output.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=changewatch_{metric}_{period}d.csv"},
    )
