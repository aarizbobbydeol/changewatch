from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import pytest
from app.analysis import dispatch_verdict_alert, format_slack_blocks, generate_likely_cause
from app.detector import calculate_p95, evaluate_deploy


@pytest.fixture(autouse=True)
def source_not_silent():
    """These verdict tests mock the queries for traffic and errors; the app's source is reporting."""
    with patch("app.sources.is_source_silent", new=AsyncMock(return_value=False)):
        yield


def test_p95_calculation():
    samples = list(range(1, 101))
    p95 = calculate_p95(samples)
    assert p95 == 95.0

    assert calculate_p95([]) == 0.0
    assert calculate_p95([10.0]) == 10.0


def test_slack_blocks_formatting():
    blocks_payload = format_slack_blocks(
        project_name="my-test-app",
        verdict="rollback",
        reasons=["25 new errors appeared"],
        commit_sha="abcdef123456",
        author="Alice",
        message="breaking commit",
        likely_cause="KeyError in checkout.py",
        top_errors=[{"title": "KeyError: token", "count": 25}],
    )
    assert "blocks" in blocks_payload
    assert "ROLLBACK" in blocks_payload["text"]
    assert any("KeyError in checkout.py" in str(b) for b in blocks_payload["blocks"])


@pytest.mark.asyncio
async def test_verdict_rule_no_data():
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    deploy = {
        "id": 1,
        "deployed_at": now_dt - timedelta(minutes=15),
    }

    # Zero traffic and zero events
    mock_conn.fetchrow.side_effect = [
        {"reqs": 0, "errs": 0},  # traffic after
        {"reqs": 0, "errs": 0},  # traffic before
    ]
    mock_conn.fetchval.side_effect = [
        0,  # events after count
        0,  # errors after count
        0,  # errors before count
    ]
    mock_conn.fetch.side_effect = [
        [],  # new issues
        [],  # dur after
        [],  # dur before
    ]

    verdict, reasons, is_final, is_flagged = await evaluate_deploy(
        mock_conn, project_id=1, deploy=deploy, now_dt=now_dt
    )
    assert verdict == "no_data"
    assert "no traffic seen" in reasons[0]
    assert is_flagged is False


@pytest.mark.asyncio
async def test_verdict_rule_rollback_new_issues():
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    deploy = {
        "id": 1,
        "deployed_at": now_dt - timedelta(minutes=15),
    }

    # Traffic present
    mock_conn.fetchrow.side_effect = [
        {"reqs": 100, "errs": 25},
        {"reqs": 100, "errs": 0},
    ]
    mock_conn.fetchval.side_effect = [
        25,  # events after
        25,  # errors after
        0,   # errors before
    ]
    # 2 new issues with 25 combined events (> 20 threshold)
    mock_conn.fetch.side_effect = [
        [{"id": 1, "ev_count": 15}, {"id": 2, "ev_count": 10}],  # new issues
        [],  # dur after
        [],  # dur before
    ]

    verdict, reasons, is_final, is_flagged = await evaluate_deploy(
        mock_conn, project_id=1, deploy=deploy, now_dt=now_dt
    )
    assert verdict == "rollback"
    assert any("2 new error types" in r for r in reasons)
    assert is_flagged is True


@pytest.mark.asyncio
async def test_verdict_rule_safe():
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    deploy = {
        "id": 1,
        "deployed_at": now_dt - timedelta(minutes=15),
    }

    mock_conn.fetchrow.side_effect = [
        {"reqs": 500, "errs": 0},
        {"reqs": 400, "errs": 0},
    ]
    mock_conn.fetchval.side_effect = [
        500,  # events after
        0,    # errors after
        0,    # errors before
    ]
    mock_conn.fetch.side_effect = [
        [],  # no new issues
        [{"duration_ms": 20.0}] * 20,  # dur after
        [{"duration_ms": 20.0}] * 20,  # dur before
    ]

    verdict, reasons, is_final, is_flagged = await evaluate_deploy(
        mock_conn, project_id=1, deploy=deploy, now_dt=now_dt
    )
    assert verdict == "safe"
    assert is_flagged is False


@pytest.mark.asyncio
async def test_generate_likely_cause_scrubs_and_returns():
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "commit_sha": "a1b2c3d4e5f6",
        "author": "Alice",
        "message": "Update database credentials",
        "diff": "AWS_CREDENTIAL AKIA1234567890ABCDEF\napi_key = 'my_secret'\n+ raise KeyError('token')",
        "deployed_at": datetime.now(timezone.utc),
    }
    mock_conn.fetch.return_value = [
        {"title": "KeyError: token", "count": 25, "sample_trace": "File app.py line 42 with sk-1234567890abcdef"}
    ]

    mock_llm = AsyncMock()
    mock_llm.complete.return_value = (
        "Likely cause: KeyError in app.py due to missing token. Suggested fix: Check token key in dictionary."
    )

    with patch("app.analysis.get_llm_client", return_value=mock_llm):
        cause = await generate_likely_cause(mock_conn, project_id=1, deploy_id=10)

        assert cause is not None
        assert "KeyError" in cause

        # Verify LLM prompt was scrubbed
        call_kwargs = mock_llm.complete.call_args[1]
        prompt_sent = call_kwargs["prompt"]
        assert "AKIA1234567890ABCDEF" not in prompt_sent
        assert "sk-1234567890abcdef" not in prompt_sent
        assert "[REDACTED_AWS_KEY]" in prompt_sent


@pytest.mark.asyncio
async def test_dispatch_verdict_alert_idempotent():
    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        # Fetch project and deploy metadata
        {"project_name": "app", "commit_sha": "abc", "author": "dev", "message": "msg"},
        # First alert insert succeeds
        {"id": 1},
    ]
    mock_conn.fetch.return_value = []  # top errors

    with patch("app.analysis.queue_notification", new_callable=AsyncMock) as mock_queue:
        mock_queue.return_value = True

        # First alert dispatch
        res1 = await dispatch_verdict_alert(
            mock_conn, 1, 10, "rollback", ["reasons"], "verdict_first"
        )
        assert res1 is True
        mock_queue.assert_called_once()

        # Second alert dispatch for same deploy and kind returns False (already exists)
        mock_conn.fetchrow.side_effect = [
            {"project_name": "app", "commit_sha": "abc", "author": "dev", "message": "msg"},
            None,  # Alert insert returns None due to UNIQUE (deploy_id, kind)
        ]
        res2 = await dispatch_verdict_alert(
            mock_conn, 1, 10, "rollback", ["reasons"], "verdict_first"
        )
        assert res2 is False
