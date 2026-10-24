"""Tests for Performance Addendum Stage 7:
1. Native PostgreSQL partitioning by week:
   - Partition retention drops only expired partitions (O(1) catalog update).
   - Default partition and active partitions are strictly preserved.
2. Database read/write pool separation:
   - Dedicated read pool enforces statement_timeout = 5000ms.
   - Write pool and read pool can be sized and closed independently.
3. Migration 009 schema validation:
   - Partitioned table definition, weekly pre-allocation, indexes, and RLS policy.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.db import (
    TENANT_SCOPE_SQL,
    close_db_pool,
    get_db_pool,
    get_read_db_pool,
    init_db_pool,
    read_project_transaction,
)
from app.retention import _cleanup_with_conn, drop_expired_partitions


# -----------------------------------------------------------------------------
# 1. Partition Retention Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drop_expired_partitions_drops_only_old():
    """
    Verify drop_expired_partitions queries pg_inherits, parses upper bounds,
    drops only expired partitions, and preserves active and default partitions.
    """
    mock_conn = AsyncMock()

    # Cutoff is 2026-10-15
    cutoff = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)

    # 4 Partitions in database:
    # 1. events_default (DEFAULT) -> MUST KEEP
    # 2. events_2026_09_28 (TO 2026-10-05) -> EXPIRED, MUST DROP
    # 3. events_2026_10_05 (TO 2026-10-12) -> EXPIRED, MUST DROP
    # 4. events_2026_10_12 (TO 2026-10-19) -> ACTIVE (upper > cutoff), MUST KEEP
    mock_conn.fetch.return_value = [
        {"partition_name": "events_default", "bound_expr": "DEFAULT"},
        {
            "partition_name": "events_2026_09_28",
            "bound_expr": "FOR VALUES FROM ('2026-09-28 00:00:00+00') TO ('2026-10-05 00:00:00+00')",
        },
        {
            "partition_name": "events_2026_10_05",
            "bound_expr": "FOR VALUES FROM ('2026-10-05 00:00:00+00') TO ('2026-10-12 00:00:00+00')",
        },
        {
            "partition_name": "events_2026_10_12",
            "bound_expr": "FOR VALUES FROM ('2026-10-12 00:00:00+00') TO ('2026-10-19 00:00:00+00')",
        },
    ]

    dropped = await drop_expired_partitions(mock_conn, cutoff)

    assert dropped == ["events_2026_09_28", "events_2026_10_05"]

    # Verify executed DROP TABLE calls
    executed_sql = [call[0][0] for call in mock_conn.execute.call_args_list]
    assert "DROP TABLE IF EXISTS events_2026_09_28 CASCADE;" in executed_sql
    assert "DROP TABLE IF EXISTS events_2026_10_05 CASCADE;" in executed_sql
    assert not any("events_default" in sql for sql in executed_sql)
    assert not any("events_2026_10_12" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_cleanup_with_conn_integrates_partition_dropping():
    """Verify _cleanup_with_conn reports dropped partitions alongside row counts."""
    mock_conn = AsyncMock()

    cutoff = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    mock_conn.fetch.return_value = [
        {
            "partition_name": "events_2026_10_01",
            "bound_expr": "FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-10-08 00:00:00+00')",
        }
    ]
    # Fallback loop batch delete for events in default, traffic delete
    mock_conn.execute.side_effect = [
        None,             # DROP TABLE
        "DELETE 0",       # Batch delete events in default partition (0 left)
        "DELETE 50",      # Traffic delete
    ]

    stats = await _cleanup_with_conn(mock_conn, cutoff=cutoff, batch_size=5000)

    assert stats["partitions_dropped"] == 1
    assert stats["dropped_partition_names"] == ["events_2026_10_01"]
    assert stats["traffic_deleted"] == 50


# -----------------------------------------------------------------------------
# 2. Separate Read and Write Connection Pools
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_and_write_pool_separation():
    """Verify init_db_pool initializes separate pools with statement_timeout for read pool."""
    mock_write_pool = MagicMock()
    mock_write_pool.close = AsyncMock()
    mock_read_pool = MagicMock()
    mock_read_pool.close = AsyncMock()

    async def _mock_create_pool(**kwargs):
        if "server_settings" in kwargs and kwargs["server_settings"].get("statement_timeout") == "5000ms":
            return mock_read_pool
        return mock_write_pool

    with patch("asyncpg.create_pool", side_effect=_mock_create_pool):
        pool = await init_db_pool(dsn="postgresql://localhost/test", min_size=2, max_size=8)
        assert pool is mock_write_pool
        assert get_db_pool() is mock_write_pool
        assert get_read_db_pool() is mock_read_pool

        # Close pools
        await close_db_pool()
        mock_write_pool.close.assert_called_once()
        mock_read_pool.close.assert_called_once()


@pytest.mark.asyncio
async def test_read_project_transaction_enforces_rls_on_read_pool():
    """Verify read_project_transaction uses read pool and enforces RLS."""
    mock_conn = AsyncMock()
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_acquire = MagicMock()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_read_pool = MagicMock()
    mock_read_pool.acquire.return_value = mock_acquire

    with patch("app.db.get_read_db_pool", return_value=mock_read_pool):
        async with read_project_transaction(project_id=42) as conn:
            assert conn is mock_conn
            mock_conn.execute.assert_called_with(TENANT_SCOPE_SQL, "42")


# -----------------------------------------------------------------------------
# 3. Migration 009 SQL Structure & Coverage
# -----------------------------------------------------------------------------

def test_migration_009_partition_events_sql_exists():
    """Verify 009_partition_events.sql contains required partitioning constructs."""
    migration_file = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "009_partition_events.sql"
    assert migration_file.exists(), "009_partition_events.sql must exist"

    content = migration_file.read_text(encoding="utf-8")
    assert "PARTITION BY RANGE (occurred_at)" in content
    assert "CREATE TABLE IF NOT EXISTS events_default PARTITION OF events DEFAULT" in content
    assert "events_project_policy" in content
    assert "ENABLE ROW LEVEL SECURITY" in content
    assert "FORCE ROW LEVEL SECURITY" in content
    assert "CREATE INDEX IF NOT EXISTS events_project_time" in content
    assert "CREATE UNIQUE INDEX IF NOT EXISTS events_project_event_id" in content
