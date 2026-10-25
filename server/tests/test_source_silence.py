"""Only traffic ends a silence (live Postgres): an error from a background job while the web app
sends no heartbeats must not tell the team that data is flowing again."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.sources import record_source_activity


@pytest.mark.asyncio
async def test_live_error_event_does_not_end_a_silence():
    try:
        c = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await c.close()
    except Exception:
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    env = f"silence-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    try:
        with patch("app.outbox.queue_notification", new=AsyncMock(return_value=True)) as queued:
            async with project_transaction(1) as conn:
                await record_source_activity(conn, 1, env, is_heartbeat=True, occurred_at=now - timedelta(minutes=20))
                await conn.execute(
                    "UPDATE sources SET status = 'silent', silent_since = $2 WHERE environment = $1",
                    env, now - timedelta(minutes=8))

                await record_source_activity(conn, 1, env, is_heartbeat=False, occurred_at=now)
                after_error = await conn.fetchrow(
                    "SELECT status, silent_since, last_event_at FROM sources WHERE environment = $1", env)
                notified_after_error = queued.await_count

                await record_source_activity(conn, 1, env, is_heartbeat=True, occurred_at=now + timedelta(seconds=5))
                after_heartbeat = await conn.fetchval("SELECT status FROM sources WHERE environment = $1", env)
                await conn.execute("DELETE FROM sources WHERE environment = $1", env)
    finally:
        await close_db_pool()

    assert after_error["status"] == "silent" and after_error["silent_since"] is not None
    assert after_error["last_event_at"] == now  # the error is still recorded
    assert notified_after_error == 0
    assert after_heartbeat == "active"
    assert queued.await_count == 1 and queued.await_args.kwargs["payload"]["type"] == "source_recovered"
