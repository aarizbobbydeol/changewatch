"""Risk review of each deploy's diff: the rules, the AI pass, storage and where it shows."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import require_read_project_id
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.review import get_deploy_review, review_line, rules_review, run_deploy_review
from app.scrub import scrub_text

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
+API_KEY = "[REDACTED_SECRET_KEY]"
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


def _diff(path, added, removed=()):
    """A one-hunk diff of a new or changed file: the removed lines, then the added ones."""
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"


def _issues(diff, path):
    return [(f["line"], f["issue"]) for f in rules_review(diff, [path])]


def test_scrubbed_names_emails_and_ips_are_not_a_committed_secret():
    # The stored diff is scrubbed: a value assigned to a secret-sounding name becomes [REDACTED_SECRET],
    # emails and IPs become [REDACTED_EMAIL]/[REDACTED_IP]. None of that means a secret was committed.
    lines = ["self.token = token", "token = get_token(request)", "self.password = password",
             "auth = HTTPBasicAuth(user, pw)", "api_key: str = Field(...)", 'contact = "jane@example.com"',
             'host = "10.0.0.12"']
    assert _issues(scrub_text(_diff("app/auth.py", lines)), "app/auth.py") == []
    assert _issues(scrub_text(_diff("src/a.ts", ["const token = req.headers.authorization;"])), "src/a.ts") == []
    # A recognisable key keeps its own label after scrubbing, and that is reported
    leaked = scrub_text(_diff("app/settings.py", ['AWS_KEY = "AKIAIOSFODNN7EXAMPLE"', "GH_TOKEN = ghp_" + "a" * 36]))
    assert [line for line, issue in _issues(leaked, "app/settings.py") if issue.startswith("A secret was committed")] == [1, 2]


def test_rules_leave_method_calls_english_regex_literals_and_docstrings_alone():
    quiet = [
        ("src/a.js", ["const m = pattern.exec(line);"]),
        ("src/users.ts", ["const users = await User.find(q).exec();"]),
        ("app/a.py", ["rows = session.exec(select(User))"]),
        ("app/a.py", ['raise ValueError(f"Could not update {name}")']),
        ("app/a.py", ['msg = f"Select {count} items"']),
        ("app/a.py", ['msg = "Could not delete {}".format(name)']),
        ("src/a.js", ['s = s.replace(/[^a-z]/g, "-");']),
        ("src/a.js", ["if (/chrome/i.test(ua)) {}"]),
        ("app/a.py", ["def bounds(xs):", '    """The smallest and the largest value.', "", "    Returns the min and/or max", '    """']),
        ("src/a.ts", ["/**", " * Returns the min and/or max", " */"]),
    ]
    for path, lines in quiet:
        assert _issues(_diff(path, lines), path) == [], lines
    # Still found: a plain eval/exec call, SQL built from strings, a division after a docstring
    assert _issues(_diff("app/a.py", ["exec(code)"]), "app/a.py") == [(1, "Runs code built at runtime (eval/exec)")]
    assert _issues(_diff("app/a.py", ['rows = db.fetch(f"select * from users where id = {uid}")']), "app/a.py") == [
        (1, "Builds SQL from an f-string (injection risk)")]
    assert _issues(_diff("app/a.py", ['q = "UPDATE users SET name = {}".format(n)']), "app/a.py") == [
        (1, "Builds SQL with .format() (injection risk)")]
    assert _issues(_diff("app/a.py", ["def mean(xs):", '    """The mean."""', "    return sum(xs) / count"]), "app/a.py") == [
        (3, "Divides by a value that may be zero")]


def test_a_tightened_or_rewritten_check_is_not_a_removed_check():
    tightened = _diff("app/v.py", ["if user is None or user.deleted:"], removed=["if user is None:"])
    rewritten = _diff("app/v.py", ["if len(items) == 0:"], removed=["if not items:"])
    replaced = _diff("app/v.py", ["if not user.email:"], removed=["if user is None:"])
    assert _issues(tightened, "app/v.py") == []
    assert _issues(rewritten, "app/v.py") == []
    assert _issues(replaced, "app/v.py") == [(None, "Removes a check: `if user is None:`")]


def test_except_then_pass_on_the_next_line_swallows_every_exception():
    added = _diff("app/jobs.py", ["try:", "    run()", "except Exception:", "    pass"])
    assert _issues(added, "app/jobs.py") == [(3, "Swallows every exception")]
    # The except line changed, the pass below it did not
    widened = ("diff --git a/app/jobs.py b/app/jobs.py\n@@ -1,4 +1,4 @@\n try:\n     run()\n"
               "-except ValueError:\n+except Exception as exc:\n     pass\n")
    assert _issues(widened, "app/jobs.py") == [(3, "Swallows every exception")]


def test_added_lines_starting_with_plus_plus_or_minus_minus_keep_line_numbers():
    diff = ("diff --git a/a.js b/a.js\n--- a/a.js\n+++ b/a.js\n@@ -1,1 +1,3 @@\n--- old note\n"
            "+++i;\n+--j;\n+const r = a / b;\n")
    assert _issues(diff, "a.js") == [(3, "Divides by a value that may be zero")]
    quoted = 'diff --git "a/my app.py" "b/my app.py"\n--- "a/my app.py"\n+++ "b/my app.py"\n@@ -0,0 +1,1 @@\n+eval(x)\n'
    assert [f["file"] for f in rules_review(quoted, [])] == ["my app.py"]


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
        from app.outbox import read_payload

        payload = await read_payload(await conn.fetchrow(
            "SELECT * FROM outbox WHERE project_id = $1 AND dedupe_key LIKE $2", pid, f"alert:{dep}:%"))
    text = payload["text"]
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
    assert "[REDACTED_SECRET_KEY]" in sent_prompt  # the diff went out scrubbed


@pytest.mark.asyncio
async def test_live_investigator_runs_a_queued_review_job(deploy):
    from app.investigator.worker import REVIEWS_PROCESSING, process_next_investigation

    pid, _, dep = deploy
    job = json.dumps({"kind": "deploy_review", "project_id": pid, "deploy_id": dep})
    redis = MagicMock()
    redis.lmove = AsyncMock(side_effect=[None, job])  # no investigation waiting, then the review
    redis.lrem = AsyncMock()
    assert await process_next_investigation(redis) is True
    redis.lrem.assert_awaited_once_with(REVIEWS_PROCESSING, 1, job)
    async with project_transaction(pid) as conn:
        assert await conn.fetchval("SELECT risk FROM deploy_reviews WHERE deploy_id = $1", dep) == "high"


@pytest.mark.asyncio
async def test_live_a_long_ai_answer_is_trimmed_not_thrown_away(deploy, monkeypatch):
    pid, _, dep = deploy
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "DRY_RUN_LLM", False)
    llm = AsyncMock()
    llm.model = "claude-sonnet-5-5"
    llm.last_usage = (12_000, 3_000)
    answer = {"risk": "high", "summary": "s" * 700, "tests_to_add": [f"test {i}" for i in range(6)],
              "findings": [{"file": "app/billing.py", "line": 40 + i, "severity": "critical" if i == 9 else "high",
                            "issue": f"AI issue {i} " + "x" * 400, "why": "w" * 600} for i in range(10)]}
    llm.complete.return_value = "Here is the review:\n" + json.dumps(answer)  # a preamble before the JSON
    with patch("app.llm.get_llm_client", return_value=llm):
        async with project_transaction(pid) as conn:
            result = await run_deploy_review(conn, pid, dep)
    ai = [f for f in result["findings"] if f["source"] == "ai"]
    assert result["source"] == "rules+ai" and result["risk"] == "high"
    assert 0 < len(ai) <= 8 and all(len(f["issue"]) <= 300 and len(f["why"]) <= 500 for f in ai)
    assert len(result["summary"]) <= 600 and len(result["tests_to_add"]) == 5
    assert "at most 8 findings" in llm.complete.call_args.kwargs["system"]  # the model is told the limits


@pytest.mark.asyncio
async def test_live_code_retention_removes_old_reviews(deploy):
    # A review quotes lines of the diff, so it goes when the code goes (CODE_RETENTION_DAYS)
    from app.retention import _cleanup_with_conn

    pid, _, dep = deploy
    async with project_transaction(pid) as conn:
        await run_deploy_review(conn, pid, dep)
        await conn.execute("UPDATE deploy_reviews SET created_at = '2001-01-01T00:00:00Z' WHERE deploy_id = $1", dep)
    # Cutoffs far in the past, so only this test's backdated review is old enough (the database is shared)
    events_cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
    async with get_connection() as conn:
        await _cleanup_with_conn(conn, events_cutoff, 5000, datetime(2000, 6, 1, tzinfo=timezone.utc))
    async with project_transaction(pid) as conn:
        assert await get_deploy_review(conn, pid, dep) is not None  # newer than the code cutoff: kept
    async with get_connection() as conn:
        stats = await _cleanup_with_conn(conn, events_cutoff, 5000, datetime(2001, 6, 1, tzinfo=timezone.utc))
    async with project_transaction(pid) as conn:
        assert await get_deploy_review(conn, pid, dep) is None
    assert stats["code_records_cleaned"] >= 1


@pytest.mark.asyncio
async def test_live_deploy_reviews_wait_until_no_investigation_is_queued(monkeypatch):
    """Each review can be a minute-long AI call: an incident queued after a burst of deploys goes first."""
    from contextlib import asynccontextmanager

    import redis.asyncio as aioredis

    from app.investigator import worker

    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
    except Exception:
        pytest.skip("Redis not running. Skipping live queue test.")
    run = uuid.uuid4().hex[:8]
    keys = {name: f"test-{run}-{name.lower()}" for name in (
        "INVESTIGATIONS_QUEUE", "INVESTIGATIONS_PROCESSING", "INVESTIGATIONS_DEAD",
        "REVIEWS_QUEUE", "REVIEWS_PROCESSING", "REVIEWS_DEAD")}
    for name, key in keys.items():
        monkeypatch.setattr(worker, name, key, raising=False)
    order = []

    @asynccontextmanager
    async def no_db(project_id):
        yield None

    async def review(conn, project_id, deploy_id):
        order.append(("review", deploy_id))

    async def investigate(conn, project_id, incident_id, deep_mode=False):
        order.append(("incident", incident_id))
        if order.count(("incident", 98)) == 1 and incident_id == 98:
            raise RuntimeError("try again")

    monkeypatch.setattr(worker, "project_transaction", no_db)
    monkeypatch.setattr(worker, "execute_investigation", investigate)
    monkeypatch.setattr("app.review.run_deploy_review", review)
    try:
        for dep in range(1, 4):
            await redis.lpush(keys["REVIEWS_QUEUE"], json.dumps({"kind": "deploy_review", "project_id": 1, "deploy_id": dep}))
        await redis.lpush(keys["INVESTIGATIONS_QUEUE"], json.dumps({"project_id": 1, "incident_id": 99}))
        assert await worker.process_next_investigation(redis, timeout=1) is True
        await redis.lpush(keys["INVESTIGATIONS_QUEUE"], json.dumps({"project_id": 1, "incident_id": 98}))
        for _ in range(10):
            if not await redis.llen(keys["REVIEWS_QUEUE"]) and not await redis.llen(keys["INVESTIGATIONS_QUEUE"]):
                break
            await worker.process_next_investigation(redis, timeout=1)
        # 98 failed once and was retried before the reviews; nothing is left in flight
        assert order == [("incident", 99), ("incident", 98), ("incident", 98),
                         ("review", 1), ("review", 2), ("review", 3)]
        assert not await redis.llen(keys["REVIEWS_PROCESSING"]) and not await redis.llen(keys["INVESTIGATIONS_PROCESSING"])

        # A review interrupted by a crash goes back on the review queue at startup
        await redis.lpush(keys["REVIEWS_PROCESSING"], json.dumps({"kind": "deploy_review", "project_id": 1, "deploy_id": 4}))
        assert await worker.requeue_interrupted_investigations(redis) == 1
        assert await redis.llen(keys["REVIEWS_QUEUE"]) == 1
    finally:
        await redis.delete(*keys.values())
        await redis.aclose()


@pytest.mark.asyncio
async def test_live_new_deploy_queues_its_review_on_the_review_queue(deploy):
    from app.auth import require_ingest_project_id
    from app.investigator import worker
    from app.main import app

    pid, _, _ = deploy
    redis = MagicMock()
    redis.lpush = AsyncMock()
    try:
        app.dependency_overrides[require_ingest_project_id] = lambda: pid
        with patch("app.main.get_redis", return_value=redis):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/v1/deploys", json={"commit_sha": f"q{uuid.uuid4().hex[:8]}", "environment": "staging"})
    finally:
        app.dependency_overrides.pop(require_ingest_project_id, None)
    assert resp.status_code == 200
    queue, job = redis.lpush.await_args.args
    assert queue == getattr(worker, "REVIEWS_QUEUE", None)
    assert json.loads(job) == {"kind": "deploy_review", "project_id": pid, "deploy_id": resp.json()["deploy_id"]}
