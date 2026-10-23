"""Analytics Package."""
from app.analytics.metrics import (
    METRIC_DEFINITIONS,
    MetricResult,
    compute_releases,
    compute_change_failure_rate,
    compute_time_to_detect,
    compute_time_to_fix,
    compute_alerts_accuracy,
    compute_tool_uptime,
    compute_hotspots,
    compute_categories,
    compute_alert_speed,
)
from app.analytics.router import router as analytics_router

__all__ = [
    "METRIC_DEFINITIONS",
    "MetricResult",
    "compute_releases",
    "compute_change_failure_rate",
    "compute_time_to_detect",
    "compute_time_to_fix",
    "compute_alerts_accuracy",
    "compute_tool_uptime",
    "compute_hotspots",
    "compute_categories",
    "compute_alert_speed",
    "analytics_router",
]
