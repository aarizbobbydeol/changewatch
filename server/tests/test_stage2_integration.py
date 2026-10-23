import json
import time
import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis
from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.main import app
from app.queue import close_redis, init_redis
from app.worker import process_single_event


async def is_services_up() -> bool:
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
async def test_live_stage2_pipeline():
    """Live integration test: deploy hook -> event ingest -> worker processing -> DB link."""
    if not await is_services_up():
        pytest.skip("Live PostgreSQL or Redis not running. Skipping live integration test.")

    await init_db_pool()
    await init_redis()

    try:
        # 1. Setup project
        async with project_transaction(1) as conn:
            proj = await conn.fetchrow(
                "SELECT id, api_key FROM projects WHERE api_key = 'cw_test_key_123'"
            )
            assert proj is not None, "Seed project not found in live DB"
            api_key = proj["api_key"]
            proj_id = proj["id"]

        # 2. Call deploy hook via HTTP
        commit_sha = f"live_sha_{int(time.time())}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            dep_resp = await client.post(
                "/v1/deploys",
                headers={"X-API-Key": api_key},
                json={
                    "commit_sha": commit_sha,
                    "author": "integration_tester",
                    "message": "Integration test deploy",
                    "diff": "diff --git a/app.py b/app.py",
                    "environment": "prod",
                },
            )
            assert dep_resp.status_code == 200
            deploy_id = dep_resp.json()["deploy_id"]

            # 3. Call events ingest via HTTP
            now_ts = time.time()
            ingest_resp = await client.post(
                "/v1/events",
                headers={"X-API-Key": api_key},
                json={
                    "events": [
                        {
                            "type": "error",
                            "exc_type": "ZeroDivisionError",
                            "message": "division by zero in calculate_metrics",
                            "stack_trace": "File 'calc.py', line 12 in div\n  x / 0",
                            "release": commit_sha,
                            "environment": "prod",
                            "occurred_at": now_ts,
                            "extra": {"user_id": 999},
                        },
                        {
                            "type": "heartbeat",
                            "requests": 50,
                            "errors": 1,
                            "release": commit_sha,
                            "environment": "prod",
                            "occurred_at": now_ts,
                        },
                    ]
                },
            )
            assert ingest_resp.status_code == 200
            assert ingest_resp.json()["accepted"] == 2

        # 4. Worker consumption from Redis
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        raw_error = await r.lpop("events")
        assert raw_error is not None
        await process_single_event(json.loads(raw_error))

        raw_heartbeat = await r.lpop("events")
        assert raw_heartbeat is not None
        await process_single_event(json.loads(raw_heartbeat))
        await r.aclose()

        # 5. Verify database rows and deploy link
        async with project_transaction(proj_id) as conn:
            event_row = await conn.fetchrow(
                "SELECT id, issue_id, deploy_id FROM events WHERE deploy_id = $1", deploy_id
            )
            assert event_row is not None
            assert event_row["deploy_id"] == deploy_id

            issue_row = await conn.fetchrow(
                "SELECT id, fingerprint, title FROM issues WHERE id = $1", event_row["issue_id"]
            )
            assert issue_row is not None
            assert "ZeroDivisionError" in issue_row["title"]

            traffic_row = await conn.fetchrow(
                "SELECT requests, errors FROM traffic WHERE deploy_id = $1", deploy_id
            )
            assert traffic_row is not None
            assert traffic_row["requests"] == 50
            assert traffic_row["errors"] == 1

    finally:
        await close_db_pool()
        await close_redis()
