-- Migration 006: Application-Level Column Encryption (BYTEA Columns)
-- Specified in Security Addendum Section C2

-- 1. Encrypted columns for deploys
ALTER TABLE deploys
  ADD COLUMN IF NOT EXISTS diff_enc BYTEA,
  ADD COLUMN IF NOT EXISTS codeowners_enc BYTEA;

-- 2. Encrypted columns for events
ALTER TABLE events
  ADD COLUMN IF NOT EXISTS stack_trace_enc BYTEA,
  ADD COLUMN IF NOT EXISTS extra_enc BYTEA;

-- 3. Encrypted columns for projects
ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS slack_webhook_url_enc BYTEA;

-- 4. Deploy files table for full code context
CREATE TABLE IF NOT EXISTS deploy_files (
  id BIGSERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deploy_id INT NOT NULL REFERENCES deploys(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  content_enc BYTEA,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, deploy_id, path)
);
ALTER TABLE deploy_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE deploy_files FORCE ROW LEVEL SECURITY;
CREATE POLICY deploy_files_project_isolation ON deploy_files
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- 5. Investigations table for AI investigation results and steps
CREATE TABLE IF NOT EXISTS investigations (
  id BIGSERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deploy_id INT NOT NULL REFERENCES deploys(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending',
  result_enc BYTEA,
  steps_enc BYTEA,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE investigations FORCE ROW LEVEL SECURITY;
CREATE POLICY investigations_project_isolation ON investigations
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- 6. Notification channels table
CREATE TABLE IF NOT EXISTS notification_channels (
  id SERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  config_enc BYTEA,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE notification_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_channels FORCE ROW LEVEL SECURITY;
CREATE POLICY notification_channels_project_isolation ON notification_channels
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
