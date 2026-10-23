-- Migration 014: Sources (Dead-man's switch) and System Alerts (Watchdog)
-- Specified in Trust and Reliability Addendum (TRUST Stages 2 and 3)

CREATE TABLE IF NOT EXISTS sources (
  id BIGSERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  environment TEXT NOT NULL DEFAULT 'prod',
  app_name TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'active',     -- active | silent | idle | recovering
  last_heartbeat_at TIMESTAMPTZ,
  last_event_at TIMESTAMPTZ,
  typical_gap_sec INT NOT NULL DEFAULT 60,
  silent_since TIMESTAMPTZ,
  expected_hours JSONB,                      -- e.g. {"weekdays": [8, 20]} or NULL for 24/7
  silence_alerts BOOLEAN NOT NULL DEFAULT true, -- false for staging or low-traffic
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, environment, app_name)
);

CREATE INDEX IF NOT EXISTS sources_proj_env ON sources (project_id, environment);

ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE sources FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sources_project_policy ON sources;
CREATE POLICY sources_project_policy ON sources
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

CREATE TABLE IF NOT EXISTS system_alerts (
  id BIGSERIAL PRIMARY KEY,
  project_id INT REFERENCES projects(id) ON DELETE CASCADE,
  component TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'high',     -- high | critical | info
  title TEXT NOT NULL,
  detail TEXT,
  status TEXT NOT NULL DEFAULT 'active',     -- active | resolved
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sys_alerts_comp ON system_alerts (component, status);
CREATE INDEX IF NOT EXISTS sys_alerts_proj ON system_alerts (project_id);

ALTER TABLE system_alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_alerts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sys_alerts_project_policy ON system_alerts;
CREATE POLICY sys_alerts_project_policy ON system_alerts
  FOR ALL
  USING (project_id IS NULL OR project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id IS NULL OR project_id = NULLIF(current_setting('app.project_id', true), '')::int);
