-- Migration 005: API Key Hashing and Role Separation (Ingest vs Read)
-- Specified in Security Addendum Section C3

-- 1. Add hashed key columns and visible prefixes to projects
ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS ingest_key_hash TEXT,
  ADD COLUMN IF NOT EXISTS ingest_key_prefix TEXT,
  ADD COLUMN IF NOT EXISTS read_key_hash TEXT,
  ADD COLUMN IF NOT EXISTS read_key_prefix TEXT;

-- 2. Drop NOT NULL on legacy plaintext api_key so new projects do not store plaintext keys
ALTER TABLE projects ALTER COLUMN api_key DROP NOT NULL;
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_api_key_key;

-- 3. Unique indexes on key hashes
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_ingest_key_hash ON projects (ingest_key_hash) WHERE ingest_key_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_read_key_hash ON projects (read_key_hash) WHERE read_key_hash IS NOT NULL;

