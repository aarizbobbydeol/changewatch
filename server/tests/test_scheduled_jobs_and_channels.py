"""Scheduled jobs (escalation, auto-resolve, digest timing), decision metering, signed webhooks, SMTP TLS."""
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.jobs as jobs
from app.incident_manager import meter_decision_call
from app.notifiers.channels import EmailNotifier, WebhookNotifier


def test_digest_due_only_on_configured_day_and_hour(monkeypatch):
    monkeypatch.setattr(jobs.settings, "DIGEST_DAY", "mon")
    monkeypatch.setattr(jobs.settings, "DIGEST_HOUR", 9)
    monkeypatch.setattr(jobs.settings, "DIGEST_TIMEZONE", "Asia/Kolkata")
    # Monday 2026-10-26 03:30 UTC = 09:00 IST -> due, keyed by ISO week
    assert jobs.digest_due(datetime(2026, 10, 26, 3, 30, tzinfo=timezone.utc)) == "2026-W44"
    # 08:59 IST on Monday -> not yet
    assert jobs.digest_due(datetime(2026, 10, 26, 3, 29, tzinfo=timezone.utc)) is None
    # Tuesday -> not the digest day
    assert jobs.digest_due(datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)) is None


@pytest.mark.asyncio
async def test_escalation_notifies_team_once_per_level():
    conn = MagicMock()
    escalated = [{"incident_id": 7, "project_id": 2, "level": 1, "target": "owners", "title": "KeyError in billing"}]
    with patch("app.trust.calm.run_escalation_checks", new=AsyncMock(return_value=escalated)), \
         patch("app.jobs.queue_notification", new=AsyncMock(return_value=True)) as queue:
        assert await jobs.run_escalations(conn) == 1
    kwargs = queue.await_args.kwargs
    assert kwargs["dedupe_key"] == "escalation:7:1"
    assert "Still unacknowledged" in kwargs["payload"]["title"]


@pytest.mark.asyncio
async def test_auto_resolve_posts_resolution_with_fix_note():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="ZeroDivisionError in billing")
    resolved = [{"incident_id": 4, "project_id": 1, "fix_note": "likely fixed by deploy abc12345 by dev", "is_revert": False}]
    with patch("app.trust.calm.run_calm_auto_resolve", new=AsyncMock(return_value=resolved)), \
         patch("app.jobs.queue_notification", new=AsyncMock(return_value=True)) as queue:
        assert await jobs.run_auto_resolve(conn) == 1
    payload = queue.await_args.kwargs["payload"]
    assert payload["title"] == "✅ Resolved: ZeroDivisionError in billing"
    assert "likely fixed by deploy abc12345" in payload["body"]
    assert queue.await_args.kwargs["dedupe_key"] == "incident:4:resolved"


def _savepoint_conn():
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


@pytest.mark.asyncio
async def test_only_hosted_decisions_are_metered():
    rules = {"severity": SimpleNamespace(source="rules")}
    hosted = {"severity": SimpleNamespace(source="jev"), "category": SimpleNamespace(source="jev")}
    with patch("app.metering.record_usage", new=AsyncMock(return_value=1)) as record:
        assert await meter_decision_call(_savepoint_conn(), 1, 9, rules) is False
        record.assert_not_awaited()
        assert await meter_decision_call(_savepoint_conn(), 1, 9, hosted) is True
    assert record.await_args.kwargs["meter"].value == "decision_call"
    assert record.await_args.kwargs["idempotency_key"] == "decision_call:incident:9"


@pytest.mark.asyncio
async def test_metering_failure_never_blocks_the_alert():
    hosted = {"severity": SimpleNamespace(source="jev")}
    with patch("app.metering.record_usage", new=AsyncMock(side_effect=RuntimeError("budget table locked"))):
        assert await meter_decision_call(_savepoint_conn(), 1, 9, hosted) is False


@pytest.mark.asyncio
async def test_webhook_signature_covers_timestamp_and_exact_body():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            received["headers"] = dict(self.headers)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/hook"
        await WebhookNotifier().send({"text": "Possible regression", "incident_id": 3}, {"url": url, "secret": "s3cret"})
    finally:
        httpd.shutdown()

    ts = received["headers"]["X-ChangeWatch-Timestamp"]
    expected = hmac.new(b"s3cret", ts.encode() + b"." + received["body"], hashlib.sha256).hexdigest()
    assert received["headers"]["X-ChangeWatch-Signature"] == f"sha256={expected}"
    assert abs(time.time() - int(ts)) < 60
    assert json.loads(received["body"])["incident_id"] == 3


def _smtp_mock(offers_starttls):
    server = MagicMock()
    server.has_extn = MagicMock(return_value=offers_starttls)
    smtp = MagicMock()
    smtp.return_value.__enter__ = MagicMock(return_value=server)
    smtp.return_value.__exit__ = MagicMock(return_value=False)
    return smtp, server


@pytest.mark.asyncio
async def test_email_uses_starttls_before_login():
    smtp, server = _smtp_mock(offers_starttls=True)
    config = {"smtp_host": "mail.test", "smtp_port": 587, "to_addrs": "ops@test", "smtp_user": "u", "smtp_password": "p"}
    with patch("smtplib.SMTP", smtp):
        await EmailNotifier().send({"text": "alert"}, config)
    calls = [c[0] for c in server.method_calls]
    assert calls.index("starttls") < calls.index("login")


@pytest.mark.asyncio
async def test_email_refuses_password_without_tls():
    smtp, server = _smtp_mock(offers_starttls=False)
    config = {"smtp_host": "mail.test", "to_addrs": "ops@test", "smtp_user": "u", "smtp_password": "p"}
    with patch("smtplib.SMTP", smtp):
        with pytest.raises(RuntimeError, match="unencrypted"):
            await EmailNotifier().send({"text": "alert"}, config)
    server.login.assert_not_called()


@pytest.mark.asyncio
async def test_real_calm_auto_resolve_runs_and_skips_silent_apps():
    """Runs the real function (its call into is_source_silent was broken while it was unused)."""
    from app.trust.calm import run_calm_auto_resolve

    candidate = {
        "id": 4, "project_id": 1, "deploy_id": 9, "status": "possible", "last_event_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "environment": "prod", "deployed_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "commit_sha": "abc1234567",
    }
    newer = {"id": 10, "commit_sha": "def9876543", "author": "dev", "message": "Fix discount", "deployed_at": datetime(2026, 1, 2, tzinfo=timezone.utc)}

    def conn_with(silent):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[candidate])
        conn.fetchval = AsyncMock(return_value=1 if silent else None)  # is_source_silent
        conn.fetchrow = AsyncMock(return_value=newer)
        conn.execute = AsyncMock()
        return conn

    with patch("app.trust.calm.record_incident_event", new=AsyncMock()):
        resolved = await run_calm_auto_resolve(conn_with(silent=False))
        skipped = await run_calm_auto_resolve(conn_with(silent=True))

    assert [r["incident_id"] for r in resolved] == [4]
    assert resolved[0]["fix_note"] == "likely fixed by deploy def98765 by dev"
    assert skipped == []  # a silent app is never "fixed"


@pytest.mark.asyncio
async def test_second_escalation_waits_a_full_interval():
    from app.trust.calm import run_escalation_checks

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    await run_escalation_checks(conn)
    sql = conn.fetch.await_args.args[0]
    assert "last_escalated_at <= $1" in sql
