"""App log lines sent with an error (live Postgres): validated, scrubbed, stored encrypted only,
and shown on the incident, to the investigator and in the export."""
import time
import uuid

import asyncpg
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.investigator.tools import get_logs_before_error
from app.schemas import EventItem
from app.worker import process_event_batch


def test_api_limits_log_lines():
    now = time.time()
    line = {"ts": now, "level": "ERROR", "logger": "shop", "message": "x"}
    EventItem(type="error", occurred_at=now, logs=[line] * 50)
    for bad in ([line] * 51, [{**line, "message": "x" * 501}], [{**line, "ts": float("nan")}]):
        with pytest.raises(ValidationError):
            EventItem(type="error", occurred_at=now, logs=bad)


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def project():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"logs-{uuid.uuid4().hex[:8]}")
    yield pid
    async with get_connection() as conn:
        for table in ("incidents", "event_rollups", "events", "issues", "deploys", "sources"):
            await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", pid)
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_logs_are_scrubbed_encrypted_and_read_back(project):
    run = uuid.uuid4().hex[:8]
    now = time.time()
    event = {
        "event_id": f"{run}-1", "project_id": project, "type": "error", "exc_type": "TimeoutError",
        "message": "payment provider timed out", "stack_trace": f'File "app/pay_{run}.py", line 3, in charge',
        "occurred_at": now - 5,
        "logs": [
            {"ts": now - 9, "level": "WARNING", "logger": "shop.pay", "message": "retry 1 for jane.doe@example.com"},
            {"ts": now - 6, "level": "ERROR", "logger": "shop.pay", "message": "giving up, api_key=sk_live_51Habcdefghijk"},
        ],
    }
    await process_event_batch([event])
    async with project_transaction(project) as conn:
        row = await conn.fetchrow("SELECT issue_id, logs_enc FROM events WHERE event_id = $1", f"{run}-1")
        logs = await get_logs_before_error(conn, project, row["issue_id"])
    assert row["logs_enc"] is not None
    raw = bytes(row["logs_enc"])
    assert b"shop.pay" not in raw and b"retry" not in raw  # stored encrypted only
    assert [line["level"] for line in logs] == ["WARNING", "ERROR"]
    text = " ".join(line["message"] for line in logs)
    assert "jane.doe@example.com" not in text and "sk_live_51Habcdefghijk" not in text
    assert "retry 1 for" in text and "giving up" in text


@pytest.mark.asyncio
async def test_live_logs_are_ignored_on_slow_requests(project):
    run = uuid.uuid4().hex[:8]
    now = time.time()
    await process_event_batch([{
        "event_id": f"{run}-s", "project_id": project, "type": "slow_request", "request_path": f"/slow/{run}",
        "duration_ms": 2500.0, "occurred_at": now - 2,
        "logs": [{"ts": now - 3, "level": "WARNING", "logger": "x", "message": "should not be stored"}],
    }])
    async with project_transaction(project) as conn:
        stored = await conn.fetchval("SELECT logs_enc FROM events WHERE event_id = $1", f"{run}-s")
    assert stored is None
