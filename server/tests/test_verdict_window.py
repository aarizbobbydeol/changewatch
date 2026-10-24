"""A deploy's verdict must not count errors introduced by the next deploy (live Postgres)."""
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.detector import evaluate_deploy


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_live_errors_from_the_next_deploy_are_not_blamed_on_the_previous_one():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")

    await init_db_pool()
    try:
        run = uuid.uuid4().hex[:10]
        env = f"window-{run}"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        t0 = now - timedelta(minutes=20)
        async with project_transaction(1) as conn:
            a = await conn.fetchval(
                "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, $2, $3) RETURNING id",
                f"a{run}", env, t0,
            )
            b = await conn.fetchval(
                "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES (1, $1, $2, $3) RETURNING id",
                f"b{run}", env, t0 + timedelta(minutes=5),
            )
            # Deploy A only sees a long-known error; deploy B introduces a new error with 25 events
            known = await conn.fetchval(
                "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
                "VALUES (1, $1, 'TimeoutError: known', $2, $2, 'open', 5) RETURNING id",
                f"fp-known-{run}", t0 - timedelta(hours=1),
            )
            await conn.execute(
                "INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, stack_trace) "
                "SELECT $1 || g, $2, 1, $3, $4::timestamptz + g * interval '1 second', 'trace' FROM generate_series(1, 5) g",
                f"{run}-ok-", known, a, t0 + timedelta(minutes=1),
            )
            issue = await conn.fetchval(
                "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
                "VALUES (1, $1, 'ZeroDivisionError: window test', $2, $2, 'open', 25) RETURNING id",
                f"fp-{run}", t0 + timedelta(minutes=6),
            )
            await conn.execute(
                "INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, stack_trace) "
                "SELECT $1 || g, $2, 1, $3, $4::timestamptz + g * interval '1 second', 'trace' FROM generate_series(1, 25) g",
                f"{run}-bad-", issue, b, t0 + timedelta(minutes=6),
            )

            deploy_a = {"id": a, "deployed_at": t0, "environment": env, "next_deployed_at": t0 + timedelta(minutes=5)}
            verdict_a, reasons_a, _, _ = await evaluate_deploy(conn, 1, deploy_a, now)
            deploy_b = {"id": b, "deployed_at": t0 + timedelta(minutes=5), "environment": env, "next_deployed_at": None}
            verdict_b, reasons_b, _, _ = await evaluate_deploy(conn, 1, deploy_b, now)
            # Without the cap (the old behaviour), A was blamed for B's errors
            uncapped = dict(deploy_a, next_deployed_at=None)
            verdict_uncapped, _, _, _ = await evaluate_deploy(conn, 1, uncapped, now)

            await conn.execute("DELETE FROM events WHERE event_id LIKE $1", f"{run}-%")
            await conn.execute("DELETE FROM issues WHERE id = ANY($1::int[])", [issue, known])
            await conn.execute("DELETE FROM deploys WHERE id = ANY($1::int[])", [a, b])

        assert verdict_b == "rollback", reasons_b
        assert verdict_a != "rollback" and not any("new error" in r for r in reasons_a), reasons_a
        assert verdict_uncapped == "rollback"
    finally:
        await close_db_pool()
