"""The install-wide GITHUB_READ_TOKEN reads only what the owner allowed: a project reads files only
from repositories allowed for it (a deploy names its repository itself), and no file path or name
can resolve the GitHub URL into another repository."""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
from app.github_client import ReadOnlyGitHubClient

MIGRATION = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "034_project_repositories.sql"


@pytest.mark.asyncio
async def test_no_path_or_name_can_reach_another_repository():
    client = ReadOnlyGitHubClient(token="ghp_test")
    sent = []

    async def fake_get(endpoint, params=None, headers=None):
        sent.append((endpoint, params))
        return "content"

    with patch.object(client, "get", side_effect=fake_get):
        assert await client.get_file_content("acme", "shop", "/srv/app/my file.py", ref="a" * 40) == "content"
        for owner, repo, path, ref in (
            ("acme", "shop", "../../../repos/victim/secret/contents/x", "main"),  # resolves to another repo
            ("acme", "shop", "app/./x.py", "main"),
            ("acme", "shop", "app\\..\\..\\x.py", "main"),
            ("..", "victim", "x.py", "main"),
            ("acme", "..", "x.py", "main"),
            ("acme", "shop", "x.py", "../main"),
        ):
            with pytest.raises(ValueError):
                await client.get_file_content(owner, repo, path, ref=ref)
    assert sent == [("/repos/acme/shop/contents/srv/app/my%20file.py", {"ref": "a" * 40})]


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def projects():
    """Projects A and B, each with an ephemeral deploy (no stored file content) naming Acme/Shop."""
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    made = []
    async with get_connection() as conn:
        for name in ("a", "b"):
            made.append({"id": await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id",
                                                   f"repo-{name}-{uuid.uuid4().hex[:8]}")})
    for p in made:
        async with project_transaction(p["id"]) as conn:
            p["dep"] = await conn.fetchval(
                "INSERT INTO deploys (project_id, commit_sha, deployed_at, repository) VALUES ($1, $2, $3, 'Acme/Shop') RETURNING id",
                p["id"], "c" * 40, datetime.now(timezone.utc) - timedelta(minutes=5))
    yield made
    async with get_connection() as conn:
        for p in made:
            for table in ("project_repositories", "deploys"):
                await conn.execute(f"DELETE FROM {table} WHERE project_id = $1", p["id"])
            await conn.execute("DELETE FROM projects WHERE id = $1", p["id"])
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_a_project_reads_only_the_repositories_allowed_for_it(projects, monkeypatch):
    from app.investigator.tools import get_file_at_commit

    a, b = projects
    async with project_transaction(a["id"]) as conn:
        await conn.execute("INSERT INTO project_repositories (project_id, repository) VALUES ($1, 'acme/shop')", a["id"])
    monkeypatch.setattr(settings, "GITHUB_READ_TOKEN", "ghp_install_owner_token")
    fetch = AsyncMock(return_value="def total():\n    return 1\n")
    with patch.object(ReadOnlyGitHubClient, "get_file_content", fetch):
        async with project_transaction(a["id"]) as conn:
            allowed = await get_file_at_commit(conn, a["id"], a["dep"], "app/billing.py", 1, 2)
        async with project_transaction(b["id"]) as conn:
            refused = await get_file_at_commit(conn, b["id"], b["dep"], "app/billing.py", 1, 2)
    assert allowed["available"] is True  # names ignore case: Acme/Shop is acme/shop
    assert refused["available"] is False
    assert fetch.await_count == 1 and fetch.await_args.args[:3] == ("Acme", "Shop", "app/billing.py")


@pytest.mark.asyncio
async def test_live_upgrade_allows_a_repository_only_one_project_named(projects):
    a, b = projects
    solo = f"solo-{uuid.uuid4().hex[:8]}/app"
    async with project_transaction(a["id"]) as conn:
        await conn.execute("INSERT INTO deploys (project_id, commit_sha, deployed_at, repository) VALUES ($1, 'd1', now(), $2)",
                           a["id"], solo)
    async with get_connection() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(MIGRATION.read_text())  # what the upgrade runs, rolled back afterwards
            rows = await conn.fetch("SELECT project_id, repository FROM project_repositories WHERE project_id = ANY($1)",
                                    [a["id"], b["id"]])
        finally:
            await tx.rollback()
    assert sorted((r["project_id"], r["repository"]) for r in rows) == [(a["id"], solo)]  # not acme/shop: both named it


@pytest.mark.asyncio
async def test_live_cli_allows_lists_and_removes(projects, capsys):
    from app.cli import repo_access_cmd

    a, _ = projects
    await close_db_pool()  # the CLI opens and closes its own pool
    assert await repo_access_cmd("allow", a["id"], "Acme/Shop") is True
    assert await repo_access_cmd("allow", a["id"], "../..") is False
    assert await repo_access_cmd("list", a["id"]) is True
    assert await repo_access_cmd("remove", a["id"], "acme/shop") is True
    assert await repo_access_cmd("remove", a["id"], "acme/shop") is False
    assert await repo_access_cmd("allow", 2_000_000_000, "acme/shop") is False
    await init_db_pool()
    out = capsys.readouterr().out
    assert f"Project {a['id']} may now read acme/shop with the shared GitHub token." in out
    assert "Give the repository as owner/name" in out and "\nacme/shop\n" in out
    assert f"Project {a['id']} may no longer read acme/shop." in out and "No project with ID 2000000000." in out
