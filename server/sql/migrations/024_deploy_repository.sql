-- Migration 024: Remember which repository a deploy came from (owner/name)
--
-- With CODE_MODE=ephemeral the server stores no file contents; the AI investigator then reads a
-- changed file from GitHub at the deploy's commit (GITHUB_READ_TOKEN, read-only). That needs the
-- repository, which deploys never recorded, so the fallback could not work.
ALTER TABLE deploys ADD COLUMN IF NOT EXISTS repository TEXT;
