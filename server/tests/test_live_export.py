"""GET /v1/export against the real schema (the mocked test cannot catch a wrong column)."""
import time
import uuid

import asyncpg
import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient

from app.auth import require_read_project_id
from app.config import settings
from app.db import close_db_pool, init_db_pool
from app.main import app
from app.queue import close_redis, init_redis
from app.worker import process_event_batch


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
async def test_live_export_returns_decrypted_events_and_issue_counts():
    if not await _services_up():
        pytest.skip("Live PostgreSQL or Redis not running. Skipping live integration test.")

    await init_db_pool()
    await init_redis()
    app.dependency_overrides[require_read_project_id] = lambda: 1
    try:
        run = uuid.uuid4().hex[:10]
        trace = f'File "app/export_{run}.py", line 9, in g\n  raise KeyError'
        await process_event_batch([
            {
                "event_id": f"{run}-x",
                "project_id": 1,
                "type": "error",
                "exc_type": "KeyError",
                "message": f"export {run}",
                "stack_trace": trace,
                "request_path": "/export-check",
                "extra": {"plan": "pro"},
                "occurred_at": time.time() - 7200,
            }
        ])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/v1/export")
        assert resp.status_code == 200
        data = resp.json()

        event = next(e for e in data["events"] if e["event_id"] == f"{run}-x")
        assert event["stack_trace"] == trace  # stored encrypted, exported decrypted
        assert event["extra"] == {"plan": "pro"}
        assert event["request_path"] == "/export-check"
        issue = next(i for i in data["issues"] if i["id"] == event["issue_id"])
        assert issue["title"] == f"KeyError: export {run}" and issue["event_count"] == 1
        assert {"deploys", "traffic", "project"} <= data.keys()
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
        await close_db_pool()
        await close_redis()


@pytest.mark.asyncio
@pytest.mark.parametrize("metric,header", [
    ("overview", "Metric,Value,Unit,N,Sample OK,Note"),
    ("verdicts", "Week,Safe,Watch,Rollback,No data,Total"),
    ("ttf", "Week,Median minutes,N,Sample OK"),
    ("hotspots", "Area,Incident Count,Share Percent"),
    ("categories", "Category,Count,Share Percent"),
])
async def test_live_analytics_csv_export_every_metric(metric, header):
    """Each dashboard CSV button works against the real schema (overview used to fail with 500)."""
    if not await _services_up():
        pytest.skip("Live PostgreSQL or Redis not running. Skipping live integration test.")

    await init_db_pool()
    app.dependency_overrides[require_read_project_id] = lambda: 1
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/v1/analytics/export.csv?metric={metric}&period=30&env=prod")
            unknown = await client.get("/v1/analytics/export.csv?metric=nope")
        assert resp.status_code == 200, resp.text
        assert resp.text.splitlines()[0] == header
        assert unknown.status_code == 422
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
        await close_db_pool()


@pytest.mark.asyncio
async def test_live_incident_timeline_unknown_id_is_404():
    if not await _services_up():
        pytest.skip("Live PostgreSQL or Redis not running. Skipping live integration test.")

    await init_db_pool()
    app.dependency_overrides[require_read_project_id] = lambda: 1
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/incidents/2000000000/timeline")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
        await close_db_pool()
