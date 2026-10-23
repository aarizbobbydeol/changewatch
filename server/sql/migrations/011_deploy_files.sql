-- Migration 011: Deploy Files and CODEOWNERS for Release Prep
-- Specified in AI SRE Prompt Stage A

ALTER TABLE deploys ADD COLUMN IF NOT EXISTS codeowners TEXT;

CREATE TABLE IF NOT EXISTS deploy_files (
  id BIGSERIAL PRIMARY KEY,
  deploy_id INT NOT NULL REFERENCES deploys(id) ON DELETE CASCADE,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  status TEXT NOT NULL,
  added_ranges JSONB NOT NULL DEFAULT '[]',
  authors JSONB NOT NULL DEFAULT '[]',
  content TEXT,
  is_dependency_file BOOLEAN NOT NULL DEFAULT false,
  is_config_file BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS deploy_files_deploy ON deploy_files (deploy_id);
CREATE INDEX IF NOT EXISTS deploy_files_project_path ON deploy_files (project_id, path);

-- Row-Level Security
ALTER TABLE deploy_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE deploy_files FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS deploy_files_project_policy ON deploy_files;
CREATE POLICY deploy_files_project_policy ON deploy_files
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
