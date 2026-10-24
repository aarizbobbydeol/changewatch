-- Migration 019: Keep the verdict announced at each checkpoint
--
-- deploy_analysis.verdict is overwritten every detector cycle, so the verdict sent at the
-- first (10 min) checkpoint was lost. Analytics ("change failure rate", "alerts that were
-- real"), the weekly digest and the final-checkpoint "verdict changed" alert all need it.
ALTER TABLE deploy_analysis ADD COLUMN IF NOT EXISTS first_verdict TEXT;
ALTER TABLE deploy_analysis ADD COLUMN IF NOT EXISTS final_verdict TEXT;
