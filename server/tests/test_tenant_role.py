"""Tenant transactions must run as a role that cannot bypass row-level security."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.db as db
from app.db import TENANT_SCOPE_SQL


def _mock_pool(row):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool, conn


@pytest.mark.asyncio
async def test_superuser_switches_to_tenant_role_after_setting_project(monkeypatch):
    pool, conn = _mock_pool({"bypasses_rls": True, "can_switch": True})
    assert await db.detect_tenant_role(pool) is True
    monkeypatch.setattr(db, "_db_pool", pool)

    async with db.project_transaction(7):
        pass

    calls = [c.args for c in conn.execute.call_args_list]
    assert calls[0] == (TENANT_SCOPE_SQL, "7")
    assert calls[1] == ("SET LOCAL ROLE changewatch_tenant",)


@pytest.mark.asyncio
async def test_non_bypassing_user_keeps_its_role(monkeypatch):
    pool, conn = _mock_pool({"bypasses_rls": False, "can_switch": True})
    assert await db.detect_tenant_role(pool) is False
    monkeypatch.setattr(db, "_db_pool", pool)

    async with db.project_transaction(7):
        pass

    assert all("SET LOCAL ROLE" not in str(c.args) for c in conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_bypassing_user_without_tenant_role_warns(caplog):
    pool, _ = _mock_pool({"bypasses_rls": True, "can_switch": False})
    assert await db.detect_tenant_role(pool) is False
    assert "relies on application filters only" in caplog.text
