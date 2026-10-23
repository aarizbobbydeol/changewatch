-- Migration 012: SRE Incidents, Notification Channels, Team Members, and Typed Decisions
-- Specified in AI SRE Prompt Stage B and Typed Decisions Addendum Stages 1 & 2

-- 1. Extend projects table with SRE settings
ALTER TABLE projects ADD COLUMN IF NOT EXISTS critical_routes JSONB NOT NULL DEFAULT '[]';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS notify_quiet BOOLEAN NOT NULL DEFAULT false;

-- 2. Incidents table
CREATE TABLE IF NOT EXISTS incidents (
  id SERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deploy_id INT NOT NULL REFERENCES deploys(id) ON DELETE CASCADE,
  issue_id INT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'possible',     -- possible | confirmed | resolved
  title TEXT,
  where_file TEXT,
  where_line INT,
  where_function TEXT,
  in_diff BOOLEAN,
  owners JSONB NOT NULL DEFAULT '[]',
  event_count INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  confirmed_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ,
  alerted_at TIMESTAMPTZ,
  first_event_at TIMESTAMPTZ,
  last_event_at TIMESTAMPTZ,
  category TEXT,
  first_alert_ms INT,
  UNIQUE (deploy_id, issue_id)
);

CREATE INDEX IF NOT EXISTS incidents_project_created ON incidents (project_id, created_at);
CREATE INDEX IF NOT EXISTS incidents_deploy ON incidents (deploy_id);
CREATE INDEX IF NOT EXISTS incidents_issue ON incidents (issue_id);

ALTER TABLE incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE incidents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS incidents_project_policy ON incidents;
CREATE POLICY incidents_project_policy ON incidents
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- 3. Notification channels table
CREATE TABLE IF NOT EXISTS notification_channels (
  id SERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type TEXT NOT NULL,                         -- slack | discord | teams | email | webhook
  config JSONB NOT NULL DEFAULT '{}',
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE notification_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_channels FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS notif_channels_project_policy ON notification_channels;
CREATE POLICY notif_channels_project_policy ON notification_channels
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- 4. Team members table
CREATE TABLE IF NOT EXISTS team_members (
  id SERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  git_emails TEXT[] NOT NULL DEFAULT '{}',
  slack_user_id TEXT,
  discord_user_id TEXT,
  email TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS team_members_project ON team_members (project_id);

ALTER TABLE team_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE team_members FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS team_members_project_policy ON team_members;
CREATE POLICY team_members_project_policy ON team_members
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- 5. Typed Decisions table
CREATE TABLE IF NOT EXISTS decisions (
  id BIGSERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  incident_id INT REFERENCES incidents(id) ON DELETE CASCADE,
  question_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  answer JSONB NOT NULL,
  probability DOUBLE PRECISION,
  confidence DOUBLE PRECISION,
  source TEXT NOT NULL,
  model TEXT,
  catalog_version TEXT NOT NULL,
  state_hash TEXT NOT NULL,
  latency_ms INT,
  cost_micros BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS decisions_incident ON decisions (incident_id);
CREATE INDEX IF NOT EXISTS decisions_project_created ON decisions (project_id, created_at);

ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE decisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS decisions_project_policy ON decisions;
CREATE POLICY decisions_project_policy ON decisions
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
