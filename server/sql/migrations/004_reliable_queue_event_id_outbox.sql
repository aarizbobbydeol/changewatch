-- Migration 004: Event Deduplication ID and Durable Outbox Table
-- Specified in Trust & Reliability Addendum Section A1

-- 1. Add event_id to events table for idempotency and replay deduplication
ALTER TABLE events ADD COLUMN IF NOT EXISTS event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS events_project_event_id ON events (project_id, event_id) WHERE event_id IS NOT NULL;

-- 2. Create durable outbox table for reliable notification delivery
CREATE TABLE IF NOT EXISTS outbox (
  id BIGSERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  channel_id INT,
  dedupe_key TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL,
  attempts INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL DEFAULT 'pending',
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS outbox_pending_idx
  ON outbox (status, next_attempt_at)
  WHERE status = 'pending';

-- 3. Enable Row-Level Security on outbox
ALTER TABLE outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS outbox_project_policy ON outbox;
CREATE POLICY outbox_project_policy ON outbox
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
