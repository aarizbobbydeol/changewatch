"""One bug is one issue, whatever its message says; a big fleet does not lose its errors to the
heartbeat limit; an upgrade keeps existing issues (live Postgres for the last two)."""
import time
import uuid
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import get_api_key, require_ingest_project_id
from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.fingerprint import compute_fingerprint, legacy_fingerprint, normalize_path

TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/orders.py", line 31, in checkout\n'
    "    validate(order)\n"
    '  File "/srv/app/orders.py", line 12, in validate\n'
    '    raise ValueError(f"order {order.id} has no items")\n'
    "ValueError: order <N> has no items\n"
)


def trace(n) -> str:
    return TRACE.replace("<N>", str(n))


def test_messages_with_ids_are_one_issue():
    a = compute_fingerprint("error", "ValueError", trace(1234), message="order 1234 has no items")
    b = compute_fingerprint("error", "ValueError", trace(98765), message="order 98765 has no items")
    assert a == b
    other_type = compute_fingerprint("error", "KeyError", trace(1234))
    other_place = compute_fingerprint("error", "ValueError", trace(1).replace("validate(order)", "check(order)"))
    assert len({a, other_type, other_place}) == 3


def test_chained_exceptions_group_by_their_frames():
    chained = (trace(1) + "\nDuring handling of the above exception, another exception occurred:\n\n"
               + trace(2).replace("ValueError: order 2", "RuntimeError: retry 7 for order 2"))
    again = (trace(3) + "\nDuring handling of the above exception, another exception occurred:\n\n"
             + trace(4).replace("ValueError: order 4", "RuntimeError: retry 9 for order 4"))
    assert compute_fingerprint("error", "RuntimeError", chained) == compute_fingerprint("error", "RuntimeError", again)


def test_messages_without_a_trace_group_without_their_numbers():
    a = compute_fingerprint("error", "TimeoutError", None, message="request 81 timed out after 30s (id 7f3a9c2e1b4d)")
    b = compute_fingerprint("error", "TimeoutError", None, message="request 92 timed out after 31s (id 0a1b2c3d4e5f)")
    assert a == b


def test_slow_paths_group_by_route():
    assert normalize_path("/users/42/orders/550e8400-e29b-41d4-a716-446655440000?x=1") == "/users/:id/orders/:id"
    assert compute_fingerprint("slow_request", request_path="/orders/1") == compute_fingerprint(
        "slow_request", request_path="/orders/2")
    assert compute_fingerprint("slow_request", request_path="/orders/1") != compute_fingerprint(
        "slow_request", request_path="/users/1")


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def live_db():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    yield
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_upgrade_keeps_the_existing_issue(live_db):
    from app.worker import process_event_batch

    run = uuid.uuid4().hex[:8]
    stack = trace(5).replace("orders.py", f"orders_{run}.py")
    old_fp = legacy_fingerprint("error", "ValueError", stack, message="order 5 has no items")
    async with project_transaction(1) as conn:
        old_id = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES (1, $1, 'ValueError: order 5 has no items', now() - interval '3 days', now() - interval '1 day', "
            "'open', 40) RETURNING id", old_fp)
    event = {"event_id": f"{run}-1", "project_id": 1, "type": "error", "exc_type": "ValueError",
             "message": "order 5 has no items", "stack_trace": stack, "occurred_at": time.time() - 30}
    with patch("app.worker.check_incident_for_issue", new=AsyncMock()):
        await process_event_batch([event])
    async with project_transaction(1) as conn:
        rows = await conn.fetch("SELECT id, fingerprint, event_count FROM issues WHERE title LIKE 'ValueError: order%' "
                                "AND id IN (SELECT issue_id FROM events WHERE event_id = $1)", f"{run}-1")
        await conn.execute("DELETE FROM issues WHERE id = $1", old_id)
    assert [(r["id"], r["event_count"]) for r in rows] == [(old_id, 41)]  # same issue, history kept
    assert rows[0]["fingerprint"] == compute_fingerprint("error", "ValueError", stack)


@pytest.mark.asyncio
async def test_live_heartbeats_over_the_limit_do_not_hold_back_errors(live_db, monkeypatch):
    from app.main import app
    from app.queue import close_redis, init_redis

    await init_redis()
    monkeypatch.setattr(settings, "HEARTBEAT_LIMIT_PER_MIN", 2)
    key = f"cw_ingest_fleet_{uuid.uuid4().hex}"
    app.dependency_overrides[require_ingest_project_id] = lambda: 1
    app.dependency_overrides[get_api_key] = lambda: key
    now = time.time()
    beat = {"type": "heartbeat", "requests": 50, "errors": 1, "occurred_at": now}
    error = {"type": "error", "exc_type": "KeyError", "message": "x", "occurred_at": now}
    run = uuid.uuid4().hex[:8]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post("/v1/events", json={"events": [dict(beat, event_id=f"{run}-b1"),
                                                                     dict(beat, event_id=f"{run}-b2")]})
            over = await client.post("/v1/events", json={"events": [dict(beat, event_id=f"{run}-b3"),
                                                                    dict(error, event_id=f"{run}-e1")]})
    finally:
        app.dependency_overrides.pop(require_ingest_project_id, None)
        app.dependency_overrides.pop(get_api_key, None)
        # Take this test's events back off the shared queue (other tests read it)
        from app.queue import get_redis

        redis = get_redis()
        for raw in await redis.lrange("events", 0, -1):
            if run in (raw.decode() if isinstance(raw, bytes) else raw):
                await redis.lrem("events", 0, raw)
        await close_redis()
    assert first.status_code == 200
    assert over.status_code == 200  # was 429 for the whole batch, error included
    assert over.json() == {"accepted": 1, "dropped": 1}


def test_frames_sent_without_indentation_still_tell_errors_apart():
    a = compute_fingerprint("error", "KeyError", 'File "app/a.py", line 9, in g\n  raise KeyError\nKeyError: 1')
    b = compute_fingerprint("error", "KeyError", 'File "app/b.py", line 9, in g\n  raise KeyError\nKeyError: 2')
    assert a != b
    assert a == compute_fingerprint("error", "KeyError", 'File "app/a.py", line 12, in g\n  raise KeyError\nKeyError: 3')
