"""The operations panel reports real numbers, not placeholders (live Postgres and Redis)."""
import time
import uuid
from datetime import date
from unittest.mock import AsyncMock

import asyncpg
import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient

from app.auth import require_read_project_id
from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.ingest_stats import count_dropped, dropped_totals
from app.main import app
from app.queue import close_redis, get_redis, init_redis
from app.worker import process_event_batch


@pytest.mark.asyncio
async def test_dropped_counters_sum_per_reason_and_never_raise():
    redis = AsyncMock()
    redis.mget.return_value = [b"3", None, b"2", b"5", None, None]  # rate_limit x3 days, backpressure x3 days
    totals = await dropped_totals(redis, 7, date(2026, 10, 1), date(2026, 10, 3))
    assert totals == {"rate_limit": 5, "backpressure": 5}
    keys = redis.mget.call_args.args[0]
    assert keys[0] == "ingest:dropped:7:2026-10-01:rate_limit" and keys[-1] == "ingest:dropped:7:2026-10-03:backpressure"

    broken = AsyncMock()
    broken.incrby.side_effect = ConnectionError("down")
    await count_dropped(broken, 7, "backpressure", 4)  # does not raise
    await count_dropped(None, 7, "backpressure", 4)


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


@pytest.mark.asyncio
async def test_live_operations_panel_counts_real_data():
    if not await _services_up():
        pytest.skip("Live PostgreSQL or Redis not running. Skipping live integration test.")

    await init_db_pool()
    await init_redis()
    app.dependency_overrides[require_read_project_id] = lambda: 1
    try:
        run = uuid.uuid4().hex[:10]
        await process_event_batch([
            {"event_id": f"{run}-{i}", "project_id": 1, "type": "error", "exc_type": "OSError",
             "message": f"ops {run}", "stack_trace": f'File "app/ops_{run}.py", line 1', "occurred_at": time.time() - 60}
            for i in range(3)
        ])
        async with project_transaction(1) as conn:
            await conn.execute(
                "INSERT INTO outbox (project_id, dedupe_key, payload, status) VALUES (1, $1, '{}', 'sent'), (1, $2, '{}', 'failed')",
                f"ops-sent-{run}", f"ops-failed-{run}",
            )
        await count_dropped(get_redis(), 1, "backpressure", 7)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ops = (await client.get("/v1/analytics/operations?period=1")).json()

        async with project_transaction(1) as conn:
            await conn.execute("DELETE FROM outbox WHERE dedupe_key LIKE $1", f"ops-%-{run}")

        assert ops["ingest_stats"]["received"] >= 3 and ops["ingest_stats"]["stored"] >= 3
        assert ops["ingest_stats"]["dropped_backpressure"] >= 7
        assert ops["notifications"]["delivered"] >= 1 and ops["notifications"]["failed"] >= 1
        assert "%" in ops["notifications"]["formatted"]
        assert ops["sources"]["formatted"] in ("Data flowing", "No data received yet") or "silent" in ops["sources"]["formatted"]
        assert "Not enabled" not in str(ops)  # the AI investigator and calibration exist
        assert ops["calibration"]["formatted"]
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
        await close_db_pool()
        await close_redis()
