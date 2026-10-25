"""Who owns the failing code, what the investigator is told, and what the reports count."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio

from app.codeowners import parse_codeowners
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.incident_manager import _repo_relative
from app.sre_prep import StackFrame


# --- CODEOWNERS (GitHub semantics) ------------------------------------------------------------

def test_directory_patterns_own_what_is_inside():
    owners = parse_codeowners(
        "* @org/platform\n"
        "/src/payments @org/payments\n"
        "**/migrations @org/dba\n"
        "docs/* @org/docs\n"
        "*.js @js-owner #This is an inline comment.\n"
        "/apps/ @org/apps\n"
        "/apps/github\n"
    )
    assert owners.resolve_owners("src/payments/charge.py") == ["@org/payments"]
    assert owners.resolve_owners("app/migrations/0007.py") == ["@org/dba"]
    assert owners.resolve_owners("migrations/0001.py") == ["@org/dba"]
    assert owners.resolve_owners("docs/intro.md") == ["@org/docs"]
    assert owners.resolve_owners("docs/api/intro.md") == ["@org/platform"]  # docs/* is direct children only
    assert owners.resolve_owners("web/app.js") == ["@js-owner"]  # the comment is not an owner
    assert owners.resolve_owners("apps/github/x.py") == []  # a rule with no owners takes ownership away
    assert owners.resolve_owners("apps/other/x.py") == ["@org/apps"]


# --- Repository paths from server paths -------------------------------------------------------

def _f(path, line=1):
    return StackFrame(file=path, line=line, function="f", code="")


def test_unchanged_file_gets_its_repo_path_from_a_changed_one():
    frames = [_f("/srv/app/app/api/routes.py"), _f("/srv/app/app/billing/charge.py")]
    assert _repo_relative(frames, frames[-1], ["app/api/routes.py"]) == "app/billing/charge.py"


def test_longest_match_wins_over_a_same_named_file():
    frames = [_f("/srv/app/app/billing.py"), _f("/srv/app/app/other/billing.py")]
    # "billing.py" at the root would put the root at /srv/app/app/other/; app/billing.py is longer
    assert _repo_relative(frames, frames[0], ["billing.py", "app/billing.py"]) == "app/billing.py"


# --- Dependency changes -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_changed_dependency_lines_are_reported():
    from app.investigator.tools import get_dependency_changes

    requirements = "\n".join(["aiohttp==3.9.0", "anyio==4.0", "boto3==1.30", "sqlalchemy==2.0.30", "uvicorn==0.30"])
    package_json = '{\n  "name": "shop",\n  "scripts": {\n    "build": "next build"\n  },\n  "dependencies": {\n    "next": "16.0.0",\n    "react": "19.2.0"\n  }\n}'
    pyproject = '[project]\nname = "shop"\nversion = "1.0"\ndependencies = [\n    "httpx>=0.28",\n    "pydantic>=2",\n]\n'
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"path": "requirements.txt", "status": "modified", "content": requirements, "content_enc": None, "added_ranges": [[4, 4]]},
        {"path": "web/package.json", "status": "modified", "content": package_json, "content_enc": None, "added_ranges": [[8, 8]]},
        {"path": "pyproject.toml", "status": "modified", "content": pyproject, "content_enc": None, "added_ranges": [[2, 3], [6, 6]]},
    ]
    changes = await get_dependency_changes(conn, project_id=1, deploy_id=1)
    assert [(c["file"], c["package"]) for c in changes] == [
        ("requirements.txt", "sqlalchemy"), ("web/package.json", "react"), ("pyproject.toml", "pydantic")]


# --- What leaves for the AI provider ----------------------------------------------------------

@pytest.mark.asyncio
async def test_secret_guard_runs_on_every_outbound_prompt(monkeypatch):
    from app import llm

    monkeypatch.setenv("SECRET_GUARD_ENABLED", "true")
    client = llm.AnthropicClient.__new__(llm.AnthropicClient)
    client.model, client.effort, client.last_usage = "m", "low", None
    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        resp = MagicMock()
        resp.content = []
        resp.usage = MagicMock(input_tokens=1, output_tokens=1)
        return resp

    client.client = MagicMock()
    client.client.beta.messages.create = create
    with patch("app.llm.apply_secret_guard", wraps=llm.apply_secret_guard) as guard:
        await client.complete("system", "token: abcdefghijklmnopqrstuvwxyz0123", 100)
    assert guard.call_count == 2  # system and prompt
    assert "abcdefghijklmnopqrstuvwxyz0123" not in str(sent["messages"])


@pytest.mark.asyncio
async def test_rules_triage_reads_the_error_text():
    from app.decisions.catalog import Q_CATEGORY, get_triage_questions
    from app.decisions.engine import build_decision_state
    from app.decisions.rules_provider import RulesDecisionProvider

    state = build_decision_state(
        error_title="RuntimeError: Missing environment variable STRIPE_KEY in settings", exc_type="RuntimeError",
        frames=[], request_path="/pay", event_count=5, in_diff=False, is_critical=False, commit_message="x")
    answers = await RulesDecisionProvider().decide(state, get_triage_questions())
    assert answers[Q_CATEGORY].choice == "config_or_env"


# --- Small commands and messages --------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_project_needs_purge(capsys):
    from app.cli import delete_project_cmd

    with patch("app.cli.init_db_pool", new=AsyncMock()) as pool, pytest.raises(SystemExit) as exit_info:
        await delete_project_cmd(3, purge=False)
    assert exit_info.value.code == 2
    pool.assert_not_awaited()
    assert "--purge" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_email_subject_says_what_the_message_is():
    from app.notifiers.channels import EmailNotifier

    subjects = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def has_extn(self, name):
            return False

        def sendmail(self, sender, to, raw):
            subjects.append([line for line in raw.splitlines() if line.startswith("Subject:")][0])

        send_message = sendmail

    config = {"to_addrs": ["oncall@example.com"], "smtp_tls": "none"}
    with patch("smtplib.SMTP", FakeSMTP):
        for payload in ({"type": "weekly_digest", "text": "digest"},
                        {"type": "incident_resolved", "title": "✅ Resolved: KeyError", "text": "ok"},
                        {"text": "⚠️ Possible regression ..."}):
            try:
                await EmailNotifier().send(payload, config)
            except Exception:
                pass
    assert subjects[:3] == ["Subject: ChangeWatch weekly digest", "Subject: =?utf-8?b?4pyFIFJlc29sdmVkOiBLZXlFcnJvcg==?=",
                            "Subject: ChangeWatch Regression Alert"] or (
        "digest" in subjects[0] and "Regression" in subjects[2] and "Regression" not in subjects[1])


# --- Live: owners, digests, failure rate, reopen, calibration ---------------------------------

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
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"own-{uuid.uuid4().hex[:8]}")
    yield pid
    async with get_connection() as conn:
        for table in ("decisions", "alert_feedback", "incident_events", "outbox", "incidents", "events", "event_rollups",
                      "deploy_analysis", "deploy_files", "issues", "deploys"):
            try:
                await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", pid)
            except asyncpg.UndefinedColumnError:
                await conn.execute(f"DELETE FROM {table} WHERE deploy_id IN (SELECT id FROM deploys WHERE project_id = $1)", pid)
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_error_in_an_unchanged_file_gets_its_owners_and_repo_path(project):
    from app.incident_manager import check_and_trigger_incident

    now = datetime.now(timezone.utc)
    trace = ('Traceback (most recent call last):\n  File "/srv/app/app/api/routes.py", line 9, in pay\n    charge()\n'
             '  File "/srv/app/app/billing/charge.py", line 30, in charge\n    raise KeyError("card")\nKeyError: card\n')
    async with project_transaction(project) as conn:
        dep = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at, codeowners) "
            "VALUES ($1, 'own1', 'prod', $2, $3) RETURNING id",
            project, now - timedelta(minutes=5), "* @core\n/app/billing/ @billing\n")
        await conn.execute("INSERT INTO deploy_files (deploy_id, project_id, path, status, added_ranges) "
                           "VALUES ($1, $2, 'app/api/routes.py', 'modified', '[[9, 9]]')", dep, project)
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, 'fp-own', 'KeyError: card', now(), now(), 'open', 3) RETURNING id", project)
        for i in range(3):
            await conn.execute("INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment) "
                               "VALUES ($1, $2, $3, $4, $5, 'prod')", f"own-{i}-{uuid.uuid4().hex}", issue, project, dep,
                               now - timedelta(seconds=3 - i))
        with patch("app.incident_manager.dispatch_incident_alert", new=AsyncMock()) as dispatch, \
             patch("app.trust.calm.is_issue_muted", new=AsyncMock(return_value=(False, None))):
            await check_and_trigger_incident(conn=conn, project_id=project, deploy_id=dep, issue_id=issue,
                                             payload={"request_path": "/pay", "stack_trace": trace, "exc_type": "KeyError"},
                                             occurred_at_dt=now, new_events=3)
    kwargs = dispatch.call_args.kwargs
    assert kwargs["where_file"] == "app/billing/charge.py"  # not /srv/app/...
    assert kwargs["raw_owners"] == ["@billing"]  # not just the catch-all @core
    assert kwargs["in_diff"] is False


@pytest.mark.asyncio
async def test_live_digest_counts_its_environment_fix_minutes_and_ever_confirmed(project):
    from app.trust.digest import generate_weekly_digest

    now = datetime.now(timezone.utc)
    async with project_transaction(project) as conn:
        ids = {}
        for env in ("prod", "staging"):
            ids[env] = await conn.fetchval(
                "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES ($1, $2, $3, $4) RETURNING id",
                project, f"d-{env}", env, now - timedelta(days=2))
        async def issue(n):
            return await conn.fetchval(
                "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
                "VALUES ($1, $2, 'E', now(), now(), 'open', 1) RETURNING id", project, f"fp-dig-{n}")
        created = now - timedelta(days=1)
        # prod: three incidents, confirmed and resolved after 30, 45 and 50 minutes
        for n, minutes in enumerate((30, 45, 50)):
            await conn.execute(
                "INSERT INTO incidents (project_id, deploy_id, issue_id, status, created_at, first_event_at, last_event_at, "
                "confirmed_at, resolved_at) VALUES ($1, $2, $3, 'resolved', $4, $4, $5, $4, $5)",
                project, ids["prod"], await issue(f"p{n}"), created, created + timedelta(minutes=minutes))
        for n in range(1, 5):  # staging: four open incidents
            await conn.execute(
                "INSERT INTO incidents (project_id, deploy_id, issue_id, status, created_at) VALUES ($1, $2, $3, 'possible', $4)",
                project, ids["staging"], await issue(n), created)
        digest = await generate_weekly_digest(conn, project, environment="prod", now_dt=now)
    text = digest.get("text") or digest.get("formatted_text") or str(digest)
    assert "3 incident(s)" in text  # staging's four are not in the prod digest
    assert "median fix time: 45 min" in text  # minutes, not minutes divided by 60


@pytest.mark.asyncio
async def test_live_reopened_incident_is_not_resolved_again_at_once(project):
    from app.trust.actions import execute_incident_action
    from app.trust.calm import run_calm_auto_resolve

    now = datetime.now(timezone.utc)
    async with project_transaction(project) as conn:
        dep = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES ($1, 'ro', 'prod', $2) RETURNING id",
            project, now - timedelta(hours=3))
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, 'fp-ro', 'E', now(), now(), 'open', 1) RETURNING id", project)
        inc = await conn.fetchval(
            "INSERT INTO incidents (project_id, deploy_id, issue_id, status, last_event_at, resolved_at) "
            "VALUES ($1, $2, $3, 'resolved', $4, $4) RETURNING id", project, dep, issue, now - timedelta(hours=2))
        await execute_incident_action(conn=conn, project_id=project, incident_id=inc, action="reopen", actor="t")
    async with get_connection() as conn:
        resolved = [r["incident_id"] for r in await run_calm_auto_resolve(conn, now_dt=datetime.now(timezone.utc))]
        status = await conn.fetchval("SELECT status FROM incidents WHERE id = $1", inc)
    assert inc not in resolved and status == "confirmed"


@pytest.mark.asyncio
async def test_live_calibration_counts_each_incident_once(project):
    from app.trust.calibration import compute_decision_calibration

    async with project_transaction(project) as conn:
        dep = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha) VALUES ($1, 'cal') RETURNING id", project)
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, 'fp-cal', 'E', now(), now(), 'open', 1) RETURNING id", project)
        inc = await conn.fetchval(
            "INSERT INTO incidents (project_id, deploy_id, issue_id, status) VALUES ($1, $2, $3, 'possible') RETURNING id",
            project, dep, issue)
        await conn.execute(
            "INSERT INTO decisions (project_id, incident_id, question_id, kind, answer, probability, source, "
            "catalog_version, state_hash) VALUES ($1, $2, 'is_regression_from_release', 'noul', '{}', 0.92, 'rules', "
            "'1', 'h')", project, inc)
        for i in range(10):
            await conn.execute("INSERT INTO alert_feedback (incident_id, project_id, actor, label) VALUES ($1, $2, $3, 'false')",
                               inc, project, f"user{i}")
        result = await compute_decision_calibration(conn, project)
    assert result["total_samples"] == 1  # ten people rating one incident is still one data point
