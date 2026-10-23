-- Migration 001: Initial ChangeWatch Schema
-- Converted from schema.sql without hardcoded production keys

CREATE TABLE IF NOT EXISTS projects (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  api_key TEXT NOT NULL UNIQUE,
  slack_webhook_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deploys (
  id SERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  commit_sha TEXT NOT NULL,
  author TEXT,
  message TEXT,
  diff TEXT,
  environment TEXT NOT NULL DEFAULT 'prod',
  deployed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deploys_lookup ON deploys (project_id, commit_sha);
CREATE INDEX IF NOT EXISTS deploys_time ON deploys (project_id, environment, deployed_at DESC);

CREATE TABLE IF NOT EXISTS issues (
  id SERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  title TEXT,
  first_seen TIMESTAMPTZ NOT NULL,
  last_seen TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  UNIQUE (project_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  issue_id INT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deploy_id INT REFERENCES deploys(id) ON DELETE SET NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  stack_trace TEXT,
  request_path TEXT,
  duration_ms REAL,
  extra JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_project_time ON events (project_id, occurred_at);
CREATE INDEX IF NOT EXISTS events_deploy ON events (deploy_id);

CREATE TABLE IF NOT EXISTS deploy_analysis (
  id SERIAL PRIMARY KEY,
  deploy_id INT NOT NULL UNIQUE REFERENCES deploys(id) ON DELETE CASCADE,
  new_issues INT,
  errors_before INT,
  errors_after INT,
  flagged BOOLEAN NOT NULL DEFAULT false,
  likely_cause TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
  id SERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deploy_id INT NOT NULL UNIQUE REFERENCES deploys(id) ON DELETE CASCADE,
  channel TEXT NOT NULL,
  sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Row Level Security (RLS) Policies
ALTER TABLE deploys ENABLE ROW LEVEL SECURITY;
ALTER TABLE deploys FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS deploys_project_policy ON deploys;
CREATE POLICY deploys_project_policy ON deploys
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

ALTER TABLE issues ENABLE ROW LEVEL SECURITY;
ALTER TABLE issues FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS issues_project_policy ON issues;
CREATE POLICY issues_project_policy ON issues
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS events_project_policy ON events;
CREATE POLICY events_project_policy ON events
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS alerts_project_policy ON alerts;
CREATE POLICY alerts_project_policy ON alerts
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
