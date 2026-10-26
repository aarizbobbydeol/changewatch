-- Migration 034: which GitHub repositories each project may read with the install-wide
-- GITHUB_READ_TOKEN (the investigator's fallback when a file's content is not stored). A deploy
-- names its repository itself, so without this list a project could name another team's
-- repository and read it with the owner's token. The install owner manages it with
-- `python -m app.cli allow-repo` / `remove-repo` / `list-repos`.
CREATE TABLE IF NOT EXISTS project_repositories (
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    repository TEXT NOT NULL,               -- owner/name, lower case (GitHub names ignore case)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, repository)
);

ALTER TABLE project_repositories ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_repositories FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS project_repositories_project_isolation ON project_repositories;
CREATE POLICY project_repositories_project_isolation ON project_repositories
    USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
    WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

DROP POLICY IF EXISTS project_repositories_system_scope ON project_repositories;
CREATE POLICY project_repositories_system_scope ON project_repositories
    USING (current_setting('app.system_scope', true) = 'all')
    WITH CHECK (current_setting('app.system_scope', true) = 'all');

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'changewatch_tenant') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON project_repositories TO changewatch_tenant;
  END IF;
END $$;

-- Existing installs keep working: a repository that only one project's deploys ever named is
-- allowed for that project. One named by several projects is allowed for none until the owner
-- decides. (System scope, so the backfill sees every project's deploys under row-level security.)
SELECT set_config('app.system_scope', 'all', true);
INSERT INTO project_repositories (project_id, repository)
SELECT min(project_id), lower(repository)
FROM deploys
WHERE repository IS NOT NULL
GROUP BY lower(repository)
HAVING count(DISTINCT project_id) = 1
ON CONFLICT DO NOTHING;
SELECT set_config('app.system_scope', '', true);
