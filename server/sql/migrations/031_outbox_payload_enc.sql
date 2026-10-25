-- Migration 031: alert and notification messages stored encrypted.
-- A message can quote customer code (a deploy risk review finding, the AI's suggested change), so
-- it is stored only as ciphertext in payload_enc, and payload keeps '{}'. Both are cleared once the
-- message was sent, skipped or permanently failed: the message is not kept after that.
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS payload_enc BYTEA;

-- Messages already finished before this migration were kept in plain text: clear them too.
-- Pending ones are still delivered from their plain payload, then cleared.
UPDATE outbox SET payload = '{}'::jsonb WHERE status <> 'pending' AND payload <> '{}'::jsonb;
