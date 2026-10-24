"""Concurrent first encryptions for a project must all use the one data key that is saved (live)."""
import asyncio
import uuid

import asyncpg
import pytest

from app.config import settings
from app.crypto import decrypt_data_key, get_master_key_bytes, get_or_create_project_data_key, invalidate_project_data_key_cache
from app.db import close_db_pool, get_connection, init_db_pool


@pytest.mark.asyncio
async def test_live_concurrent_first_encryptions_share_one_key():
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
    except Exception:
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")

    await init_db_pool()
    try:
        async with get_connection() as conn:
            pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"keyrace-{uuid.uuid4().hex[:8]}")
        invalidate_project_data_key_cache(pid)
        keys = await asyncio.gather(*[get_or_create_project_data_key(pid) for _ in range(8)])
        invalidate_project_data_key_cache(pid)
        async with get_connection() as conn:
            stored = await conn.fetchval("SELECT data_key_enc FROM projects WHERE id = $1", pid)
            await conn.execute("DELETE FROM projects WHERE id = $1", pid)
        assert len(set(keys)) == 1
        assert decrypt_data_key(stored, get_master_key_bytes()) == keys[0]
    finally:
        await close_db_pool()
