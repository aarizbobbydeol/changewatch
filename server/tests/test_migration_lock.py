"""Two API processes starting at once on an empty database both come up (live Postgres).

uvicorn --workers 2 runs the startup migrations in each process at the same moment; without the
advisory lock the second one fails with "relation already exists" halfway through.
"""
import asyncio
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.config import settings
from app.migrations import MIGRATIONS_DIR, run_migrations


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


@pytest.mark.asyncio
async def test_live_concurrent_startup_migrations_on_an_empty_database():
    try:
        admin = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
    except Exception:
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    name = f"cw_mig_{uuid.uuid4().hex[:10]}"
    try:
        await admin.execute(f"CREATE DATABASE {name}")
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("Database user cannot create databases")
    pools = []
    try:
        url = _with_db(settings.DATABASE_URL, name)
        pools = [await asyncpg.create_pool(url, min_size=1, max_size=2) for _ in range(3)]
        results = await asyncio.gather(*(run_migrations(p) for p in pools))

        expected = sorted(f.name for f in MIGRATIONS_DIR.glob("*.sql"))
        assert sorted(v for r in results for v in r) == expected  # each applied exactly once
        async with pools[0].acquire() as conn:
            stored = [r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations ORDER BY 1")]
        assert stored == expected
    finally:
        for p in pools:
            await p.close()
        await admin.execute(f"DROP DATABASE IF EXISTS {name}")
        await admin.close()
