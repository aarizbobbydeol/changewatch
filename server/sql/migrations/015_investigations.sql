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
  cost_micros INT NOT NULL DEFAULT 0,
  not_sure_reason TEXT                        -- why confidence is low (or why the run was paused)
);

-- Migration 006 already created an early, deploy-based placeholder `investigations`
-- table, so on every real database the CREATE above is a no-op. Upgrade that table
-- in place so the columns used by the investigator exist.
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS incident_id INT REFERENCES incidents(id) ON DELETE CASCADE;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS steps JSONB NOT NULL DEFAULT '[]';
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS result JSONB;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS result_enc BYTEA;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS input_tokens INT NOT NULL DEFAULT 0;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS output_tokens INT NOT NULL DEFAULT 0;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS cached_from INT REFERENCES investigations(id) ON DELETE SET NULL;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS cost_micros INT NOT NULL DEFAULT 0;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS not_sure_reason TEXT;
ALTER TABLE investigations ALTER COLUMN status SET DEFAULT 'running';
DO $$
BEGIN
  -- The placeholder required a deploy; investigations are keyed by incident now.
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'investigations' AND column_name = 'deploy_id') THEN
    ALTER TABLE investigations ALTER COLUMN deploy_id DROP NOT NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS investigations_incident ON investigations (incident_id);
CREATE INDEX IF NOT EXISTS investigations_proj_time ON investigations (project_id, started_at);

ALTER TABLE investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE investigations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS investigations_project_policy ON investigations;
CREATE POLICY investigations_project_policy ON investigations
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
