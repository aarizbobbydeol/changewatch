-- Migration 021: Count each event once, even when it is sent again
--
-- The SDK re-sends a batch when the API times out or answers 429/503, and the worker
-- re-queues a batch whose transaction failed. Full event rows were already idempotent
-- (ON CONFLICT on event_id), but issue counts, rollups and heartbeat traffic were added
-- again on every re-send, and storm control keeps no row at all for most events in a
-- storm. The worker now claims each event_id here, in the same transaction as those
-- counters, and skips events that were already claimed. A rolled-back batch releases its
-- claims, so its retry still counts. Rows older than DEDUPE_WINDOW_HOURS are deleted by
-- the hourly retention job.
CREATE TABLE IF NOT EXISTS ingest_dedupe (
    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_ingest_dedupe_seen_at ON ingest_dedupe (seen_at);

ALTER TABLE ingest_dedupe ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_dedupe FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ingest_dedupe_project_isolation ON ingest_dedupe;
CREATE POLICY ingest_dedupe_project_isolation ON ingest_dedupe
    USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
    WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
