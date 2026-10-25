"""The incident timeline and the postmortem draft, from real rows (live Postgres)."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import require_read_project_id
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.trust.postmortem import _duration, build_postmortem
from app.trust.timeline import build_incident_timeline

T0 = datetime(2026, 10, 20, 14, 0, 0, tzinfo=timezone.utc)


def test_durations_read_like_a_person_wrote_them():
    assert [_duration(s) for s in (12, 600, 3900, 7200, 93600)] == ["12 s", "10 min", "1 h 5 min", "2 h", "1 d 2 h"]


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def incident():
    """A resolved incident with everything a real one has: deploy, events, traffic, alerts,
    investigation, verdict, acknowledgement, rating and auto-resolve. Yields (project, other project, id)."""
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    run = uuid.uuid4().hex[:8]
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"pm-{run}")
        other = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"pm-other-{run}")
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at, author, message) "
            "VALUES ($1, 'abcdef1234567', 'prod', $2, 'dev@acme.example', 'Apply discount to totals\nlong body') RETURNING id",
            pid, T0)
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'ZeroDivisionError: division by zero', $3, $4, 'open', 42) RETURNING id",
            pid, f"fp-{run}", T0 + timedelta(seconds=30), T0 + timedelta(minutes=20))
        inc = await conn.fetchval(
            """INSERT INTO incidents (project_id, deploy_id, issue_id, status, title, where_file, where_line,
                   where_function, in_diff, owners, event_count, category, created_at, confirmed_at, resolved_at,
                   first_event_at, last_event_at, first_alert_ms)
               VALUES ($1, $2, $3, 'resolved', 'ZeroDivisionError: division by zero', 'app/billing.py', 42,
                   'total_cents', true, '["@acme/payments"]', 42, 'logic_bug', $4, $5, $6, $7, $8, 1500)
               RETURNING id""",
            pid, dep, issue, T0 + timedelta(seconds=31), T0 + timedelta(seconds=40),
            T0 + timedelta(minutes=55), T0 + timedelta(seconds=30), T0 + timedelta(minutes=20))
        for i, path in enumerate(["/checkout"] * 3 + ["/cart"]):
            await conn.execute(
                "INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment, request_path) "
                "VALUES ($1, $2, $3, $4, $5, 'prod', $6)", f"{run}-{i}", issue, pid, dep, T0 + timedelta(minutes=i), path)
        for m in range(0, 21):
            await conn.execute(
                "INSERT INTO traffic (project_id, environment, bucket, requests, errors, deploy_id) VALUES ($1, 'prod', $2, 100, $3, $4)",
                pid, T0 + timedelta(minutes=m), 10 if m == 5 else 2, dep)
        await conn.execute(
            "INSERT INTO outbox (project_id, channel_id, dedupe_key, payload, status, created_at, sent_at) VALUES "
            "($1, 1, $2, '{}', 'sent', $3, $4), ($1, 2, $5, '{}', 'sent', $3, $4), ($1, 1, $6, '{}', 'sent', $7, $7)",
            pid, f"incident:{inc}:possible:channel:1", T0 + timedelta(seconds=31), T0 + timedelta(seconds=32),
            f"incident:{inc}:possible:channel:2", f"incident:{inc}:confirmed:channel:1", T0 + timedelta(seconds=41))
        await conn.execute(
            "INSERT INTO investigations (incident_id, project_id, status, started_at, finished_at, result) "
            "VALUES ($1, $2, 'done', $3, $4, $5)", inc, pid, T0 + timedelta(seconds=33), T0 + timedelta(seconds=50),
            json.dumps({"cause": "The new line divides by order.discount, which is 0 without a discount.",
                        "confidence": "high", "evidence": [{"kind": "diff", "detail": "Line 42 now divides by discount"}],
                        "suggested_fix": {"description": "Guard against a zero discount", "rollback_recommended": True}}))
        await conn.execute(
            "INSERT INTO deploy_analysis (deploy_id, new_issues, errors_before, errors_after, flagged, verdict, "
            "first_verdict, verdict_updated_at) VALUES ($1, 1, 0, 42, true, 'rollback', 'rollback', $2)", dep, T0)
        await conn.execute("INSERT INTO alerts (project_id, deploy_id, channel, kind, sent_at) VALUES ($1, $2, 'slack', 'verdict_first', $3)",
                           pid, dep, T0 + timedelta(minutes=10))
        for etype, actor, detail, at in (
            ("acked", "alice@acme.example", {}, T0 + timedelta(minutes=2)),
            ("feedback", "alice@acme.example", {"label": "real"}, T0 + timedelta(minutes=3)),
            ("resolved", "auto_resolver", {"auto_resolve": True, "fix_note": "likely fixed by deploy 99887766"},
             T0 + timedelta(minutes=55)),
        ):
            await conn.execute("INSERT INTO incident_events (incident_id, project_id, type, actor, detail, at) "
                               "VALUES ($1, $2, $3, $4, $5, $6)", inc, pid, etype, actor, json.dumps(detail), at)
    yield pid, other, inc
    async with get_connection() as conn:
        for p in (pid, other):
            for table in ("incident_events", "alerts", "investigations", "outbox", "incidents", "events", "traffic",
                          "deploy_analysis", "issues", "deploys"):
                if table in ("deploy_analysis",):
                    await conn.execute("DELETE FROM deploy_analysis WHERE deploy_id IN (SELECT id FROM deploys WHERE project_id = $1)", p)
                else:
                    await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", p)
            await conn.execute("DELETE FROM projects WHERE id = $1", p)
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_timeline_tells_the_whole_story_in_order(incident):
    pid, _, inc = incident
    async with project_transaction(pid) as conn:
        timeline = await build_incident_timeline(conn, pid, inc)
    events = [t["event"] for t in timeline]
    assert events == ["deployed", "first_error", "opened", "alert_sent", "confirmed", "alert_sent", "investigated",
                      "acked", "feedback", "verdict", "last_error", "resolved"], events
    text = " | ".join(t["detail"] for t in timeline)
    assert "Deploy abcdef12 to prod: Apply discount to totals" in text
    assert "Possible-regression alert delivered to 2 channels" in text
    assert "Release verdict: ROLLBACK (first check)" in text
    assert "Resolved automatically: likely fixed by deploy 99887766" in text
    assert all(t["at"].endswith("+00:00") for t in timeline)


@pytest.mark.asyncio
async def test_live_postmortem_draft_is_complete_and_blameless(incident):
    pid, _, inc = incident
    async with project_transaction(pid) as conn:
        draft = await build_postmortem(conn, pid, inc)
    md = draft["markdown"]
    for heading in ("## Summary", "## Impact", "## Detection", "## Timeline (UTC)", "## Root cause", "## Resolution",
                    "## Action items"):
        assert heading in md
    assert "`app/billing.py:42` (a line changed in this release). 42 errors over 19 min." in md
    assert "ChangeWatch alerted 1 s after the first error" in md
    assert "Requests during the incident: 2100, of which 50 failed (2.4%); worst minute 10.0% failed" in md
    assert "`/checkout` (3), `/cart` (1)" in md
    assert "_AI investigation, confidence high. Check it before publishing._" in md
    assert "- [ ] Fix: Guard against a zero discount" in md
    assert "- [ ] Add a test that covers `app/billing.py:42` (total_cents)" in md
    assert "@acme/payments" in md
    assert "dev@acme.example" not in md  # the commit author is not named
    assert "All times on 2026-10-20 unless a date is shown." in md and "- 14:00:00 Deploy abcdef12" in md


@pytest.mark.asyncio
async def test_live_api_serves_the_draft_and_the_full_timeline_only_to_the_project(incident):
    pid, other, inc = incident
    from app.main import app

    try:
        app.dependency_overrides[require_read_project_id] = lambda: pid
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            draft = await client.get(f"/api/incidents/{inc}/postmortem")
            detail = await client.get(f"/api/incidents/{inc}")
        app.dependency_overrides[require_read_project_id] = lambda: other
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            foreign = await client.get(f"/api/incidents/{inc}/postmortem")
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
    assert draft.status_code == 200 and draft.json()["draft"] is True
    assert draft.json()["markdown"].startswith("# Postmortem (draft): ZeroDivisionError")
    assert detail.status_code == 200 and len(detail.json()["timeline"]) == 12
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_live_unchanged_final_verdict_is_not_called_a_change(incident):
    pid, _, inc = incident
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval("SELECT deploy_id FROM incidents WHERE id = $1", inc)
        await conn.execute("UPDATE deploy_analysis SET final_verdict = 'rollback' WHERE deploy_id = $1", dep)
        await conn.execute("INSERT INTO alerts (project_id, deploy_id, channel, kind, sent_at) VALUES ($1, $2, 'slack', 'verdict_final', $3)",
                           pid, dep, T0 + timedelta(minutes=30))
        timeline = await build_incident_timeline(conn, pid, inc)
    verdicts = [t["detail"] for t in timeline if t["event"] == "verdict"]
    assert verdicts == ["Release verdict: ROLLBACK (first check)", "Final check: verdict stays ROLLBACK"]
