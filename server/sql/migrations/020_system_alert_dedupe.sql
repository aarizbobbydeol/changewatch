-- Migration 020: One active operator system alert per component
--
-- The watchdog runs in both the detector and the notifier, so each can notice the other
-- failing. This index makes raising an alert idempotent across them: only the process
-- whose INSERT succeeds sends the notification.
CREATE UNIQUE INDEX IF NOT EXISTS sys_alerts_one_active_per_component
  ON system_alerts (component)
  WHERE project_id IS NULL AND status = 'active';
