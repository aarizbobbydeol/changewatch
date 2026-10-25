"""Cost tracking (live Postgres): the app's AI spend per release and what an incident cost.
Money is integer micro-USD throughout."""
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import require_read_project_id
from app.config import settings
from app.costs import ai_cost_for_deploy, incident_cost, price_ai_call, usd
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.worker import process_event_batch


def test_prices_are_exact_and_unknown_models_are_not_guessed():
    # claude-sonnet-5-5 is $2 / $10 per million tokens in the default price list
    assert price_ai_call("claude-sonnet-5-5", 1_000_000, 100_000) == 3_000_000
    assert price_ai_call("claude-haiku-5-5", 3, 3) == 2  # 0.3 + 1.5 micros, rounded half up
    assert price_ai_call("some-other-model", 1000, 1000) is None
    assert [usd(m) for m in (3_000_000, 500_000, 135_000, 60_000, 4_200, 0, None)] == [
        "$3.00", "$0.50", "$0.14", "$0.06", "$0.0042", "$0.00", None]


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
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"cost-{uuid.uuid4().hex[:8]}")
    yield pid
    async with get_connection() as conn:
        for table in ("outbox", "ai_usage", "traffic", "investigations", "incidents", "event_rollups", "events",
                      "issues", "deploy_analysis", "deploys", "sources"):
            if table == "deploy_analysis":
                await conn.execute("DELETE FROM deploy_analysis WHERE deploy_id IN (SELECT id FROM deploys WHERE project_id = $1)", pid)
            else:
                await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", pid)
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    await close_db_pool()


def _usage(project, run, i, **fields):
    return {"event_id": f"{run}-{i}", "project_id": project, "type": "ai_usage", "environment": "prod", **fields}


@pytest.mark.asyncio
async def test_live_ai_usage_is_summed_priced_and_tied_to_the_release(project):
    run = uuid.uuid4().hex[:8]
    now = time.time()
    async with project_transaction(project) as conn:
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, $2, $3) RETURNING id",
                                  project, f"rel{run}", datetime.fromtimestamp(now - 600, tz=timezone.utc))
    minute = now - (now % 60) + 5
    await process_event_batch([
        _usage(project, run, 1, model="claude-sonnet-5-5", feature="search", calls=10, input_tokens=100_000,
               output_tokens=10_000, release=f"rel{run}", occurred_at=minute),
        _usage(project, run, 2, model="claude-sonnet-5-5", feature="search", calls=5, input_tokens=50_000,
               output_tokens=5_000, release=f"rel{run}", occurred_at=minute + 10),
        _usage(project, run, 3, model="gpt-x", calls=4, input_tokens=1000, output_tokens=1000, release=f"rel{run}",
               occurred_at=minute + 20),
        _usage(project, run, 4, model="gpt-x", calls=2, cost_micros=7_000, release=f"rel{run}", occurred_at=minute + 30),
    ])
    await process_event_batch([_usage(project, run, 1, model="claude-sonnet-5-5", feature="search", calls=10,
                                      input_tokens=100_000, output_tokens=10_000, occurred_at=minute)])  # a re-send
    async with project_transaction(project) as conn:
        rows = await conn.fetch("SELECT model, feature, deploy_id, calls, input_tokens, cost_micros, unpriced_calls "
                                "FROM ai_usage WHERE project_id = $1 ORDER BY model", project)
    assert [dict(r) for r in rows] == [
        {"model": "claude-sonnet-5-5", "feature": "search", "deploy_id": dep, "calls": 15, "input_tokens": 150_000,
         "cost_micros": 450_000, "unpriced_calls": 0},  # (150k x $2 + 15k x $10) per million; the re-send not counted
        {"model": "gpt-x", "feature": "", "deploy_id": dep, "calls": 6, "input_tokens": 1000,
         "cost_micros": 7_000, "unpriced_calls": 4},  # the app's own cost is used; the rest is counted, not guessed
    ]


@pytest.mark.asyncio
async def test_live_release_that_doubles_ai_spend_per_request_is_called_out_once(project):
    from app.detector import _maybe_alert_ai_cost

    deployed = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15)
    async with project_transaction(project) as conn:
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, 'aicost1234', $2) RETURNING id",
                                  project, deployed)
        for m in range(-20, 15):  # 100 requests a minute; AI cost per minute 2,000 micros before, 9,000 after
            after = m >= 0
            await conn.execute("INSERT INTO traffic (project_id, environment, bucket, requests, errors, deploy_id) "
                               "VALUES ($1, 'prod', $2, 100, 0, $3)", project, deployed + timedelta(minutes=m), dep if after else None)
            await conn.execute("INSERT INTO ai_usage (project_id, environment, bucket, model, feature, deploy_id, calls, cost_micros) "
                               "VALUES ($1, 'prod', $2, 'claude-sonnet-5-5', 'summary', $3, 4, $4)",
                               project, deployed + timedelta(minutes=m), dep if after else None, 9_000 if after else 2_000)
        cost = await ai_cost_for_deploy(conn, project, dep)
        await _maybe_alert_ai_cost(conn, project, dep, "aicost1234")
        await _maybe_alert_ai_cost(conn, project, dep, "aicost1234")
        messages = await conn.fetch("SELECT payload FROM outbox WHERE project_id = $1 AND dedupe_key LIKE 'ai_cost:%'", project)
    assert cost["before"]["per_1k_requests_micros"] == 20_000 and cost["after"]["per_1k_requests_micros"] == 90_000
    assert cost["change"] == "4.5x" and cost["jumped"] is True
    assert cost["before"]["per_1k_requests"] == "$0.02" and cost["after"]["cost"] == "$0.14"
    assert len(messages) == 1
    assert "AI spend up 4.5x after deploy aicost12" in str(messages[0]["payload"])


@pytest.mark.asyncio
async def test_live_ai_cost_change_is_exact_and_leaves_out_calls_without_a_price(project):
    deployed = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15)
    cases = {  # environment: (requests, calls, unpriced calls, cost) before, then after
        "tiny": ((0, 1000, 0, 1_900), (0, 60_000, 0, 120_000)),  # 1.9 then 2.0 micros a call
        "mixed": ((0, 1000, 400, 60_000), (0, 1000, 0, 130_000)),  # 100 then 130 per priced call
        "unknown": ((1000, 100, 60, 40_000), (1000, 100, 0, 100_000)),  # most calls before had no price
    }
    costs = {}
    async with project_transaction(project) as conn:
        for env, windows in cases.items():
            dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at, environment) "
                                      "VALUES ($1, $2, $3, $4) RETURNING id", project, f"c-{env}", deployed, env)
            for (requests, calls, unpriced, cost), minute, dep_id in zip(windows, (-10, 5), (None, dep)):
                if requests:
                    await conn.execute("INSERT INTO traffic (project_id, environment, bucket, requests, errors, deploy_id) "
                                       "VALUES ($1, $2, $3, $4, 0, $5)", project, env, deployed + timedelta(minutes=minute),
                                       requests, dep_id)
                await conn.execute("INSERT INTO ai_usage (project_id, environment, bucket, model, feature, deploy_id, calls, "
                                   "unpriced_calls, cost_micros) VALUES ($1, $2, $3, 'm', '', $4, $5, $6, $7)",
                                   project, env, deployed + timedelta(minutes=minute), dep_id, calls, unpriced, cost)
            costs[env] = await ai_cost_for_deploy(conn, project, dep)
    # Not 1 then 2 micros (rounded down) and "2.0x": the real change is 5%
    assert (costs["tiny"]["basis"], costs["tiny"]["change"], costs["tiny"]["jumped"]) == ("per_call", "1.1x", False)
    # Calls to a model without a price cost nothing here, so they do not dilute the cost per call
    assert (costs["mixed"]["before"]["per_call_micros"], costs["mixed"]["after"]["per_call_micros"]) == (100, 130)
    assert (costs["mixed"]["change"], costs["mixed"]["jumped"]) == ("1.3x", False)
    # 2.5x per 1,000 requests only because most calls before had no known cost: not compared
    assert (costs["unknown"]["basis"], costs["unknown"]["change"], costs["unknown"]["jumped"]) == (None, None, False)


@pytest.mark.asyncio
async def test_live_ai_cost_alert_is_not_held_back_by_another_deploys_alert(project):
    from app.detector import _maybe_alert_ai_cost
    from app.outbox import queue_notification

    deployed = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15)
    async with project_transaction(project) as conn:
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, 'aicost5678', $2) RETURNING id",
                                  project, deployed)
        for m in range(-20, 15):  # AI cost per request 4.5x after the deploy
            after = m >= 0
            await conn.execute("INSERT INTO traffic (project_id, environment, bucket, requests, errors, deploy_id) "
                               "VALUES ($1, 'prod', $2, 100, 0, $3)", project, deployed + timedelta(minutes=m), dep if after else None)
            await conn.execute("INSERT INTO ai_usage (project_id, environment, bucket, model, feature, deploy_id, calls, cost_micros) "
                               "VALUES ($1, 'prod', $2, 'claude-sonnet-5-5', 'summary', $3, 4, $4)",
                               project, deployed + timedelta(minutes=m), dep if after else None, 9_000 if after else 2_000)
        # A deploy whose id starts with this one's digits (deploy 7 and deploy 73) already alerted
        await queue_notification(conn, project, f"ai_cost:{dep}3:channel:1", {"text": "other deploy"}, fan_out=False)
        await _maybe_alert_ai_cost(conn, project, dep, "aicost5678")
        await _maybe_alert_ai_cost(conn, project, dep, "aicost5678")
        mine = await conn.fetchval("SELECT count(*) FROM outbox WHERE project_id = $1 AND dedupe_key = $2", project, f"ai_cost:{dep}")
    assert mine == 1


@pytest.mark.asyncio
async def test_live_incident_cost_and_the_value_setting(project):
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)
    async with project_transaction(project) as conn:
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, 'inc1', $2) RETURNING id",
                                  project, start)
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'E', now(), now(), 'open', 1) RETURNING id", project, uuid.uuid4().hex)
        inc = await conn.fetchval(
            "INSERT INTO incidents (project_id, deploy_id, issue_id, status, first_event_at, last_event_at, resolved_at) "
            "VALUES ($1, $2, $3, 'resolved', $4, $5, $6) RETURNING id",
            project, dep, issue, start + timedelta(minutes=1), start + timedelta(minutes=20), start + timedelta(minutes=26))
        for m in (1, 5, 20, 40):  # minute 40 is after the incident was resolved
            await conn.execute("INSERT INTO event_rollups (project_id, issue_id, deploy_id, bucket, count) VALUES ($1, $2, $3, $4, 25)",
                               project, issue, dep, start + timedelta(minutes=m))
        await conn.execute("INSERT INTO investigations (incident_id, project_id, status, model, input_tokens, output_tokens) "
                           "VALUES ($1, $2, 'done', 'claude-sonnet-5-5', 20000, 2000)", inc, project)
        without_value = await incident_cost(conn, project, inc)

    from app.main import app

    app.dependency_overrides[require_read_project_id] = lambda: project
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            saved = await client.put("/api/settings", json={"failed_request_value_micros": 500_000})
            negative = await client.put("/api/settings", json={"failed_request_value_micros": -1})
            shown = (await client.get("/api/settings")).json()["failed_request_value_micros"]
            detail = (await client.get(f"/api/incidents/{inc}")).json()
            draft = (await client.get(f"/api/incidents/{inc}/postmortem")).json()["markdown"]
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)

    assert without_value["failed_requests"] == 75 and without_value["minutes"] == 25
    assert without_value["investigation_cost"] == "$0.06" and without_value["estimated_impact"] is None
    assert saved.status_code == 200 and negative.status_code == 422 and shown == 500_000
    assert detail["cost"]["estimated_impact"] == "$37.50"  # 75 x $0.50
    assert "Estimated impact: $37.50 (75 failed requests at $0.50 each" in draft
    assert "AI investigation cost: $0.06" in draft


@pytest.mark.asyncio
async def test_live_incident_cost_counts_only_the_incidents_environment(project):
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=60)
    async with project_transaction(project) as conn:
        prod = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at, environment) "
                                   "VALUES ($1, 'p1', $2, 'prod') RETURNING id", project, start)
        staging = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at, environment) "
                                      "VALUES ($1, 's1', $2, 'staging') RETURNING id", project, start - timedelta(minutes=60))
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'E', now(), now(), 'open', 1) RETURNING id", project, uuid.uuid4().hex)
        inc = await conn.fetchval(
            "INSERT INTO incidents (project_id, deploy_id, issue_id, status, first_event_at, last_event_at, resolved_at) "
            "VALUES ($1, $2, $3, 'resolved', $4, $5, $6) RETURNING id",
            project, prod, issue, start + timedelta(minutes=1), start + timedelta(minutes=20), start + timedelta(minutes=30))
        # The same error during the incident: in prod, in staging under a load test, and from an
        # environment with no deploys (no deploy_id)
        for dep_id, minute, count in ((prod, 5, 25), (staging, 6, 50_000), (None, 7, 300)):
            await conn.execute("INSERT INTO event_rollups (project_id, issue_id, deploy_id, bucket, count) VALUES ($1, $2, $3, $4, $5)",
                               project, issue, dep_id, start + timedelta(minutes=minute), count)
        cost = await incident_cost(conn, project, inc)
    assert cost["failed_requests"] == 25


def test_sdk_ai_usage_reaches_the_server_format():
    import sys
    from pathlib import Path

    sdk = Path(__file__).resolve().parents[2] / "sdk"
    sys.path.insert(0, str(sdk))
    from changewatch import Client
    from app.schemas import EventItem

    client = Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9")
    client._running = False
    client.record_ai_usage("claude-sonnet-5-5", 1200, 300, feature="summary")
    client.record_ai_usage("claude-sonnet-5-5", 800, 100, feature="summary")
    client.record_ai_usage("gpt-x", 10, 10, cost_usd="0.0123")
    with client._lock:
        client._flush_ai_usage_locked(time.time())
    events = [client._queue.get_nowait() for _ in range(client._queue.qsize())]
    for event in events:
        EventItem(**event)  # what the API will accept
    by_model = {e["model"]: e for e in events}
    assert (by_model["claude-sonnet-5-5"]["calls"], by_model["claude-sonnet-5-5"]["input_tokens"]) == (2, 2000)
    assert "cost_micros" not in by_model["claude-sonnet-5-5"]
    assert by_model["gpt-x"]["cost_micros"] == 12_300  # exact, from a string
    with patch.object(client, "_running", False):
        client.stop()


@pytest.mark.asyncio
async def test_live_retention_removes_old_ai_usage(project):
    from app.retention import cleanup_old_records

    old = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=45)
    recent = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=1)
    async with project_transaction(project) as conn:
        for bucket in (old, recent):
            await conn.execute("INSERT INTO ai_usage (project_id, environment, bucket, model, calls) VALUES ($1, 'prod', $2, 'm', 1)",
                               project, bucket)
    async with get_connection() as conn:
        await cleanup_old_records(conn, retention_days=30)
        left = [r["bucket"] for r in await conn.fetch("SELECT bucket FROM ai_usage WHERE project_id = $1", project)]
    assert left == [recent]
