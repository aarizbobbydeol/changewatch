import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
from app.fingerprint import compute_fingerprint, normalize_stack_trace
from app.main import app
from app.worker import find_deploy, handle_event_with_retry, process_single_event


@pytest.mark.asyncio
async def test_fingerprint_normalization():
    trace1 = "Traceback (most recent call last):\n  File 'app.py', line 45, in test\n  Object at 0x7ffee4b18920"
    trace2 = "Traceback (most recent call last):\n  File 'app.py', line 99, in test\n  Object at 0x102938a0bc"

    fp1 = compute_fingerprint("error", exc_type="ValueError", stack_trace=trace1)
    fp2 = compute_fingerprint("error", exc_type="ValueError", stack_trace=trace2)

    assert fp1 == fp2, "Fingerprints should be identical despite different line numbers and memory addresses"


@pytest.mark.asyncio
async def test_fingerprint_slow_request():
    fp_slow = compute_fingerprint("slow_request", request_path="/api/slow-endpoint")
    fp_slow_again = compute_fingerprint("slow_request", request_path="/api/slow-endpoint")
    fp_diff = compute_fingerprint("slow_request", request_path="/api/other")

    assert fp_slow == fp_slow_again
    assert fp_slow != fp_diff


@pytest.mark.asyncio
async def test_deploy_hook_success():
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 101}

    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_pool_acquire = MagicMock()
    mock_pool_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_pool_acquire

    mock_redis = AsyncMock()
    mock_redis.get.return_value = "1"  # cached project_id 1

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/deploys",
                headers={"X-API-Key": "cw_test_key_123"},
                json={
                    "commit_sha": "a1b2c3d4",
                    "author": "Alice",
                    "message": "Release v1.0",
                    "diff": "diff content",
                    "environment": "prod",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["deploy_id"] == 101


@pytest.mark.asyncio
async def test_ingest_events_validation_timestamp():
    """Reject events with occurred_at > 60s in future or > 24 hours in past."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = "1"

    now = time.time()
    future_time = now + 120.0  # 2 minutes in future
    old_time = now - 90000.0   # 25 hours ago

    with patch("app.auth.get_redis", return_value=mock_redis):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Future event
            resp_future = await client.post(
                "/v1/events",
                headers={"X-API-Key": "cw_test_key_123"},
                json={
                    "events": [
                        {
                            "type": "error",
                            "exc_type": "ValueError",
                            "message": "Future error",
                            "occurred_at": future_time,
                        }
                    ]
                },
            )
            assert resp_future.status_code == 422

            # Old event
            resp_old = await client.post(
                "/v1/events",
                headers={"X-API-Key": "cw_test_key_123"},
                json={
                    "events": [
                        {
                            "type": "error",
                            "exc_type": "ValueError",
                            "message": "Old error",
                            "occurred_at": old_time,
                        }
                    ]
                },
            )
            assert resp_old.status_code == 422


@pytest.mark.asyncio
async def test_ingest_events_rate_limit_exceeded():
    """Return 429 when rate limit of 600 events per minute is exceeded."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = "1"

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.main.check_rate_limit", new_callable=AsyncMock) as mock_rl:
        mock_rl.return_value = (False, 605, 600)  # Rate limit exceeded

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/events",
                headers={"X-API-Key": "cw_test_key_123"},
                json={
                    "events": [
                        {
                            "type": "error",
                            "exc_type": "KeyError",
                            "message": "User not found",
                            "occurred_at": time.time(),
                        }
                    ]
                },
            )
            assert resp.status_code == 429
            assert "Rate limit exceeded" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ingest_events_success_queue_pipeline():
    """Events are pushed to Redis list 'events' and accepted immediately."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = "1"

    mock_pipe = AsyncMock()
    mock_pipe.rpush = MagicMock()
    mock_pipe.execute = AsyncMock(return_value=[1, 2])
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.main.get_redis", return_value=mock_redis), \
         patch("app.main.check_rate_limit", new_callable=AsyncMock) as mock_rl:
        mock_rl.return_value = (True, 2, 600)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/events",
                headers={"X-API-Key": "cw_test_key_123"},
                json={
                    "events": [
                        {
                            "type": "error",
                            "exc_type": "KeyError",
                            "message": "test error",
                            "occurred_at": time.time(),
                        },
                        {
                            "type": "heartbeat",
                            "requests": 150,
                            "errors": 2,
                            "occurred_at": time.time(),
                        },
                    ]
                },
            )
            assert resp.status_code == 200
            assert resp.json()["accepted"] == 2
            assert mock_pipe.rpush.call_count == 2


@pytest.mark.asyncio
async def test_find_deploy_exact_and_fallback():
    mock_conn = AsyncMock()
    from datetime import datetime, timezone
    now_dt = datetime.now(timezone.utc)

    # 1. Exact match returns row
    mock_conn.fetchrow.return_value = {"id": 10}
    dep1 = await find_deploy(mock_conn, 1, "prod", now_dt, release="sha_123")
    assert dep1 == 10

    # 2. Exact match returns None, fallback returns row
    mock_conn.fetchrow.side_effect = [None, {"id": 9}]
    dep2 = await find_deploy(mock_conn, 1, "prod", now_dt, release="nonexistent")
    assert dep2 == 9


@pytest.mark.asyncio
async def test_worker_dead_letter_after_3_retries():
    """Event failing 3 times is pushed to dead-letter queue events:dead."""
    mock_redis = AsyncMock()
    mock_redis.rpush = AsyncMock()

    payload = {
        "project_id": 1,
        "type": "error",
        "occurred_at": time.time(),
        "_tries": 2,  # Already failed twice, this is the 3rd attempt
    }

    with patch("app.worker.get_redis", return_value=mock_redis), \
         patch("app.worker.process_single_event", side_effect=RuntimeError("DB write failed")):
        await handle_event_with_retry(payload)

        # Should be pushed to events:dead
        mock_redis.rpush.assert_called_once()
        args = mock_redis.rpush.call_args[0]
        assert args[0] == "events:dead"
        saved_payload = json.loads(args[1])
        assert saved_payload["_tries"] == 3
