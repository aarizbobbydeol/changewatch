"""Memory of past incidents (live Postgres): what counts as similar, what is remembered about it,
and where it shows up (the alert, the investigation, the incident API)."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.notifiers.incident_alert import format_incident_instant_message
from app.trust.memory import find_similar_incidents, seen_before_line

NOW = datetime.now(timezone.utc).replace(microsecond=0)


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def history():
    """A project with past incidents of every kind, and one other project. Yields ids."""
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    run = uuid.uuid4().hex[:8]
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"mem-{run}")
        other = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"mem-o-{run}")
    ids = {}

    async def incident(conn, project, key, *, issue, file, func, title, days_ago, resolved_after_min=None):
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, $2, $3) RETURNING id",
                                  project, f"{key}{run}", NOW - timedelta(days=days_ago, minutes=5))
        created = NOW - timedelta(days=days_ago)
        ids[key] = await conn.fetchval(
            """INSERT INTO incidents (project_id, deploy_id, issue_id, status, title, where_file, where_line,
                   where_function, created_at, first_event_at, resolved_at)
               VALUES ($1, $2, $3, $4, $5, $6, 42, $7, $8, $8, $9) RETURNING id""",
            project, dep, issue, "resolved" if resolved_after_min else "confirmed", title, file, func, created,
            created + timedelta(minutes=resolved_after_min) if resolved_after_min else None)
        return ids[key]

    async with project_transaction(pid) as conn:
        issue_a = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'ZeroDivisionError: division by zero', now(), now(), 'open', 1) RETURNING id", pid, f"a{run}")
        issue_b = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'KeyError: x', now(), now(), 'open', 1) RETURNING id", pid, f"b{run}")
        same = await incident(conn, pid, "same", issue=issue_a, file="app/billing.py", func="total_cents",
                              title="ZeroDivisionError: division by zero", days_ago=30, resolved_after_min=25)
        await conn.execute("INSERT INTO incident_events (incident_id, project_id, type, actor, detail, at) "
                           "VALUES ($1, $2, 'resolved', 'auto_resolver', $3, $4)", same, pid,
                           json.dumps({"auto_resolve": True, "fix_note": "likely fixed by deploy 1a2b3c4d"}),
                           NOW - timedelta(days=30) + timedelta(minutes=25))
        await conn.execute("INSERT INTO investigations (incident_id, project_id, status, result) VALUES ($1, $2, 'done', $3)",
                           same, pid, json.dumps({"cause": "Division by order.discount, which is 0 without a discount."}))
        await conn.execute("INSERT INTO alert_feedback (incident_id, project_id, actor, label) VALUES ($1, $2, 't', 'real')", same, pid)
        await incident(conn, pid, "func", issue=issue_b, file="app/billing.py", func="total_cents",
                       title="KeyError: x", days_ago=10)
        await incident(conn, pid, "file", issue=issue_b, file="app/billing.py", func="other_fn", title="KeyError: x", days_ago=5)
        await incident(conn, pid, "elsewhere", issue=issue_b, file="app/users.py", func="f",
                       title="ZeroDivisionError: other", days_ago=3)
        await incident(conn, pid, "ancient", issue=issue_a, file="app/billing.py", func="total_cents",
                       title="ZeroDivisionError: division by zero", days_ago=400)
    async with project_transaction(other) as conn:
        issue_o = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'ZeroDivisionError: division by zero', now(), now(), 'open', 1) RETURNING id", other, f"o{run}")
        await incident(conn, other, "foreign", issue=issue_o, file="app/billing.py", func="total_cents",
                       title="ZeroDivisionError: division by zero", days_ago=2)
    yield {"pid": pid, "other": other, "issue_a": issue_a, **ids}
    async with get_connection() as conn:
        for p in (pid, other):
            for table in ("alert_feedback", "investigations", "incident_events", "outbox", "incidents", "events", "issues", "deploys"):
                await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", p)
            await conn.execute("DELETE FROM projects WHERE id = $1", p)
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_similar_means_same_error_function_or_file_in_this_project(history):
    async with project_transaction(history["pid"]) as conn:
        similar = await find_similar_incidents(
            conn, history["pid"], issue_id=history["issue_a"], where_file="app/billing.py",
            where_function="total_cents", title="ZeroDivisionError: division by zero", limit=10)
    assert [(s["id"], s["match"]) for s in similar] == [
        (history["same"], "the same error"), (history["func"], "the same function"), (history["file"], "the same file")]
    best = similar[0]  # older than 180 days, another file, another project: not remembered
    assert best["resolution"] == "resolved automatically (likely fixed by deploy 1a2b3c4d)"
    assert best["resolved_after"] == "25 min" and best["rated"] == "a real problem"
    assert best["cause"].startswith("Division by order.discount")
    assert seen_before_line(similar) == (
        f"Seen before: incident #{history['same']} on {best['when'][:10]} (the same error), "
        "resolved automatically (likely fixed by deploy 1a2b3c4d) after 25 min.")


@pytest.mark.asyncio
async def test_live_new_incident_alert_says_it_was_seen_before(history):
    from app.incident_manager import check_and_trigger_incident

    pid = history["pid"]
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, 'newdeploy01', $2) RETURNING id",
                                  pid, NOW - timedelta(minutes=5))
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'KeyError: y', now(), now(), 'open', 3) RETURNING id", pid, uuid.uuid4().hex)
        for i in range(3):
            await conn.execute("INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment) "
                               "VALUES ($1, $2, $3, $4, $5, 'prod')", uuid.uuid4().hex, issue, pid, dep, NOW - timedelta(seconds=3 - i))
        trace = 'Traceback (most recent call last):\n  File "app/billing.py", line 40, in total_cents\n    x[y]\nKeyError: y\n'
        with patch("app.incident_manager.dispatch_incident_alert", new=AsyncMock()) as dispatch, \
             patch("app.trust.calm.is_issue_muted", new=AsyncMock(return_value=(False, None))):
            await check_and_trigger_incident(conn=conn, project_id=pid, deploy_id=dep, issue_id=issue,
                                             payload={"request_path": "/x", "stack_trace": trace, "exc_type": "KeyError"},
                                             occurred_at_dt=NOW, new_events=3)
    line = dispatch.call_args.kwargs["triage_info"]["seen_before"]
    assert line.startswith(f"Seen before: incident #{history['func']}") and "(the same function)" in line


def test_alert_text_carries_the_seen_before_line():
    msg = format_incident_instant_message(
        status="possible", commit_sha="abcdef12", author="a", message="m", error_title="KeyError: y", event_count=3,
        where_file="app/billing.py", where_line=40, where_function="total_cents", in_diff=False, owners=[],
        dashboard_url="https://cw.example/incidents/1", triage_info={"seen_before": "Seen before: incident #9 on 2026-10-01 (the same function)."})
    assert "Seen before: incident #9" in msg["text"]
    assert "Seen before: incident #9" in json.dumps(msg["blocks"])
