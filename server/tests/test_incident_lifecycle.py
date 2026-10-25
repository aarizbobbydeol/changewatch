"""Incidents follow the error, not a frozen timestamp (live Postgres): a resolved incident that
fires again is reopened and alerted, auto-resolve waits while the error still fires, and a
deploy's verdict counts only its own window."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.detector import evaluate_deploy
from app.incident_manager import check_and_trigger_incident
from app.trust.calm import run_calm_auto_resolve


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


async def _deploy_issue(conn, env, deployed_at, first_seen):
    run = uuid.uuid4().hex[:8]
    dep = await conn.fetchval(
        "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, $2, $3) RETURNING id",
        f"lc{run}", env, deployed_at)
    issue = await conn.fetchval(
        "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
        "VALUES (1, $1, 'KeyError: lifecycle', $2, $2, 'open', 5) RETURNING id", f"fp-lc-{run}", first_seen)
    return dep, issue


async def _events(conn, issue, dep, env, times):
    for t in times:
        await conn.execute(
            "INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment, type) "
            "VALUES ($1, $2, 1, $3, $4, $5, 'error')", uuid.uuid4().hex, issue, dep, t, env)


async def _cleanup(conn, env):
    await conn.execute("DELETE FROM events WHERE environment = $1", env)
    await conn.execute("DELETE FROM traffic WHERE environment = $1", env)
    await conn.execute("DELETE FROM incidents WHERE deploy_id IN (SELECT id FROM deploys WHERE environment = $1)", env)
    await conn.execute("DELETE FROM deploys WHERE environment = $1", env)


@pytest.mark.asyncio
async def test_live_resolved_incident_that_fires_again_is_reopened_and_alerted(live_db):
    env = f"lc-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with project_transaction(1) as conn:
        try:
            dep, issue = await _deploy_issue(conn, env, now - timedelta(minutes=40), now - timedelta(minutes=35))
            inc = await conn.fetchval(
                "INSERT INTO incidents (project_id, deploy_id, issue_id, status, event_count, resolved_at, title, "
                "where_file, where_line) VALUES (1, $1, $2, 'resolved', 5, $3, 'KeyError: lifecycle', 'app/x.py', 3) "
                "RETURNING id", dep, issue, now - timedelta(minutes=5))
            await _events(conn, issue, dep, env, [now - timedelta(seconds=s) for s in (3, 2, 1)])
            with patch("app.incident_manager.dispatch_incident_alert", new=AsyncMock()) as dispatch, \
                 patch("app.trust.calm.is_issue_muted", new=AsyncMock(return_value=(False, None))):
                await check_and_trigger_incident(
                    conn=conn, project_id=1, deploy_id=dep, issue_id=issue,
                    payload={"request_path": "/x", "stack_trace": 'File "app/x.py", line 3, in f'},
                    occurred_at_dt=now, new_events=3)
            row = await conn.fetchrow("SELECT status, resolved_at, event_count FROM incidents WHERE id = $1", inc)
        finally:
            await _cleanup(conn, env)
            await conn.execute("DELETE FROM issues WHERE id = $1", issue)

    assert (row["status"], row["resolved_at"], row["event_count"]) == ("confirmed", None, 8)
    kwargs = dispatch.call_args.kwargs
    assert kwargs["status"] == "confirmed" and kwargs["triage_info"]["returned"] is True
    assert kwargs["dedupe_suffix"].startswith(":reopened:")  # not swallowed as a repeat of the first alert


@pytest.mark.asyncio
async def test_live_auto_resolve_waits_while_the_error_still_fires(live_db):
    env = f"lc-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with get_connection() as conn:
        try:
            # The deploy is 3 hours old: incidents.last_event_at stopped moving long ago
            dep, issue = await _deploy_issue(conn, env, now - timedelta(hours=3), now - timedelta(hours=3))
            inc = await conn.fetchval(
                "INSERT INTO incidents (project_id, deploy_id, issue_id, status, event_count, last_event_at) "
                "VALUES (1, $1, $2, 'confirmed', 50, $3) RETURNING id",
                dep, issue, now - timedelta(days=400))  # oldest first, so it is in the batch
            await _events(conn, issue, None, env, [now - timedelta(minutes=1)])  # still firing
            still_firing = [r["incident_id"] for r in await run_calm_auto_resolve(conn, now_dt=now)]
            await conn.execute("DELETE FROM events WHERE environment = $1", env)
            quiet = [r["incident_id"] for r in await run_calm_auto_resolve(conn, now_dt=now)]
        finally:
            await _cleanup(conn, env)
            await conn.execute("DELETE FROM issues WHERE id = $1", issue)

    assert inc not in still_firing
    assert inc in quiet


@pytest.mark.asyncio
async def test_live_verdict_ignores_events_after_its_window(live_db):
    env = f"lc-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t0 = now - timedelta(minutes=50)
    async with project_transaction(1) as conn:
        try:
            dep, issue = await _deploy_issue(conn, env, t0, t0 - timedelta(hours=1))
            for m in range(-30, 10):  # steady traffic before and after, no errors
                await conn.execute(
                    "INSERT INTO traffic (project_id, environment, bucket, requests, errors, deploy_id) "
                    "VALUES (1, $1, $2, 100, 0, $3)", env, t0 + timedelta(minutes=m), dep if m >= 0 else None)
            # 40 errors still tagged with this release, 20 minutes after the next deploy went out
            await _events(conn, issue, dep, env, [t0 + timedelta(minutes=30, seconds=i) for i in range(40)])
            verdict, reasons, _, _ = await evaluate_deploy(
                conn, 1, {"id": dep, "deployed_at": t0, "environment": env,
                          "next_deployed_at": t0 + timedelta(minutes=10)}, now)
        finally:
            await _cleanup(conn, env)
            await conn.execute("DELETE FROM issues WHERE id = $1", issue)

    assert verdict == "safe", reasons


@pytest.mark.asyncio
async def test_live_slow_ai_explanation_does_not_hold_up_the_verdict_alert(live_db):
    import asyncio
    import time

    env = f"lc-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t0 = now - timedelta(minutes=15)  # past the first checkpoint
    async with project_transaction(1) as conn:
        dep, issue = await _deploy_issue(conn, env, t0, t0 + timedelta(minutes=1))  # a new error type
        for m in range(-30, 15):
            await conn.execute(
                "INSERT INTO traffic (project_id, environment, bucket, requests, errors, deploy_id) "
                "VALUES (1, $1, $2, 100, $3, $4)", env, t0 + timedelta(minutes=m), 3 if m >= 1 else 0,
                dep if m >= 0 else None)
        await _events(conn, issue, dep, env, [t0 + timedelta(minutes=1, seconds=i) for i in range(40)])

    async def hanging_provider(*args, **kwargs):
        await asyncio.sleep(60)

    from app.detector import process_active_deploys_once
    try:
        with patch("app.detector.generate_likely_cause", new=hanging_provider), \
             patch("app.detector._first_ai_attempt", new=AsyncMock(return_value=True)), \
             patch("app.detector.LIKELY_CAUSE_TIMEOUT_SEC", 0.3), \
             patch("app.detector.dispatch_verdict_alert", new=AsyncMock()) as dispatch:
            start = time.monotonic()
            await process_active_deploys_once()
            elapsed = time.monotonic() - start
    finally:
        async with project_transaction(1) as conn:
            await conn.execute("DELETE FROM deploy_analysis WHERE deploy_id = $1", dep)
            await _cleanup(conn, env)
            await conn.execute("DELETE FROM issues WHERE id = $1", issue)

    mine = [c.kwargs for c in dispatch.call_args_list if c.kwargs["deploy_id"] == dep]
    assert mine and mine[0]["verdict"] == "rollback" and mine[0]["likely_cause"] is None
    assert elapsed < 15  # every flagged deploy waited at most the 0.3 s timeout, not 60 s
