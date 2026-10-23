-- Migration 008: Add event_count to issues table
-- Performance Addendum Section 3 (Storm Control)

ALTER TABLE issues ADD COLUMN IF NOT EXISTS event_count INT NOT NULL DEFAULT 1;

-- Backfill existing issues count from events table
UPDATE issues
SET event_count = GREATEST(1, sub.cnt)
FROM (
    SELECT issue_id, COUNT(*) AS cnt
    FROM events
    GROUP BY issue_id
) sub
WHERE issues.id = sub.issue_id AND issues.event_count = 1;
