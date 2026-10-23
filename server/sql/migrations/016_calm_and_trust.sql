-- Migration 016: Calm and Trust Features
-- Implements TRUST Stages 4 to 8 and TD Stages 6 & 8
-- Introduces:
-- 1. Incidents lifecycle extension (ack, assign, escalation counters)
-- 2. incident_events table (audit timeline of actions and state transitions)
-- 3. alert_feedback table (real, false, unclear ratings)
-- 4. ignore_rules table (temporary expiring mute rules)

-- 1. Extend incidents table
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS acked_at TIMESTAMPTZ;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS acked_by TEXT;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS assigned_to TEXT;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS escalated_count INT NOT NULL DEFAULT 0;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS last_escalated_at TIMESTAMPTZ;

-- 2. Incident events audit table
CREATE TABLE IF NOT EXISTS incident_events (
  id BIGSERIAL PRIMARY KEY,
  incident_id INT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type TEXT NOT NULL,                         -- created | alerted | confirmed | acked | assigned | resolved | reopened | escalated | muted | feedback
  actor TEXT NOT NULL DEFAULT 'system',
  detail JSONB NOT NULL DEFAULT '{}',
  detail_enc BYTEA,
  at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ie_incident_at ON incident_events (incident_id, at ASC);
CREATE INDEX IF NOT EXISTS ie_project_at ON incident_events (project_id, at DESC);

ALTER TABLE incident_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS incident_events_project_policy ON incident_events;
CREATE POLICY incident_events_project_policy ON incident_events
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- 3. Alert feedback table
CREATE TABLE IF NOT EXISTS alert_feedback (
  id BIGSERIAL PRIMARY KEY,
  incident_id INT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  actor TEXT NOT NULL DEFAULT 'system',
  label TEXT NOT NULL,                         -- real | false | unclear
  comment TEXT,
  comment_enc BYTEA,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS af_incident ON alert_feedback (incident_id);
CREATE INDEX IF NOT EXISTS af_project_created ON alert_feedback (project_id, created_at);

ALTER TABLE alert_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_feedback FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS alert_feedback_project_policy ON alert_feedback;
CREATE POLICY alert_feedback_project_policy ON alert_feedback
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- 4. Ignore (mute) rules table
CREATE TABLE IF NOT EXISTS ignore_rules (
  id BIGSERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                         -- fingerprint | path_prefix | exc_type
  pattern TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_by TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ir_project_expires ON ignore_rules (project_id, expires_at);

ALTER TABLE ignore_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE ignore_rules FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ignore_rules_project_policy ON ignore_rules;
CREATE POLICY ignore_rules_project_policy ON ignore_rules
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
