"""Tests for Performance Addendum Stage 4:
1. Thread-safe LRUTTLCache (LRU eviction, TTL expiry, invalidation, concurrency)
2. Deploy cache invalidation on new deploy registration
3. Redis sliding-window atomic counters (10s buckets, 6-bucket sliding window sum)
4. Event rollups aggregation and timeline integration
"""
import asyncio
import concurrent.futures
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.cache import (
    LRUTTLCache,
    api_key_cache,
    deploy_lookup_cache,
    issue_lookup_cache,
    invalidate_deploy_cache,
    increment_sliding_window_counter,
    get_sliding_window_sum,
)


def test_lru_ttl_cache_basic_and_ttl():
    cache = LRUTTLCache[str, str](maxsize=3, default_ttl=0.1)

    # 1. Set and get
    cache.set("a", "alpha")
    assert cache.get("a") == "alpha"
    assert len(cache) == 1

    # 2. TTL expiry
    time.sleep(0.15)
    assert cache.get("a") is None
    assert len(cache) == 0


def test_lru_eviction():
    cache = LRUTTLCache[str, int](maxsize=3, default_ttl=60.0)

    cache.set("k1", 1)
    cache.set("k2", 2)
    cache.set("k3", 3)
    assert len(cache) == 3

    # Access k1 to make k2 the least recently used
    assert cache.get("k1") == 1

    # Insert k4, should evict k2
    cache.set("k4", 4)
    assert len(cache) == 3
    assert cache.get("k2") is None
    assert cache.get("k1") == 1
    assert cache.get("k3") == 3
    assert cache.get("k4") == 4


def test_cache_invalidation():
    cache = LRUTTLCache[str, str](maxsize=10, default_ttl=60.0)
    cache.set("x", "100")
    assert cache.get("x") == "100"
    assert cache.invalidate("x") is True
    assert cache.get("x") is None
    assert cache.invalidate("non_existent") is False


def test_project_invalidation():
    cache = LRUTTLCache[tuple, int](maxsize=100, default_ttl=300.0)
    cache.set((1, "prod", "sha1"), 101)
    cache.set((1, "staging", "sha2"), 102)
    cache.set((2, "prod", "sha3"), 201)

    assert len(cache) == 3
    removed = cache.invalidate_project(1)
    assert removed == 2
    assert len(cache) == 1
    assert cache.get((2, "prod", "sha3")) == 201
    assert cache.get((1, "prod", "sha1")) is None


def test_deploy_lookup_cache_invalidation_helper():
    deploy_lookup_cache.clear()
    deploy_lookup_cache.set((42, "prod", "commit_a"), 999)
    deploy_lookup_cache.set((42, "prod", "commit_b"), 1000)
    deploy_lookup_cache.set((43, "prod", "commit_c"), 1001)

    evicted = invalidate_deploy_cache(42)
    assert evicted == 2
    assert deploy_lookup_cache.get((42, "prod", "commit_a")) is None
    assert deploy_lookup_cache.get((43, "prod", "commit_c")) == 1001


def test_cache_concurrent_thread_safety():
    cache = LRUTTLCache[int, int](maxsize=50, default_ttl=10.0)

    def worker(worker_id: int):
        for i in range(100):
            cache.set(i % 30, worker_id * 1000 + i)
            _ = cache.get(i % 30)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, w) for w in range(8)]
        concurrent.futures.wait(futures)

    # Should have executed without exceptions and maintained maxsize bound
    assert len(cache) <= 50


@pytest.mark.asyncio
async def test_redis_sliding_window_counter():
    mock_redis = AsyncMock()
    mock_pipeline = MagicMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipeline)
    mock_pipeline.execute = AsyncMock(return_value=[5])

    # 1. Test counter increment
    result = await increment_sliding_window_counter(
        redis=mock_redis,
        project_id=1,
        deploy_id=10,
        issue_id=42,
        count=5,
        bucket_size_sec=10,
        expiry_sec=120,
    )
    assert result == 5
    mock_pipeline.incrby.assert_called_once()
    mock_pipeline.expire.assert_called_once()

    # 2. Test sliding window sum (MGET over 6 buckets)
    mock_redis.mget.return_value = ["2", "3", None, "5", "0", None]
    total = await get_sliding_window_sum(
        redis=mock_redis,
        project_id=1,
        deploy_id=10,
        issue_id=42,
        num_buckets=6,
        bucket_size_sec=10,
    )
    assert total == 10  # 2 + 3 + 5 + 0 = 10
    mock_redis.mget.assert_called_once()
    args, _ = mock_redis.mget.call_args
    assert len(args[0]) == 6


@pytest.mark.asyncio
async def test_worker_batch_populates_event_rollups_and_sliding_window():
    from app.worker import process_event_batch

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"fingerprint": "fp1", "id": 101}]
    mock_conn.fetchrow.return_value = {"id": 202}

    mock_redis = AsyncMock()
    mock_pipe = MagicMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)
    mock_pipe.execute = AsyncMock(return_value=[])

    events = [
        {
            "project_id": 1,
            "type": "error",
            "occurred_at": 1700000000.0,
            "message": "Crash 1",
            "release": "v1.0",
        },
        {
            "project_id": 1,
            "type": "error",
            "occurred_at": 1700000010.0,
            "message": "Crash 2",
            "release": "v1.0",
        },
    ]

    with patch("app.worker.project_transaction") as mock_tx, \
         patch("app.worker.get_redis", return_value=mock_redis):
        
        mock_tx.return_value.__aenter__.return_value = mock_conn

        await process_event_batch(events)

        # Verify event_rollups table insert was called
        execute_calls = mock_conn.execute.call_args_list
        rollup_call = None
        for call in execute_calls:
            sql = call[0][0]
            if "INSERT INTO event_rollups" in sql:
                rollup_call = call
                break

        assert rollup_call is not None, "event_rollups insert must be executed in batch"
        # Check args passed: project_id, issue_ids, deploy_ids, buckets, counts
        assert rollup_call[0][1] == 1  # project_id
        assert rollup_call[0][5] == [2]  # 2 events grouped into 1 rollup row

        # Verify Redis sliding-window pipeline was invoked
        mock_pipe.incrby.assert_called()
        mock_pipe.expire.assert_called()
        mock_pipe.execute.assert_called()
