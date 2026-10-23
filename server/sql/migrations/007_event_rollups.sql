-- Migration 007: Event Rollups Table
-- Performance Addendum Section 4 & 5

CREATE TABLE IF NOT EXISTS event_rollups (
    id BIGSERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    issue_id INT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    deploy_id INT REFERENCES deploys(id) ON DELETE SET NULL,
    bucket TIMESTAMPTZ NOT NULL,
    count INT NOT NULL DEFAULT 1
);

-- Unique index ensuring atomic upserts even when deploy_id is NULL
CREATE UNIQUE INDEX IF NOT EXISTS event_rollups_unique_idx
    ON event_rollups (project_id, issue_id, COALESCE(deploy_id, -1), bucket);

CREATE INDEX IF NOT EXISTS idx_event_rollups_project_bucket ON event_rollups(project_id, bucket);
CREATE INDEX IF NOT EXISTS idx_event_rollups_issue_bucket ON event_rollups(project_id, issue_id, bucket);

ALTER TABLE event_rollups ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_rollups FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS event_rollups_project_isolation ON event_rollups;
CREATE POLICY event_rollups_project_isolation ON event_rollups
    USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
    WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
