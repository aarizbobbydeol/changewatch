"""Pure Metric Calculation Functions for Analytics Dashboard & Operations.

Specified in Analytics & Operations Dashboard Addendum (AN Stage 1).
All mathematical calculations on data are pure functions over in-memory rows/dicts.
Never queries raw events directly; always uses pre-aggregated rows or incident records.
Enforces sample_ok minimums, no fake numbers, and never ranks individual people.
"""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class MetricResult:
    id: str
    value: Optional[float]
    formatted: str
    unit: str
    n: int
    sample_ok: bool
    note: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _percentile(values: List[float], p: float) -> float:
    """Compute p-th percentile (0.0 to 1.0) of a sorted float list."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_v[int(k)]
    d0 = sorted_v[int(f)] * (c - k)
    d1 = sorted_v[int(c)] * (k - f)
    return d0 + d1


# ==============================================================================
# Metric 1: Releases
# ==============================================================================
def compute_releases(deploys: List[Dict[str, Any]]) -> MetricResult:
    """
    Count of deploys in the window, plus share of deploys safe at first check.
    Min n = 5 deploys.
    """
    total = len(deploys)
    if total < 5:
        return MetricResult(
            id="releases",
            value=None,
            formatted=f"Not enough data yet (n={total})",
            unit="deploys",
            n=total,
            sample_ok=False,
            note="Minimum 5 deploys required in the selected period.",
        )

    # First check verdicts: safe / eligible (pending excluded from denominator; no_data included)
    eligible = [d for d in deploys if d.get("first_verdict") and d.get("first_verdict") != "pending"]
    safe_cnt = sum(1 for d in eligible if d.get("first_verdict") == "safe")
    safe_pct = round((safe_cnt / len(eligible)) * 100.0, 1) if eligible else 0.0

    return MetricResult(
        id="releases",
        value=float(total),
        formatted=str(total),
        unit="releases",
        n=total,
        sample_ok=True,
        note=f"{safe_cnt} of {len(eligible)} ({safe_pct}%) safe at first check",
        details={
            "total_releases": total,
            "eligible_for_verdict": len(eligible),
            "safe_count": safe_cnt,
            "safe_percent": safe_pct,
        },
    )


# ==============================================================================
# Metric 2: Change Failure Rate
# ==============================================================================
def compute_change_failure_rate(
    deploys: List[Dict[str, Any]],
    now_dt: Optional[datetime] = None,
) -> MetricResult:
    """
    Deploys that had at least one confirmed incident or rollback verdict within 24h,
    divided by deploys in the window that are at least 24 hours old.
    Min n = 10 eligible deploys.
    """
    now = now_dt or datetime.now(timezone.utc)
    one_day_sec = 86400.0

    # Filter to deploys at least 24h old
    eligible = [
        d for d in deploys
        if d.get("deployed_at") and (now - d["deployed_at"]).total_seconds() >= one_day_sec
    ]
    n_eligible = len(eligible)

    if n_eligible < 10:
        return MetricResult(
            id="change_failure_rate",
            value=None,
            formatted=f"Not enough data yet (n={n_eligible})",
            unit="percent",
            n=n_eligible,
            sample_ok=False,
            note="Requires at least 10 deploys older than 24 hours so rate is not understated.",
        )

    failed = [
        d for d in eligible
        if bool(d.get("has_confirmed_incident")) or d.get("verdict") == "rollback"
    ]
    fail_cnt = len(failed)
    rate_pct = round((fail_cnt / n_eligible) * 100.0, 1)

    return MetricResult(
        id="change_failure_rate",
        value=rate_pct,
        formatted=f"{rate_pct}%",
        unit="percent",
        n=n_eligible,
        sample_ok=True,
        note=f"{fail_cnt} of {n_eligible} releases caused a confirmed regression",
        details={
            "failed_count": fail_cnt,
            "eligible_count": n_eligible,
            "failed_deploy_ids": [d.get("id") for d in failed if d.get("id")],
        },
    )


# ==============================================================================
# Metric 3: Time to Detect
# ==============================================================================
def compute_time_to_detect(incidents: List[Dict[str, Any]]) -> MetricResult:
    """
    alerted_at minus first_event_at in seconds for incidents opened in the window.
    Shows median and p95. Min n = 5 incidents.
    """
    durations = []
    for inc in incidents:
        first_at = inc.get("first_event_at")
        alert_at = inc.get("alerted_at")
        if first_at and alert_at:
            diff = (alert_at - first_at).total_seconds()
            if diff >= 0:
                durations.append(diff)

    n = len(durations)
    if n < 5:
        return MetricResult(
            id="time_to_detect",
            value=None,
            formatted=f"Not enough data yet (n={n})",
            unit="seconds",
            n=n,
            sample_ok=False,
            note="Minimum 5 alerted incidents required to calculate time to detect.",
        )

    med_sec = round(_percentile(durations, 0.50), 1)
    p95_sec = round(_percentile(durations, 0.95), 1)

    return MetricResult(
        id="time_to_detect",
        value=med_sec,
        formatted=f"{med_sec}s",
        unit="seconds",
        n=n,
        sample_ok=True,
        note=f"p95: {p95_sec}s (measures tool detection delay from first error)",
        details={"median_sec": med_sec, "p95_sec": p95_sec},
    )


# ==============================================================================
# Metric 4: Time to Fix
# ==============================================================================
def compute_time_to_fix(resolved_incidents: List[Dict[str, Any]]) -> MetricResult:
    """
    last_event_at minus created_at in minutes for resolved incidents.
    Min n = 3 resolved incidents.
    """
    durations_min = []
    for inc in resolved_incidents:
        created_at = inc.get("created_at")
        last_event_at = inc.get("last_event_at")
        if created_at and last_event_at:
            diff = (last_event_at - created_at).total_seconds() / 60.0
            if diff >= 0:
                durations_min.append(diff)

    n = len(durations_min)
    if n < 3:
        return MetricResult(
            id="time_to_fix",
            value=None,
            formatted=f"Not enough data yet (n={n})",
            unit="minutes",
            n=n,
            sample_ok=False,
            note="Minimum 3 resolved incidents required to calculate time to fix.",
        )

    med_min = round(_percentile(durations_min, 0.50), 1)
    p95_min = round(_percentile(durations_min, 0.95), 1)

    return MetricResult(
        id="time_to_fix",
        value=med_min,
        formatted=f"{med_min} min",
        unit="minutes",
        n=n,
        sample_ok=True,
        note=f"p95: {p95_min} min (error duration from incident open to last error)",
        details={"median_min": med_min, "p95_min": p95_min},
    )


# ==============================================================================
# Metric 5: Alerts that were Real (Precision & Coverage)
# ==============================================================================
def compute_alerts_accuracy(feedback: List[Dict[str, Any]]) -> MetricResult:
    """
    real / (real + false) among rated alert feedback in window.
    Min n = 10 rated incidents.
    """
    rated = [f for f in feedback if f.get("label") in ("real", "false")]
    n_rated = len(rated)
    total_incidents = len(feedback)

    if n_rated < 10:
        return MetricResult(
            id="alerts_real",
            value=None,
            formatted=f"Not enough data yet (n={n_rated})",
            unit="percent",
            n=n_rated,
            sample_ok=False,
            note=f"{n_rated} of {total_incidents} alerts rated. Needs 10 ratings.",
        )

    real_cnt = sum(1 for f in rated if f.get("label") == "real")
    precision = round((real_cnt / n_rated) * 100.0, 1)

    return MetricResult(
        id="alerts_real",
        value=precision,
        formatted=f"{precision}%",
        unit="percent",
        n=n_rated,
        sample_ok=True,
        note=f"{real_cnt} of {n_rated} rated alerts were real issues",
        details={
            "precision_percent": precision,
            "real_count": real_cnt,
            "false_count": n_rated - real_cnt,
            "rated_coverage": f"{n_rated} of {total_incidents}",
        },
    )


# ==============================================================================
# Metric 6: Tool Uptime ("Fully Operational")
# ==============================================================================
def compute_tool_uptime(
    status_logs: List[Dict[str, Any]],
    window_minutes: float,
    now: Optional[datetime] = None,
) -> MetricResult:
    """
    1 - (degraded_minutes / window_minutes).
    Requires at least 24h (1440m) of history, otherwise 'Collecting data'.
    A stretch still open (no ended_at) counts until `now`; overlapping stretches of different
    components count once.
    """
    if window_minutes < 1440.0:
        return MetricResult(
            id="tool_uptime",
            value=None,
            formatted="Collecting data",
            unit="percent",
            n=int(window_minutes),
            sample_ok=False,
            note="Requires at least 24 hours of operating history.",
        )

    now = now or datetime.now(timezone.utc)
    spans = sorted(
        (log["started_at"], log.get("ended_at") or now)
        for log in status_logs
        if log.get("status") in ("degraded", "down") and log.get("started_at")
    )
    degraded_min = 0.0
    cur_start = cur_end = None
    for start, end in spans:
        if cur_end is not None and start <= cur_end:
            cur_end = max(cur_end, end)
            continue
        if cur_end is not None:
            degraded_min += (cur_end - cur_start).total_seconds() / 60.0
        cur_start, cur_end = start, end
    if cur_end is not None:
        degraded_min += (cur_end - cur_start).total_seconds() / 60.0

    uptime = max(0.0, min(100.0, round((1.0 - (degraded_min / window_minutes)) * 100.0, 1)))

    return MetricResult(
        id="tool_uptime",
        value=uptime,
        formatted=f"{uptime}%",
        unit="percent",
        n=int(window_minutes),
        sample_ok=True,
        note=f"Fully operational ({round(degraded_min, 1)} degraded min)",
        details={"uptime_percent": uptime, "degraded_minutes": round(degraded_min, 1)},
    )


# ==============================================================================
# Metric 9: Where Problems Come From (Hotspot Code Areas)
# ==============================================================================
def compute_hotspots(incidents: List[Dict[str, Any]], depth: int = 1) -> List[Dict[str, Any]]:
    """
    Group incidents by top path segment of where_file (never people).
    Returns top 8 areas with counts and share.
    """
    area_counts: Dict[str, int] = {}
    total = 0

    for inc in incidents:
        # Exclude incidents marked false alarm
        if inc.get("is_false_alarm"):
            continue

        raw_file = inc.get("where_file") or "unknown"
        norm = os.path.normpath(raw_file).replace("\\", "/").lstrip("/")
        parts = norm.split("/")
        area = "/".join(parts[:depth]) if len(parts) >= depth else (parts[0] if parts else "unknown")

        area_counts[area] = area_counts.get(area, 0) + 1
        total += 1

    sorted_areas = sorted(area_counts.items(), key=lambda x: x[1], reverse=True)[:8]

    return [
        {
            "area": area,
            "count": count,
            "share_percent": round((count / total) * 100.0, 1) if total > 0 else 0.0,
        }
        for area, count in sorted_areas
    ]


# ==============================================================================
# Metric 10: Likely Causes by Category
# ==============================================================================
def compute_categories(incidents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group incidents by error_category."""
    cat_counts: Dict[str, int] = {}
    total = len(incidents)

    for inc in incidents:
        cat = inc.get("category") or "uncategorized"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    sorted_cats = sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)
    return [
        {
            "category": cat,
            "count": count,
            "share_percent": round((count / total) * 100.0, 1) if total > 0 else 0.0,
        }
        for cat, count in sorted_cats
    ]


# ==============================================================================
# Metric 11: Alert Speed
# ==============================================================================
def compute_alert_speed(incidents: List[Dict[str, Any]]) -> MetricResult:
    """
    first_alert_ms as median and p95 against the 5,000 ms target.
    Min n = 5 incidents.
    """
    latencies = [
        float(inc["first_alert_ms"])
        for inc in incidents
        if inc.get("first_alert_ms") is not None and inc["first_alert_ms"] > 0
    ]
    n = len(latencies)
    if n < 5:
        return MetricResult(
            id="alert_speed",
            value=None,
            formatted=f"Not enough data yet (n={n})",
            unit="ms",
            n=n,
            sample_ok=False,
            note="Minimum 5 incidents required to benchmark alert speed.",
        )

    med_ms = round(_percentile(latencies, 0.50), 1)
    p95_ms = round(_percentile(latencies, 0.95), 1)

    return MetricResult(
        id="alert_speed",
        value=med_ms,
        formatted=f"{int(med_ms)} ms",
        unit="ms",
        n=n,
        sample_ok=True,
        note=f"p95: {int(p95_ms)} ms (target: < 5,000 ms)",
        details={"median_ms": med_ms, "p95_ms": p95_ms, "target_ms": 5000},
    )


# ==============================================================================
# Metric Definitions Catalog
# ==============================================================================
METRIC_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "releases": {
        "title": "Releases",
        "formula": "count(deploys in window)",
        "window": "Selected period (7, 30, 90 days)",
        "min_sample": 5,
        "caveats": "Includes deploys in selected environment. Pending verdicts excluded from safe-check ratio.",
    },
    "change_failure_rate": {
        "title": "Change Failure Rate",
        "formula": "deploys with confirmed incident or rollback / eligible deploys (>= 24h old)",
        "window": "Selected period",
        "min_sample": 10,
        "caveats": "Deploys younger than 24 hours are excluded so early numbers are not understated.",
    },
    "time_to_detect": {
        "title": "Time to Detect",
        "formula": "alerted_at - first_event_at (median & p95)",
        "window": "Selected period",
        "min_sample": 5,
        "caveats": "Measures tool alert delay from first error arrival, not duration bug lived in host app.",
    },
    "time_to_fix": {
        "title": "Time to Fix",
        "formula": "last_event_at - created_at for resolved incidents",
        "window": "Selected period",
        "min_sample": 3,
        "caveats": "Measures error duration; does not include auto-resolve wait time.",
    },
    "alerts_real": {
        "title": "Alerts that were Real",
        "formula": "real / (real + false) for rated incidents",
        "window": "Selected period",
        "min_sample": 10,
        "caveats": "Unrated and unclear feedback excluded from denominator.",
    },
    "tool_uptime": {
        "title": "Tool Uptime",
        "formula": "1 - (minutes with degraded component / total minutes)",
        "window": "Selected period (min 24h)",
        "min_sample": 1440,
        "caveats": "Logged by component watchdog monitoring ingest, worker, detector, queue, and database.",
    },
}
