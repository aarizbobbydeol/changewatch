"""The AI investigation job is queued only once its incident is saved (live Postgres).

The investigator picks a job up within milliseconds. If the job is queued inside the worker's
transaction, the investigator cannot see the incident yet, fails three quick retries and drops it,
so the team never gets the AI reply.
"""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.worker import process_event_batch


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
async def test_live_investigation_job_is_queued_after_the_incident_is_saved(live_db):
    run = uuid.uuid4().hex[:10]
    sha = f"inv{run}"
    async with project_transaction(1) as conn:
        await conn.execute(
            "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, 'prod', $2)",
            sha, datetime.now(timezone.utc) - timedelta(minutes=1),
        )

    seen_when_queued = []

    async def lpush(queue, raw):
        if queue != "investigations":
            return 1
        incident_id = json.loads(raw)["incident_id"]
        # What the investigator would see at this moment, from its own connection
        other = await asyncpg.connect(settings.DATABASE_URL)
        try:
            seen_when_queued.append(await other.fetchval("SELECT count(*) FROM incidents WHERE id = $1", incident_id))
        finally:
            await other.close()
        return 1

    redis = MagicMock()
    redis.lpush = AsyncMock(side_effect=lpush)
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    redis.pipeline = MagicMock(return_value=pipe)

    now = time.time()
    events = [{"event_id": f"{run}-{i}", "project_id": 1, "type": "error", "exc_type": "KeyError",
               "message": f"inv {run}", "stack_trace": f'File "app/inv_{run}.py", line 9, in f',
               "release": sha, "environment": "prod", "occurred_at": now - 5 + i} for i in range(4)]
    with patch("app.worker.get_redis", return_value=redis), \
         patch("app.queue.get_redis", return_value=redis), \
         patch("app.worker.record_stored_event_metric", new=AsyncMock()):
        await process_event_batch(events)

    assert seen_when_queued == [1]  # queued exactly once, and the incident was already visible

    async with project_transaction(1) as conn:
        await conn.execute(
            "DELETE FROM incidents WHERE deploy_id IN (SELECT id FROM deploys WHERE commit_sha = $1)", sha
        )
