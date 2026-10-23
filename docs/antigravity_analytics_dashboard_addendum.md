# ADDENDUM: Analytics and operations dashboard

Goal: one page where a team can see, at a glance, **how their releases are doing and how well ChangeWatch itself is working**: release health, incident speed and accuracy, where problems come from, the tool's own operations, and usage. It matches the "Analytics dashboard" mockup (if I attach screenshots, follow them).

This addendum fits the master prompt's milestones (see section J). Do not build it early. If earlier specs promised something that is missing, tell me first. I work on macOS with no Homebrew; use `python3` and a venv. When this conflicts with earlier prompts, this one wins; record changes in `DECISIONS.md`.

## 0. Rules

1. **One source of truth for every number.** All metric math lives in `server/app/analytics/metrics.py` as pure functions over query results. The API, the weekly digest, and the Health and Trust pages all call these functions. A parity test proves the digest and the dashboard show identical numbers.
2. **Honest data only.** Never show fake or placeholder numbers in real views. Every tile shows its **sample size** and **freshness**. When there is not enough data, show "Not enough data yet (n=3)" instead of a number. When a feature is off or its milestone is not built, show "Not enabled" with one line on how to turn it on, never a zero.
3. **Definitions are shown, not hidden.** Every tile has an info tooltip with its definition, formula, window, and caveats, served from the API so the UI cannot drift from the code.
4. **No individual rankings.** No endpoint or view may aggregate or rank by person (author, assignee, owner as a person). Use areas of code and trends only. Add a static test for this.
5. **Read-only and cheap.** The page only reads. It must never query the raw `events` table for aggregates; use `deploys`, `incidents`, `incident_events`, `deploy_analysis`, `alert_feedback`, `decisions`, `investigations`, `usage_daily`, `budget_state`, `sources`, `event_rollups`, and the small tables in section C.
6. Everything is project-scoped through the key and covered by tenant isolation tests. Times use the project timezone (default UTC, user-selectable in the UI).

## A. Page layout (match the mockup)

Header: app and environment selectors (default `prod`), period selector (7, 30, 90 days; default 30; kept in the URL), timezone, "Updated N seconds ago".

1. **KPI row (6 tiles):** Releases, Change failure rate, Time to detect, Time to fix, Alerts that were real, Tool uptime.
2. **Charts (2):** Release verdicts per week (stacked bars: safe, watch, rollback) and median time to fix per week (line).
3. **Breakdowns (2):** Where problems come from (areas of code) and Likely causes by category.
4. **Is the tool working well? (panel):** alert speed (median and p95 against the 5 second target), app data flowing (per source), events received, stored, and dropped, notifications delivered, AI investigations, and the calibration line.
5. **Usage this month (panel):** spend so far, budget bar, per-meter units, forecast.

## B. Metric definitions (exact)

For each metric, implement exactly this, show `n`, and mark `sample_ok` using the minimum shown.

1. **Releases:** count of `deploys` in the window and environment. Also "safe at first check": deploys whose first-check verdict is `safe`, divided by deploys that have a first-check verdict (`pending` excluded from the denominator; `no_data` stays in the denominator and is counted in the tooltip). Minimum n = 5.
2. **Change failure rate:** deploys that had at least one incident that became `confirmed` (or a `rollback` verdict) within 24 hours after the deploy, divided by deploys in the window **that are at least 24 hours old** (younger deploys are excluded so the number is not understated; say so in the tooltip). Show as "4 of 52" plus the percentage. Minimum n = 10 eligible deploys.
3. **Time to detect:** for incidents opened in the window, `alerted_at` minus `first_event_at`, where `first_event_at` is the time of the first event of that issue after the deploy. Show median and p95. This measures the tool's own delay, not how long the bug lived in the app; say so. Also show "deploy to first alert" as a secondary value. Minimum n = 5 incidents.
4. **Time to fix:** for incidents **resolved** in the window (automatically or manually), `last_event_at` minus `created_at`, which is how long the errors lasted. Using the resolve timestamp would add the auto-resolve waiting time, so show that variant only in the tooltip. Open incidents are excluded and shown as "N still open". Minimum n = 3.
5. **Alerts that were real:** among incidents alerted in the window that have feedback, `real / (real + false)`; `unclear` is excluded. Always show coverage ("34 of 57 rated"). Minimum 10 rated.
6. **Tool uptime ("fully operational"):** `1 - minutes with any core component degraded or down / total minutes in the window`, using `component_status_log` for ingest, worker, detector, and notifier. Also show degraded minutes in the tooltip. Show one decimal. Need at least 24 hours of history, otherwise "collecting data".
7. **Verdicts per week:** bucket deploys by ISO week in the selected timezone (use `date_trunc('week', ... at time zone tz)`), colored by the final verdict (use the first-check verdict if the final check has not run; exclude `pending`). Include `no_data` as a grey segment.
8. **Time to fix per week:** metric 4 grouped by week; weeks with fewer than 3 resolved incidents show as a gap with a tooltip.
9. **Where problems come from (areas of code):** incidents in the window that were not marked false alarm, grouped by `area(where_file)`. `area` is the first `AREA_DEPTH` path segments (default 1, configurable per project, with an optional list of path prefixes for monorepos). Unknown file maps to "unknown". Show the top 8 with counts and share. Never show people.
10. **Likely causes by category:** `error_category` from the decision layer if present, otherwise from the investigator, otherwise "uncategorized". Show the source mix (model, rules) in the tooltip.
11. **Alert speed:** `first_alert_ms` from `incidents` as median and p95, with a marker at 5,000 ms. Minimum n = 5.
12. **App data flowing:** per source (project, environment): age of `last_heartbeat_at`, status `ok` or `silent` using the silent threshold from the trust spec.
13. **Events received, stored, dropped:** from `ingest_stats_daily` (section C). Received = accepted by the API. Stored = full rows written. Counted only = counted but not stored because of storm control (normal during a crash loop). Dropped = rejected or dropped (rate limit, backpressure, invalid). Explain in the tooltip that stored is lower than received by design.
14. **Notifications delivered:** from the outbox, `delivered / (delivered + permanently failed)` in the window, plus retried count and median delivery delay. Channel breakdown in the tooltip.
15. **AI investigations:** count, median duration, and the split by confidence (high, medium, low), plus failed or partial counts. If the investigator is disabled, show "Not enabled".
16. **Calibration line:** from the calibration job, the bin closest to 90 percent for `is_regression_from_release`: "When we said about 90 percent, we were right X percent (n cases)". Minimum 10 cases in that bin, otherwise "collecting data". Link to the Trust page. If the decision layer is not enabled, show "Not enabled".
17. **Usage this month:** from `usage_daily` and `budget_state`: spend so far (micro-USD converted to dollars with proper rounding), soft and hard limits, percent used, per-meter units with free allowance used, and the forecast from the usage spec. In self-hosted installs label the AI part as "estimated AI provider cost". If metering is off, show "Not enabled".

## C. Data additions (new migration; row-level security on each; all small tables)

```sql
CREATE TABLE component_status_log (
  id BIGSERIAL PRIMARY KEY,
  project_id INT,                          -- null for system-wide components
  component TEXT NOT NULL,                 -- ingest | worker | detector | notifier | investigator | database | queue
  status TEXT NOT NULL,                    -- ok | degraded | down
  started_at TIMESTAMPTZ NOT NULL,
  ended_at TIMESTAMPTZ,
  detail TEXT
);
CREATE INDEX csl_time ON component_status_log (component, started_at);

CREATE TABLE ingest_stats_daily (
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  day DATE NOT NULL,
  received BIGINT NOT NULL DEFAULT 0,
  stored BIGINT NOT NULL DEFAULT 0,
  counted_only BIGINT NOT NULL DEFAULT 0,
  dropped_rate_limit BIGINT NOT NULL DEFAULT 0,
  dropped_backpressure BIGINT NOT NULL DEFAULT 0,
  dropped_invalid BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, day)
);
```
Also ensure `incidents` has `first_event_at`, `alerted_at`, `last_event_at`, `resolved_at`, `category`, and `where_file` (add any missing columns), and add indexes: `incidents (project_id, created_at)`, `deploys (project_id, environment, deployed_at)`, `alert_feedback (incident_id)`, `deploy_analysis (deploy_id)`.
The watchdog writes `component_status_log` on every state change (close the previous interval, open the new one). The worker and ingest increment `ingest_stats_daily` using batched counters (flush every minute, same pattern as usage counters). Nothing in the hot path may slow down.

## D. API (read key; all take `period` or `from` and `to`, `env`, and `tz`)

- `GET /v1/analytics/overview`: all six KPI tiles in one response.
- `GET /v1/analytics/verdicts-by-week`, `GET /v1/analytics/ttf-by-week`
- `GET /v1/analytics/hotspots`, `GET /v1/analytics/categories`
- `GET /v1/analytics/operations`: alert speed, sources, ingest stats, notifications, AI investigations, calibration line
- `GET /v1/analytics/usage-summary`
- `GET /v1/analytics/definitions`: the text, formula, window, minimum sample, and caveats for every metric (single source for the UI tooltips and `docs/METRICS.md`)
- `GET /v1/analytics/export.csv?metric=<id>` for each panel

Every metric object looks like `{ "id", "value", "unit", "n", "sample_ok", "note", "computed_at" }`. When `sample_ok` is false, `value` is null and the UI shows the "not enough data" state. Responses are cached for 60 seconds with an ETag. Target: each endpoint under 200 ms p95 on a test dataset of 50,000 deploys and 20,000 incidents; run `EXPLAIN ANALYZE` on every query and record results.

## E. UI (Next.js, same design language as the other pages)

- Route `/analytics`, tiles and charts as in the mockup. Use the charting library already in the project (recharts); do not add another.
- Tiles show value, one-line context, `n`, and an info icon with the definition from the API.
- **Not color alone:** verdict segments also use distinct patterns or labels and a text legend; keyboard accessible; sufficient contrast.
- **Drill-downs:** clicking Change failure rate opens the list of failing deploys; clicking a hotspot area opens that area's incidents; clicking a week bar opens that week's deploys.
- CSV export on each panel; auto-refresh every 60 seconds, paused when the tab is hidden; "Updated N seconds ago".
- States for every tile and panel: loading, error with retry, insufficient data, not enabled, and empty. Responsive down to a phone width; print-friendly.
- A small "How these numbers are calculated" link to `docs/METRICS.md`.

## F. Stages (stop after each and report)

1. Metric definitions and pure functions in `metrics.py`, with fixtures and expected values
2. Migration, status-log writer in the watchdog, ingest stats counters
3. API endpoints, definitions endpoint, caching, `EXPLAIN` review
4. UI page: KPI tiles, charts, breakdowns, operations and usage panels, all states
5. Drill-downs, CSV export, auto-refresh, accessibility pass
6. Digest and Health and Trust page parity (same functions), `docs/METRICS.md` generated from the definitions
7. Performance test dataset and final review

## G. Where it fits in the milestones

- Stages 1 to 3 can start after **M3**: releases, verdicts, incidents, alert speed, ingest stats, and sources already exist.
- Tiles that depend on later work simply show "Not enabled": AI investigations (M5), calibration (M5 and M6), feedback accuracy and notification stats (M6), tool uptime (needs the watchdog from M4), usage (M8).
- The full page is expected to be complete after M8.

## H. Tests (all required)

- A tiny fixture dataset with hand-computed expected values for every metric in section B.
- Edge cases: deploys younger than 24 hours excluded from change failure rate; `no_data` and `pending` verdicts; open versus resolved incidents; incidents with no feedback; duplicate or reopened incidents; week boundaries and daylight saving time in several timezones; weeks with too few incidents; micro-USD to dollar rounding.
- Minimum sample rules: values are null and `sample_ok` is false below the minimum, and the UI shows the right state.
- Tenant isolation on every endpoint; the admin or owner data never appears here.
- A static check that no endpoint aggregates or ranks by person.
- Digest parity: the weekly digest numbers equal the dashboard numbers for the same period.
- Performance test: every endpoint within the target on the large test dataset.
- CSV export matches the API data.
- UI tests for loading, error, insufficient data, not enabled, and empty states, plus an accessibility check.
- "No fake data": with an empty database, no tile shows a number.

## I. Definition of done

On a project with real activity, every tile on the page shows a correct, sourced number with its sample size, or an honest "not enough data" or "not enabled" state. The tooltips match the documented definitions. The digest and the dashboard agree. Every endpoint meets its performance target. No view shows or ranks individual people.
