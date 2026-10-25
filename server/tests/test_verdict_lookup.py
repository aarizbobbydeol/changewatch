"""GET /v1/verdict finds the right deploy for a full SHA, a short SHA or a release name (live Postgres)."""
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import require_read_project_id
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction

NOW = datetime.now(timezone.utc)
FULL_SHA = "0123abc" + "d" * 33


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def project():
    """Deploys whose names start alike, newest last."""
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    run = uuid.uuid4().hex[:8]
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"vl-{run}")
    ids = {}
    async with project_transaction(pid) as conn:
        for minutes, sha in enumerate(("0123abc", "release-1.2", "release-1.20", "web-v1.2", "release-2.0-hotfix", FULL_SHA)):
            ids[sha] = await conn.fetchval(
                "INSERT INTO deploys (project_id, commit_sha, environment, deployed_at) VALUES ($1, $2, 'prod', $3) RETURNING id",
                pid, sha, NOW - timedelta(hours=1) + timedelta(minutes=minutes))
    yield {"pid": pid, "ids": ids}
    async with get_connection() as conn:
        await conn.execute("DELETE FROM deploys WHERE project_id = $1", pid)
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_exact_name_wins_and_only_a_hex_sha_matches_by_prefix(project):
    from app.main import app

    app.dependency_overrides[require_read_project_id] = lambda: project["pid"]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def deploy_for(sha):
                res = await client.get("/v1/verdict", params={"sha": sha, "environment": "prod"})
                return res.json()["deploy_id"] if res.status_code == 200 else res.status_code

            ids = project["ids"]
            assert await deploy_for("release-1.2") == ids["release-1.2"]  # not the newer release-1.20
            assert await deploy_for("0123abc") == ids["0123abc"]  # the exact SHA, not a newer one it starts
            assert await deploy_for(FULL_SHA[:12]) == ids[FULL_SHA]  # a short SHA still finds its deploy
            assert await deploy_for(FULL_SHA) == ids[FULL_SHA]
            assert await deploy_for("web_v1.2") == 404  # "_" is not a wildcard
            assert await deploy_for("release-2.0") == 404  # a release name must match in full
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
