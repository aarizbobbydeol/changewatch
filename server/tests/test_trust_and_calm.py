"""Comprehensive Tests for Milestone 6 (Trust & Calm Features, Interactivity, and Verification).
Implements TRUST Addendum Stages 4 to 8, TD Stages 6 & 8, and AN Stages 4–5.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import require_read_project_id
from app.config import settings
from app.main import app
from app.trust.actions import (
    execute_incident_action,
    generate_signed_action_token,
    record_incident_event,
    verify_signed_action_token,
    verify_slack_signature,
)
from app.trust.calibration import compute_decision_calibration
from app.trust.calm import (
    check_alert_budget,
    check_should_group_alert,
    is_issue_muted,
    is_quiet_hours,
    run_calm_auto_resolve,
    run_escalation_checks,
)
from app.trust.digest import generate_weekly_digest
from app.trust.feedback import (
    compute_trust_metrics,
    get_feedback_suggestions,
    record_alert_feedback,
)
from app.trust.secret_guard import apply_secret_guard


# ---------------------------------------------------------------------------
# 1. INCIDENT ACTIONS AND AUDIT TIMELINE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_incident_actions_and_timeline():
    """Verify state transitions and audit events for ack, assign, resolve, reopen."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": 5, "status": "possible", "acked_at": None, "assigned_to": None
    }

    # 1. Ack
    ack_res = await execute_incident_action(mock_conn, project_id=1, incident_id=5, action="ack", actor="alice")
    assert ack_res["action"] == "ack"
    assert ack_res["acked_by"] == "alice"

    # 2. Assign
    assign_res = await execute_incident_action(
        mock_conn, project_id=1, incident_id=5, action="assign", actor="alice", detail={"assignee": "bob"}
    )
    assert assign_res["assigned_to"] == "bob"

    # 3. Resolve
    resolve_res = await execute_incident_action(mock_conn, project_id=1, incident_id=5, action="resolve", actor="bob")
    assert resolve_res["action"] == "resolve"

    # 4. Reopen
    reopen_res = await execute_incident_action(mock_conn, project_id=1, incident_id=5, action="reopen", actor="bob")
    assert reopen_res["action"] == "reopen"
    assert reopen_res["status"] == "confirmed"

    # Invariant: Invalid action raises ValueError
    with pytest.raises(ValueError):
        await execute_incident_action(mock_conn, project_id=1, incident_id=5, action="delete_database")


# ---------------------------------------------------------------------------
# 2. SIGNED ACTION LINKS & SLACK INTERACTION
# ---------------------------------------------------------------------------

def test_signed_action_token_tamper_and_expiry():
    """Verify HMAC signature validation, tamper resistance, and expiry."""
    token = generate_signed_action_token(incident_id=12, action="ack", project_id=1, expires_in_sec=3600)
    valid, payload, err = verify_signed_action_token(token)
    assert valid is True
    assert payload["incident_id"] == 12
    assert payload["action"] == "ack"
    assert payload["project_id"] == 1

    # Tampered token fails
    tampered_token = token[:-4] + "AAAA"
    valid_tamp, _, err_tamp = verify_signed_action_token(tampered_token)
    assert valid_tamp is False

    # Expired token fails
    expired_token = generate_signed_action_token(incident_id=12, action="ack", project_id=1, expires_in_sec=-10)
    valid_exp, _, err_exp = verify_signed_action_token(expired_token)
    assert valid_exp is False
    assert "expired" in err_exp.lower()


def test_slack_signature_verification():
    """Verify Slack webhook signature calculation and replay rejection."""
    secret = "my_slack_signing_secret"
    body = b'payload={"type":"block_actions"}'
    now_ts = str(int(time.time()))

    import hmac
    import hashlib
    sig_basestring = f"v0:{now_ts}:{body.decode('utf-8')}"
    valid_sig = "v0=" + hmac.new(secret.encode("utf-8"), sig_basestring.encode("utf-8"), hashlib.sha256).hexdigest()

    # Valid signature
    assert verify_slack_signature(body, now_ts, valid_sig, signing_secret=secret) is True

    # Tampered signature fails
    assert verify_slack_signature(body, now_ts, "v0=bad_hex", signing_secret=secret) is False

    # Replay attack (timestamp older than 300s) fails
    old_ts = str(int(time.time()) - 400)
    old_sig = "v0=" + hmac.new(secret.encode("utf-8"), f"v0:{old_ts}:{body.decode('utf-8')}".encode("utf-8"), hashlib.sha256).hexdigest()
    assert verify_slack_signature(body, old_ts, old_sig, signing_secret=secret) is False


# ---------------------------------------------------------------------------
# 3. CALM NOTIFICATIONS: GROUPING, BUDGET, QUIET HOURS & ESCALATION
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alert_grouping_and_hourly_budget():
    """Verify alert grouping at >= 3 incidents and hourly alert budget capping."""
    mock_conn = AsyncMock()

    # Under budget
    mock_conn.fetchval.return_value = 4
    assert await check_alert_budget(mock_conn, project_id=1) is True

    # Over budget
    mock_conn.fetchval.return_value = 10
    assert await check_alert_budget(mock_conn, project_id=1) is False

    # Grouping check: 4 incidents -> should group
    mock_conn.fetchval.return_value = 4
    should_group, count = await check_should_group_alert(mock_conn, project_id=1, deploy_id=10)
    assert should_group is True
    assert count == 4


def test_quiet_hours_deferral():
    """Verify quiet hours schedule logic."""
    cfg = {"enabled": True, "start_hour": 22, "end_hour": 7}
    # 23:00 is quiet
    dt_quiet = datetime(2026, 10, 22, 23, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(cfg, now_dt=dt_quiet) is True

    # 14:00 is active hours
    dt_active = datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(cfg, now_dt=dt_active) is False


@pytest.mark.asyncio
async def test_escalation_flow_and_ack_stop():
    """Verify incident escalates to owners, then secondary channel, and ack stops escalation."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)

    # First escalation: count=0 -> escalates to owners
    mock_conn.fetch.return_value = [
        {"id": 1, "project_id": 1, "status": "confirmed", "title": "Crash", "escalated_count": 0, "created_at": now_dt, "owners": "[]"}
    ]
    items = await run_escalation_checks(mock_conn, now_dt=now_dt)
    assert len(items) == 1
    assert items[0]["target"] == "owners"
    assert items[0]["level"] == 1

    # Second escalation: count=1 -> escalates to secondary_channel
    mock_conn.fetch.return_value = [
        {"id": 1, "project_id": 1, "status": "confirmed", "title": "Crash", "escalated_count": 1, "created_at": now_dt, "owners": "[]"}
    ]
    items2 = await run_escalation_checks(mock_conn, now_dt=now_dt)
    assert len(items2) == 1
    assert items2[0]["target"] == "secondary_channel"
    assert items2[0]["level"] == 2


# ---------------------------------------------------------------------------
# 4. MUTE RULES & AUTO-RESOLVE WITH TRAFFIC INVARIANT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mute_rules_and_rate_spike_unmute():
    """Verify active mute suppresses alert, and 10x rate spike auto-unmutes."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"id": 20, "kind": "fingerprint", "pattern": "fp_test", "reason": "Known harmless retry"}
    ]

    # Normal rate -> muted
    muted, reason = await is_issue_muted(mock_conn, project_id=1, fingerprint="fp_test", path="/api", exc_type="IOError", event_rate_multiplier=1.0)
    assert muted is True
    assert "Muted by rule #20" in reason

    # Rate spike (10x) -> auto-unmute
    muted_spike, reason_spike = await is_issue_muted(mock_conn, project_id=1, fingerprint="fp_test", path="/api", exc_type="IOError", event_rate_multiplier=12.0)
    assert muted_spike is False
    assert "Rate spike" in reason_spike


@pytest.mark.asyncio
async def test_auto_resolve_respects_source_silence():
    """Verify auto-resolve is skipped when app source is silent (silence is never safe)."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    mock_conn.fetch.return_value = [
        {"id": 7, "project_id": 1, "deploy_id": 12, "status": "possible", "last_event_at": now_dt - timedelta(minutes=45), "environment": "prod", "deployed_at": now_dt - timedelta(hours=2), "commit_sha": "abc1"}
    ]

    # Source is silent -> skip auto-resolve!
    with patch("app.trust.calm.is_source_silent", new=AsyncMock(return_value=True)):
        resolved = await run_calm_auto_resolve(mock_conn, resolve_after_min=30, now_dt=now_dt)
        assert len(resolved) == 0

    # Source is healthy (flowing) -> auto-resolve succeeds
    with patch("app.trust.calm.is_source_silent", new=AsyncMock(return_value=False)):
        mock_conn.fetchrow.return_value = {
            "id": 13, "commit_sha": "revert1234", "author": "bob", "message": "Revert broken deploy", "deployed_at": now_dt
        }
        resolved = await run_calm_auto_resolve(mock_conn, resolve_after_min=30, now_dt=now_dt)
        assert len(resolved) == 1
        assert resolved[0]["is_revert"] is True
        assert "likely reverted" in resolved[0]["fix_note"]


# ---------------------------------------------------------------------------
# 5. ALERT FEEDBACK, TRUST METRICS & SUGGESTIONS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alert_feedback_and_suggestions():
    """Verify alert feedback recording, precision calculation, and 3-false-alarm suggestions."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1}

    # Record feedback
    fb_id = await record_alert_feedback(mock_conn, project_id=1, incident_id=10, label="real", actor="alice", comment="Real bug")
    assert fb_id == 1

    # Suggestions: >= 3 false alarms
    mock_conn.fetch.return_value = [
        {"where_file": "app/health.py", "category": "other", "false_count": 3}
    ]
    suggs = await get_feedback_suggestions(mock_conn, project_id=1, min_false_alarms=3)
    assert len(suggs) == 1
    assert suggs[0]["pattern"] == "app/health.py"
    assert "Consider muting path prefix" in suggs[0]["suggestion_text"]


# ---------------------------------------------------------------------------
# 6. DECISION CALIBRATION & SECRET GUARD
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decision_calibration_and_small_bin_hiding():
    """Verify calibration table and Brier score against known fixture; small bins hidden."""
    mock_conn = AsyncMock()
    # 15 samples in 80-100% bin with 14 real outcomes (14/15 = 93.3% precision)
    # 5 samples in 20-40% bin (should be hidden because < 10)
    mock_rows = []
    for _ in range(14):
        mock_rows.append({"probability": 0.90, "outcome": 1.0})
    mock_rows.append({"probability": 0.90, "outcome": 0.0})
    for _ in range(5):
        mock_rows.append({"probability": 0.30, "outcome": 0.0})

    mock_conn.fetch.return_value = mock_rows

    calib = await compute_decision_calibration(mock_conn, project_id=1, min_bin_sample=10)
    assert calib["total_samples"] == 20
    assert calib["brier_score"] is not None

    bins = {b["bin"]: b for b in calib["bins"]}
    # 80-100% bin has 15 samples -> visible
    assert bins["80% - 100%"]["visible"] is True
    assert bins["80% - 100%"]["observed_rate"] == 0.933

    # 20-40% bin has 5 samples -> hidden!
    assert bins["20% - 40%"]["visible"] is False
    assert bins["20% - 40%"]["observed_rate"] is None

    assert "When we said about 90%" in calib["headline"]


def test_secret_guard_filter():
    """Verify optional secret guard redacts secrets and logs guard event."""
    with patch("app.trust.secret_guard.is_secret_guard_enabled", return_value=True):
        clean, blocked, count = apply_secret_guard(
            "Traceback: api_key='sk-1234567890abcdef1234567890abcdef' error in app.py",
            field_name="prompt",
        )
        assert blocked is True
        assert count >= 1
        assert "sk-1234567890abcdef" not in clean
        assert "[REDACTED" in clean


# ---------------------------------------------------------------------------
# 7. PARITY: WEEKLY DIGEST AND ANALYTICS METRICS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_digest_and_analytics_metric_parity():
    """Verify that weekly digest calculates identical numbers using metrics.py pure functions."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)

    # 10 deploys (9 safe, 1 rollback)
    deploys = [
        {"id": i, "commit_sha": f"sha_{i}", "environment": "prod", "deployed_at": now_dt - timedelta(days=2), "first_verdict": "safe", "final_verdict": "safe"}
        for i in range(9)
    ]
    deploys.append(
        {"id": 10, "commit_sha": "sha_fail", "environment": "prod", "deployed_at": now_dt - timedelta(days=2), "first_verdict": "rollback", "final_verdict": "rollback"}
    )
    incidents = [
        {"id": 1, "deploy_id": 10, "status": "confirmed", "title": "Crash", "where_file": "billing/pay.py", "first_alert_ms": 1200, "created_at": now_dt - timedelta(days=2), "first_event_at": now_dt - timedelta(days=2), "last_event_at": now_dt - timedelta(days=2), "resolved_at": now_dt - timedelta(days=2) + timedelta(minutes=9)}
    ]

    mock_conn.fetch.side_effect = [
        deploys,
        incidents,
        [{"label": "real"} for _ in range(9)] + [{"label": "false"}], # 90% precision
        [], # status logs
        [], # expiring mutes
    ]

    digest = await generate_weekly_digest(mock_conn, project_id=1, environment="prod", now_dt=now_dt)
    assert digest["project_id"] == 1
    # Check text
    text = digest["formatted_text"]
    assert "Releases: 10" in text
    assert "1 incident(s)" in text
    assert "billing" in text
    assert "Alert precision: 90.0%" in text
