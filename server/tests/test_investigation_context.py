"""What the AI investigator is given (live Postgres): the change itself, who hit the error, when it
started, the other new errors and the previous deploys, each section within its size limit."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.investigator.agent import build_investigation_prompt, pregather_context

DEPLOYED = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=20)

DIFF = """diff --git a/app/util.py b/app/util.py
@@ -1,2 +1,3 @@
 import os
+import sys
 X = 1
diff --git a/app/billing.py b/app/billing.py
@@ -3,3 +3,4 @@ def header():
 # far away from the failing line
+FAR_CHANGE = True
 pass
@@ -40,3 +41,3 @@ def total_cents(order):
     \"\"\"Total in cents.\"\"\"
-    return sum(i.price for i in order.items)
+    return sum(i.price for i in order.items) / order.discount
"""


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def incident():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    run = uuid.uuid4().hex[:8]
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"ctx-{run}")
    async with project_transaction(pid) as conn:
        await conn.execute("INSERT INTO deploys (project_id, commit_sha, deployed_at, message) VALUES ($1, 'prev0001aaaa', $2, 'Earlier release\nbody')",
                           pid, DEPLOYED - timedelta(hours=3))
        dep = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, deployed_at, message, diff) "
            "VALUES ($1, 'abcd1234ffff', $2, 'Apply discount to totals', $3) RETURNING id", pid, DEPLOYED, DIFF)
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'ZeroDivisionError: division by zero', $3, $3, 'open', 30) RETURNING id",
            pid, f"fp-{run}", DEPLOYED + timedelta(minutes=1))
        await conn.execute(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'KeyError: coupon', $3, $3, 'open', 4)", pid, f"fp2-{run}", DEPLOYED + timedelta(minutes=2))
        inc = await conn.fetchval(
            """INSERT INTO incidents (project_id, deploy_id, issue_id, status, title, where_file, where_line,
                   where_function, in_diff, event_count)
               VALUES ($1, $2, $3, 'confirmed', 'ZeroDivisionError: division by zero', 'app/billing.py', 42,
                   'total_cents', true, 30) RETURNING id""", pid, dep, issue)
        for i in range(6):
            await conn.execute(
                "INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment, request_path, extra) "
                "VALUES ($1, $2, $3, $4, $5, 'prod', $6, $7)",
                f"{run}-{i}", issue, pid, dep, DEPLOYED + timedelta(minutes=1, seconds=i),
                "/checkout" if i < 4 else "/cart", json.dumps({"method": "POST", "plan": "pro" if i % 2 else "free"}))
        for minute, n in ((1, 12), (2, 18)):
            await conn.execute("INSERT INTO event_rollups (project_id, issue_id, deploy_id, bucket, count) VALUES ($1, $2, $3, $4, $5)",
                               pid, issue, dep, DEPLOYED + timedelta(minutes=minute), n)
    yield pid, inc
    async with get_connection() as conn:
        for table in ("incidents", "event_rollups", "events", "issues", "deploys"):
            await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", pid)
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_prompt_carries_the_change_the_requests_and_the_timing(incident):
    pid, inc = incident
    async with project_transaction(pid) as conn:
        context = await pregather_context(conn, pid, inc)
    prompt = build_investigation_prompt(context)

    assert "+    return sum(i.price for i in order.items) / order.discount" in prompt  # the hunk at line 42
    assert "FAR_CHANGE" not in prompt  # a hunk far from the failing line is left out
    assert '"path": "/checkout"' in prompt and '"count": 4' in prompt
    assert '"POST": 6' in prompt and '"plan"' in prompt
    assert "Before the deploy: 0; after: 30" in prompt and "+1 min: 12, +2 min: 18" in prompt
    assert "KeyError: coupon" in prompt  # the other new error of this deploy
    assert "prev0001" in prompt and "Earlier release" in prompt
    assert "this line was changed by the deploy" in prompt


@pytest.mark.asyncio
async def test_live_one_huge_section_cannot_crowd_out_the_rest(incident):
    pid, inc = incident
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval("SELECT deploy_id FROM incidents WHERE id = $1", inc)
        huge = DIFF.replace("+    return sum(i.price for i in order.items) / order.discount",
                            "\n".join(f"+    line_{n} = {n}" for n in range(5000)))
        await conn.execute("UPDATE deploys SET diff = $2 WHERE id = $1", dep, huge)
        context = await pregather_context(conn, pid, inc)
    prompt = build_investigation_prompt(context)
    assert "[cut to fit]" in prompt
    assert len(prompt) < 30000
    assert "Previous deploys" in prompt and "prev0001" in prompt  # later sections still there


@pytest.mark.asyncio
async def test_live_error_rate_counts_the_deploys_own_minute_as_after(incident):
    from app.investigator.tools import get_error_rate

    pid, inc = incident
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval("SELECT deploy_id FROM incidents WHERE id = $1", inc)
        for minute, deploy_id, requests, errors in ((-2, None, 300, 0), (-1, None, 300, 0), (0, dep, 120, 30)):
            await conn.execute(
                "INSERT INTO traffic (project_id, environment, bucket, requests, errors, deploy_id) VALUES ($1, 'prod', $2, $3, $4, $5)",
                pid, DEPLOYED + timedelta(minutes=minute), requests, errors, deploy_id)
        # the deploy happened 30 s into its minute
        await conn.execute("UPDATE deploys SET deployed_at = $2 WHERE id = $1", dep, DEPLOYED + timedelta(seconds=30))
        rate = await get_error_rate(conn, pid, dep)
        await conn.execute("DELETE FROM traffic WHERE project_id = $1", pid)
    assert rate["before"] == {"requests": 600, "errors": 0, "rate": 0.0}
    assert rate["after"] == {"requests": 120, "errors": 30, "rate": 0.25}
