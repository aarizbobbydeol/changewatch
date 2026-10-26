"""Runbooks: matching an error to the team's steps, the API, and where they show (alert, incident,
AI prompt, postmortem, export, CLI). Also the chat-safety of alert text, which runbook titles share."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.auth import require_manage_project_id, require_read_project_id
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.notifiers.channels import DiscordNotifier, SlackNotifier, TeamsNotifier, _slack_safe_blocks, slack_safe
from app.runbooks import RunbookIn, match_runbook, runbook_line

NOW = datetime.now(timezone.utc)


def _rb(exc=None, path=None, route=None):
    return {"match_exception": exc, "match_path": path, "match_route": route}


def test_input_needs_a_matcher_a_one_line_title_and_an_https_link():
    ok = RunbookIn(title="  Billing divide by zero ", steps=" 1. Roll back\n", match_exception=" ZeroDivisionError ",
                   match_path="", url="https://wiki.example/rb/billing?x=1&y=2")
    assert (ok.title, ok.steps, ok.match_exception, ok.match_path) == ("Billing divide by zero", "1. Roll back",
                                                                        "ZeroDivisionError", None)
    for bad in (
        {"title": "t"},                                                   # no matcher
        {"title": "t", "match_path": "  "},                               # blank is no matcher
        {"title": "a\nb", "match_exception": "E"},                         # two lines
        {"title": "t", "match_exception": "E", "url": "javascript:alert(1)"},
        {"title": "t", "match_exception": "E", "url": "http://wiki.example/rb"},
        {"title": "t", "match_exception": "E", "url": "https://wiki.example/a b"},
        {"title": "t", "match_exception": "E", "url": "https://x.example/|<!channel>"},
        {"title": "t", "match_exception": "E", "steps": "x" * 5001},
    ):
        with pytest.raises(ValidationError):
            RunbookIn(**bad)


def test_every_set_matcher_must_match_and_the_most_specific_wins():
    exc, path, routes = "ZeroDivisionError", "/srv/app/billing/totals.py", ["/checkout/pay"]
    assert match_runbook(_rb(exc="ZeroDivisionError"), exc, path, routes) == (100, ["error type"])
    assert match_runbook(_rb(exc="Timeout"), "requests.exceptions.Timeout", path, routes)  # dotted name
    assert match_runbook(_rb(exc="KeyError"), exc, path, routes) is None
    assert match_runbook(_rb(path="app/billing/"), exc, path, routes)  # a folder, and an absolute trace path
    assert match_runbook(_rb(path="app/billing/*.py"), exc, path, routes)
    assert match_runbook(_rb(path="app/users/"), exc, path, routes) is None
    assert match_runbook(_rb(path="billing"), exc, path, routes) is None  # a pattern is a whole path, not a word
    assert match_runbook(_rb(route="/checkout*"), exc, path, routes)
    assert match_runbook(_rb(route="/cart*"), exc, path, routes) is None
    assert match_runbook(_rb(route="/checkout*"), exc, path, []) is None  # no request seen: no route match
    assert match_runbook(_rb(exc="ZeroDivisionError", path="app/users/"), exc, path, routes) is None  # all must match
    both = match_runbook(_rb(exc="ZeroDivisionError", route="/checkout/"), exc, path, routes)
    assert both[1] == ["error type", "route"]
    assert match_runbook(_rb(path="app/billing/totals.py"), exc, path, routes)[0] > match_runbook(_rb(path="app/"), exc, path, routes)[0]
    assert match_runbook(_rb(exc="ZeroDivisionError"), exc, path, routes)[0] > match_runbook(_rb(path="app/billing/totals.py"), exc, path, routes)[0]


def test_alert_line_names_the_best_runbook():
    assert runbook_line([]) is None
    assert runbook_line([{"title": "Billing", "url": "https://wiki.example/b"}]) == "Runbook: Billing (https://wiki.example/b)"
    assert runbook_line([{"title": "Billing", "url": None}]) == "Runbook: Billing (steps on the incident page)"


def test_slack_text_cannot_ping_everyone_or_hide_a_link(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://cw.example")
    text = ("ValueError: bad <!channel> <!here|here> <https://evil.example/login|Fix it here> & more "
            "<@U12345> <https://cw.example/incidents/7|View Incident> <https://cw.example.evil.test/x|View>")
    safe = slack_safe(text, ["U12345"])
    assert "<!channel>" not in safe and "&lt;!channel&gt;" in safe and "&lt;!here|here&gt;" in safe
    assert "Fix it here (https://evil.example/login)" in safe and "<https://evil.example" not in safe
    assert "&amp; more" in safe
    assert "<@U12345>" in safe and "<https://cw.example/incidents/7|View Incident>" in safe  # ours stay
    assert "&lt;@U12345&gt;" in slack_safe(text)  # a mention ChangeWatch did not make is only text
    assert "<https://cw.example.evil.test" not in safe  # a look-alike host is not ours
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "A & B"}},
              {"type": "section", "text": {"type": "mrkdwn", "text": "<!everyone>"}}]
    assert _slack_safe_blocks(blocks) == [blocks[0], {"type": "section", "text": {"type": "mrkdwn", "text": "&lt;!everyone&gt;"}}]


class _Recorder:
    """Stands in for httpx.AsyncClient and records what would be posted."""
    posted = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **k):
        _Recorder.posted.append(json)

        class Res:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "ts": "1.2"}
        return Res()


@pytest.mark.asyncio
async def test_slack_and_discord_messages_are_made_safe_when_sent(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://cw.example")
    _Recorder.posted = []
    payload = {"text": "KeyError: '@everyone <!channel> [Fix](https://evil.example)'",
               "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "<https://evil.example|Fix>"}}],
               "details": {"title": "t", "description": "[Fix](https://evil.example)", "where": "a.py:1"}}
    with patch("app.notifiers.channels.httpx.AsyncClient", _Recorder):
        await SlackNotifier().send(payload, {"webhook_url": "https://hooks.slack.example/T/B/x"})
        await SlackNotifier().send(payload, {"bot_token": "xoxb-test", "channel_id": "C1"})
        await DiscordNotifier().send(payload, {"webhook_url": "https://discord.example/api/webhooks/1/x"})
    for slack in _Recorder.posted[:2]:
        assert "<!channel>" not in slack["text"] and "Fix (https://evil.example)" in json.dumps(slack["blocks"])
    discord = _Recorder.posted[2]
    assert discord["allowed_mentions"] == {"parse": [], "users": []}  # "@everyone" stays text
    assert "](" not in discord["content"] and "](" not in discord["embeds"][0]["description"]
    assert payload["text"].startswith("KeyError: '@everyone <!channel>")  # the caller's payload is not changed


@pytest.mark.asyncio
async def test_discord_embed_fields_never_carry_a_masked_link():
    # Owners can be commit author emails, which any contributor sets to any text
    _Recorder.posted = []
    payload = {"text": "x", "details": {"title": "t", "description": "d", "where": "a.py:1",
                                         "owners": "[Approve rollback](https://evil.example/sso)"}}
    with patch("app.notifiers.channels.httpx.AsyncClient", _Recorder):
        await DiscordNotifier().send(payload, {"webhook_url": "https://discord.example/api/webhooks/1/x"})
    assert "](" not in json.dumps(_Recorder.posted[0]["embeds"])


@pytest.mark.asyncio
async def test_an_alert_pings_only_the_owners_changewatch_resolved(monkeypatch):
    from app.notifiers.incident_alert import dispatch_incident_alert

    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://cw.example")
    conn = AsyncMock()
    conn.fetchrow.return_value = {"name": "shop", "notify_quiet": False}
    conn.fetch.side_effect = [
        [{"name": "Alice", "git_emails": ["alice@acme.example"], "slack_user_id": "U0ALICE1",
          "discord_user_id": "111111111111111111", "email": None},
         {"name": "Bob", "git_emails": ["bob@acme.example"], "slack_user_id": None,
          "discord_user_id": "bob#1234", "email": None}],  # not a Discord ID: Discord would refuse the alert
        [{"id": 1, "type": "slack", "config": {}}, {"id": 2, "type": "discord", "config": {}}],
    ]
    # Mentions in text ChangeWatch does not control: an error message echoing a user's input, the
    # commit message, and an owner taken from a commit author that matches no team member
    with patch("app.notifiers.incident_alert.queue_notification", new=AsyncMock()) as queued:
        await dispatch_incident_alert(
            conn, project_id=1, incident_id=7, status="confirmed", commit_sha="2222222222222222", author="alice@acme.example",
            message="fix <@U0CEO1234>", error_title="ValueError: user <@U0CEO1234> <@222222222222222222>", event_count=3,
            where_file="app/x.py", where_line=4, where_function="f", in_diff=True,
            raw_owners=["alice@acme.example", "bob@acme.example", "<@U0EVIL99>"])
    slack_payload, discord_payload = (c.kwargs["payload"] for c in queued.call_args_list)
    _Recorder.posted = []
    with patch("app.notifiers.channels.httpx.AsyncClient", _Recorder):
        await SlackNotifier().send(slack_payload, {"bot_token": "xoxb-test", "channel_id": "C1"})
        await DiscordNotifier().send(discord_payload, {"webhook_url": "https://discord.example/api/webhooks/1/x"})
    slack = json.dumps(_Recorder.posted[0])
    assert "<@U0ALICE1>" in slack  # the code owner is still mentioned
    assert "<@U0CEO1234>" not in slack and "<@U0EVIL99>" not in slack and "&lt;@U0CEO1234&gt;" in slack
    discord = _Recorder.posted[1]
    assert "<@111111111111111111>" in discord["content"]
    assert discord["allowed_mentions"] == {"parse": [], "users": ["111111111111111111"]}


@pytest.mark.asyncio
async def test_teams_cards_show_html_from_quoted_text_as_written():
    # Teams connector cards render limited HTML as well as Markdown, links and images included
    _Recorder.posted = []
    payload = {"text": 'ValueError: Invalid input <a href="https://evil.example/login">View incident in ChangeWatch</a>',
               "details": {"description": '<img src="https://evil.example/t.png"> & [Fix](https://evil.example)'}}
    with patch("app.notifiers.channels.httpx.AsyncClient", _Recorder):
        await TeamsNotifier().send(payload, {"webhook_url": "https://teams.example/webhook"})
    card = _Recorder.posted[0]
    assert "<a " not in json.dumps(card) and "<img" not in json.dumps(card) and "](" not in json.dumps(card)
    section = card["sections"][0]
    assert section["activityTitle"].startswith('ValueError: Invalid input &lt;a href="https://evil.example/login"&gt;')
    assert section["facts"][0]["value"] == '&lt;img src="https://evil.example/t.png"&gt; &amp; [Fix] (https://evil.example)'


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def project():
    """A project with a confirmed ZeroDivisionError incident on /checkout, and another project."""
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    run = uuid.uuid4().hex[:8]
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"rb-{run}")
        other = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"rb-o-{run}")
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, $2, $3) RETURNING id",
                                  pid, f"rb{run}", NOW - timedelta(minutes=20))
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'ZeroDivisionError: division by zero', $3, $3, 'open', 3) RETURNING id",
            pid, f"fp-{run}", NOW - timedelta(minutes=15))
        inc = await conn.fetchval(
            """INSERT INTO incidents (project_id, deploy_id, issue_id, status, title, where_file, where_line,
                   where_function, in_diff, event_count, created_at, first_event_at)
               VALUES ($1, $2, $3, 'confirmed', 'ZeroDivisionError: division by zero', 'app/billing.py', 40,
                   'total_cents', true, 3, $4, $4) RETURNING id""", pid, dep, issue, NOW - timedelta(minutes=15))
        for i in range(3):
            await conn.execute(
                "INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment, request_path) "
                "VALUES ($1, $2, $3, $4, $5, 'prod', $6)", f"{run}-{i}", issue, pid, dep,
                NOW - timedelta(minutes=15, seconds=-i), "/checkout/pay?step=2")
    yield {"pid": pid, "other": other, "dep": dep, "issue": issue, "inc": inc}
    async with get_connection() as conn:
        for p in (pid, other):
            for table in ("runbooks", "outbox", "alerts", "incident_events", "investigations", "incidents", "events", "issues", "deploys"):
                await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", p)
            await conn.execute("DELETE FROM projects WHERE id = $1", p)
    await close_db_pool()


async def _client_as(pid):
    from app.main import app

    app.dependency_overrides[require_read_project_id] = lambda: pid
    app.dependency_overrides[require_manage_project_id] = lambda: pid  # adding and editing runbooks
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def as_project():
    from app.main import app

    yield _client_as
    app.dependency_overrides.pop(require_read_project_id, None)
    app.dependency_overrides.pop(require_manage_project_id, None)


BILLING = {"title": "Billing totals fail", "steps": "1. Check discounts in /admin/discounts\n2. Roll back if over 1%",
           "match_exception": "ZeroDivisionError", "match_route": "/checkout/", "url": "https://wiki.example/rb/billing"}


@pytest.mark.asyncio
async def test_live_api_adds_lists_edits_and_deletes_only_this_projects_runbooks(project, as_project, monkeypatch):
    pid, other = project["pid"], project["other"]
    async with await as_project(pid) as client:
        created = await client.post("/api/runbooks", json=BILLING)
        assert created.status_code == 201
        rb = created.json()
        assert rb["steps"] == BILLING["steps"] and rb["match_path"] is None
        assert (await client.post("/api/runbooks", json={"title": "No matcher"})).status_code == 422
        assert (await client.post("/api/runbooks", json={**BILLING, "url": "javascript:alert(1)"})).status_code == 422
        listed = (await client.get("/api/runbooks")).json()
        assert [r["id"] for r in listed] == [rb["id"]]
        edited = await client.put(f"/api/runbooks/{rb['id']}", json={**BILLING, "title": "Billing totals", "match_route": None})
        assert edited.status_code == 200 and edited.json()["title"] == "Billing totals" and edited.json()["match_route"] is None
        assert (await client.put("/api/runbooks/99999999999", json=BILLING)).status_code == 422  # out of range, not a 500
        monkeypatch.setattr("app.runbooks.MAX_RUNBOOKS", 1)
        assert (await client.post("/api/runbooks", json=BILLING)).status_code == 409
    async with get_connection() as conn:
        stored = await conn.fetchrow("SELECT steps, steps_enc FROM runbooks WHERE id = $1", rb["id"])
    assert stored["steps"] is None and b"discounts" not in bytes(stored["steps_enc"])  # encrypted only
    async with await as_project(other) as client:
        assert (await client.get("/api/runbooks")).json() == []
        assert (await client.put(f"/api/runbooks/{rb['id']}", json=BILLING)).status_code == 404
        assert (await client.delete(f"/api/runbooks/{rb['id']}")).status_code == 404
    async with await as_project(pid) as client:
        assert (await client.delete(f"/api/runbooks/{rb['id']}")).status_code == 200
        assert (await client.get("/api/runbooks")).json() == []


@pytest.mark.asyncio
async def test_live_out_of_range_ids_are_rejected_not_a_server_error(project, as_project):
    async with await as_project(project["pid"]) as client:
        for path in ("/api/incidents/99999999999", "/api/incidents/99999999999/postmortem", "/api/deploys/99999999999/review"):
            res = await client.get(path)
            assert res.status_code == 422, path
            assert res.json() == {"detail": "A number in the request is out of range"}


async def _add(pid, **fields):
    from app.runbooks import create_runbook

    async with project_transaction(pid) as conn:
        return (await create_runbook(conn, pid, RunbookIn(**fields)))["id"]


@pytest.mark.asyncio
async def test_live_incident_shows_matching_runbooks_best_first(project, as_project):
    pid = project["pid"]
    best = await _add(pid, **BILLING)                                     # error type + route
    folder = await _add(pid, title="Billing code", match_path="app/billing")  # the file itself, not a folder
    await _add(pid, title="Users", match_path="app/users/")                # another file
    await _add(pid, title="Cart", match_route="/cart*")                    # another route
    await _add(pid, title="KeyError", match_exception="KeyError")          # another error
    await _add(project["other"], title="Other project", match_exception="ZeroDivisionError")
    async with await as_project(pid) as client:
        detail = (await client.get(f"/api/incidents/{project['inc']}")).json()
        postmortem = (await client.get(f"/api/incidents/{project['inc']}/postmortem")).json()["markdown"]
    assert [r["id"] for r in detail["runbooks"]] == [best]
    assert detail["runbooks"][0]["matched_on"] == ["error type", "route"]
    assert detail["runbooks"][0]["steps"].startswith("1. Check discounts")
    assert "Runbook for this error: Billing totals fail (https://wiki.example/rb/billing)" in postmortem
    assert "Write a runbook" not in postmortem
    assert folder  # created, but "app/billing" is not app/billing.py


@pytest.mark.asyncio
async def test_live_postmortem_without_a_runbook_suggests_writing_one(project, as_project):
    async with await as_project(project["pid"]) as client:
        postmortem = (await client.get(f"/api/incidents/{project['inc']}/postmortem")).json()["markdown"]
    assert "- [ ] Write a runbook for `ZeroDivisionError` in `app/billing.py` (Settings → Runbooks)" in postmortem


@pytest.mark.asyncio
async def test_live_new_incident_alert_links_the_runbook(project):
    from app.incident_manager import check_and_trigger_incident

    pid = project["pid"]
    await _add(pid, title="Coupon lookups", match_exception="KeyError", match_path="app/coupons.py",
               url="https://wiki.example/rb/coupons")
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, 'rbnew0001', $2) RETURNING id",
                                  pid, NOW - timedelta(minutes=5))
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'KeyError: coupon', now(), now(), 'open', 3) RETURNING id", pid, uuid.uuid4().hex)
        for i in range(3):
            await conn.execute("INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment) "
                               "VALUES ($1, $2, $3, $4, $5, 'prod')", uuid.uuid4().hex, issue, pid, dep, NOW - timedelta(seconds=3 - i))
        trace = 'Traceback (most recent call last):\n  File "app/coupons.py", line 12, in lookup\n    c[k]\nKeyError: coupon\n'
        with patch("app.incident_manager.dispatch_incident_alert", new=AsyncMock()) as dispatch, \
             patch("app.trust.calm.is_issue_muted", new=AsyncMock(return_value=(False, None))):
            await check_and_trigger_incident(conn=conn, project_id=pid, deploy_id=dep, issue_id=issue,
                                             payload={"request_path": "/c", "stack_trace": trace, "exc_type": "KeyError"},
                                             occurred_at_dt=NOW, new_events=3)
    assert dispatch.call_args.kwargs["triage_info"]["runbook"] == "Runbook: Coupon lookups (https://wiki.example/rb/coupons)"

    from app.notifiers.incident_alert import format_incident_instant_message

    msg = format_incident_instant_message(
        status="possible", commit_sha="abcdef12", author="a", message="m", error_title="KeyError: coupon", event_count=3,
        where_file="app/coupons.py", where_line=12, where_function="lookup", in_diff=False, owners=[],
        dashboard_url="https://cw.example/incidents/1", triage_info={"runbook": "Runbook: Coupon lookups (https://wiki.example/rb/coupons)"})
    assert "Runbook: Coupon lookups" in msg["text"] and "Runbook: Coupon lookups" in json.dumps(msg["blocks"])


@pytest.mark.asyncio
async def test_live_confirmed_alert_also_links_the_runbook(project):
    """The confirmed alert, the one teams act on, carries the same line as the first one (it was
    built without it, so whichever arrived first decided whether the team saw the runbook)."""
    from app.incident_manager import check_and_trigger_incident

    pid = project["pid"]
    await _add(pid, title="Coupon lookups", match_exception="KeyError", url="https://wiki.example/rb/coupons")
    async with project_transaction(pid) as conn:
        dep = await conn.fetchval("INSERT INTO deploys (project_id, commit_sha, deployed_at) VALUES ($1, 'rbconf001', $2) RETURNING id",
                                  pid, NOW - timedelta(minutes=5))
        issue = await conn.fetchval(
            "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
            "VALUES ($1, $2, 'KeyError: coupon', now(), now(), 'open', 3) RETURNING id", pid, uuid.uuid4().hex)
        for i in range(3):
            await conn.execute("INSERT INTO events (event_id, issue_id, project_id, deploy_id, occurred_at, environment) "
                               "VALUES ($1, $2, $3, $4, $5, 'prod')", uuid.uuid4().hex, issue, pid, dep, NOW - timedelta(seconds=3 - i))
        trace = 'Traceback (most recent call last):\n  File "app/coupons.py", line 12, in lookup\n    c[k]\nKeyError: coupon\n'
        with patch("app.incident_manager.dispatch_incident_alert", new=AsyncMock()) as dispatch, \
             patch("app.trust.calm.is_issue_muted", new=AsyncMock(return_value=(False, None))):
            for new_events in (3, 10):  # 10 more confirms it (CONFIRM_MIN_EVENTS defaults to 10)
                await check_and_trigger_incident(conn=conn, project_id=pid, deploy_id=dep, issue_id=issue,
                                                 payload={"request_path": "/c", "stack_trace": trace, "exc_type": "KeyError"},
                                                 occurred_at_dt=NOW, new_events=new_events)
    sent = {c.kwargs["status"]: c.kwargs.get("triage_info") or {} for c in dispatch.call_args_list}
    assert sent["confirmed"]["runbook"] == "Runbook: Coupon lookups (https://wiki.example/rb/coupons)"
    assert sent["possible"]["runbook"] == sent["confirmed"]["runbook"]


@pytest.mark.asyncio
async def test_live_ai_investigation_is_given_the_runbook(project):
    from app.investigator.agent import build_investigation_prompt, pregather_context

    pid = project["pid"]
    await _add(pid, **BILLING)
    async with project_transaction(pid) as conn:
        context = await pregather_context(conn, pid, project["inc"])
    prompt = build_investigation_prompt(context)
    assert "## The team's runbooks for errors like this" in prompt
    assert "Billing totals fail (matched on error type, route):\n1. Check discounts" in prompt
    assert "|runbook\"" in prompt  # the AI may cite it as evidence


@pytest.mark.asyncio
async def test_live_export_and_cli_include_runbooks(project, capsys, tmp_path):
    from app.cli import add_runbook_cmd, list_runbooks_cmd
    from app.main import app
    from app.runbooks import list_runbooks

    pid = project["pid"]
    await close_db_pool()  # the CLI opens and closes its own pool
    steps_file = tmp_path / "worker.md"
    steps_file.write_text("1. Restart the worker\n")
    assert await add_runbook_cmd(pid, "Worker stuck", steps_file=str(steps_file), match_path="app/worker.py") > 0
    assert await add_runbook_cmd(pid, "No matcher") == 0
    await list_runbooks_cmd(pid)
    await init_db_pool()
    out = capsys.readouterr().out
    assert "Runbook created" in out and "Runbook not added: set at least one of" in out
    assert "Worker stuck | match_path=app/worker.py | 21 characters of steps" in out
    async with project_transaction(pid) as conn:
        assert [r["steps"] for r in await list_runbooks(conn, pid)] == ["1. Restart the worker"]

    with patch.object(settings, "ADMIN_API_KEY", "admin-key-for-tests"):
        app.dependency_overrides[require_read_project_id] = lambda: pid
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                export = (await client.get("/v1/export", headers={"X-Admin-Key": "admin-key-for-tests"})).json()
        finally:
            app.dependency_overrides.pop(require_read_project_id, None)
    assert [r["title"] for r in export["runbooks"]] == ["Worker stuck"]
