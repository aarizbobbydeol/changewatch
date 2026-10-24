from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.cli import generate_api_key
from app.migrations import run_migrations


def test_cli_generate_api_key():
    key1 = generate_api_key()
    key2 = generate_api_key()
    assert key1.startswith("cw_live_")
    assert key2.startswith("cw_live_")
    assert key1 != key2
    assert len(key1) > 20


@pytest.mark.asyncio
async def test_migration_runner_tracking():
    mock_conn = AsyncMock()
    # First query: no migrations applied yet
    mock_conn.fetch.return_value = []

    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_pool_acquire = MagicMock()
    mock_pool_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_pool_acquire

    applied = await run_migrations(mock_pool)
    assert len(applied) >= 2
    assert "001_init.sql" in applied
    assert "002_verdict_and_traffic.sql" in applied

    # Check that schema_migrations was queried and created
    mock_conn.execute.assert_any_call(
        """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version TEXT PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
    )


@pytest.mark.asyncio
async def test_migration_runner_idempotent():
    mock_conn = AsyncMock()
    # All migrations already applied, no unhashed projects
    mock_conn.fetch.side_effect = [
        [
            {"version": "001_init.sql"},
            {"version": "002_verdict_and_traffic.sql"},
            {"version": "003_add_data_key_and_crypto.sql"},
            {"version": "004_reliable_queue_event_id_outbox.sql"},
            {"version": "005_api_key_hashing_and_roles.sql"},
            {"version": "006_encrypt_columns.sql"},
            {"version": "007_event_rollups.sql"},
            {"version": "008_issues_event_count.sql"},
            {"version": "009_partition_events.sql"},
            {"version": "010_usage_metering.sql"},
            {"version": "011_deploy_files.sql"},
            {"version": "012_sre_incidents_and_decisions.sql"},
            {"version": "013_analytics_and_status.sql"},
            {"version": "014_sources_and_system_alerts.sql"},
            {"version": "015_investigations.sql"},
            {"version": "016_calm_and_trust.sql"},
            {"version": "017_reconcile_stub_tables.sql"},
            {"version": "018_restricted_tenant_role.sql"},
            {"version": "019_first_and_final_verdicts.sql"},
            {"version": "020_system_alert_dedupe.sql"},
        ],
        [],  # unhashed projects



    ]

    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_pool_acquire = MagicMock()
    mock_pool_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_pool_acquire

    applied = await run_migrations(mock_pool)
    assert applied == []  # Nothing to apply
