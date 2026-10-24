import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.decisions import (
    CircuitBreaker,
    RulesDecisionProvider,
    decide_with_fallback,
    get_triage_questions,
    Q_REGRESSION,
    Q_NOISE,
    Q_SEVERITY,
    Q_CATEGORY,
    Q_OWNER,
)
from app.incident_manager import check_and_trigger_incident, auto_resolve_stale_incidents
from app.notifiers.incident_alert import (
    format_incident_instant_message,
    resolve_team_mentions,
)
from app.notifiers.channels import (
    DiscordNotifier,
    TeamsNotifier,
    WebhookNotifier,
    get_notifier,
)


@pytest.fixture(autouse=True)
def no_mute_rules():
    """These tests replay a fixed sequence of database answers; mute rules are tested on their own."""
    with patch("app.trust.calm.is_issue_muted", new=AsyncMock(return_value=(False, None))):
        yield


# ==============================================================================
# 1. Typed Decisions Layer Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_rules_decision_provider():
    provider = RulesDecisionProvider()
    questions = get_triage_questions(owner_candidates=["alice", "bob"])

    # Scenario A: inside diff, critical route
    state = {
        "title": "KeyError: 'user_id'",
        "exc_type": "KeyError",
        "request_path": "/api/checkout",
        "event_count": 12,
        "in_diff": True,
        "is_critical": True,
    }
    answers = await provider.decide(state, questions)

    assert answers[Q_REGRESSION].probability == 1.0
    assert answers[Q_REGRESSION].source == "rules"
    assert answers[Q_SEVERITY].score == 4.0  # critical
    assert answers[Q_CATEGORY].choice == "missing_data_or_null"
    assert answers[Q_OWNER].choice == "alice"  # first deterministic candidate

    # Scenario B: noise error outside diff
    noise_state = {
        "title": "ConnectionResetError: client closed connection",
        "exc_type": "ConnectionResetError",
        "request_path": "/healthz",
        "event_count": 1,
        "in_diff": False,
        "is_critical": False,
    }
    noise_answers = await provider.decide(noise_state, questions)
    assert noise_answers[Q_REGRESSION].probability == 0.40
    assert noise_answers[Q_NOISE].probability == 0.95


@pytest.mark.asyncio
async def test_decide_with_fallback_timeout():
    # Provider that sleeps longer than timeout
    class SlowProvider:
        async def decide(self, state, questions):
            await asyncio.sleep(0.5)
            return {}

    slow = SlowProvider()
    questions = get_triage_questions()
    state = {"title": "Error", "in_diff": False}

    # Should fall back to rules provider on timeout
    answers = await decide_with_fallback(state, questions, provider=slow, timeout_ms=50)
    assert Q_REGRESSION in answers
    assert answers[Q_REGRESSION].source == "rules"


def test_circuit_breaker():
    cb = CircuitBreaker(failure_threshold=3, cooldown_sec=10.0)
    assert not cb.is_open()

    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open()

    cb.record_failure()
    assert cb.is_open()

    cb.record_success()
    assert not cb.is_open()


# ==============================================================================
# 2. Mentions Resolution and Instant Message Formatting
# ==============================================================================

def test_resolve_team_mentions():
    team_members = [
        {"name": "Alice Smith", "git_emails": ["alice@company.com"], "slack_user_id": "U111", "discord_user_id": "D111"},
        {"name": "Bob Jones", "git_emails": ["bob@company.com"], "slack_user_id": "U222", "discord_user_id": "D222"},
    ]

    # Matching Slack mention
    slack_mentions = resolve_team_mentions(["alice@company.com", "unknown_dev"], team_members, channel_type="slack")
    assert slack_mentions == ["<@U111>", "unknown_dev"]

    # Matching Discord mention
    discord_mentions = resolve_team_mentions(["bob@company.com"], team_members, channel_type="discord")
    assert discord_mentions == ["<@D222>"]

    # Fallback when empty
    assert resolve_team_mentions([], team_members) == ["Unassigned"]


def test_format_incident_instant_message():
    msg = format_incident_instant_message(
        status="possible",
        commit_sha="abcdef1234567890",
        author="alice",
        message="feat: new checkout flow",
        error_title="KeyError: 'item'",
        event_count=3,
        where_file="services/order.py",
        where_line=45,
        where_function="create_order",
        in_diff=True,
        owners=["<@U111>"],
        dashboard_url="http://localhost:3000/incidents/10",
        triage_info={"regression_prob": 0.95, "severity": 3.0, "category": "missing_data_or_null", "source": "rules"},
    )
    assert "Possible regression" in msg["text"]
    assert "services/order.py:45" in msg["text"]
    assert "this line was changed in this release" in msg["text"]
    assert "<@U111>" in msg["text"]
    assert len(msg["blocks"]) >= 4


# ==============================================================================
# 3. Incident Trigger and Threshold Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_incident_trigger_threshold_and_open():
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    deployed_at = now_dt - timedelta(minutes=5)

    # 1. Mock deploy row
    mock_conn.fetchrow.side_effect = [
        # deploy row
        {"deployed_at": deployed_at, "commit_sha": "sha_deploy_1", "author": "alice", "message": "fix issue", "codeowners": "* @team"},
        # issue row
        {"title": "ZeroDivisionError: division by zero", "fingerprint": "fp-test", "first_seen": deployed_at + timedelta(seconds=10), "event_count": 3},
        # project critical_routes
        {"critical_routes": ["/checkout"]},
        # recent event count in window: 3 (meets min_events=3)
        {"cnt": 3},
        # existing incident check: None
        None,
        # incident insert returning new incident id 501
        {"id": 501, "is_new": True},
        # project notify_quiet
        {"name": "Test Project", "notify_quiet": False},
        # outbox insert returning id
        {"id": 999},
    ]

    mock_conn.fetch.side_effect = [
        # deploy_files
        [{"path": "app/math.py", "status": "modified", "added_ranges": [[10, 20]], "authors": ["alice"]}],
        # team_members
        [{"name": "alice", "git_emails": ["alice@test.com"], "slack_user_id": "U123", "discord_user_id": None, "email": "a@t.com"}],
        # notification_channels
        [{"id": 1, "type": "slack", "config": {"webhook_url": "https://hooks.slack.com/services/test"}}],
    ]
    mock_conn.execute = AsyncMock()

    payload = {
        "request_path": "/api/calculate",
        "stack_trace": '  File "app/math.py", line 15, in compute\n    1 / 0\n',
        "exc_type": "ZeroDivisionError",
    }

    inc_id = await check_and_trigger_incident(
        conn=mock_conn,
        project_id=1,
        deploy_id=10,
        issue_id=20,
        payload=payload,
        occurred_at_dt=now_dt,
    )
    assert inc_id == 501

    # Assert outbox notification queued
    fetchrow_calls = [str(c) for c in mock_conn.fetchrow.call_args_list]
    assert any("outbox" in c for c in fetchrow_calls)


@pytest.mark.asyncio
async def test_incident_trigger_critical_route_single_event():
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    deployed_at = now_dt - timedelta(minutes=2)

    mock_conn.fetchrow.side_effect = [
        # deploy row
        {"deployed_at": deployed_at, "commit_sha": "sha_deploy_2", "author": "bob", "message": "update checkout", "codeowners": None},
        # issue row
        {"title": "KeyError: 'cart_id'", "fingerprint": "fp-test", "first_seen": deployed_at + timedelta(seconds=5), "event_count": 1},
        # project critical_routes contains "/checkout"
        {"critical_routes": ["/checkout"]},
        # recent count: only 1 event
        {"cnt": 1},
        # existing incident check: None
        None,
        # incident insert returning id 502
        {"id": 502, "is_new": True},
        # project notify_quiet
        {"name": "Test Project", "notify_quiet": False},
        # outbox insert returning id
        {"id": 999},
    ]

    mock_conn.fetch.side_effect = [
        # deploy_files
        [],
        # team_members
        [],
        # notification_channels
        [],
    ]
    mock_conn.execute = AsyncMock()

    payload = {
        "request_path": "/checkout/pay",
        "stack_trace": "",
        "exc_type": "KeyError",
    }

    # Should open immediately because /checkout/pay matches critical route "/checkout"
    inc_id = await check_and_trigger_incident(
        conn=mock_conn,
        project_id=1,
        deploy_id=11,
        issue_id=21,
        payload=payload,
        occurred_at_dt=now_dt,
    )
    assert inc_id == 502


@pytest.mark.asyncio
async def test_incident_upgrade_possible_to_confirmed():
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    deployed_at = now_dt - timedelta(minutes=10)

    # Existing incident with event_count = 9
    mock_conn.fetchrow.side_effect = [
        # deploy row
        {"deployed_at": deployed_at, "commit_sha": "sha_deploy_3", "author": "carol", "message": "msg", "codeowners": None},
        # issue row
        {"title": "RuntimeError: db pool timeout", "fingerprint": "fp-test", "first_seen": deployed_at + timedelta(seconds=1), "event_count": 10},
        # project critical_routes
        {"critical_routes": []},
        # recent count: 10
        {"cnt": 10},
        # existing incident check: status='possible', count=9 -> becomes 10 (triggers upgrade to confirmed)
        {"id": 503, "status": "possible", "event_count": 9, "first_event_at": deployed_at},
        # fetch incident details for confirmed alert dispatch
        {"title": "RuntimeError: db pool timeout", "where_file": "db.py", "where_line": 30, "where_function": "get_conn", "in_diff": False, "owners": "[]"},
        # project notify_quiet
        {"name": "Test Project", "notify_quiet": False},
        # outbox insert returning id
        {"id": 999},
    ]


    mock_conn.fetch.side_effect = [
        # team_members
        [],
        # notification_channels
        [],
    ]
    mock_conn.execute = AsyncMock()

    payload = {"request_path": "/api/users", "stack_trace": "", "exc_type": "RuntimeError"}
    inc_id = await check_and_trigger_incident(
        conn=mock_conn,
        project_id=1,
        deploy_id=12,
        issue_id=22,
        payload=payload,
        occurred_at_dt=now_dt,
    )
    assert inc_id == 503

    # Verify status was updated to confirmed
    update_calls = [str(c) for c in mock_conn.execute.call_args_list if "UPDATE incidents" in str(c)]
    assert any("confirmed" in c for c in update_calls)


@pytest.mark.asyncio
async def test_auto_resolve_stale_incidents():
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = "UPDATE 3"

    resolved_count = await auto_resolve_stale_incidents(mock_conn, resolve_after_min=30)
    assert resolved_count == 3
    sql = mock_conn.execute.call_args[0][0]
    assert "status = 'resolved'" in sql


# ==============================================================================
# 4. Channel Notifier Implementations
# ==============================================================================

@pytest.mark.asyncio
async def test_discord_notifier_payload():
    notifier = get_notifier("discord")
    assert isinstance(notifier, DiscordNotifier)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        res = await notifier.send(
            payload={"text": "Alert text", "details": {"title": "Error", "status": "confirmed"}},
            config={"webhook_url": "https://discord.com/api/webhooks/123/abc"},
        )
        assert res == "discord_delivered"
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_teams_notifier_payload():
    notifier = get_notifier("teams")
    assert isinstance(notifier, TeamsNotifier)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        res = await notifier.send(
            payload={"text": "Teams alert", "details": {"Status": "Possible"}},
            config={"webhook_url": "https://outlook.office.com/webhook/test"},
        )
        assert res == "teams_delivered"
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_notifier_with_signature():
    notifier = get_notifier("webhook")
    assert isinstance(notifier, WebhookNotifier)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        res = await notifier.send(
            payload={"event": "incident.opened", "id": 10},
            config={"url": "https://example.com/webhook", "secret": "supersecretkey"},
        )
        assert res == "webhook_delivered"
        call_kwargs = mock_post.call_args[1]
        assert "X-ChangeWatch-Signature" in call_kwargs["headers"]
        assert call_kwargs["headers"]["X-ChangeWatch-Signature"].startswith("sha256=")


@pytest.mark.asyncio
async def test_cli_add_and_list_channels(capsys):
    from app.cli import add_channel_cmd, list_channels_cmd

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "type": "slack", "enabled": True, "created_at": "now"}
    mock_conn.fetch.return_value = [
        {"id": 1, "type": "slack", "config": {"bot_token": "xoxb-12345", "channel_id": "C123"}, "enabled": True, "created_at": "now"}
    ]

    with patch("app.cli.init_db_pool", AsyncMock()), \
         patch("app.cli.close_db_pool", AsyncMock()), \
         patch("app.cli.get_connection") as mock_get_conn:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_get_conn.return_value = mock_ctx

        await add_channel_cmd(project_id=1, channel_type="slack", config_json='{"bot_token": "xoxb-12345", "channel_id": "C123"}', enabled=True)
        out = capsys.readouterr().out
        assert "Channel created: ID=1, Type=slack, Enabled=True" in out

        await list_channels_cmd(project_id=1)
        out = capsys.readouterr().out
        assert "SLACK (enabled=True)" in out
        assert "******" in out  # token must be redacted in display


@pytest.mark.asyncio
async def test_cli_add_and_list_members(capsys):
    from app.cli import add_member_cmd, list_members_cmd

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 10, "name": "Alice"}
    mock_conn.fetch.return_value = [
        {"id": 10, "name": "Alice", "git_emails": ["alice@corp.com"], "slack_user_id": "U999", "discord_user_id": None, "email": "alice@corp.com"}
    ]

    with patch("app.cli.init_db_pool", AsyncMock()), \
         patch("app.cli.close_db_pool", AsyncMock()), \
         patch("app.cli.get_connection") as mock_get_conn:
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_get_conn.return_value = mock_ctx

        await add_member_cmd(project_id=1, name="Alice", git_emails="alice@corp.com", slack_user_id="U999")
        out = capsys.readouterr().out
        assert "Team member created: ID=10, Name=Alice" in out

        await list_members_cmd(project_id=1)
        out = capsys.readouterr().out
        assert "Alice | Slack=U999" in out



@pytest.mark.asyncio
async def test_muted_issue_opens_incident_without_alert_or_investigation():
    """A mute rule silences the alert (and the AI investigation), but the incident is still recorded."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    deployed_at = now_dt - timedelta(minutes=5)
    mock_conn.fetchrow.side_effect = [
        {"deployed_at": deployed_at, "commit_sha": "sha1", "author": "alice", "message": "m", "codeowners": ""},
        {"title": "KeyError: 'x'", "fingerprint": "fp-muted", "first_seen": deployed_at, "event_count": 5},
        {"critical_routes": []},
        {"cnt": 5},
        None,  # no existing incident
        {"id": 77, "is_new": True},
    ]
    mock_conn.fetch.return_value = []
    payload = {"request_path": "/x", "stack_trace": '  File "app/x.py", line 1, in f\n', "exc_type": "KeyError"}
    with patch("app.trust.calm.is_issue_muted", new=AsyncMock(return_value=(True, "Muted by rule #3 (noisy)"))), \
         patch("app.incident_manager.dispatch_incident_alert", new=AsyncMock()) as dispatch, \
         patch("app.queue.get_redis") as get_redis:
        inc_id = await check_and_trigger_incident(
            conn=mock_conn, project_id=1, deploy_id=10, issue_id=20, payload=payload, occurred_at_dt=now_dt,
        )
    assert inc_id == 77
    dispatch.assert_not_called()
    get_redis.assert_not_called()
