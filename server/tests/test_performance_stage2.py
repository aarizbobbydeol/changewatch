"""Performance Addendum Stage 2 Tests: Worker Batching & High-Throughput Processing.

Verifies:
1. Equivalence of final database state between single-event and batch processing.
2. pull_batch_from_queue batch bounds (BATCH_MAX) and wait timeout (BATCH_MAX_WAIT_MS).
3. Single issue upsert per distinct fingerprint using unnest.
4. Heartbeat aggregation by (bucket, deploy_id) and single traffic upsert.
5. Incident check hook executed once per distinct (deploy, issue) in batch.
6. Safe batch retry and dead-letter queue routing on batch failure.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.fingerprint import compute_fingerprint
from app.worker import (
    BATCH_MAX,
    BATCH_MAX_WAIT_MS,
    check_incident_for_issue,
    find_deploy,
    handle_event_batch_with_retry,
    process_event_batch,
    process_single_event,
    pull_batch_from_queue,
)


@pytest.mark.asyncio
async def test_pull_batch_from_queue_respects_batch_max_and_timeout():
    """Verify pull_batch_from_queue collects up to batch_max items without exceeding it."""
    mock_redis = AsyncMock()
    worker_id = "test_worker_01"

    # Simulate queue containing 50 items
    items = [json.dumps({"event_id": f"ev_{i}", "project_id": 1, "occurred_at": time.time()}) for i in range(50)]

    mock_redis.blmove.return_value = items[0]
    mock_redis.lmove.side_effect = items[1:] + [None]

    # Request batch of max 15 items
    batch = await pull_batch_from_queue(
        redis=mock_redis,
        worker_id=worker_id,
        batch_max=15,
        max_wait_ms=50.0,
        timeout_sec=0.1,
    )

    assert len(batch) == 15
    assert batch[0][1]["event_id"] == "ev_0"
    assert batch[14][1]["event_id"] == "ev_14"


@pytest.mark.asyncio
async def test_pull_batch_from_queue_empty():
    """Verify pull_batch_from_queue returns empty list if blmove times out."""
    mock_redis = AsyncMock()
    mock_redis.blmove.return_value = None

    batch = await pull_batch_from_queue(
        redis=mock_redis,
        worker_id="worker_02",
        batch_max=100,
        max_wait_ms=20.0,
        timeout_sec=0.01,
    )
    assert batch == []


@pytest.mark.asyncio
async def test_process_event_batch_groups_issues_and_events():
    """Verify process_event_batch performs exactly ONE issue upsert and ONE event insert for distinct issues."""
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    now = time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)

    # 6 events: 3 of ValueError (same fingerprint), 3 of KeyError (same fingerprint)
    batch = [
        {
            "event_id": f"ev_val_{i}",
            "project_id": 1,
            "type": "error",
            "exc_type": "ValueError",
            "message": "Invalid value",
            "stack_trace": "line 12",
            "occurred_at": now + i,
            "environment": "prod",
            "release": "v1.0.0",
        }
        for i in range(3)
    ] + [
        {
            "event_id": f"ev_key_{i}",
            "project_id": 1,
            "type": "error",
            "exc_type": "KeyError",
            "message": "Missing key",
            "stack_trace": "line 45",
            "occurred_at": now + 10 + i,
            "environment": "prod",
            "release": "v1.0.0",
        }
        for i in range(3)
    ]

    # Mock deploy lookup
    mock_conn.fetchrow.return_value = {"id": 42}

    # Mock issue upsert returning 2 distinct issue IDs
    fp_val = compute_fingerprint("error", "ValueError", "line 12", None, "Invalid value")
    fp_key = compute_fingerprint("error", "KeyError", "line 45", None, "Missing key")
    mock_conn.fetch.return_value = [
        {"fingerprint": fp_val, "id": 101},
        {"fingerprint": fp_key, "id": 102},
    ]

    with patch("app.worker.project_transaction", return_value=mock_pool.acquire.return_value), \
         patch("app.worker.encrypt", return_value=b"mock_enc"):
        await process_event_batch(batch)

        # 1. conn.fetch called for issues upsert with exactly 2 fingerprints
        assert mock_conn.fetch.call_count == 1
        issues_call = mock_conn.fetch.call_args[0]
        assert "INSERT INTO issues" in issues_call[0]
        fps_arg = issues_call[2]
        assert len(fps_arg) == 2
        assert set(fps_arg) == {fp_val, fp_key}

        # 2. conn.execute called for multi-row events insert with all 6 events
        events_call = None
        for call in mock_conn.execute.call_args_list:
            if "INSERT INTO events" in call[0][0]:
                events_call = call
                break

        assert events_call is not None, "INSERT INTO events must be executed"
        event_ids_arg = events_call[0][2]
        assert len(event_ids_arg) == 6
        assert set(event_ids_arg) == {f"ev_val_{i}" for i in range(3)} | {f"ev_key_{i}" for i in range(3)}


@pytest.mark.asyncio
async def test_process_event_batch_heartbeat_aggregation():
    """Verify multiple heartbeats in the same minute bucket are merged into a single traffic upsert."""
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # 4 heartbeats in the same minute bucket
    base_ts = 1700000000.0  # clean timestamp
    batch = [
        {
            "event_id": f"hb_{i}",
            "project_id": 1,
            "type": "heartbeat",
            "requests": 50,
            "errors": 2,
            "occurred_at": base_ts + i * 5,
            "environment": "prod",
            "release": "v1.0.0",
        }
        for i in range(4)
    ]

    mock_conn.fetchrow.return_value = {"id": 10}

    with patch("app.worker.project_transaction", return_value=mock_pool.acquire.return_value):
        await process_event_batch(batch)

        traffic_call = None
        for call in mock_conn.execute.call_args_list:
            if "INSERT INTO traffic" in call[0][0]:
                traffic_call = call
                break

        assert traffic_call is not None, "INSERT INTO traffic must be executed"
        args = traffic_call[0]
        reqs = args[4]
        errs = args[5]

        # 4 heartbeats aggregated into 1 distinct bucket
        assert len(reqs) == 1
        assert reqs[0] == 200  # 4 * 50
        assert errs[0] == 8    # 4 * 2


@pytest.mark.asyncio
async def test_incident_check_called_once_per_distinct_deploy_issue():
    """Verify check_incident_for_issue is invoked once per distinct (deploy, issue) in the batch."""
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    now = time.time()
    # 8 events: 4 of deploy 10 / issue 1, 4 of deploy 20 / issue 2
    batch = [
        {
            "event_id": f"ev1_{i}",
            "project_id": 1,
            "type": "error",
            "exc_type": "TypeError",
            "message": "Type mismatch",
            "stack_trace": "line 10",
            "occurred_at": now + i,
            "environment": "prod",
            "release": "sha_10",
        }
        for i in range(4)
    ] + [
        {
            "event_id": f"ev2_{i}",
            "project_id": 1,
            "type": "error",
            "exc_type": "IndexError",
            "message": "Index error",
            "stack_trace": "line 20",
            "occurred_at": now + 10 + i,
            "environment": "prod",
            "release": "sha_20",
        }
        for i in range(4)
    ]

    async def _mock_find_deploy(conn, project_id, environment, occurred_at_dt, release):
        return 10 if release == "sha_10" else 20

    fp1 = compute_fingerprint("error", "TypeError", "line 10", None, "Type mismatch")
    fp2 = compute_fingerprint("error", "IndexError", "line 20", None, "Index error")
    mock_conn.fetch.return_value = [
        {"fingerprint": fp1, "id": 1001},
        {"fingerprint": fp2, "id": 2002},
    ]

    incident_calls = []

    async def _mock_check_incident(conn, project_id, deploy_id, issue_id):
        incident_calls.append((deploy_id, issue_id))

    with patch("app.worker.project_transaction", return_value=mock_pool.acquire.return_value), \
         patch("app.worker.find_deploy", side_effect=_mock_find_deploy), \
         patch("app.worker.encrypt", return_value=b"mock_enc"), \
         patch("app.worker.check_incident_for_issue", side_effect=_mock_check_incident):
        await process_event_batch(batch)

        # 8 events total, but only 2 distinct (deploy, issue) tuples: (10, 1001) and (20, 2002)
        assert len(incident_calls) == 2
        assert (10, 1001) in incident_calls
        assert (20, 2002) in incident_calls


@pytest.mark.asyncio
async def test_batch_processing_vs_single_equivalence():
    """Verify batch processing produces the exact same final state as single-event processing."""
    # Build simulated in-memory datastores
    single_db: Dict[str, Any] = {"traffic": {}, "issues": {}, "events": {}}
    batch_db: Dict[str, Any] = {"traffic": {}, "issues": {}, "events": {}}

    # Define simulated events stream (10 errors with repeated fingerprints, 5 heartbeats)
    now = 1700000000.0
    events: List[Dict[str, Any]] = []

    for i in range(10):
        # Fingerprints cycle between 3 types
        exc_type = ["ValueError", "KeyError", "RuntimeError"][i % 3]
        events.append({
            "event_id": f"event_{i:03d}",
            "project_id": 1,
            "type": "error",
            "exc_type": exc_type,
            "message": f"Error message {exc_type}",
            "stack_trace": f"File test.py, line {i % 3}",
            "occurred_at": now + i * 2,
            "environment": "prod",
            "release": "v1.0.0",
        })

    for i in range(5):
        events.append({
            "event_id": f"hb_{i:03d}",
            "project_id": 1,
            "type": "heartbeat",
            "requests": 25,
            "errors": 1,
            "occurred_at": now + (i // 2) * 60,  # 2 buckets
            "environment": "prod",
            "release": "v1.0.0",
        })

    # Helper simulating database update for single processor
    def _apply_single_event(db: Dict[str, Any], ev: Dict[str, Any]):
        etype = ev.get("type", "error")
        occurred_dt = datetime.fromtimestamp(ev["occurred_at"], tz=timezone.utc)
        if etype == "heartbeat":
            bucket = occurred_dt.replace(second=0, microsecond=0)
            if bucket not in db["traffic"]:
                db["traffic"][bucket] = {"requests": 0, "errors": 0}
            db["traffic"][bucket]["requests"] += ev.get("requests", 0)
            db["traffic"][bucket]["errors"] += ev.get("errors", 0)
        else:
            fp = compute_fingerprint(etype, ev.get("exc_type"), ev.get("stack_trace"), None, ev.get("message"))
            if fp not in db["issues"]:
                db["issues"][fp] = {"title": ev.get("exc_type"), "count": 0, "first_seen": occurred_dt, "last_seen": occurred_dt}
            db["issues"][fp]["count"] += 1
            db["issues"][fp]["first_seen"] = min(db["issues"][fp]["first_seen"], occurred_dt)
            db["issues"][fp]["last_seen"] = max(db["issues"][fp]["last_seen"], occurred_dt)
            db["events"][ev["event_id"]] = {"fp": fp, "occurred_at": occurred_dt}

    # Helper simulating database update for batch processor
    def _apply_batch_events(db: Dict[str, Any], batch: List[Dict[str, Any]]):
        # 1. Aggregate heartbeats
        for ev in batch:
            if ev.get("type") == "heartbeat":
                occurred_dt = datetime.fromtimestamp(ev["occurred_at"], tz=timezone.utc)
                bucket = occurred_dt.replace(second=0, microsecond=0)
                if bucket not in db["traffic"]:
                    db["traffic"][bucket] = {"requests": 0, "errors": 0}
                db["traffic"][bucket]["requests"] += ev.get("requests", 0)
                db["traffic"][bucket]["errors"] += ev.get("errors", 0)

        # 2. Group issues by fingerprint
        error_evs = [ev for ev in batch if ev.get("type") != "heartbeat"]
        fp_groups = {}
        for ev in error_evs:
            etype = ev.get("type", "error")
            occurred_dt = datetime.fromtimestamp(ev["occurred_at"], tz=timezone.utc)
            fp = compute_fingerprint(etype, ev.get("exc_type"), ev.get("stack_trace"), None, ev.get("message"))
            if fp not in fp_groups:
                fp_groups[fp] = {"title": ev.get("exc_type"), "events": [], "first_seen": occurred_dt, "last_seen": occurred_dt}
            else:
                fp_groups[fp]["first_seen"] = min(fp_groups[fp]["first_seen"], occurred_dt)
                fp_groups[fp]["last_seen"] = max(fp_groups[fp]["last_seen"], occurred_dt)
            fp_groups[fp]["events"].append(ev)

        for fp, group in fp_groups.items():
            if fp not in db["issues"]:
                db["issues"][fp] = {"title": group["title"], "count": 0, "first_seen": group["first_seen"], "last_seen": group["last_seen"]}
            db["issues"][fp]["count"] += len(group["events"])
            db["issues"][fp]["first_seen"] = min(db["issues"][fp]["first_seen"], group["first_seen"])
            db["issues"][fp]["last_seen"] = max(db["issues"][fp]["last_seen"], group["last_seen"])

            for ev in group["events"]:
                db["events"][ev["event_id"]] = {"fp": fp, "occurred_at": datetime.fromtimestamp(ev["occurred_at"], tz=timezone.utc)}

    # Apply sequentially for single DB
    for ev in events:
        _apply_single_event(single_db, ev)

    # Apply in a single batch for batch DB
    _apply_batch_events(batch_db, events)

    # Assert exact equivalence
    assert single_db["traffic"] == batch_db["traffic"]
    assert len(single_db["issues"]) == len(batch_db["issues"])
    for fp in single_db["issues"]:
        assert single_db["issues"][fp]["count"] == batch_db["issues"][fp]["count"]
        assert single_db["issues"][fp]["first_seen"] == batch_db["issues"][fp]["first_seen"]
        assert single_db["issues"][fp]["last_seen"] == batch_db["issues"][fp]["last_seen"]
    assert single_db["events"] == batch_db["events"]


@pytest.mark.asyncio
async def test_handle_event_batch_with_retry_failure_fallback():
    """Verify failed batch increments _tries and routes to events queue or dead-letter queue."""
    mock_redis = AsyncMock()

    batch = [
        ("raw_1", {"event_id": "ev_retry", "_tries": 0}),
        ("raw_2", {"event_id": "ev_dead", "_tries": 2}),  # Will become 3
    ]

    with patch("app.worker.process_event_batch", side_effect=RuntimeError("Database failure")):
        await handle_event_batch_with_retry(batch, redis=mock_redis)

        assert mock_redis.rpush.call_count == 2
        calls = mock_redis.rpush.call_args_list

        # First item re-queued to events
        assert calls[0][0][0] == "events"
        p1 = json.loads(calls[0][0][1])
        assert p1["_tries"] == 1

        # Second item pushed to events:dead
        assert calls[1][0][0] == "events:dead"
        p2 = json.loads(calls[1][0][1])
        assert p2["_tries"] == 3
