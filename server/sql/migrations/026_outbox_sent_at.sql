-- Migration 026: when each message was actually delivered.
-- The incident timeline and postmortem show "alert delivered at" (created_at is only when it was
-- queued; a message can wait for retries).
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS outbox_dedupe_prefix_idx ON outbox (project_id, dedupe_key text_pattern_ops);
