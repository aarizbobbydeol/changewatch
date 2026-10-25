"""Billing, ratings and the timeline (live Postgres): fair metering under concurrency and late
events, price books that start on their date, budget warnings that are sent, unbilled AI
fallbacks, and incident actions and ratings scoped to the caller's project."""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import require_read_project_id
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.main import app
from app.metering import (
    Meter,
    PriceBookEntry,
    _effective_price_book_version,
    flush_usage_events,
    record_usage,
)


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def projects():
    """Two fresh projects (ids), removed afterwards with everything that belongs to them."""
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    run = uuid.uuid4().hex[:8]
    async with get_connection() as conn:
        ids = [await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"bill-{run}-{i}")
               for i in range(2)]
    yield ids
    async with get_connection() as conn:
        for pid in ids:
            for table in ("alert_feedback", "incident_events", "usage_events", "usage_daily", "budget_state",
                          "budgets", "outbox", "incidents", "event_rollups", "traffic", "events", "issues", "deploys"):
                await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", pid)
            await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    await close_db_pool()


ONE_FREE = {"release_monitored": PriceBookEntry(version=1, meter="release_monitored", pricing_mode="fixed",
                                                 unit_price_micros=20000, margin_multiplier=1.0,
                                                 min_unit_price_micros=0, free_units_per_month=1)}


@pytest.mark.asyncio
async def test_live_two_records_at_once_cannot_both_take_the_last_free_unit(projects):
    pid = projects[0]

    async def deploy(i):
        async with project_transaction(pid) as conn:
            await record_usage(conn, pid, Meter.RELEASE_MONITORED, 1, ref_type="deploy", ref_id=i,
                               idempotency_key=f"deploy:race:{i}")
            await asyncio.sleep(0.3)  # still uncommitted while the other one reads

    with patch("app.metering.get_active_price_book", new=AsyncMock(return_value=ONE_FREE)):
        await asyncio.gather(deploy(1), deploy(2))
    async with get_connection() as conn:
        free, billed = await conn.fetchrow(
            "SELECT sum(free_units_applied), sum(billed_micros) FROM usage_daily WHERE project_id = $1", pid)
    assert (free, billed) == (1, 20000)


class FakeRedis:
    def __init__(self):
        self.kv, self.sets = {}, {}

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

    async def get(self, key):
        return self.kv.get(key)

    async def decrby(self, key, n):
        self.kv[key] = int(self.kv.get(key, 0)) - n
        return self.kv[key]

    def add_events(self, pid, minute_epoch, n):
        key = f"usage:events:{pid}:{minute_epoch}"
        self.kv[key] = int(self.kv.get(key, 0)) + n
        self.sets.setdefault("usage:pending_event_buckets", set()).add(f"{pid}:{minute_epoch}")


@pytest.mark.asyncio
async def test_live_late_events_for_a_flushed_minute_are_still_counted(projects):
    pid = projects[0]
    minute = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()) // 60 * 60
    redis = FakeRedis()
    redis.add_events(pid, minute, 5)
    async with get_connection() as conn:
        assert await flush_usage_events(conn, redis) == 5
        redis.add_events(pid, minute, 3)  # the SDK delivered more events of that minute later
        assert await flush_usage_events(conn, redis) == 3
        units = await conn.fetchval(
            "SELECT sum(units) FROM usage_events WHERE project_id = $1 AND meter = 'stored_events'", pid)
    assert units == 8
    assert redis.sets["usage:pending_event_buckets"] == set()


@pytest.mark.asyncio
async def test_live_failed_flush_keeps_the_count_for_the_next_one(projects):
    pid = projects[0]
    minute = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()) // 60 * 60
    redis = FakeRedis()
    redis.add_events(pid, minute, 7)
    async with get_connection() as conn:
        with patch("app.metering.record_usage", new=AsyncMock(side_effect=asyncpg.PostgresError("down"))):
            with pytest.raises(asyncpg.PostgresError):
                await flush_usage_events(conn, redis)
        assert await flush_usage_events(conn, redis) == 7  # nothing was lost


@pytest.mark.asyncio
async def test_live_price_book_published_for_later_does_not_apply_yet(projects):
    async with get_connection() as conn:
        before = await _effective_price_book_version(conn)
        try:
            await conn.execute(
                """INSERT INTO price_book (version, meter, pricing_mode, unit_price_micros, margin_multiplier,
                                           min_unit_price_micros, free_units_per_month, effective_from)
                   VALUES (9001, 'release_monitored', 'fixed', 99999, 1.0, 0, 0, now() + interval '30 days')""")
            after = await _effective_price_book_version(conn)
        finally:
            await conn.execute("DELETE FROM price_book WHERE version = 9001")
    assert after == before != 9001


@pytest.mark.asyncio
async def test_live_crossing_a_budget_threshold_queues_a_readable_warning(projects):
    pid = projects[0]
    async with project_transaction(pid) as conn:
        await conn.execute(
            "INSERT INTO budgets (project_id, soft_limit_micros, hard_limit_micros) VALUES ($1, 30000, 100000)", pid)
        with patch("app.metering.get_active_price_book", new=AsyncMock(return_value={
            "release_monitored": PriceBookEntry(version=1, meter="release_monitored", pricing_mode="fixed",
                                                unit_price_micros=20000, margin_multiplier=1.0,
                                                min_unit_price_micros=0, free_units_per_month=0)})):
            await record_usage(conn, pid, Meter.RELEASE_MONITORED, 1, idempotency_key="deploy:budget:1")
        rows = await conn.fetch("SELECT dedupe_key, payload FROM outbox WHERE project_id = $1", pid)
    payloads = [json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"] for r in rows]
    assert [r["dedupe_key"].split(":")[-1] for r in rows] == ["50"]  # 20,000 of 30,000 = 66%
    assert payloads[0]["title"].startswith("Budget: 50%") and "$0.02 of $0.03" in payloads[0]["body"]


@pytest.mark.asyncio
async def test_ai_fallback_answer_is_stored_as_partial_and_not_billed():
    from app.investigator.agent import run_investigation

    now = datetime.now(timezone.utc)
    conn = AsyncMock()

    async def fetchrow(query, *args):
        q = query.lower()
        if "from incidents i" in q:
            return {"id": 1, "deploy_id": 10, "issue_id": 5, "where_file": "app/user.py", "where_line": 20,
                    "where_function": "login", "title": "KeyError: 'token'", "event_count": 12, "created_at": now,
                    "commit_sha": "abc12345", "commit_author": "dev", "commit_message": "Add auth",
                    "deployed_at": now, "environment": "prod", "fingerprint": "fp1", "in_diff": True,
                    "category": "logic_bug"}
        if "insert into investigations" in q:
            return {"id": 77}
        return None

    conn.fetchrow.side_effect = fetchrow
    conn.fetch.return_value = []
    with patch("app.crypto.encrypt", new=AsyncMock(return_value=b"enc")), \
         patch("app.investigator.agent.get_llm_client") as factory, \
         patch("app.investigator.agent.queue_notification", new=AsyncMock()), \
         patch("app.metering.record_investigation_usage", new=AsyncMock()) as usage:
        llm = AsyncMock()
        llm.complete.side_effect = TimeoutError("provider timed out")
        llm.last_usage = (900, 0)
        factory.return_value = llm
        await run_investigation(conn, project_id=1, incident_id=1)

    insert = next(c.args for c in conn.fetchrow.call_args_list if "insert into investigations" in c.args[0].lower())
    assert insert[-1] == "partial"
    assert usage.call_args.kwargs["status"] == "failed"  # records nothing: no free unit, no charge
    lock = conn.execute.call_args_list[0].args
    assert "pg_advisory_xact_lock" in lock[0] and lock[1] == "investigation:1:1"


async def _incident(conn, pid):
    dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha) VALUES ($1, $2) RETURNING id",
                              pid, uuid.uuid4().hex[:12])
    issue = await conn.fetchval(
        "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
        "VALUES ($1, $2, 'KeyError: x', now(), now(), 'open', 1) RETURNING id", pid, uuid.uuid4().hex)
    return await conn.fetchval(
        "INSERT INTO incidents (project_id, deploy_id, issue_id, status) VALUES ($1, $2, $3, 'possible') RETURNING id",
        pid, dep, issue)


@pytest.mark.asyncio
async def test_live_ratings_and_actions_stay_in_the_callers_project(projects):
    mine, theirs = projects
    async with project_transaction(theirs) as conn:
        their_incident = await _incident(conn, theirs)
    async with project_transaction(mine) as conn:
        my_incident = await _incident(conn, mine)

    app.dependency_overrides[require_read_project_id] = lambda: mine
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            other = await client.post(f"/api/incidents/{their_incident}/feedback", json={"label": "false"})
            other_action = await client.post(f"/api/incidents/{their_incident}/actions", json={"action": "resolve"})
            missing = await client.post("/api/incidents/999999999/feedback", json={"label": "real"})
            bad_label = await client.post(f"/api/incidents/{my_incident}/feedback", json={"label": "meh"})
            bad_action = await client.post(f"/api/incidents/{my_incident}/actions", json={"action": "dismiss"})
            ok = await client.post(f"/api/incidents/{my_incident}/feedback", json={"label": "real"})
            resolved = await client.post(f"/api/incidents/{my_incident}/actions", json={"action": "resolve"})
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)

    assert (other.status_code, other_action.status_code, missing.status_code) == (404, 404, 404)
    assert (bad_label.status_code, bad_action.status_code) == (422, 422)  # not 500
    assert (ok.status_code, resolved.status_code) == (200, 200)
    async with get_connection() as conn:
        planted = await conn.fetchval("SELECT count(*) FROM alert_feedback WHERE incident_id = $1", their_incident)
        status = await conn.fetchval("SELECT status FROM incidents WHERE id = $1", their_incident)
    assert (planted, status) == (0, "possible")


@pytest.mark.asyncio
async def test_live_overview_counts_real_ratings_and_timeline_leaves_slow_requests_out(projects):
    pid = projects[0]
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    async with project_transaction(pid) as conn:
        for i in range(10):
            inc = await _incident(conn, pid)
            await conn.execute(
                "INSERT INTO alert_feedback (incident_id, project_id, actor, label) VALUES ($1, $2, 't', $3)",
                inc, pid, "real" if i < 8 else "false")
        slow = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'Slow: /orders/1', now(), now(), 'open', 30) RETURNING id", pid, uuid.uuid4().hex)
        await conn.execute("INSERT INTO event_rollups (project_id, issue_id, bucket, count) VALUES ($1, $2, $3, 30)",
                           pid, slow, now - timedelta(minutes=2))

    app.dependency_overrides[require_read_project_id] = lambda: pid
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            overview = (await client.get("/v1/analytics/overview?period=7")).json()
            timeline = (await client.get("/api/timeline?hours=1")).json()
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)

    assert overview["kpis"]["alerts_real"]["value"] == 80.0
    buckets = timeline if isinstance(timeline, list) else timeline.get("timeline", [])
    assert sum(b["errors"] for b in buckets) == 0  # 30 slow requests are not errors
    assert sum(b["requests"] for b in buckets) == 0  # and no requests were made up
