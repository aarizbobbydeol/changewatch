"""The AI "likely cause" on verdicts counts toward the project's AI budget like an investigation
(live Postgres): recorded once per deploy, added to the month's spend, never with a fake provider."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

from app.analysis import generate_likely_cause
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def deploy():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"lc-{uuid.uuid4().hex[:8]}")
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, deployed_at, message, diff) VALUES ($1, 'lc0001', $2, 'm', $3) RETURNING id",
            pid, datetime.now(timezone.utc) - timedelta(minutes=12), "diff --git a/a.py b/a.py\n+x = 1 / y\n")
    yield pid, dep
    async with get_connection() as conn:
        for table in ("usage_events", "usage_daily", "budget_state", "deploys"):
            await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", pid)
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    await close_db_pool()


def _llm():
    llm = AsyncMock()
    llm.model = "claude-sonnet-5-5"
    llm.last_usage = (9_000, 300)
    llm.complete.return_value = "a.py divides by y, which is 0 on the new path. Fix: check y first."
    return llm


@pytest.mark.asyncio
async def test_live_likely_cause_counts_toward_the_ai_budget_once(deploy, monkeypatch):
    pid, dep = deploy
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "DRY_RUN_LLM", False)
    async with project_transaction(pid) as conn:
        # This month's free AI units are used up, so the call is billed and the budget sees it
        await conn.execute("INSERT INTO usage_daily (project_id, day, meter, units, our_cost_micros, billed_micros, free_units_applied) "
                           "VALUES ($1, current_date, 'ai_investigation', 1000, 0, 0, 1000)", pid)
    with patch("app.analysis.get_llm_client", return_value=_llm()):
        for _ in range(2):  # a second explanation of the same deploy is not counted again
            async with project_transaction(pid) as conn:
                assert (await generate_likely_cause(conn, pid, dep)).startswith("a.py divides")
    async with project_transaction(pid) as conn:
        rows = await conn.fetch("SELECT meter, units, our_cost_micros, billed_micros, ref_type, ref_id FROM usage_events WHERE project_id = $1", pid)
        spent = await conn.fetchval("SELECT coalesce(sum(spent_micros), 0) FROM budget_state WHERE project_id = $1", pid)
    assert [(r["meter"], r["units"], r["ref_type"], r["ref_id"]) for r in rows] == [("ai_investigation", 1, "deploy", dep)]
    assert rows[0]["our_cost_micros"] > 0
    assert rows[0]["billed_micros"] >= rows[0]["our_cost_micros"] and spent == rows[0]["billed_micros"]  # what the budget counts


@pytest.mark.asyncio
async def test_live_fake_provider_and_failed_calls_are_not_counted(deploy, monkeypatch):
    pid, dep = deploy
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    with patch("app.analysis.get_llm_client", return_value=_llm()):
        async with project_transaction(pid) as conn:
            await generate_likely_cause(conn, pid, dep)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "DRY_RUN_LLM", False)
    failing = _llm()
    failing.complete.side_effect = RuntimeError("provider down")
    with patch("app.analysis.get_llm_client", return_value=failing):
        async with project_transaction(pid) as conn:
            assert await generate_likely_cause(conn, pid, dep) is None
    async with project_transaction(pid) as conn:
        assert await conn.fetchval("SELECT count(*) FROM usage_events WHERE project_id = $1", pid) == 0
