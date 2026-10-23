-- Migration 013: Analytics, Component Status Log, and Ingest Stats Daily
-- Specified in Analytics Dashboard Addendum (AN Stage 2)

CREATE TABLE IF NOT EXISTS component_status_log (
  id BIGSERIAL PRIMARY KEY,
  project_id INT REFERENCES projects(id) ON DELETE CASCADE,
  component TEXT NOT NULL,                 -- ingest | worker | detector | notifier | investigator | database | queue
  status TEXT NOT NULL,                    -- ok | degraded | down
  started_at TIMESTAMPTZ NOT NULL,
  ended_at TIMESTAMPTZ,
  detail TEXT
);

CREATE INDEX IF NOT EXISTS csl_time ON component_status_log (component, started_at);
CREATE INDEX IF NOT EXISTS csl_proj ON component_status_log (project_id);

CREATE TABLE IF NOT EXISTS ingest_stats_daily (
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

-- Index optimizations from AN Section C
CREATE INDEX IF NOT EXISTS deploys_proj_env_time ON deploys (project_id, environment, deployed_at);
CREATE INDEX IF NOT EXISTS deploy_analysis_deploy ON deploy_analysis (deploy_id);

-- Row-Level Security
ALTER TABLE ingest_stats_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_stats_daily FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ingest_stats_project_policy ON ingest_stats_daily;
CREATE POLICY ingest_stats_project_policy ON ingest_stats_daily
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

ALTER TABLE component_status_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE component_status_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS csl_project_policy ON component_status_log;
CREATE POLICY csl_project_policy ON component_status_log
  FOR ALL
  USING (project_id IS NULL OR project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id IS NULL OR project_id = NULLIF(current_setting('app.project_id', true), '')::int);
