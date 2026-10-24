"""Unit tests for Usage Metering Addendum Stage 2.
Tests recording hooks for deploys (release_monitored), investigations (ai_investigation),
stored events Redis aggregation, and periodic flush.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.metering import (
    Meter,
    PriceBookEntry,
    flush_usage_events,
    record_investigation_usage,
    record_stored_event_metric,
    record_usage,
)


@pytest.fixture
def sample_price_book():
    return {
        "release_monitored": PriceBookEntry(
            version=1,
            meter="release_monitored",
            pricing_mode="fixed",
            unit_price_micros=20000,
            margin_multiplier=1.0,
            min_unit_price_micros=0,
            free_units_per_month=200,
        ),
        "ai_investigation": PriceBookEntry(
            version=1,
            meter="ai_investigation",
            pricing_mode="cost_plus",
            unit_price_micros=0,
            margin_multiplier=1.30,
            min_unit_price_micros=1000,
            free_units_per_month=20,
        ),
        "ai_investigation_deep": PriceBookEntry(
            version=1,
            meter="ai_investigation_deep",
            pricing_mode="cost_plus",
            unit_price_micros=0,
            margin_multiplier=1.30,
            min_unit_price_micros=5000,
            free_units_per_month=0,
        ),
        "stored_events": PriceBookEntry(
            version=1,
            meter="stored_events",
            pricing_mode="fixed",
            unit_price_micros=2,
            margin_multiplier=1.0,
            min_unit_price_micros=0,
            free_units_per_month=100000,
        ),
    }


@pytest.mark.asyncio
async def test_record_usage_idempotency_and_ledger(sample_price_book):
    """Verify that record_usage inserts into usage_events, usage_daily, and budget_state in one transaction.
    Replaying the same idempotency key must not double count.
    """
    mock_conn = AsyncMock()

    with patch("app.metering.get_active_price_book", return_value=sample_price_book):
        # 1. First record: free units used = 0
        mock_conn.fetchval.return_value = 0
        mock_conn.fetchrow.return_value = {"id": 101}

        now_dt = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        usage_id = await record_usage(
            conn=mock_conn,
            project_id=1,
            meter=Meter.RELEASE_MONITORED,
            units=1,
            ref_type="deploy",
            ref_id=42,
            idempotency_key="deploy:42",
            occurred_at=now_dt,
        )
        assert usage_id == 101

        # Check that usage_events, usage_daily, and budget_state were updated
        assert mock_conn.fetchrow.call_count == 1
        assert mock_conn.execute.call_count == 2  # usage_daily and budget_state

        # 2. Duplicate / replayed record: fetchrow returns None (ON CONFLICT DO NOTHING)
        mock_conn.fetchrow.return_value = None
        mock_conn.execute.reset_mock()

        dup_id = await record_usage(
            conn=mock_conn,
            project_id=1,
            meter=Meter.RELEASE_MONITORED,
            units=1,
            ref_type="deploy",
            ref_id=42,
            idempotency_key="deploy:42",
            occurred_at=now_dt,
        )
        assert dup_id is None
        # On duplicate, usage_daily and budget_state are NOT touched
        assert mock_conn.execute.call_count == 0


@pytest.mark.asyncio
async def test_record_investigation_fairness_rules(sample_price_book):
    """Verify AI investigation fairness rules:
    - Failed investigations record nothing.
    - Cached investigations record nothing.
    - Partial investigations record real token cost with units=1; billed only if result delivered.
    """
    mock_conn = AsyncMock()

    with patch("app.metering.get_active_price_book", return_value=sample_price_book), \
         patch("app.metering.settings.LLM_PRICES_JSON", json.dumps({"claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0}})):

        # 1. Failed investigation records nothing
        res_failed = await record_investigation_usage(
            conn=mock_conn,
            project_id=1,
            investigation_id=10,
            status="failed",
        )
        assert res_failed is None

        # 2. Cached investigation records nothing
        res_cached = await record_investigation_usage(
            conn=mock_conn,
            project_id=1,
            investigation_id=11,
            status="done",
            cached_from="hash123",
        )
        assert res_cached is None

        # 3. Done investigation (billable after free units exhausted)
        mock_conn.fetchval.return_value = 20  # All 20 free units used
        mock_conn.fetchrow.return_value = {"id": 201}

        # 1,000 input tokens ($0.003 -> 3,000 micros), 200 output tokens ($0.003 -> 3,000 micros) = 6,000 micros
        # Margin 1.30 -> 6,000 * 1.30 = 7,800 micros
        res_done = await record_investigation_usage(
            conn=mock_conn,
            project_id=1,
            investigation_id=12,
            status="done",
            input_tokens=1000,
            output_tokens=200,
            provider="anthropic",
        )
        assert res_done == 201

        # Check call arguments to usage_events insert
        insert_args = mock_conn.fetchrow.call_args[0]
        # SQL parameters: ($1: project_id, $2: meter, $3: units, $4: occurred_at, $5: ref_type, $6: ref_id,
        #                  $7: our_cost_micros, $8: billed_micros, $9: free_applied, $10: pb_version, $11: idem_key)
        assert insert_args[1] == 1  # project_id
        assert insert_args[2] == "ai_investigation"
        assert insert_args[3] == 1  # units
        assert insert_args[7] == 6000  # our_cost_micros
        assert insert_args[8] == 7800  # billed_micros
        assert insert_args[9] == 0  # free_applied
        assert insert_args[11] == "inv:12"

        # 4. Partial investigation without result delivered:
        # units=1, our_cost recorded, but billed_micros = 0
        mock_conn.fetchrow.return_value = {"id": 202}
        res_partial_undelivered = await record_investigation_usage(
            conn=mock_conn,
            project_id=1,
            investigation_id=13,
            status="partial",
            input_tokens=500,
            output_tokens=50,
            provider="anthropic",
            result_delivered=False,
        )
        assert res_partial_undelivered == 202
        partial_args = mock_conn.fetchrow.call_args[0]
        assert partial_args[7] > 0  # our_cost is tracked
        assert partial_args[8] == 0  # customer is NOT billed ($0)

        # 5. Local / fake provider: our cost = 0 and billed = 0
        mock_conn.fetchrow.return_value = {"id": 203}
        res_local = await record_investigation_usage(
            conn=mock_conn,
            project_id=1,
            investigation_id=14,
            status="done",
            input_tokens=1000,
            output_tokens=200,
            provider="ollama",
        )
        assert res_local == 203
        local_args = mock_conn.fetchrow.call_args[0]
        assert local_args[7] == 0  # our_cost = 0
        assert local_args[8] == 0  # billed = 0


@pytest.mark.asyncio
async def test_stored_events_redis_aggregation_and_flush(sample_price_book):
    """Verify stored events aggregation in Redis per (project, minute) and periodic flush."""
    mock_redis = AsyncMock()
    now_dt = datetime(2026, 6, 15, 14, 30, 22, tzinfo=timezone.utc)
    m_epoch = int(now_dt.replace(second=0, microsecond=0).timestamp())

    # 1. Worker records stored events to Redis
    await record_stored_event_metric(mock_redis, project_id=7, occurred_at=now_dt, count=25)
    mock_redis.incrby.assert_called_once_with(f"usage:events:7:{m_epoch}", 25)
    mock_redis.sadd.assert_called_once_with("usage:pending_event_buckets", f"7:{m_epoch}")

    # 2. Flush aggregated bucket from Redis
    mock_conn = AsyncMock()
    mock_redis.smembers.return_value = {f"7:{m_epoch}".encode()}

    # Pipeline returns [count_val, delete_res, srem_res]
    mock_pipe = MagicMock()
    mock_pipe.execute = AsyncMock(return_value=["25", 1, 1])
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    mock_conn.fetchval.return_value = 0  # free units applied so far
    mock_conn.fetchrow.return_value = {"id": 301}

    with patch("app.metering.get_active_price_book", return_value=sample_price_book):
        flushed_count = await flush_usage_events(conn=mock_conn, redis=mock_redis, force=True)
        assert flushed_count == 25

        # Check call to insert usage_events
        insert_args = mock_conn.fetchrow.call_args[0]
        assert insert_args[1] == 7  # project_id
        assert insert_args[2] == "stored_events"
        assert insert_args[3] == 25  # units
        assert insert_args[11] == f"events:7:{m_epoch}"  # idempotency_key


@pytest.mark.asyncio
async def test_deploy_hook_usage_recording_integration():
    """Verify that POST /v1/deploys automatically triggers release_monitored recording."""
    from app.auth import require_ingest_project_id
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    app.dependency_overrides[require_ingest_project_id] = lambda: 5
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("app.main.project_transaction") as mock_ptx, \
                 patch("app.main.record_usage", new_callable=AsyncMock) as mock_record_usage:

                mock_conn = AsyncMock()
                mock_conn.fetchrow.return_value = {"id": 99}
                mock_ptx.return_value.__aenter__.return_value = mock_conn

                res = await client.post(
                    "/v1/deploys",
                    json={
                        "commit_sha": "abc1234567890",
                        "author": "alice@example.com",
                        "message": "Deploying release v1.0",
                        "environment": "prod",
                    },
                    headers={"Authorization": "Bearer cw_ingest_dummykey123456789"},
                )
                assert res.status_code == 200
                assert res.json() == {"ok": True, "deploy_id": 99}

                # Check that record_usage was invoked for release_monitored
                mock_record_usage.assert_called_once()
                call_kwargs = mock_record_usage.call_args.kwargs
                assert call_kwargs["project_id"] == 5
                assert call_kwargs["meter"] == Meter.RELEASE_MONITORED
                assert call_kwargs["units"] == 1
                assert call_kwargs["ref_type"] == "deploy"
                assert call_kwargs["ref_id"] == 99
                assert call_kwargs["idempotency_key"] == "deploy:99"
    finally:
        app.dependency_overrides.pop(require_ingest_project_id, None)


@pytest.mark.asyncio
async def test_usage_daily_equals_sum_of_usage_events(sample_price_book):
    """Verify ledger invariant: usage_daily is strictly equal to the sum of usage_events."""
    # Simulate a series of usage records in memory
    recorded_events = []
    daily_rollups = {}

    mock_conn = AsyncMock()

    async def fake_fetchrow(sql, *args):
        # args: (project_id, meter, units, occurred_at, ref_type, ref_id,
        #        our_cost_micros, billed_micros, free_applied, pb_version, idem_key)
        for e in recorded_events:
            if e["project_id"] == args[0] and e["idempotency_key"] == args[10]:
                return None  # conflict
        ev = {
            "id": len(recorded_events) + 1,
            "project_id": args[0],
            "meter": args[1],
            "units": args[2],
            "occurred_at": args[3],
            "our_cost_micros": args[6],
            "billed_micros": args[7],
            "free_units_applied": args[8],
            "idempotency_key": args[10],
        }
        recorded_events.append(ev)
        return {"id": ev["id"]}

    async def fake_execute(sql, *args):
        if "INSERT INTO usage_daily" in sql:
            # args: (project_id, day, meter, units, our_cost, billed, free_applied)
            key = (args[0], args[1], args[2])
            curr = daily_rollups.get(key, {"units": 0, "our_cost": 0, "billed": 0, "free": 0})
            curr["units"] += args[3]
            curr["our_cost"] += args[4]
            curr["billed"] += args[5]
            curr["free"] += args[6]
            daily_rollups[key] = curr

    mock_conn.fetchrow.side_effect = fake_fetchrow
    mock_conn.execute.side_effect = fake_execute
    mock_conn.fetchval.return_value = 0

    now_dt = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    with patch("app.metering.get_active_price_book", return_value=sample_price_book):
        # Record 10 distinct deploys
        for i in range(10):
            await record_usage(
                conn=mock_conn,
                project_id=1,
                meter=Meter.RELEASE_MONITORED,
                units=1,
                idempotency_key=f"deploy:{i}",
                occurred_at=now_dt + timedelta(minutes=i),
            )

        # Attempt to replay first 5 (must be ignored)
        for i in range(5):
            await record_usage(
                conn=mock_conn,
                project_id=1,
                meter=Meter.RELEASE_MONITORED,
                units=1,
                idempotency_key=f"deploy:{i}",
                occurred_at=now_dt + timedelta(minutes=i),
            )

    # Verify ledger parity
    total_event_units = sum(e["units"] for e in recorded_events)
    total_event_billed = sum(e["billed_micros"] for e in recorded_events)
    total_event_free = sum(e["free_units_applied"] for e in recorded_events)

    rollup = daily_rollups[(1, now_dt.date(), "release_monitored")]
    assert rollup["units"] == total_event_units == 10
    assert rollup["billed"] == total_event_billed
    assert rollup["free"] == total_event_free


@pytest.mark.asyncio
async def test_crash_loop_storm_control_caps_stored_events(accept_event_claims):
    """Verify that during a crash loop of 100,000 errors, stored_events is capped by storm control."""
    from app.config import settings
    from app.worker import process_single_event

    mock_redis = AsyncMock()
    mock_pipe = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    # Mock redis.incr for storm control: returns values incrementing up to 50
    storm_counter = 0

    async def fake_redis_incr(key):
        nonlocal storm_counter
        storm_counter += 1
        return storm_counter

    mock_redis.incr.side_effect = fake_redis_incr
    stored_metric_calls = []

    async def fake_stored_metric(redis, project_id, occurred_at, count=1):
        stored_metric_calls.append(count)

    mock_conn = AsyncMock()
    accept_event_claims(mock_conn)
    mock_conn.fetchrow.return_value = {"id": 1}

    mock_ptx = MagicMock()
    mock_ptx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ptx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.worker.get_redis", return_value=mock_redis), \
         patch("app.worker.project_transaction", return_value=mock_ptx), \
         patch("app.worker.record_stored_event_metric", side_effect=fake_stored_metric), \
         patch("app.worker.find_deploy", return_value=1), \
         patch("app.crypto.encrypt", return_value=b"enc"):

        now_dt = datetime.now(timezone.utc)

        # Send 50 crash loop events
        for i in range(50):
            payload = {
                "project_id": 1,
                "event_id": f"crash_evt_{i}",
                "type": "error",
                "message": "Crash loop unhandled exception",
                "occurred_at": now_dt.timestamp(),
                "environment": "prod",
            }
            await process_single_event(payload)

        # Total full stored event rows capped by STORE_FULL_PER_MIN (default 20)
        assert len(stored_metric_calls) == settings.STORE_FULL_PER_MIN
        assert sum(stored_metric_calls) == settings.STORE_FULL_PER_MIN


@pytest.mark.asyncio
async def test_usage_recording_failure_does_not_break_deploy_or_ingest():
    """Verify error isolation: recording failures never break deploy creation."""
    from app.auth import require_ingest_project_id
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    app.dependency_overrides[require_ingest_project_id] = lambda: 3
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("app.main.project_transaction") as mock_ptx, \
                 patch("app.main.record_usage", side_effect=RuntimeError("Database lock timeout on usage table")):

                mock_conn = AsyncMock()
                mock_conn.fetchrow.return_value = {"id": 105}
                mock_ptx.return_value.__aenter__.return_value = mock_conn

                res = await client.post(
                    "/v1/deploys",
                    json={
                        "commit_sha": "def4567890abc",
                        "author": "bob@example.com",
                        "message": "Deploy release v1.1",
                        "environment": "prod",
                    },
                    headers={"Authorization": "Bearer cw_ingest_dummy"},
                )
                # Deploy MUST still succeed with HTTP 200 OK!
                assert res.status_code == 200
                assert res.json() == {"ok": True, "deploy_id": 105}
    finally:
        app.dependency_overrides.pop(require_ingest_project_id, None)

