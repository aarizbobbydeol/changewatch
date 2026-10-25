-- Migration 028: cost tracking.
--
-- ai_usage: the token use and cost of the customer's own AI calls (SDK record_ai_usage), summed
-- per environment, minute, model and feature, and tied to the deploy that served them, so a
-- release that makes AI spend per request jump can be spotted. Money is integer micro-USD.
--
-- projects.failed_request_value_micros: optional business value of one failed request, used to
-- estimate what an incident cost.
CREATE TABLE IF NOT EXISTS ai_usage (
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    environment TEXT NOT NULL DEFAULT 'prod',
    bucket TIMESTAMPTZ NOT NULL,
    model TEXT NOT NULL,
    feature TEXT NOT NULL DEFAULT '',
    deploy_id INT REFERENCES deploys(id) ON DELETE SET NULL,
    calls BIGINT NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cost_micros BIGINT NOT NULL DEFAULT 0,
    unpriced_calls BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, environment, bucket, model, feature)
);

CREATE INDEX IF NOT EXISTS ai_usage_deploy ON ai_usage (deploy_id);

ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_usage FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ai_usage_project_isolation ON ai_usage;
CREATE POLICY ai_usage_project_isolation ON ai_usage
    USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
    WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

-- System jobs (retention, the detector) work across projects (see migration 022)
DROP POLICY IF EXISTS ai_usage_system_scope ON ai_usage;
CREATE POLICY ai_usage_system_scope ON ai_usage
    USING (current_setting('app.system_scope', true) = 'all')
    WITH CHECK (current_setting('app.system_scope', true) = 'all');

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'changewatch_tenant') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON ai_usage TO changewatch_tenant;
  END IF;
END $$;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS failed_request_value_micros BIGINT
    CHECK (failed_request_value_micros IS NULL OR failed_request_value_micros >= 0);
