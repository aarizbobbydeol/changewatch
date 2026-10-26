"""Draft revert pull requests (opt-in), against a stand-in GitHub that records every request: off by
default, turned on with the manage key and a token stored encrypted, and then only a new tree,
commit, changewatch/ branch and DRAFT pull request are created; nothing else is ever written."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import undo_pr
from app.auth import hash_api_key
from app.cache import api_key_cache
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.github_client import ReadOnlyViolationError

PREV, SHA, HEAD = "1" * 40, "2" * 40, "3" * 40
TOKEN = "github_pat_" + "A1b2C3d4" * 4
API = "https://github.test/api"


class FakeGitHub:
    """Answers the calls a draft revert pull request needs; records everything it is sent."""

    def __init__(self, files=None, new_tree="T-new", pulls_status=200):
        self.requests = []
        self.files = files if files is not None else [
            {"filename": "app/billing.py", "status": "modified"},
            {"filename": "app/new.py", "status": "added"},
            {"filename": "app/old.py", "status": "removed"},
            {"filename": "app/b.py", "status": "renamed", "previous_filename": "app/a.py"},
        ]
        self.new_tree, self.pulls_status = new_tree, pulls_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body, request.headers.get("authorization")))
        r = "repos/acme/shop"
        answers = {
            ("GET", r): {"default_branch": "main"},
            ("GET", f"{r}/git/ref/heads/main"): {"object": {"sha": HEAD}},
            ("GET", f"{r}/git/commits/{HEAD}"): {"tree": {"sha": "T-head"}, "parents": [{"sha": SHA}]},
            ("GET", f"{r}/git/commits/{PREV}"): {"tree": {"sha": "T-prev"}, "parents": []},
            ("GET", f"{r}/compare/{PREV}...{SHA}"): {"files": self.files},
            ("GET", f"{r}/git/trees/T-prev"): {"truncated": False, "tree": [
                {"path": "app", "mode": "040000", "type": "tree", "sha": "t-app"},
                {"path": "app/billing.py", "mode": "100644", "type": "blob", "sha": "b-billing"},
                {"path": "app/old.py", "mode": "100755", "type": "blob", "sha": "b-old"},
                {"path": "app/a.py", "mode": "100644", "type": "blob", "sha": "b-a"},
            ]},
            ("POST", f"{r}/git/trees"): {"sha": self.new_tree},
            ("POST", f"{r}/git/commits"): {"sha": "C-undo"},
            ("POST", f"{r}/git/refs"): {"ref": "x"},
        }
        if (request.method, path) == ("POST", f"{r}/pulls"):
            if self.pulls_status != 200:
                return httpx.Response(self.pulls_status, json={"message": "Resource not accessible by personal access token"})
            return httpx.Response(201, json={"html_url": "https://github.com/acme/shop/pull/7", "number": 7})
        if (request.method, path) in answers:
            return httpx.Response(200, json=answers[(request.method, path)])
        return httpx.Response(405, json={"message": f"unexpected {request.method} {path}"})

    def writes(self):
        return [(m, p, b) for m, p, b, _ in self.requests if m != "GET"]


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def setup(monkeypatch):
    """A project with keys, two prod deploys of acme/shop and an incident on the second one."""
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    api_key_cache.clear()
    run = uuid.uuid4().hex[:10]
    keys = {"read": f"cw_read_{run}", "manage": f"cw_manage_{run}"}
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name, read_key_hash, manage_key_hash) VALUES ($1, $2, $3) RETURNING id",
                                  f"undo-{run}", hash_api_key(keys["read"]), hash_api_key(keys["manage"]))
    now = datetime.now(timezone.utc)
    async with project_transaction(pid) as conn:
        await conn.execute("INSERT INTO deploys (project_id, commit_sha, deployed_at, repository) VALUES ($1, $2, $3, 'acme/shop')",
                           pid, PREV, now - timedelta(hours=2))
        dep = await conn.fetchval(
            "INSERT INTO deploys (project_id, commit_sha, deployed_at, repository) VALUES ($1, $2, $3, 'acme/shop') RETURNING id",
            pid, SHA, now - timedelta(minutes=10))
        await conn.execute("INSERT INTO deploy_files (deploy_id, project_id, path, status, added_ranges) "
                           "VALUES ($1, $2, 'app/billing.py', 'modified', '[]')", dep, pid)
        issue = await conn.fetchval("INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen) "
                                    "VALUES ($1, $2, 'ZeroDivisionError: division by zero', now(), now()) RETURNING id", pid, run)
        inc = await conn.fetchval(
            """INSERT INTO incidents (project_id, deploy_id, issue_id, status, title, where_file, where_line, where_function, in_diff)
               VALUES ($1, $2, $3, 'confirmed', 'ZeroDivisionError: division by zero', 'app/billing.py', 40, 'total', true)
               RETURNING id""", pid, dep, issue)
    monkeypatch.setattr(settings, "GITHUB_API_URL", API)
    yield {"pid": pid, "dep": dep, "inc": inc, **keys}
    monkeypatch.setattr(undo_pr, "TRANSPORT", None)
    async with get_connection() as conn:
        for table in ("incident_events", "incidents", "issues", "deploy_files", "deploys"):
            await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", pid)
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    api_key_cache.clear()
    await close_db_pool()


@pytest_asyncio.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _github(monkeypatch, **kwargs) -> FakeGitHub:
    fake = FakeGitHub(**kwargs)
    monkeypatch.setattr(undo_pr, "TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


async def _turn_on(client, s):
    res = await client.put("/api/undo-pr", json={"token": TOKEN}, headers={"X-API-Key": s["manage"]})
    assert res.status_code == 200 and res.json() == {"enabled": True}


@pytest.mark.asyncio
async def test_live_off_by_default_and_turned_on_only_with_the_manage_key(setup, client, monkeypatch):
    s = setup
    fake = _github(monkeypatch)
    read, manage = {"X-API-Key": s["read"]}, {"X-API-Key": s["manage"]}
    assert (await client.get("/api/undo-pr", headers=read)).json() == {"enabled": False}
    off = await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "deploy"}, headers=manage)
    assert off.status_code == 409 and "off for this project" in off.json()["detail"]
    assert (await client.put("/api/undo-pr", json={"token": TOKEN}, headers=read)).status_code == 403
    assert (await client.put("/api/undo-pr", json={"token": "short"}, headers=manage)).status_code == 422
    await _turn_on(client, s)
    shown = await client.get("/api/undo-pr", headers=read)
    assert shown.json() == {"enabled": True} and TOKEN not in shown.text
    async with get_connection() as conn:
        stored = await conn.fetchval("SELECT undo_pr_token_enc FROM projects WHERE id = $1", s["pid"])
    assert TOKEN.encode() not in bytes(stored)  # encrypted only
    assert (await client.put("/api/undo-pr", json={"token": None}, headers=manage)).json() == {"enabled": False}
    assert fake.requests == []  # nothing reached GitHub


@pytest.mark.asyncio
async def test_live_opens_a_draft_that_puts_the_deploys_files_back_and_writes_nothing_else(setup, client, monkeypatch):
    s = setup
    fake = _github(monkeypatch)
    await _turn_on(client, s)
    assert (await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "deploy"},
                              headers={"X-API-Key": s["read"]})).status_code == 403
    res = await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "deploy"}, headers={"X-API-Key": s["manage"]})
    assert res.status_code == 200, res.text
    pr = res.json()
    assert pr["url"] == "https://github.com/acme/shop/pull/7" and pr["already_open"] is False
    assert pr["branch"].startswith(f"changewatch/undo-incident-{s['inc']}-deploy-")

    assert {m for m, *_ in fake.requests} == {"GET", "POST"}
    assert all(auth == f"Bearer {TOKEN}" for *_, auth in fake.requests)
    writes = fake.writes()
    assert [p.split("/", 3)[3] for _, p, _ in writes] == ["git/trees", "git/commits", "git/refs", "pulls"]
    tree, commit, ref, pull = (b for _, _, b in writes)
    assert tree["base_tree"] == "T-head"
    assert sorted(tree["tree"], key=lambda e: e["path"]) == [
        {"path": "app/a.py", "mode": "100644", "type": "blob", "sha": "b-a"},             # renamed: the old name back
        {"path": "app/b.py", "mode": "100644", "type": "blob", "sha": None},              # renamed: the new name gone
        {"path": "app/billing.py", "mode": "100644", "type": "blob", "sha": "b-billing"},  # modified: as it was
        {"path": "app/new.py", "mode": "100644", "type": "blob", "sha": None},            # added: removed
        {"path": "app/old.py", "mode": "100755", "type": "blob", "sha": "b-old"},         # removed: back, mode kept
    ]
    assert commit["parents"] == [HEAD] and commit["tree"] == "T-new"
    assert ref == {"ref": f"refs/heads/{pr['branch']}", "sha": "C-undo"}
    assert pull["draft"] is True and pull["base"] == "main" and pull["head"] == pr["branch"]
    assert f"incident #{s['inc']}" in pull["body"] and "merges nothing" in pull["body"]

    # Asked again: the same pull request, nothing new sent to GitHub
    count = len(fake.requests)
    again = await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "deploy"}, headers={"X-API-Key": s["manage"]})
    assert again.json()["already_open"] is True and again.json()["url"] == pr["url"] and len(fake.requests) == count
    detail = (await client.get(f"/api/incidents/{s['inc']}", headers={"X-API-Key": s["read"]})).json()
    assert detail["fix_suggestions"]["pull_requests"] == {
        "enabled": True, "opened": [{"scope": "deploy", "url": pr["url"], "number": 7, "branch": pr["branch"]}]}
    assert any(t["detail"] == f"Draft pull request opened to undo the deploy: {pr['url']}" for t in detail["timeline"])


@pytest.mark.asyncio
async def test_live_file_scope_puts_back_only_the_failing_file(setup, client, monkeypatch):
    s = setup
    fake = _github(monkeypatch)
    await _turn_on(client, s)
    res = await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "file"}, headers={"X-API-Key": s["manage"]})
    assert res.status_code == 200, res.text
    tree = next(b for m, p, b in fake.writes() if p.endswith("git/trees"))
    assert tree["tree"] == [{"path": "app/billing.py", "mode": "100644", "type": "blob", "sha": "b-billing"}]


@pytest.mark.asyncio
async def test_live_refusals_are_plain_and_never_show_the_token(setup, client, monkeypatch):
    s = setup
    await _turn_on(client, s)
    manage = {"X-API-Key": s["manage"]}
    # GitHub refuses the pull request: the branch was made, nothing else is attempted, the token stays secret
    fake = _github(monkeypatch, pulls_status=403)
    refused = await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "deploy"}, headers=manage)
    assert refused.status_code == 502 and "write access" in refused.json()["detail"] and TOKEN not in refused.text
    assert [p.rsplit("/", 1)[-1] for _, p, _ in fake.writes()] == ["trees", "commits", "refs", "pulls"]
    # Nothing to undo: the default branch already has the files as before
    _github(monkeypatch, new_tree="T-head")
    nothing = await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "deploy"}, headers=manage)
    assert nothing.status_code == 409 and "Nothing to undo" in nothing.json()["detail"]
    # Too many files for a generated pull request
    _github(monkeypatch, files=[{"filename": f"f{i}.py", "status": "modified"} for i in range(101)])
    many = await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "deploy"}, headers=manage)
    assert many.status_code == 422 and "git revert" in many.json()["detail"]
    # A deploy that did not say which repository it came from, or named one a URL would resolve away
    for repository in (None, "../..", "acme/.."):
        async with project_transaction(s["pid"]) as conn:
            await conn.execute("UPDATE deploys SET repository = $2 WHERE id = $1", s["dep"], repository)
        fake = _github(monkeypatch)
        unknown = await client.post(f"/api/incidents/{s['inc']}/undo-pr", json={"scope": "deploy"}, headers=manage)
        assert unknown.status_code == 422 and "repository" in unknown.json()["detail"] and fake.requests == [], repository


def test_the_client_can_write_nothing_but_a_draft_revert():
    undo_pr.check_write("repos/acme/shop/git/trees", {})
    undo_pr.check_write("repos/acme/shop/git/refs", {"ref": "refs/heads/changewatch/undo-incident-1-deploy-2"})
    undo_pr.check_write("repos/acme/shop/pulls", {"draft": True})
    for path, body in (
        ("repos/acme/shop/git/refs", {"ref": "refs/heads/main"}),        # never an existing branch
        ("repos/acme/shop/git/refs", {"ref": "refs/tags/v1"}),
        ("repos/acme/shop/pulls", {"draft": False}),                     # never a ready pull request
        ("repos/acme/shop/pulls", {}),
        ("repos/acme/shop/merges", {}),                                  # never a merge
        ("repos/acme/shop/issues/7/comments", {}),                       # never a comment
        ("repos/acme/shop/git/refs/heads/main", {"sha": "x"}),
        ("repos/acme/shop/../other/git/trees", {}),
        ("repos/../../git/trees", {}),                                   # a URL would resolve out of repos/
        ("repos/acme/../pulls", {"draft": True}),
    ):
        with pytest.raises(ReadOnlyViolationError):
            undo_pr.check_write(path, body)
    client = undo_pr.UndoPullRequestClient("t" * 30, API)
    assert not any(hasattr(client, verb) for verb in ("put", "patch", "delete"))


def test_a_deploy_cannot_name_a_repository_a_url_would_resolve_away():
    from pydantic import ValidationError

    from app.schemas import DeployCreate

    assert DeployCreate(commit_sha="a", repository="acme/shop").repository == "acme/shop"
    for bad in ("../..", "acme/..", "./shop", "acme/shop/x"):
        with pytest.raises(ValidationError):
            DeployCreate(commit_sha="a", repository=bad)
