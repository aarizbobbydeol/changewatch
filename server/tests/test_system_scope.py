"""System jobs see every project and tenant transactions see only their own, whatever the
database user is (live Postgres). On managed Postgres the app user is not a superuser."""
import uuid

import asyncpg
import pytest

from app.config import settings
from app.db import TENANT_ROLE, close_db_pool, get_connection, init_db_pool, project_transaction


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_live_every_rls_table_has_a_system_scope_policy():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
        missing = await conn.fetch(
            """
            SELECT c.relname FROM pg_class c
            WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')
              AND c.relrowsecurity AND NOT c.relispartition
              AND NOT EXISTS (SELECT 1 FROM pg_policies p
                              WHERE p.tablename = c.relname AND p.policyname = c.relname || '_system_scope')
            """
        )
    finally:
        await conn.close()
    assert [r["relname"] for r in missing] == []


@pytest.mark.asyncio
async def test_live_system_scope_sees_all_projects_and_tenant_scope_only_its_own():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")

    await init_db_pool()
    try:
        run = uuid.uuid4().hex[:10]
        async with get_connection() as conn:
            if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", TENANT_ROLE):
                pytest.skip("Restricted role not available")
            other = await conn.fetchval(
                "INSERT INTO projects (name) VALUES ($1) RETURNING id", f"scope-other-{run}"
            )

        async with project_transaction(other) as conn:
            await conn.execute("INSERT INTO deploys (project_id, commit_sha) VALUES ($1, $2)", other, f"o{run}")
        async with project_transaction(1) as conn:
            await conn.execute("INSERT INTO deploys (project_id, commit_sha) VALUES (1, $1)", f"m{run}")

        shas = [f"o{run}", f"m{run}"]
        async with get_connection() as conn:
            # Act as a user that cannot bypass row-level security, like a managed-Postgres app user
            async with conn.transaction():
                await conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
                system_view = await conn.fetchval("SELECT count(*) FROM deploys WHERE commit_sha = ANY($1)", shas)
        async with project_transaction(1) as conn:
            await conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
            tenant_view = await conn.fetchval("SELECT count(*) FROM deploys WHERE commit_sha = ANY($1)", shas)

        async with get_connection() as conn:
            await conn.execute("DELETE FROM deploys WHERE commit_sha = ANY($1)", shas)
            await conn.execute("DELETE FROM projects WHERE id = $1", other)

        assert system_view == 2  # the detector, outbox and retention can work
        assert tenant_view == 1  # tenant isolation still holds
    finally:
        await close_db_pool()
