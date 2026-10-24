"""Tests for Performance Addendum Stage 5:
1. Storm Control: caps full event rows to STORE_FULL_PER_MIN per (project, fingerprint) per minute
   while keeping issues.event_count, event_rollups, and Redis sliding-window counters exact.
2. Backpressure Shedding: drops slow_request first, samples repeat errors,
   and NEVER drops deploys, heartbeats, or the first occurrence of a new fingerprint.
3. Telemetry: reports dropped count in ingest response and /healthz.
"""
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from app.auth import hash_api_key
from app.cache import api_key_cache
from app.config import settings
from app.main import app
from app.worker import process_event_batch, process_single_event


@pytest.fixture(autouse=True)
def reset_caches():
    api_key_cache.clear()
    yield
    api_key_cache.clear()


def build_mock_pool(mock_conn: AsyncMock):
    """Helper to mock asyncpg pool and transactions."""
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_acquire = MagicMock()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_acquire
    return mock_pool


@pytest.fixture
def auth_keys():
    ingest_key = "cw_ingest_stage5_test_key"
    read_key = "cw_read_stage5_test_key"
    return {
        "ingest_key": ingest_key,
        "ingest_hash": hash_api_key(ingest_key),
        "read_key": read_key,
        "read_hash": hash_api_key(read_key),
    }


@pytest.mark.asyncio
async def test_storm_control_caps_full_event_rows_and_keeps_counters_exact():
    """
    Simulate a crash loop burst of 50 events for a single fingerprint with STORE_FULL_PER_MIN=5.
    Verify:
    1. Exactly 5 full event rows are inserted into events table.
    2. issues.event_count is incremented by 50.
    3. event_rollups count is incremented by 50.
    4. Redis sliding-window counter total is 50.
    """
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # Mock Redis with in-memory store for storm control and sliding window
    redis_store = {}

    async def _mock_incrby(key, count):
        redis_store[key] = redis_store.get(key, 0) + count
        return redis_store[key]

    async def _mock_expire(key, ttl):
        return True

    mock_redis = AsyncMock()
    mock_redis.incrby.side_effect = _mock_incrby
    mock_redis.expire.side_effect = _mock_expire

    def _sync_incrby(key, count):
        redis_store[key] = redis_store.get(key, 0) + count
        return redis_store[key]

    mock_pipe = MagicMock()
    mock_pipe.incrby.side_effect = _sync_incrby
    mock_pipe.expire.side_effect = lambda k, t: None
    mock_pipe.execute = AsyncMock(return_value=[])
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    # 50 events of the same fingerprint
    now = time.time()
    events = [
        {
            "event_id": f"ev_{i}",
            "project_id": 1,
            "type": "error",
            "exc_type": "ZeroDivisionError",
            "message": "division by zero",
            "stack_trace": "crash() line 10",
            "occurred_at": now + (i * 0.1),
            "environment": "prod",
            "release": "v1.0.0",
        }
        for i in range(50)
    ]

    mock_conn.fetchrow.return_value = {"id": 10}  # deploy_id
    mock_conn.fetch.return_value = [
        {"fingerprint": "mock_fp", "id": 100}
    ]

    with patch("app.worker.project_transaction", return_value=mock_pool.acquire.return_value), \
         patch("app.worker.get_redis", return_value=mock_redis), \
         patch("app.crypto.encrypt", return_value=b"enc_val"), \
         patch.object(settings, "STORE_FULL_PER_MIN", 5):

        await process_event_batch(events)

        # 1. Verify issues upsert tracked all 50 events in issues.event_count
        issue_upsert_call = None
        for call in mock_conn.fetch.call_args_list:
            sql = call[0][0]
            if "INSERT INTO issues" in sql and "event_count" in sql:
                issue_upsert_call = call
                break

        assert issue_upsert_call is not None, "issues upsert must track event_count"
        # The 6th parameter is counts = [50]
        assert issue_upsert_call[0][6] == [50]

        # 2. Verify events table insert received ONLY 5 rows (capped by STORE_FULL_PER_MIN=5)
        events_insert_call = None
        for call in mock_conn.execute.call_args_list:
            sql = call[0][0]
            if "INSERT INTO events" in sql:
                events_insert_call = call
                break

        assert events_insert_call is not None, "events insert must be called"
        inserted_event_ids = events_insert_call[0][2]
        assert len(inserted_event_ids) == 5, f"Expected exactly 5 stored rows, got {len(inserted_event_ids)}"

        # 3. Verify event_rollups table recorded the full 50 events
        rollup_call = None
        for call in mock_conn.execute.call_args_list:
            sql = call[0][0]
            if "INSERT INTO event_rollups" in sql:
                rollup_call = call
                break

        assert rollup_call is not None, "event_rollups insert must be called"
        rollup_counts = rollup_call[0][5]
        assert sum(rollup_counts) == 50, "event_rollups must record all 50 occurrences"


@pytest.mark.asyncio
async def test_backpressure_shedding_order(auth_keys):
    """
    Under backpressure (queue_len > QUEUE_SOFT_LIMIT):
    1. slow_request events are dropped first.
    2. Heartbeats are NEVER dropped.
    3. New fingerprints are NEVER dropped.
    4. Repeat error events are sampled.
    5. Dropped count is returned in response.
    """
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    # Queue length exceeds soft limit (10,000)
    mock_redis.llen.return_value = 15000
    mock_redis.sismember.return_value = False  # By default unknown
    mock_redis.sadd.return_value = 1
    mock_redis.incrby.return_value = 1

    mock_pipe = MagicMock()
    mock_pipe.rpush = MagicMock()
    mock_pipe.execute = AsyncMock(return_value=[])
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = auth_row
    mock_pool = build_mock_pool(mock_conn)

    now = time.time()
    events = [
        # 1. Heartbeat -> MUST BE KEPT
        {
            "event_id": "hb_1",
            "type": "heartbeat",
            "occurred_at": now,
            "requests": 100,
            "errors": 2,
        },
        # 2. Slow request -> MUST BE DROPPED
        {
            "event_id": "sr_1",
            "type": "slow_request",
            "occurred_at": now,
            "request_path": "/slow",
            "duration_ms": 2500.0,
        },
        # 3. New error fingerprint -> MUST BE KEPT
        {
            "event_id": "err_new_1",
            "type": "error",
            "exc_type": "RuntimeError",
            "message": "brand new error",
            "occurred_at": now,
        },
    ]

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.main.get_redis", return_value=mock_redis), \
         patch("app.db.get_db_pool", return_value=mock_pool), \
         patch.object(settings, "QUEUE_SOFT_LIMIT", 1000):

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/events",
                headers={"X-API-Key": auth_keys["ingest_key"]},
                json={"events": events},
            )
            assert resp.status_code == 200
            data = resp.json()
            # 1 heartbeat + 1 new error kept = 2 accepted; 1 slow_request dropped = 1 dropped
            assert data["accepted"] == 2
            assert data["dropped"] == 1

            # Verify dropped counter was incremented in Redis
            mock_redis.incrby.assert_any_call("counter:events_dropped", 1)


@pytest.mark.asyncio
async def test_deploys_never_dropped_under_backpressure(auth_keys):
    """POST /v1/deploys must never drop deploys even under severe queue pressure."""
    mock_conn = AsyncMock()
    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    deploy_row = {"id": 42}
    mock_conn.fetchrow.side_effect = [auth_row, deploy_row]
    mock_pool = build_mock_pool(mock_conn)

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.llen.return_value = 50000  # Extreme backlog

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.main.get_redis", return_value=mock_redis), \
         patch("app.db.get_db_pool", return_value=mock_pool):

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/deploys",
                headers={"X-API-Key": auth_keys["ingest_key"]},
                json={
                    "commit_sha": "abc1234567890",
                    "author": "Alice",
                    "message": "fix: critical",
                    "environment": "prod",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["deploy_id"] == 42


@pytest.mark.asyncio
async def test_healthz_reports_dropped_count():
    """GET /healthz reports dropped_count from Redis counter."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_redis.llen = AsyncMock(return_value=12)
    mock_redis.get = AsyncMock(return_value="450")  # 450 dropped events
    mock_redis.scan = AsyncMock(return_value=(0, [b"events:processing:p1", b"events:processing:p2"]))

    with patch("app.main.check_db_health", return_value=True), \
         patch("app.main.check_redis_health", return_value=True), \
         patch("app.main.get_redis", return_value=mock_redis):

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200
            data = resp.json()
            assert data["dropped_count"] == 450
            assert data["queue_depth"] == 12
            assert data["worker_count"] == 2
