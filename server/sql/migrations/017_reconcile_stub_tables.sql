-- Migration 017: Reconcile placeholder tables from migration 006
--
-- Migration 006 created early placeholder versions of `deploy_files` and
-- `notification_channels`. Migrations 011 and 012 later declared the real
-- shapes with CREATE TABLE IF NOT EXISTS, which is a no-op when the table
-- already exists, so their columns were never added. On every database this
-- broke deploys that include changed files (HTTP 500) and every non-Slack
-- notification channel. Add the missing columns in place.

-- deploy_files (shape from 011_deploy_files.sql)
ALTER TABLE deploy_files ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'modified';
ALTER TABLE deploy_files ADD COLUMN IF NOT EXISTS added_ranges JSONB NOT NULL DEFAULT '[]';
ALTER TABLE deploy_files ADD COLUMN IF NOT EXISTS authors JSONB NOT NULL DEFAULT '[]';
ALTER TABLE deploy_files ADD COLUMN IF NOT EXISTS content TEXT;
ALTER TABLE deploy_files ADD COLUMN IF NOT EXISTS is_dependency_file BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE deploy_files ADD COLUMN IF NOT EXISTS is_config_file BOOLEAN NOT NULL DEFAULT false;

-- notification_channels (shape from 012_sre_incidents_and_decisions.sql)
ALTER TABLE notification_channels ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}';
ALTER TABLE notification_channels ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT true;
DO $$
BEGIN
  -- The placeholder required a channel name; channels are identified by type and id now.
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'notification_channels' AND column_name = 'name') THEN
    ALTER TABLE notification_channels ALTER COLUMN name DROP NOT NULL;
  END IF;
END $$;

-- investigations: encrypted audit log column used by the investigator (also from the 006 stub)
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS steps_enc BYTEA;
