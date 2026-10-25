-- Migration 025: component_status_log rows become intervals (started_at .. ended_at).
-- Before this, the watchdog added one row per minute while a component was unwell and never set
-- ended_at, so tool uptime was always 100%. Each old row stood for one watchdog observation.
UPDATE component_status_log SET ended_at = started_at + INTERVAL '1 minute' WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS csl_open ON component_status_log (component) WHERE ended_at IS NULL;
