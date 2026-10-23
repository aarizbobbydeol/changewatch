-- Migration 015: Investigations Table
-- Specified in Phase 2 AI SRE Stage C5

CREATE TABLE IF NOT EXISTS investigations (
  id BIGSERIAL PRIMARY KEY,
  incident_id INT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'running',     -- running | done | partial | failed
  model TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  steps JSONB NOT NULL DEFAULT '[]',          -- audit log of every tool call and its duration
  result JSONB,                               -- validated InvestigationOutput
  result_enc BYTEA,                           -- encrypted result if column encryption is active
  input_tokens INT NOT NULL DEFAULT 0,
  output_tokens INT NOT NULL DEFAULT 0,
  cached_from INT REFERENCES investigations(id) ON DELETE SET NULL,
  cost_micros INT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS investigations_incident ON investigations (incident_id);
CREATE INDEX IF NOT EXISTS investigations_proj_time ON investigations (project_id, started_at);

ALTER TABLE investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE investigations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS investigations_project_policy ON investigations;
CREATE POLICY investigations_project_policy ON investigations
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
