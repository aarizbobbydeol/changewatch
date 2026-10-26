-- Migration 032: a per-project manage key (cw_manage_...). Changing a project's settings, budget,
-- mute rules and runbooks needs it, or the project's read key together with the install's admin
-- key. The read key keeps viewing and incident actions. Stored as a SHA-256 hash, like the others.
ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS manage_key_hash TEXT,
  ADD COLUMN IF NOT EXISTS manage_key_prefix TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS projects_manage_key_hash_idx
  ON projects (manage_key_hash) WHERE manage_key_hash IS NOT NULL;
