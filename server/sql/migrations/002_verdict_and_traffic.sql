-- Migration 002: Add Traffic Heartbeats Table and Deploy Verdict Columns
-- Specified in Addendum Sections C1 and C2

-- C1. Traffic Heartbeats Table
CREATE TABLE IF NOT EXISTS traffic (
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deploy_id INT REFERENCES deploys(id) ON DELETE SET NULL,
  bucket TIMESTAMPTZ NOT NULL,
  requests INT NOT NULL DEFAULT 0,
  errors INT NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, bucket)
);
CREATE INDEX IF NOT EXISTS traffic_deploy ON traffic (deploy_id);

-- Enable Row Level Security on traffic table
ALTER TABLE traffic ENABLE ROW LEVEL SECURITY;
ALTER TABLE traffic FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS traffic_project_policy ON traffic;
CREATE POLICY traffic_project_policy ON traffic
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- C2. Deploy Verdict Columns
ALTER TABLE deploy_analysis
  ADD COLUMN IF NOT EXISTS verdict TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS verdict_reasons JSONB NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS verdict_updated_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS verdict_final BOOLEAN NOT NULL DEFAULT false;

-- C2. Alerts Kind Column and Updated Unique Constraint
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'flag';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'alerts_deploy_id_key'
  ) THEN
    ALTER TABLE alerts DROP CONSTRAINT alerts_deploy_id_key;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'alerts_deploy_kind_key'
  ) THEN
    ALTER TABLE alerts ADD CONSTRAINT alerts_deploy_kind_key UNIQUE (deploy_id, kind);
  END IF;
END $$;
