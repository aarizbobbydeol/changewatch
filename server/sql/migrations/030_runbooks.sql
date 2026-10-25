-- Migration 030: runbooks the team writes for a kind of error (app/runbooks.py).
-- Matched by error type, failing file and route; the steps are stored encrypted only.
CREATE TABLE IF NOT EXISTS runbooks (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    match_exception TEXT,                   -- error type, e.g. ZeroDivisionError
    match_path TEXT,                        -- glob on the failing file, e.g. app/billing/*
    match_route TEXT,                       -- glob on the request path, e.g. /checkout*
    steps TEXT,                             -- only for rows written before encryption
    steps_enc BYTEA,
    url TEXT,                               -- link to the team's own page (https)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT runbooks_need_a_matcher CHECK (
        match_exception IS NOT NULL OR match_path IS NOT NULL OR match_route IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS runbooks_project ON runbooks (project_id, id);

ALTER TABLE runbooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE runbooks FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS runbooks_project_isolation ON runbooks;
CREATE POLICY runbooks_project_isolation ON runbooks
    USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
    WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

DROP POLICY IF EXISTS runbooks_system_scope ON runbooks;
CREATE POLICY runbooks_system_scope ON runbooks
    USING (current_setting('app.system_scope', true) = 'all')
    WITH CHECK (current_setting('app.system_scope', true) = 'all');

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'changewatch_tenant') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON runbooks TO changewatch_tenant;
    GRANT USAGE, SELECT ON SEQUENCE runbooks_id_seq TO changewatch_tenant;
  END IF;
END $$;
