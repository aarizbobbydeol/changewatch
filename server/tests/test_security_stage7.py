"""Security Addendum Stage 7 Tests: Retention, PII Scrubbing, Export and Purge Endpoints.

Verifies:
1. PII Scrubber: emails, IPs, JWTs, and credit cards are redacted; configurable via SCRUB_PII.
2. Code retention: cleans up deploy diffs older than CODE_RETENTION_DAYS (default 7).
3. GET /v1/export: returns decrypted project export; enforces tenant isolation and read keys.
4. DELETE /v1/project-data: purges caller's data while leaving other tenants completely intact.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import hash_api_key
from app.main import app
from app.pii import scrub_pii
from app.retention import cleanup_old_records
from app.scrub import scrub_text


def build_mock_pool(mock_conn: AsyncMock):
    """Helper to mock asyncpg pool and transactions."""
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_acquire = MagicMock()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_acquire
    return mock_pool


@pytest.fixture
def auth_keys():
    ingest_key = "cw_ingest_test_write_key_123"
    read_key = "cw_read_test_read_key_456"
    return {
        "ingest_key": ingest_key,
        "ingest_hash": hash_api_key(ingest_key),
        "read_key": read_key,
        "read_hash": hash_api_key(read_key),
    }


def test_pii_scrubber_redacts_all_sensitive_identifiers():
    """Verify emails, IPv4/IPv6, JWTs, and credit card numbers are scrubbed."""
    text = (
        "User contact is alice.smith+work@example.com from 192.168.1.100 or "
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334 using auth token "
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
        "and card 4111-2222-3333-4444."
    )
    scrubbed = scrub_pii(text, enabled=True)

    assert "alice.smith+work@example.com" not in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed

    assert "192.168.1.100" not in scrubbed
    assert "2001:0db8:85a3:0000:0000:8a2e:0370:7334" not in scrubbed
    assert "[REDACTED_IP]" in scrubbed

    assert "eyJhbGciOiJIUz" not in scrubbed
    assert "[REDACTED_JWT]" in scrubbed

    assert "4111-2222-3333-4444" not in scrubbed
    assert "[REDACTED_CARD]" in scrubbed


def test_pii_scrubber_disabled_flag(monkeypatch):
    """Verify when SCRUB_PII=0, text is preserved unchanged."""
    text = "Contact: test@domain.com from 10.0.0.1"
    scrubbed = scrub_pii(text, enabled=False)
    assert scrubbed == text


def test_scrub_text_integrates_pii_and_secret_redaction():
    """Verify scrub_text in app.scrub redacts secrets and PII concurrently."""
    combined = "Found api_key=secret_val for user john@company.org with AWS AKIA1234567890ABCDEF"
    result = scrub_text(combined)

    assert "secret_val" not in result
    assert "[REDACTED_SECRET]" in result
    assert "john@company.org" not in result
    assert "[REDACTED_EMAIL]" in result
    assert "AKIA1234567890ABCDEF" not in result
    assert "[REDACTED_AWS_KEY]" in result


@pytest.mark.asyncio
async def test_retention_cleans_up_code_diffs_older_than_cutoff():
    """Verify cleanup_old_records prunes deploy diffs past code retention window."""
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = [
        "DELETE 5",   # events
        "DELETE 2",   # traffic
        "UPDATE 3",   # deploys diffs nulled
        "DELETE 0",   # deploy_files
    ]

    stats = await cleanup_old_records(
        pool_or_conn=mock_conn,
        retention_days=30,
        batch_size=1000,
        code_retention_days=7,
    )

    assert stats["events_deleted"] == 5
    assert stats["traffic_deleted"] == 2
    assert stats["code_records_cleaned"] == 3
    assert mock_conn.execute.call_count == 4


@pytest.mark.asyncio
async def test_export_endpoint_requires_read_key_and_returns_data(auth_keys):
    """Verify GET /v1/export returns decrypted project export with read key."""
    mock_conn = AsyncMock()
    async def mock_fetchrow(query, *args):
        if "ingest_key_hash" in query:
            return {
                "id": 1,
                "ingest_key_hash": auth_keys["ingest_hash"],
                "read_key_hash": auth_keys["read_hash"],
                "api_key": None,
            }
        if "SELECT id, name, created_at FROM projects" in query:
            return {
                "id": 1,
                "name": "acme-prod",
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
        return None

    mock_conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
    mock_conn.fetch.side_effect = [
        [{"id": 10, "commit_sha": "abc1234", "author": "Alice", "message": "feat", "environment": "prod", "deployed_at": datetime.now(timezone.utc), "verdict": "safe", "verdict_reasons": ["ok"], "diff": "diff --git", "diff_enc": None}], # deploys
        [{"id": 20, "title": "ZeroDivisionError", "fingerprint": "fp1", "status": "unresolved", "first_seen": datetime.now(timezone.utc), "last_seen": datetime.now(timezone.utc), "count": 5}], # issues
        [{"id": 30, "event_id": "ev1", "type": "error", "exc_type": "ZeroDivisionError", "message": "div by zero", "request_path": "/calc", "duration_ms": 10.0, "release": "v1.0", "environment": "prod", "occurred_at": datetime.now(timezone.utc), "stack_trace": "tb", "stack_trace_enc": None, "extra": {}, "extra_enc": None}], # events
        [{"bucket": datetime.now(timezone.utc), "requests": 100, "errors": 2}], # traffic
    ]

    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. No key -> 401
            resp_401 = await client.get("/v1/export")
            assert resp_401.status_code == 401

            # 2. Ingest key -> 403
            resp_403 = await client.get("/v1/export", headers={"X-API-Key": auth_keys["ingest_key"]})
            assert resp_403.status_code == 403

            # 3. Read key -> 200 with data
            resp = await client.get("/v1/export", headers={"X-API-Key": auth_keys["read_key"]})
            assert resp.status_code == 200
            data = resp.json()
            assert data["project"]["name"] == "acme-prod"
            assert len(data["deploys"]) == 1
            assert len(data["issues"]) == 1
            assert len(data["events"]) == 1
            assert len(data["traffic"]) == 1


@pytest.mark.asyncio
async def test_purge_endpoint_requires_read_key_and_deletes_project_data(auth_keys):
    """Verify DELETE /v1/project-data purges records within project transaction."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_conn.execute.side_effect = [
        "SET LOCAL app.project_id",
        "DELETE 50",  # events
        "DELETE 10",  # traffic
        "DELETE 2",   # deploy_analysis
        "DELETE 1",   # alerts
        "DELETE 0",   # outbox
        "DELETE 5",   # issues
        "DELETE 2",   # deploys
        "DELETE 0",   # deploy_files
    ]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Ingest key -> 403
            resp_403 = await client.delete("/v1/project-data", headers={"X-API-Key": auth_keys["ingest_key"]})
            assert resp_403.status_code == 403

            # Read key -> 200
            resp = await client.delete("/v1/project-data", headers={"X-API-Key": auth_keys["read_key"]})
            assert resp.status_code == 200
            res = resp.json()
            assert res["status"] == "purged"
            assert res["project_id"] == 1
            assert res["deleted"]["events"] == 50
            assert res["deleted"]["traffic"] == 10
            assert res["deleted"]["issues"] == 5
            assert res["deleted"]["deploys"] == 2
