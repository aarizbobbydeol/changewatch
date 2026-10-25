"""Risk review of each deploy's diff: the rules, the AI pass, storage and where it shows."""
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
from app.review import get_deploy_review, review_line, rules_review, run_deploy_review

DIFF = """diff --git a/app/billing.py b/app/billing.py
@@ -38,5 +38,9 @@ def total_cents(order):
-    if order.discount == 0:
-        return 0
+    url = "https://pay.example/v1/charge"  # no division here / or here
+    out = Path(base_dir) / name
+    return sum(i.price for i in order.items) / order.discount
+    rows = db.execute(f"SELECT * FROM orders WHERE id = {order_id}")
+    requests.get(url, verify=False)
+    # TODO: handle refunds
diff --git a/app/jobs.py b/app/jobs.py
@@ -1,2 +1,6 @@
+try:
+    run()
+except:
+    pass
+eval(user_code)
+API_KEY = "[REDACTED_SECRET]"
diff --git a/migrations/0042_add_discount.py b/migrations/0042_add_discount.py
@@ -0,0 +1,1 @@
+op.add_column("orders", "discount")
"""


def test_rules_find_the_risky_lines_and_leave_paths_urls_and_comments_alone():
    findings = rules_review(DIFF, ["app/billing.py", "app/jobs.py", "migrations/0042_add_discount.py", "requirements.txt"])
    got = {(f["file"], f["line"], f["issue"]) for f in findings}
    assert ("app/billing.py", 40, "Divides by a value that may be zero") in got
    assert ("app/billing.py", 41, "Builds SQL from an f-string (injection risk)") in got
    assert ("app/billing.py", 42, "Turns off TLS certificate checks") in got
    assert ("app/billing.py", 43, "Leaves a TODO/FIXME in released code") in got
    assert ("app/jobs.py", 3, "Swallows every exception") in got
    assert ("app/jobs.py", 5, "Runs code built at runtime (eval/exec)") in got
    assert any(f["issue"].startswith("A secret was committed") for f in findings)
    assert any(f["issue"] == "Removes a check: `if order.discount == 0:`" for f in findings)
    assert any(f["file"] == "migrations/0042_add_discount.py" and "database schema" in f["issue"] for f in findings)
    divisions = [f for f in findings if f["issue"].startswith("Divides")]
    assert [f["line"] for f in divisions] == [40]  # not the URL or the comment (38), or the path join (39)
    assert [f["severity"] for f in findings] == sorted((f["severity"] for f in findings), key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])
    assert len(findings) <= 10


def test_a_harmless_release_is_low_risk_with_no_line():
    diff = "diff --git a/README.md b/README.md\n@@ -1,1 +1,2 @@\n # Shop\n+Now with discounts.\n"
    findings = rules_review(diff, ["README.md", "docs/migration-guide.md"])
    assert findings == []
    assert review_line({"risk": "low", "findings": []}) is None


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
    run = uuid.uuid4().hex[:8]
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"rev-{run}")
        other = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"rev-o-{run}")
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, deployed_at, message, diff) VALUES ($1, $2, $3, 'Apply discount', $4) RETURNING id",
            pid, f"rev{run}", datetime.now(timezone.utc) - timedelta(minutes=12), DIFF)
        for path in ("app/billing.py", "app/jobs.py", "migrations/0042_add_discount.py"):
            await conn.execute("INSERT INTO deploy_files (deploy_id, project_id, path, status, added_ranges) VALUES ($1, $2, $3, 'modified', '[]')",
                               dep, pid, path)
    yield pid, other, dep
    async with get_connection() as conn:
        for p in (pid, other):
            for table in ("usage_events", "usage_daily", "budget_state", "outbox", "alerts", "deploy_reviews", "deploy_files", "deploys"):
                await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", p)
            await conn.execute("DELETE FROM projects WHERE id = $1", p)
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_rules_review_is_stored_encrypted_and_served_to_the_project_only(deploy):
    pid, other, dep = deploy
    async with project_transaction(pid) as conn:
        result = await run_deploy_review(conn, pid, dep)
        stored = await conn.fetchrow("SELECT status, risk, result_enc FROM deploy_reviews WHERE deploy_id = $1", dep)
        again = await get_deploy_review(conn, pid, dep)
    assert result["risk"] == "high" and result["source"] == "rules"
    assert (stored["status"], stored["risk"]) == ("rules", "high")
    assert b"order.discount" not in bytes(stored["result_enc"])  # it quotes code: encrypted only
    assert again["findings"] == result["findings"]

    from app.main import app

    try:
        app.dependency_overrides[require_read_project_id] = lambda: pid
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            mine = await client.get(f"/api/deploys/{dep}/review")
        app.dependency_overrides[require_read_project_id] = lambda: other
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            foreign = await client.get(f"/api/deploys/{dep}/review")
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
    assert mine.status_code == 200 and mine.json()["risk"] == "high"
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_live_verdict_alert_carries_the_review_line(deploy):
    from app.analysis import dispatch_verdict_alert

    pid, _, dep = deploy
    async with project_transaction(pid) as conn:
        await run_deploy_review(conn, pid, dep)
        await dispatch_verdict_alert(conn=conn, project_id=pid, deploy_id=dep, verdict="watch",
                                     reasons=["error count jumped to 12 vs 0 before"], kind="verdict_first")
        payload = await conn.fetchval("SELECT payload FROM outbox WHERE project_id = $1 AND dedupe_key LIKE $2", pid, f"alert:{dep}:%")
    text = json.loads(payload)["text"] if isinstance(payload, str) else payload["text"]
    assert "Risk review: HIGH: " in text


@pytest.mark.asyncio
async def test_live_ai_adds_findings_and_is_tracked_as_our_cost(deploy, monkeypatch):
    pid, _, dep = deploy
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "DRY_RUN_LLM", False)
    llm = AsyncMock()
    llm.model = "claude-sonnet-5-5"
    llm.last_usage = (12_000, 900)
    llm.complete.return_value = json.dumps({
        "risk": "high", "summary": "Totals divide by a discount that is 0 for most orders.",
        "findings": [{"file": "app/billing.py", "line": 40, "severity": "high",
                      "issue": "Division by order.discount, which is 0 without a discount", "why": "ZeroDivisionError at checkout"}],
        "tests_to_add": ["total_cents with discount=0"]})
    with patch("app.llm.get_llm_client", return_value=llm):
        async with project_transaction(pid) as conn:
            result = await run_deploy_review(conn, pid, dep)
            status = await conn.fetchval("SELECT status FROM deploy_reviews WHERE deploy_id = $1", dep)
            usage = await conn.fetchrow("SELECT meter, our_cost_micros, billed_micros FROM usage_events WHERE project_id = $1", pid)
    assert result["source"] == "rules+ai" and status == "done"
    assert any(f["source"] == "ai" and "order.discount" in f["issue"] for f in result["findings"])
    assert result["tests_to_add"] == ["total_cents with discount=0"]
    assert (usage["meter"], usage["our_cost_micros"], usage["billed_micros"]) == ("ai_review", 33_000, 0)
    sent_prompt = llm.complete.call_args.kwargs["prompt"]
    assert "[REDACTED_SECRET]" in sent_prompt  # the diff went out scrubbed


@pytest.mark.asyncio
async def test_live_investigator_runs_a_queued_review_job(deploy):
    from unittest.mock import MagicMock

    from app.investigator.worker import INVESTIGATIONS_PROCESSING, process_next_investigation

    pid, _, dep = deploy
    job = json.dumps({"kind": "deploy_review", "project_id": pid, "deploy_id": dep})
    redis = MagicMock()
    redis.blmove = AsyncMock(return_value=job)
    redis.lrem = AsyncMock()
    assert await process_next_investigation(redis) is True
    redis.lrem.assert_awaited_once_with(INVESTIGATIONS_PROCESSING, 1, job)
    async with project_transaction(pid) as conn:
        assert await conn.fetchval("SELECT risk FROM deploy_reviews WHERE deploy_id = $1", dep) == "high"
