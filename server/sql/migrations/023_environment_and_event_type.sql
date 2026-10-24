-- Migration 023: Keep environments apart, and record what kind of event each row is
--
-- Verdicts compare a deploy's traffic and errors before and after it, but traffic had one row
-- per project per minute and events had no environment: a staging deploy's errors and a staging
-- app's heartbeats counted toward the prod deploy's verdict. Heartbeats from two environments in
-- the same minute even made the worker's upsert fail ("cannot affect row a second time").
--
-- Errors were told apart from slow requests by "stack_trace IS NOT NULL", which stopped working
-- once stack traces were stored only encrypted: every HTTP error with a path was left out.
--
-- Existing rows default to 'prod' and 'error'; adding a column with a constant default does not
-- rewrite the table.
ALTER TABLE events ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'prod';
ALTER TABLE events ADD COLUMN IF NOT EXISTS type TEXT NOT NULL DEFAULT 'error';

ALTER TABLE traffic ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'prod';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'traffic'::regclass AND contype = 'p'
      AND pg_get_constraintdef(oid) = 'PRIMARY KEY (project_id, bucket)'
  ) THEN
    ALTER TABLE traffic DROP CONSTRAINT traffic_pkey;
    ALTER TABLE traffic ADD CONSTRAINT traffic_pkey PRIMARY KEY (project_id, environment, bucket);
  END IF;
END $$;
