"""One bad event, an outage or a second environment must not cost other events (live Postgres)."""
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.detector import evaluate_deploy
from app.schemas import EventItem
from app.worker import (
    DEAD_LETTER_QUEUE,
    EVENTS_QUEUE,
    TransientBatchError,
    find_deploy,
    handle_event_batch_with_retry,
    process_event_batch,
)


def test_api_rejects_values_the_database_cannot_store():
    now = time.time()
    for bad in ({"occurred_at": float("nan")}, {"occurred_at": float("inf")},
                {"occurred_at": now, "duration_ms": float("nan")},
                {"occurred_at": now, "type": "heartbeat", "requests": 2**31}):
        with pytest.raises(ValidationError):
            EventItem(**{"type": "error", **bad})
    EventItem(type="heartbeat", occurred_at=now, requests=2**31 - 1)


@pytest.mark.asyncio
async def test_transient_database_error_is_not_counted_against_the_events():
    redis = AsyncMock()
    batch = [("raw", {"project_id": 1, "event_id": "e1", "_tries": 0})]
    with patch("app.worker.process_event_batch", side_effect=asyncpg.CannotConnectNowError("starting up")):
        with pytest.raises(TransientBatchError):
            await handle_event_batch_with_retry(batch, redis=redis)
    redis.rpush.assert_not_called()  # the caller re-queues the raw item unchanged and backs off
    assert batch[0][1]["_tries"] == 0


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


def _error(run: str, i: int, **extra) -> dict:
    return {"event_id": f"{run}-{i}", "project_id": 1, "type": "error", "exc_type": "LookupError",
            "message": f"robust {run}", "stack_trace": f'File "app/robust_{run}.py", line 3, in f',
            "occurred_at": time.time() - 3600, **extra}


@pytest.mark.asyncio
async def test_live_one_bad_event_does_not_take_the_batch_with_it(live_db):
    run = uuid.uuid4().hex[:10]
    redis = AsyncMock()
    good = _error(run, 1)
    bad = _error(run, 2, occurred_at="not-a-timestamp")  # fails inside the worker, not at the API
    await handle_event_batch_with_retry([("g", good), ("b", bad)], redis=redis)

    async with project_transaction(1) as conn:
        stored = await conn.fetchval("SELECT count(*) FROM events WHERE event_id = $1", good["event_id"])
    assert stored == 1
    pushed = [c.args for c in redis.rpush.call_args_list]
    assert len(pushed) == 1 and pushed[0][0] == EVENTS_QUEUE and f'"{bad["event_id"]}"' in pushed[0][1]
    assert '"_tries": 1' in pushed[0][1]

    # Third failure: only the bad event is dead-lettered
    bad["_tries"] = 2
    redis.reset_mock()
    await handle_event_batch_with_retry([("b", bad)], redis=redis)
    assert redis.rpush.call_args.args[0] == DEAD_LETTER_QUEUE


@pytest.mark.asyncio
async def test_live_nul_characters_are_stored_without_them(live_db):
    run = uuid.uuid4().hex[:10]
    ev = _error(run, 1, request_path="/files/\x00", message=f"robust {run} \x00null", extra={"k\x00": "v\x00"})
    await process_event_batch([ev])
    async with project_transaction(1) as conn:
        path = await conn.fetchval("SELECT request_path FROM events WHERE event_id = $1", ev["event_id"])
    assert path == "/files/"


@pytest.mark.asyncio
async def test_live_heartbeats_from_two_environments_in_one_minute(live_db):
    run = uuid.uuid4().hex[:10]
    minute = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(second=5, microsecond=0)
    envs = [f"prod-{run}", f"staging-{run}"]
    batch = [{"event_id": f"{run}-hb-{env}-{i}", "project_id": 1, "type": "heartbeat", "requests": 10, "errors": 1,
              "environment": env, "occurred_at": minute.timestamp() + i} for env in envs for i in range(2)]
    await process_event_batch(batch)
    async with project_transaction(1) as conn:
        rows = await conn.fetch(
            "SELECT environment, requests, errors FROM traffic WHERE environment = ANY($1) ORDER BY environment", envs
        )
        await conn.execute("DELETE FROM traffic WHERE environment = ANY($1)", envs)
    assert [(r["environment"], r["requests"], r["errors"]) for r in rows] == [(envs[0], 20, 2), (envs[1], 20, 2)]


@pytest.mark.asyncio
async def test_live_redeployed_sha_matches_its_latest_deploy(live_db):
    run = uuid.uuid4().hex[:10]
    env = f"redeploy-{run}"
    now = datetime.now(timezone.utc)
    async with project_transaction(1) as conn:
        ids = []
        for sha, minutes_ago in ((f"x{run}", 90), (f"y{run}", 60), (f"x{run}", 30)):
            ids.append(await conn.fetchval(
                "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, $2, $3) RETURNING id",
                sha, env, now - timedelta(minutes=minutes_ago),
            ))
        found = await find_deploy(conn, 1, env, now - timedelta(minutes=10), release=f"x{run}")
        between = await find_deploy(conn, 1, env, now - timedelta(minutes=45), release=f"x{run}")
        await conn.execute("DELETE FROM deploys WHERE id = ANY($1::int[])", ids)
    assert found == ids[2]  # the rollback deploy, not the first one
    assert between == ids[0]


@pytest.mark.asyncio
async def test_live_prod_verdict_ignores_staging_and_counts_encrypted_http_errors(live_db):
    run = uuid.uuid4().hex[:10]
    prod, staging = f"prod-{run}", f"staging-{run}"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t0 = now - timedelta(minutes=20)
    async with project_transaction(1) as conn:
        prod_deploy = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, $2, $3) RETURNING id",
            f"p{run}", prod, t0)
        await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, $2, $3) RETURNING id",
            f"s{run}", staging, t0 + timedelta(minutes=5))

    # Prod: steady traffic. Staging: a burst of new errors after its own deploy.
    hb = [{"event_id": f"{run}-hb-{env}-{m}", "project_id": 1, "type": "heartbeat", "requests": 100, "errors": 0,
           "environment": env, "occurred_at": (t0 + timedelta(minutes=m)).timestamp()}
          for env in (prod, staging) for m in range(-10, 15)]
    errs = [{"event_id": f"{run}-err-{i}", "project_id": 1, "type": "error", "exc_type": "ZeroDivisionError",
             "message": f"staging only {run}", "stack_trace": f'File "app/st_{run}.py", line 1, in f',
             "request_path": "/checkout", "environment": staging,
             "occurred_at": (t0 + timedelta(minutes=6, seconds=i)).timestamp()} for i in range(30)]
    await process_event_batch(hb)
    await process_event_batch(errs)

    async with project_transaction(1) as conn:
        verdict, reasons, _, _ = await evaluate_deploy(
            conn, 1, {"id": prod_deploy, "deployed_at": t0, "environment": prod, "next_deployed_at": None}, now)
        staging_deploy = await conn.fetchval("SELECT id FROM deploys WHERE commit_sha = $1", f"s{run}")
        s_verdict, s_reasons, _, _ = await evaluate_deploy(
            conn, 1, {"id": staging_deploy, "deployed_at": t0 + timedelta(minutes=5), "environment": staging,
                      "next_deployed_at": None}, now)
        # The staging errors had a request path and an encrypted stack trace: they still count as errors
        counted = await conn.fetchval(
            "SELECT count(*) FROM events WHERE environment = $1 AND type = 'error' AND stack_trace IS NULL", staging)
        await conn.execute("DELETE FROM traffic WHERE environment = ANY($1)", [prod, staging])
        await conn.execute("DELETE FROM events WHERE environment = $1", staging)
        await conn.execute("DELETE FROM deploys WHERE environment = ANY($1)", [prod, staging])

    assert verdict == "safe", reasons
    assert s_verdict == "rollback", s_reasons
    assert counted >= 20


@pytest.mark.asyncio
async def test_api_answers_nan_with_422_and_does_not_echo_input():
    import httpx

    from app.auth import require_ingest_project_id
    from app.main import app

    app.dependency_overrides[require_ingest_project_id] = lambda: 1
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/events", headers={"Content-Type": "application/json", "X-API-Key": "x"},
                content='{"events": [{"type": "error", "message": "token=supersecret1", "occurred_at": NaN}]}',
            )
    finally:
        app.dependency_overrides.pop(require_ingest_project_id, None)
    assert resp.status_code == 422
    assert "supersecret1" not in resp.text and resp.json()["detail"][0]["loc"][-1] == "occurred_at"
