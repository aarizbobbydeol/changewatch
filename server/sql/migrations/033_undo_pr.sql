-- Migration 033: opt-in draft pull requests that undo a deploy (app/undo_pr.py). Off unless a team
-- stores a GitHub token for the project; the token is stored encrypted only.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS undo_pr_token_enc BYTEA;
