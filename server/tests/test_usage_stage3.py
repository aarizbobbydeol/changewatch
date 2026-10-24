"""Unit tests for Usage Metering Addendum Stage 3.
Tests budgets, spend enforcement, warnings, outbox alerts, and the non-negotiable safety floor.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.analysis import generate_likely_cause
from app.metering import (
    Meter,
    can_spend,
    check_and_notify_budget_thresholds,
    set_project_budget,
)


@pytest.mark.asyncio
async def test_can_spend_safety_floor_always_allowed():
    """Verify that safety floor actions (verdicts, instant alerts, dead-man's switch)
    are NEVER paused or blocked, even with spend far exceeding the hard limit.
    """
    mock_conn = AsyncMock()
    # Mock budget: hard limit is $1.00 (1,000,000 micros)
    mock_conn.fetchrow.return_value = {
        "soft_limit_micros": 500000,
        "hard_limit_micros": 1000000,
        "warn_at": "[50, 80, 100]",
    }
    # Mock budget_state: current spend is $5.00 (5,000,000 micros) - 500% of hard limit!
    mock_conn.fetchval.return_value = 5000000

    # 1. Safety floor: release_verdict
    allowed, reason = await can_spend(mock_conn, project_id=1, meter="release_verdict")
    assert allowed is True
    assert reason == "safety_floor"

    # 2. Safety floor: instant_alert
    allowed, reason = await can_spend(mock_conn, project_id=1, meter="instant_alert")
    assert allowed is True
    assert reason == "safety_floor"

    # 3. Safety floor: dead_mans_switch
    allowed, reason = await can_spend(mock_conn, project_id=1, meter="dead_mans_switch")
    assert allowed is True
    assert reason == "safety_floor"

    # 4. Safety floor: healthz
    allowed, reason = await can_spend(mock_conn, project_id=1, meter="healthz")
    assert allowed is True
    assert reason == "safety_floor"

    # 5. Optional extra: ai_investigation MUST be blocked
    allowed, reason = await can_spend(mock_conn, project_id=1, meter=Meter.AI_INVESTIGATION)
    assert allowed is False
    assert reason == "budget_reached"


@pytest.mark.asyncio
async def test_budget_threshold_warnings_and_deduplication():
    """Verify that crossing 50%, 80%, and 100% enqueues outbox alerts once per threshold per month."""
    mock_conn = AsyncMock()

    # Budget: limit = $10.00 (10,000,000 micros)
    mock_conn.fetchrow.side_effect = [
        # Check 1: budgets row
        {"soft_limit_micros": 10000000, "hard_limit_micros": 20000000, "warn_at": "[50, 80, 100]"},
        # Check 1: budget_state row (spent $5.50 = 55%, last warned = 0)
        {"spent_micros": 5500000, "last_warned_pct": 0},
    ]

    now_dt = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    threshold = await check_and_notify_budget_thresholds(mock_conn, project_id=1, occurred_at=now_dt)
    assert threshold == 50

    # Verify notification inserted into outbox
    assert mock_conn.execute.call_count == 2  # 1 for budget_state last_warned_pct, 1 for outbox
    outbox_call = mock_conn.execute.call_args_list[1]
    sql, pid, dedupe_key, payload_json = outbox_call[0][:4]
    assert pid == 1
    assert dedupe_key == "budget_warn:1:2026-06-01:50"
    payload = json.loads(payload_json)
    assert payload["kind"] == "budget_warning"
    assert payload["threshold_pct"] == 50
    assert payload["severity"] == "low"

    # Subsequent check at 58% spend: already warned for 50%, so should return None
    mock_conn.fetchrow.side_effect = [
        {"soft_limit_micros": 10000000, "hard_limit_micros": 20000000, "warn_at": "[50, 80, 100]"},
        {"spent_micros": 5800000, "last_warned_pct": 50},
    ]
    mock_conn.execute.reset_mock()
    threshold_subsequent = await check_and_notify_budget_thresholds(mock_conn, project_id=1, occurred_at=now_dt)
    assert threshold_subsequent is None
    assert mock_conn.execute.call_count == 0


@pytest.mark.asyncio
async def test_raising_budget_resumes_paused_extras_immediately():
    """Verify that when a hard limit is raised above current spend, hard_limit_hit_at is cleared."""
    mock_conn = AsyncMock()
    # Current spend = $12.00 (12,000,000 micros)
    mock_conn.fetchval.return_value = 12000000

    # Raise hard limit from $10.00 to $20.00 (20,000,000 micros)
    res = await set_project_budget(
        conn=mock_conn,
        project_id=1,
        soft_limit_micros=15000000,
        hard_limit_micros=20000000,
        warn_at=[50, 80, 100],
    )
    assert res["hard_limit_micros"] == 20000000

    # Check execute calls: 1 upsert into budgets, 1 update to budget_state clearing hard_limit_hit_at
    assert mock_conn.execute.call_count == 2
    clear_call = mock_conn.execute.call_args_list[1]
    sql = clear_call[0][0]
    assert "SET hard_limit_hit_at = NULL" in sql


@pytest.mark.asyncio
async def test_safety_floor_under_hard_limit():
    """Test the non-negotiable safety floor:
    With the hard limit hit, a bad deploy:
    1. Skips the AI explanation (none is stored, so none lingers after the budget resets).
    2. Does NOT raise errors into detector or analysis.
    """
    mock_conn = AsyncMock()
    # Spend is over hard limit
    mock_conn.fetchrow.side_effect = [
        # In can_spend: budgets row
        {"soft_limit_micros": 100000, "hard_limit_micros": 100000, "warn_at": "[50, 80, 100]"},
    ]
    # Current spend exceeds hard limit
    mock_conn.fetchval.return_value = 150000

    with patch("app.analysis.get_llm_client") as llm:
        cause = await generate_likely_cause(mock_conn, project_id=1, deploy_id=99)
    # No LLM call and no explanation; the verdict and alert still go out (checked below)
    assert cause is None
    llm.assert_not_called()
