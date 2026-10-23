-- Migration 003: Add Data Key Column for Per-Project Envelope Encryption
-- Specified in Encryption Addendum Section C1

ALTER TABLE projects ADD COLUMN IF NOT EXISTS data_key_enc BYTEA;
