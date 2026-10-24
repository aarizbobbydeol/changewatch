import asyncpg
import pytest
import redis.asyncio as aioredis
from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.queue import close_redis, init_redis


async def is_services_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_live_postgres_and_redis():
    """Integration test against live Postgres and Redis when running."""
    if not await is_services_up():
        pytest.skip("Live PostgreSQL or Redis is not currently running. Skipping integration test.")

    # Initialize pools
    await init_db_pool()
    await init_redis()

    try:
        # 1. Verify schema tables exist
        async with project_transaction(1) as conn:
            tables = await conn.fetch(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                """
            )
            table_names = {row["table_name"] for row in tables}
            required_tables = {"projects", "deploys", "issues", "events", "deploy_analysis", "alerts"}
            assert required_tables.issubset(table_names), f"Missing tables: {required_tables - table_names}"

            # 2. Verify seed project exists
            project = await conn.fetchrow(
                "SELECT id, name, api_key FROM projects WHERE api_key = 'cw_test_key_123'"
            )
            assert project is not None
            assert project["name"] == "my-test-app"
            proj_id = project["id"]

            # 3. Test RLS Isolation
            # Insert deploy for project 1
            deploy_row = await conn.fetchrow(
                """
                INSERT INTO deploys (project_id, commit_sha, author, message, environment)
                VALUES ($1, 'test_sha_rls_001', 'tester', 'test deploy', 'test')
                RETURNING id, commit_sha
                """,
                proj_id,
            )
            assert deploy_row["commit_sha"] == "test_sha_rls_001"
            deploy_id = deploy_row["id"]

            # Query under project 1
            visible = await conn.fetch(
                "SELECT id FROM deploys WHERE id = $1", deploy_id
            )
            assert len(visible) == 1

        # Query under a different project (e.g. project 9999) -> must see 0 rows
        async with project_transaction(9999) as conn2:
            hidden = await conn2.fetch(
                "SELECT id FROM deploys WHERE id = $1", deploy_id
            )
            assert len(hidden) == 0, "RLS failed: project 9999 saw project 1 deploy!"

    finally:
        await close_db_pool()
        await close_redis()
