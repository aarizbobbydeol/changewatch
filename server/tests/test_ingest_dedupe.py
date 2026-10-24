"""A re-sent event (SDK retry, worker re-queue) must be counted exactly once."""
import copy
import time
import uuid
from unittest.mock import AsyncMock

import asyncpg
import pytest
import redis.asyncio as aioredis

from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.queue import close_redis, init_redis
from app.worker import claim_new_events, process_event_batch


@pytest.mark.asyncio
async def test_claim_keeps_only_unclaimed_events_and_in_batch_first_copy():
    conn = AsyncMock()
    conn.fetch.return_value = [{"event_id": "a"}, {"event_id": "c"}]  # "b" was claimed earlier
    events = [{"event_id": "a"}, {"event_id": "b"}, {"event_id": "a"}, {"event_id": "c"}, {"type": "error"}]

    fresh = await claim_new_events(conn, 7, events)

    assert fresh == [{"event_id": "a"}, {"event_id": "c"}, {"type": "error"}]
    query, project_id, ids = conn.fetch.call_args.args
    assert "INSERT INTO ingest_dedupe" in query and "ON CONFLICT DO NOTHING" in query
    assert project_id == 7
    assert ids == ["a", "b", "c"]  # sorted and unique, so concurrent workers lock in the same order


@pytest.mark.asyncio
async def test_claim_without_event_ids_skips_the_query():
    conn = AsyncMock()
    events = [{"type": "error"}]
    assert await claim_new_events(conn, 7, events) == events
    conn.fetch.assert_not_called()


async def _services_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


async def _snapshot(run: str) -> dict:
    async with project_transaction(1) as conn:
        issue = await conn.fetchrow(
            "SELECT id, event_count FROM issues WHERE title = $1", f"LookupError: dedupe {run}"
        )
        return {
            "event_count": issue["event_count"],
            "rows": await conn.fetchval("SELECT count(*) FROM events WHERE event_id LIKE $1", f"{run}-%"),
            "rollups": await conn.fetchval(
                "SELECT coalesce(sum(count), 0) FROM event_rollups WHERE issue_id = $1", issue["id"]
            ),
            "traffic": await conn.fetchval("SELECT coalesce(sum(requests), 0) FROM traffic"),
        }


@pytest.mark.asyncio
async def test_live_resent_batch_and_rolled_back_claims():
    if not await _services_up():
        pytest.skip("Live PostgreSQL or Redis not running. Skipping live integration test.")

    await init_db_pool()
    await init_redis()
    try:
        run = uuid.uuid4().hex[:10]
        # Two hours back: traffic is one row per project per minute, so stay out of other live tests' minute
        now = time.time() - 7200
        batch = [
            {
                "event_id": f"{run}-e{i}",
                "project_id": 1,
                "type": "error",
                "exc_type": "LookupError",
                "message": f"dedupe {run}",
                "stack_trace": f'File "app/dedupe_{run}.py", line 3, in f\n  raise LookupError',
                "occurred_at": now,
            }
            for i in range(3)
        ]
        batch.append({"event_id": f"{run}-hb", "project_id": 1, "type": "heartbeat", "requests": 7, "errors": 3, "occurred_at": now})

        await process_event_batch(copy.deepcopy(batch))
        first = await _snapshot(run)
        assert first["event_count"] == 3 and first["rows"] == 3 and first["rollups"] == 3

        await process_event_batch(copy.deepcopy(batch))  # the SDK retried the same batch
        assert await _snapshot(run) == first

        # A batch whose transaction rolls back releases its claims, so the retry still counts
        retry_id = f"{run}-rollback"
        with pytest.raises(RuntimeError):
            async with project_transaction(1) as conn:
                assert await claim_new_events(conn, 1, [{"event_id": retry_id}])
                raise RuntimeError("batch failed after claiming")
        async with project_transaction(1) as conn:
            assert await claim_new_events(conn, 1, [{"event_id": retry_id}]) == [{"event_id": retry_id}]
    finally:
        await close_db_pool()
        await close_redis()
