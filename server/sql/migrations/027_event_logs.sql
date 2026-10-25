-- Migration 027: app log lines attached to an error (the SDK's capture_logs option).
-- The last log lines the app wrote before the error, scrubbed and stored encrypted only.
-- They live in the event row, so retention drops them with the event's weekly partition.
ALTER TABLE events ADD COLUMN IF NOT EXISTS logs_enc BYTEA;
