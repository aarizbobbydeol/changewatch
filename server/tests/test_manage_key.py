"""The per-project manage key (live Postgres, real keys, no stubbed checks): only it, or the read
key together with the admin key, changes a project's settings, budget, mute rules and runbooks; the
read key keeps viewing; the manage key reads nothing."""
import uuid
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import hash_api_key
from app.cache import api_key_cache
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool

ADMIN = "admin-key-for-manage-tests"


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def projects():
    """Two projects, each with an ingest, read and manage key."""
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    api_key_cache.clear()
    made = []
    async with get_connection() as conn:
        for name in ("a", "b"):
            run = uuid.uuid4().hex[:10]
            keys = {"ingest": f"cw_ingest_{run}", "read": f"cw_read_{run}", "manage": f"cw_manage_{run}"}
            pid = await conn.fetchval(
                """INSERT INTO projects (name, ingest_key_hash, read_key_hash, manage_key_hash)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                f"mk-{name}-{run}", hash_api_key(keys["ingest"]), hash_api_key(keys["read"]), hash_api_key(keys["manage"]))
            made.append({"id": pid, **keys})
    yield made
    async with get_connection() as conn:
        for p in made:
            for table in ("runbooks", "ignore_rules", "budget_state", "project_budgets"):
                try:
                    await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", p["id"])
                except asyncpg.UndefinedTableError:
                    pass
            await conn.execute("DELETE FROM projects WHERE id = $1", p["id"])
    api_key_cache.clear()
    await close_db_pool()


@pytest_asyncio.fixture
async def client():
    from app.main import app

    with patch.object(settings, "ADMIN_API_KEY", ADMIN):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


RUNBOOK = {"title": "Billing totals fail", "steps": "1. Roll back", "match_exception": "ZeroDivisionError"}


@pytest.mark.asyncio
async def test_live_changes_need_the_manage_key_or_the_admin_key(projects, client):
    a, _ = projects
    changes = [
        ("PUT", "/api/settings", {"notify_quiet": True}),
        ("PUT", "/v1/budget", {"hard_limit_micros": 5_000_000}),
        ("POST", "/api/mutes", {"kind": "exc_type", "pattern": "KeyError", "reason": "known", "expires_in_days": 1}),
        ("POST", "/api/runbooks", RUNBOOK),
    ]
    for method, path, body in changes:
        for label, headers in (("read key", {"X-API-Key": a["read"]}), ("ingest key", {"X-API-Key": a["ingest"]})):
            res = await client.request(method, path, json=body, headers=headers)
            assert res.status_code == 403, (path, label, res.status_code)
        refused = await client.request(method, path, json=body, headers={"X-API-Key": a["read"]})
        assert "manage key" in refused.json()["detail"]
        wrong_admin = await client.request(method, path, json=body, headers={"X-API-Key": a["read"], "X-Admin-Key": "nope"})
        assert wrong_admin.status_code == 401, path
        with_manage = await client.request(method, path, json=body, headers={"X-API-Key": a["manage"]})
        assert with_manage.status_code in (200, 201), (path, with_manage.status_code, with_manage.text)
        with_admin = await client.request(method, path, json=body, headers={"X-API-Key": a["read"], "X-Admin-Key": ADMIN})
        assert with_admin.status_code in (200, 201), (path, with_admin.status_code, with_admin.text)

    # Deleting and editing what was made: the same rule
    runbook_id = (await client.get("/api/runbooks", headers={"X-API-Key": a["read"]})).json()[0]["id"]
    mute_id = (await client.get("/api/mutes", headers={"X-API-Key": a["read"]})).json()[0]["id"]
    for method, path, body in (("PUT", f"/api/runbooks/{runbook_id}", {**RUNBOOK, "title": "Edited"}),
                               ("DELETE", f"/api/runbooks/{runbook_id}", None),
                               ("DELETE", f"/api/mutes/{mute_id}", None)):
        assert (await client.request(method, path, json=body, headers={"X-API-Key": a["read"]})).status_code == 403, path
        assert (await client.request(method, path, json=body, headers={"X-API-Key": a["manage"]})).status_code == 200, path


@pytest.mark.asyncio
async def test_live_a_manage_key_changes_only_its_own_project_and_reads_nothing(projects, client):
    a, b = projects
    made = await client.post("/api/runbooks", json=RUNBOOK, headers={"X-API-Key": a["manage"]})
    assert made.status_code == 201
    rid = made.json()["id"]
    # B's manage key (or B's read key with the admin key) acts on B only
    assert (await client.put(f"/api/runbooks/{rid}", json=RUNBOOK, headers={"X-API-Key": b["manage"]})).status_code == 404
    assert (await client.delete(f"/api/runbooks/{rid}", headers={"X-API-Key": b["read"], "X-Admin-Key": ADMIN})).status_code == 404
    assert (await client.get("/api/runbooks", headers={"X-API-Key": b["read"]})).json() == []
    # The manage key is not a read or ingest key
    for method, path in (("GET", "/api/settings"), ("GET", "/api/runbooks"), ("GET", "/api/incidents")):
        res = await client.request(method, path, headers={"X-API-Key": a["manage"]})
        assert res.status_code == 403 and "only changes settings" in res.json()["detail"], path
    sent = await client.post("/v1/events", json={"events": []}, headers={"X-API-Key": a["manage"]})
    assert sent.status_code == 403
    # Viewing and incident actions stay with the read key
    assert (await client.get("/api/settings", headers={"X-API-Key": a["read"]})).status_code == 200
    assert (await client.post("/api/incidents/2000000000/actions", json={"action": "ack"},
                              headers={"X-API-Key": a["read"]})).status_code == 404  # allowed, the incident does not exist


@pytest.mark.asyncio
async def test_live_cli_gives_new_projects_a_manage_key_and_replaces_it(projects, client, capsys):
    from app.cli import new_manage_key_cmd

    a, _ = projects
    await close_db_pool()  # the CLI opens and closes its own pool
    new_key = await new_manage_key_cmd(a["id"])
    assert await new_manage_key_cmd(2_000_000_000) is None
    await init_db_pool()
    out = capsys.readouterr().out
    assert new_key.startswith("cw_manage_") and new_key in out and "No project with ID 2000000000." in out
    api_key_cache.clear()  # the old key would otherwise work until its cached lookup expires (60 s)
    assert (await client.put("/api/settings", json={}, headers={"X-API-Key": a["manage"]})).status_code == 401
    assert (await client.put("/api/settings", json={}, headers={"X-API-Key": new_key})).status_code == 200
