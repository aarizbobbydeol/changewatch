-- Migration 009: Native PostgreSQL Partitioning of events table by week
-- Specified in Performance Addendum Section 5

DO $$
BEGIN
    -- Check if events table exists and is not already partitioned
    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'events' AND c.relkind != 'p'
    ) THEN
        -- Rename existing events table to events_legacy
        ALTER TABLE events RENAME TO events_legacy;

        -- Create native partitioned events table
        CREATE TABLE events (
            id BIGSERIAL,
            issue_id INT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            deploy_id INT REFERENCES deploys(id) ON DELETE SET NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            stack_trace TEXT,
            stack_trace_enc BYTEA,
            request_path TEXT,
            duration_ms REAL,
            extra JSONB NOT NULL DEFAULT '{}',
            extra_enc BYTEA,
            event_id TEXT,
            PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at);

        -- Create default fallback partition to receive non-matching or historical rows
        CREATE TABLE IF NOT EXISTS events_default PARTITION OF events DEFAULT;

        -- Copy legacy rows into partitioned table
        INSERT INTO events (
            id, issue_id, project_id, deploy_id, occurred_at,
            stack_trace, stack_trace_enc, request_path, duration_ms,
            extra, extra_enc, event_id
        )
        SELECT
            id, issue_id, project_id, deploy_id, occurred_at,
            stack_trace, stack_trace_enc, request_path, duration_ms,
            extra, extra_enc, event_id
        FROM events_legacy
        ON CONFLICT DO NOTHING;

        -- Drop legacy table
        DROP TABLE events_legacy CASCADE;
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'events'
    ) THEN
        -- Create partitioned table from scratch
        CREATE TABLE events (
            id BIGSERIAL,
            issue_id INT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            deploy_id INT REFERENCES deploys(id) ON DELETE SET NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            stack_trace TEXT,
            stack_trace_enc BYTEA,
            request_path TEXT,
            duration_ms REAL,
            extra JSONB NOT NULL DEFAULT '{}',
            extra_enc BYTEA,
            event_id TEXT,
            PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at);

        CREATE TABLE IF NOT EXISTS events_default PARTITION OF events DEFAULT;
    END IF;
END $$;

-- Pre-create weekly partitions around current date (previous week, current week, next 2 weeks)
DO $$
DECLARE
    curr_week TIMESTAMPTZ := date_trunc('week', now());
    start_ts TIMESTAMPTZ;
    end_ts TIMESTAMPTZ;
    part_name TEXT;
    i INT;
BEGIN
    FOR i IN -1..2 LOOP
        start_ts := curr_week + (i * INTERVAL '1 week');
        end_ts := start_ts + INTERVAL '1 week';
        part_name := 'events_' || to_char(start_ts, 'YYYY_MM_DD');

        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relname = part_name
        ) THEN
            BEGIN
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF events FOR VALUES FROM (%L) TO (%L);',
                    part_name, start_ts, end_ts
                );
            EXCEPTION WHEN OTHERS THEN
                -- If table already exists or default partition conflicts, continue safely
                NULL;
            END;
        END IF;
    END LOOP;
END $$;

-- Indexes on partitioned events table (propagated automatically to all partitions)
CREATE INDEX IF NOT EXISTS events_project_time ON events (project_id, occurred_at);
CREATE INDEX IF NOT EXISTS events_deploy ON events (deploy_id);
CREATE INDEX IF NOT EXISTS events_issue ON events (issue_id);
CREATE UNIQUE INDEX IF NOT EXISTS events_project_event_id ON events (project_id, event_id, occurred_at) WHERE event_id IS NOT NULL;

-- Enable and enforce Row-Level Security
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS events_project_policy ON events;
CREATE POLICY events_project_policy ON events
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
