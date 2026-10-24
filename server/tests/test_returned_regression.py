"""A resolved error that comes back after a new deploy is a regression again (live Postgres)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.incident_manager import check_and_trigger_incident


async def _setup(conn, run, quiet: bool):
    now = datetime.now(timezone.utc)
    env = f"ret-{run}"
    a = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, $2, $3) RETURNING id",
                            f"a{run}", env, now - timedelta(minutes=60))
    b = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, $2, $3) RETURNING id",
                            f"b{run}", env, now - timedelta(minutes=10))
    issue = await conn.fetchval(
        "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
        "VALUES (1, $1, 'ZeroDivisionError: back', $2, $2, 'open', 5) RETURNING id", f"fp-{run}", now - timedelta(minutes=55))
    await conn.execute(
        "INSERT INTO incidents (project_id, deploy_id, issue_id, status, resolved_at) VALUES (1, $1, $2, 'resolved', $3)",
        a, issue, now - timedelta(minutes=30))
    if not quiet:  # the error kept happening after it was marked resolved
        await conn.execute("INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment) "
                           "VALUES ($1, $2, 1, $3, $4, $5)", f"{run}-mid", issue, a, now - timedelta(minutes=20), env)
    for i in range(3):
        await conn.execute("INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment) "
                           "VALUES ($1, $2, 1, $3, $4, $5)", f"{run}-{i}", issue, b, now - timedelta(seconds=5 - i), env)
    return b, issue, now


@pytest.mark.asyncio
@pytest.mark.parametrize("quiet", [True, False])
async def test_live_resolved_error_returning_after_deploy(quiet):
    try:
        c = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await c.close()
    except Exception:
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    try:
        run = uuid.uuid4().hex[:8]
        with patch("app.incident_manager.dispatch_incident_alert", new=AsyncMock()) as dispatch, \
             patch("app.trust.calm.is_issue_muted", new=AsyncMock(return_value=(False, None))):
            async with project_transaction(1) as conn:
                b, issue, now = await _setup(conn, run, quiet)
                inc = await check_and_trigger_incident(
                    conn=conn, project_id=1, deploy_id=b, issue_id=issue,
                    payload={"request_path": "/x", "stack_trace": 'File "app/x.py", line 1, in f', "exc_type": "ZeroDivisionError"},
                    occurred_at_dt=now, new_events=3,
                )
                await conn.execute("DELETE FROM deploys WHERE commit_sha IN ($1, $2)", f"a{run}", f"b{run}")
                await conn.execute("DELETE FROM issues WHERE id = $1", issue)
        if quiet:
            assert inc is not None
            assert dispatch.call_args.kwargs["triage_info"]["returned"] is True
        else:
            assert inc is None  # it never went away: not this deploy's regression
            dispatch.assert_not_called()
    finally:
        await close_db_pool()
