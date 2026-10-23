# ChangeWatch Metric Definitions and Calculation Catalog

This document is the authoritative specification for all metrics displayed on the ChangeWatch Analytics Dashboard (`/analytics`), the Team Overview (`/`), the Trust page, and the Weekly Digest.

All metric calculations are pure functions implemented in `server/app/analytics/metrics.py`. They operate exclusively over pre-aggregated records and incident metadata, never querying raw events directly.

---

## Core Principles & Guarantees

1. **Single Source of Truth:** The dashboard, weekly digest, and trust pages use the exact same calculation functions. A number seen on the dashboard is identical to the number reported in the email or Slack digest for the same period.
2. **Honest Empty States:** If the minimum sample threshold (\(n < \text{min\_sample}\)) is not met, `sample_ok` is `false`, `value` is `null`, and the UI displays an explicit "Not enough data yet (n=...)" badge. No tile ever guesses, extrapolates, or displays placeholder zeros.
3. **No Developer Rankings:** ChangeWatch strictly measures codebase health, release stability, and tool responsiveness. As verified by automated static AST analysis, there are zero functions or queries that rank, score, or compare individual engineers.
4. **Tenant Isolation:** Every metric is computed within the authenticated project's cryptographic and database scope. No cross-tenant data is ever included.

---

## Metric Catalog

### 1. Releases
- **Metric ID:** `releases`
- **Title:** Releases & Initial Stability
- **Formula:** \(\text{count}(\text{deploys in window})\) and \(\frac{\text{deploys safe at check}}{\text{evaluated deploys}}\)
- **Window:** Selected period (default: 30 days; 7, 30, 90 days available)
- **Minimum Sample (\(n\)):** 5 deploys
- **Caveats:** Pending verdicts and deploys with `no_data` verdicts are excluded from the initial safe-check calculation until evaluation finishes.

### 2. Change Failure Rate (CFR)
- **Metric ID:** `change_failure_rate`
- **Title:** Change Failure Rate
- **Formula:** \(\frac{\text{deploys with confirmed incident or rollback}}{\text{eligible deploys (age } \ge 24\text{h)}}\)
- **Window:** Selected period
- **Minimum Sample (\(n\)):** 10 deploys
- **Caveats:** Deploys younger than 24 hours are excluded so early observations do not falsely understate failure rates.

### 3. Time to Detect (TTD)
- **Metric ID:** `time_to_detect`
- **Title:** Time to Detect
- **Formula:** \(\text{median}(\text{alerted\_at} - \text{first\_event\_at})\) and \(p95\) across incidents
- **Window:** Selected period
- **Minimum Sample (\(n\)):** 5 incidents
- **Caveats:** Measures the tool's alerting delay from when the first error event arrives at the server, not the time the bug may have lived unnoticed in the host application prior to logging.

### 4. Time to Fix (TTF)
- **Metric ID:** `time_to_fix`
- **Title:** Time to Fix (Median TTF)
- **Formula:** \(\text{median}(\text{last\_event\_at} - \text{created\_at})\) for resolved incidents
- **Window:** Selected period
- **Minimum Sample (\(n\)):** 3 resolved incidents
- **Caveats:** Measures the actual duration of the error occurrence; does not include the 30-minute auto-resolve inactivity observation window.

### 5. Alerts that were Real (Alert Accuracy)
- **Metric ID:** `alerts_real`
- **Title:** Share of Alerts that were Real
- **Formula:** \(\frac{\text{real}}{\text{real} + \text{false}}\) across rated alerts
- **Window:** Selected period
- **Minimum Sample (\(n\)):** 10 rated alerts
- **Caveats:** Unrated alerts and alerts marked as "unclear" are excluded from the denominator.

### 6. Tool Uptime
- **Metric ID:** `tool_uptime`
- **Title:** ChangeWatch Self-Hosted Uptime
- **Formula:** \(1.0 - \frac{\text{minutes with any degraded component}}{\text{total window minutes}}\)
- **Window:** Selected period (minimum 24 hours / 1,440 minutes)
- **Minimum Sample (\(n\)):** 1,440 minutes
- **Caveats:** Tracked by the continuous background watchdog monitoring the ingest API, Redis pipeline, workers, detector, and PostgreSQL.

### 7. Alert Delivery Speed
- **Metric ID:** `alert_speed`
- **Title:** Instant Alert Speed
- **Formula:** \(\text{median}(\text{first\_alert\_ms})\) and \(p95\) against the 5,000 ms SLA
- **Window:** Selected period
- **Minimum Sample (\(n\)):** 5 incidents
- **Caveats:** Measured from ingest arrival to notification outbox delivery. Target is \(p95 < 5,000\text{ ms}\).

### 8. Code Hotspots
- **Metric ID:** `hotspots`
- **Title:** Problematic Code Areas
- **Formula:** Grouping incidents by top-level source path or package
- **Window:** Selected period
- **Caveats:** Shows code paths and modules, never authors or individuals.

### 9. Error Categories
- **Metric ID:** `categories`
- **Title:** Error Category Breakdown
- **Formula:** Grouping incidents by root-cause classification (e.g., `schema_migration`, `dependency_break`, `runtime_error`, `config_drift`, `null_pointer`)
- **Window:** Selected period

---

## Machine-Readable Definitions Endpoint

The JSON schema and metadata for each metric are served dynamically by the API at:
```http
GET /v1/analytics/definitions
```
Header: `X-API-Key: <read-key>`

The dashboard tooltips and UI elements fetch this endpoint directly, ensuring documentation and user interface text never diverge.
