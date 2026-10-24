"""Operator system alerts: raised once, resolved once, redacted, sent to the fallback webhook."""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.system_alerts as sa


class FakeAlertsConn:
    """Mimics the system_alerts table plus the one-active-alert-per-component unique index."""

    def __init__(self):
        self.rows = []

    async def fetchval(self, sql, *args):
        component = args[0]
        active = [r for r in self.rows if r["component"] == component and r["status"] == "active"]
        if sql.lstrip().startswith("INSERT INTO system_alerts"):
            if active:
                return None  # ON CONFLICT DO NOTHING
            row = {"id": len(self.rows) + 1, "component": component, "title": args[2], "status": "active"}
            self.rows.append(row)
            return row["id"]
        if sql.lstrip().startswith("UPDATE system_alerts"):
            if not active:
                return None
            active[0]["status"] = "resolved"
            return active[0]["id"]
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        return [r for r in self.rows if r["status"] == "active"]


@pytest.fixture
def after_grace(monkeypatch):
    monkeypatch.setattr(sa, "_process_started_at", time.monotonic() - sa.STARTUP_GRACE_SEC - 1)


DETECTOR_DOWN = {"component": "detector", "severity": "high", "title": "Detector has not run", "detail": "lag 600s"}


@pytest.mark.asyncio
async def test_alert_raised_once_while_problem_lasts(after_grace):
    conn = FakeAlertsConn()
    with patch.object(sa, "send_fallback", new=AsyncMock(return_value=True)) as send:
        first = await sa.sync_system_alerts(conn, [DETECTOR_DOWN])
        second = await sa.sync_system_alerts(conn, [DETECTOR_DOWN])
    assert first["raised"] == ["detector"]
    assert second["raised"] == []
    assert send.await_count == 1
    assert "Detector has not run" in send.await_args.args[0]


@pytest.mark.asyncio
async def test_recovery_sent_once_when_problem_clears(after_grace):
    conn = FakeAlertsConn()
    with patch.object(sa, "send_fallback", new=AsyncMock(return_value=True)) as send:
        await sa.sync_system_alerts(conn, [DETECTOR_DOWN])
        cleared = await sa.sync_system_alerts(conn, [])
        again = await sa.sync_system_alerts(conn, [])
    assert cleared["resolved"] == ["detector"]
    assert again["resolved"] == []
    assert send.await_count == 2
    assert send.await_args.args[0].startswith("✅ Recovered")


@pytest.mark.asyncio
async def test_startup_grace_suppresses_new_alerts(monkeypatch):
    monkeypatch.setattr(sa, "_process_started_at", time.monotonic())
    conn = FakeAlertsConn()
    with patch.object(sa, "send_fallback", new=AsyncMock(return_value=True)) as send:
        result = await sa.sync_system_alerts(conn, [DETECTOR_DOWN])
    assert result["raised"] == []
    send.assert_not_awaited()


def test_down_components_become_alerts():
    status = {
        "components": {
            "database": {"status": "ok", "detail": "fine"},
            "worker": {"status": "down", "detail": "No active workers detected while queue has items"},
            "notifier_process": {"status": "degraded", "detail": "delayed"},
        },
        "system_alerts": [DETECTOR_DOWN],
    }
    alerts = {a["component"]: a for a in sa.alerts_from_watchdog(status)}
    assert set(alerts) == {"worker", "detector"}
    assert alerts["worker"]["severity"] == "critical"


@pytest.mark.asyncio
async def test_fallback_posts_redacted_text_for_slack_and_discord(monkeypatch):
    monkeypatch.setattr(sa.settings, "SYSTEM_ALERT_WEBHOOK_URL", "https://hooks.example.test/ops")
    posted = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted["url"], posted["json"] = url, json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

    monkeypatch.setattr(sa.httpx, "AsyncClient", FakeClient)
    ok = await sa.send_fallback("Redis ping failed: Error connecting to redis://:hunter2@redis:6379/0")
    assert ok is True
    body = posted["json"]
    assert body["text"] == body["content"]
    assert "hunter2" not in body["text"]


@pytest.mark.asyncio
async def test_fallback_without_url_only_logs(monkeypatch, caplog):
    monkeypatch.setattr(sa.settings, "SYSTEM_ALERT_WEBHOOK_URL", "")
    assert await sa.send_fallback("🚨 something broke") is False
    assert "SYSTEM_ALERT_WEBHOOK_URL not set" in caplog.text


@pytest.mark.asyncio
async def test_permanently_failed_alert_reaches_operator_with_redacted_error():
    from app.outbox import process_outbox_item

    conn = MagicMock()
    conn.execute = AsyncMock()
    item = {
        "id": 5,
        "project_id": 3,
        "channel_id": None,
        "dedupe_key": "incident:9:possible:default",
        "payload": {"text": "Possible regression"},
        "attempts": 9,
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }

    def failing_sender(project_id, payload):
        raise RuntimeError("Client error '404' for url 'http://10.0.0.5:9000/hook/SECRETPATH'")

    with patch("app.system_alerts.send_fallback", new=AsyncMock(return_value=True)) as send:
        delivered = await process_outbox_item(conn, item, sender_func=failing_sender)

    assert delivered is False
    stored_error = conn.execute.await_args.args[2]
    assert "SECRETPATH" not in stored_error
    message = send.await_args.args[0]
    assert "incident alert for project 3" in message
    assert "SECRETPATH" not in message
