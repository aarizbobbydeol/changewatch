-- Migration 029: a risk review of each deploy's diff (rules, plus AI when available).
-- Read-only toward the customer's code: it reads the stored diff and writes only here.
-- The result quotes code, so it is stored encrypted only.
CREATE TABLE IF NOT EXISTS deploy_reviews (
    deploy_id INT PRIMARY KEY REFERENCES deploys(id) ON DELETE CASCADE,
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL,                    -- rules | done (rules + AI)
    risk TEXT NOT NULL,                      -- low | medium | high
    result_enc BYTEA,
    model TEXT,
    input_tokens INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS deploy_reviews_project ON deploy_reviews (project_id, created_at);

ALTER TABLE deploy_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE deploy_reviews FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS deploy_reviews_project_isolation ON deploy_reviews;
CREATE POLICY deploy_reviews_project_isolation ON deploy_reviews
    USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
    WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

DROP POLICY IF EXISTS deploy_reviews_system_scope ON deploy_reviews;
CREATE POLICY deploy_reviews_system_scope ON deploy_reviews
    USING (current_setting('app.system_scope', true) = 'all')
    WITH CHECK (current_setting('app.system_scope', true) = 'all');

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'changewatch_tenant') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON deploy_reviews TO changewatch_tenant;
  END IF;
END $$;
