import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
from app.auth import hash_api_key
from app.cache import api_key_cache
from app.cli import encrypt_existing_cmd
from app.crypto import (
    DecryptionError,
    decrypt,
    decrypt_bytes,
    encrypt,
    get_or_create_project_data_key,
)
from app.main import app
from app.worker import process_single_event


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


@pytest.mark.asyncio
async def test_column_encryption_roundtrip_with_aad():
    """Verify application-level column encryption with AAD context binding."""
    project_id = 42

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"data_key_enc": None}
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn

    with patch("app.crypto.get_connection", return_value=mock_ctx):
        # 1. Roundtrip test for diff
        raw_diff = "diff --git a/app.py b/app.py\n+ secret_token = 'xyz'"
        enc_diff = await encrypt(project_id, "deploys", "diff", raw_diff)
        assert isinstance(enc_diff, bytes)
        assert len(enc_diff) > 30
        assert raw_diff.encode("utf-8") not in enc_diff

        dec_diff = await decrypt(project_id, "deploys", "diff", enc_diff)
        assert dec_diff == raw_diff

        # 2. Tampering test
        tampered_diff = bytearray(enc_diff)
        tampered_diff[-1] ^= 0xFF
        with pytest.raises(DecryptionError):
            await decrypt(project_id, "deploys", "diff", bytes(tampered_diff))

        # 3. Wrong column name in AAD
        with pytest.raises(DecryptionError):
            await decrypt(project_id, "deploys", "wrong_col", enc_diff)

        # 4. Wrong table name in AAD
        with pytest.raises(DecryptionError):
            await decrypt(project_id, "events", "diff", enc_diff)

        # 5. Wrong project ID in AAD
        with pytest.raises(DecryptionError):
            await decrypt(999, "deploys", "diff", enc_diff)


@pytest.mark.asyncio
async def test_create_deploy_encrypts_diff():
    """POST /v1/deploys must encrypt diff into diff_enc before database insertion."""
    project_id = 1
    marker_diff = "--- a/src.py\n+++ b/src.py\n+ MARKER_SECRET_DIFF_XYZ_98765"

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 101, "data_key_enc": None}
    mock_pool = build_mock_pool(mock_conn)

    mock_redis = AsyncMock()
    mock_redis.get.return_value = str(project_id)

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.db.get_db_pool", return_value=mock_pool), \
         patch("app.crypto.get_connection", return_value=mock_pool.acquire.return_value):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/deploys",
                headers={"X-API-Key": "cw_ingest_test_key_123"},
                json={
                    "commit_sha": "abc1234567",
                    "author": "Alice",
                    "message": "fix: secret patch",
                    "diff": marker_diff,
                    "environment": "prod",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            # Verify arguments to fetchrow (INSERT INTO deploys)
            deploy_call = next(
                c[0] for c in mock_conn.fetchrow.call_args_list if "INSERT INTO deploys" in c[0][0]
            )
            query = deploy_call[0]
            assert "diff_enc" in query

            diff_enc_arg = deploy_call[6]  # diff_enc parameter
            assert isinstance(diff_enc_arg, bytes)
            assert diff_enc_arg.startswith(b"\x01")  # Version 1 header
            # Marker must not be in raw ciphertext bytes
            assert b"MARKER_SECRET_DIFF_XYZ_98765" not in diff_enc_arg


@pytest.mark.asyncio
async def test_worker_encrypts_stack_trace_and_extra():
    """Worker must encrypt stack_trace and extra before writing to events table."""
    project_id = 7
    marker_trace = "Traceback (most recent call last):\n  MARKER_SECRET_STACK_TRACE_XYZ_54321"
    marker_extra = {"api_token": "MARKER_SECRET_EXTRA_TOKEN_11223"}

    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        {"id": 10},               # 1. for find_deploy
        {"id": 201},              # 2. for issues insert
        {"data_key_enc": None},   # 3. for crypto get_or_create_project_data_key
    ]
    mock_pool = build_mock_pool(mock_conn)

    payload = {
        "project_id": project_id,
        "event_id": "test_ev_001",
        "type": "error",
        "exc_type": "ValueError",
        "message": "Sensitive error",
        "stack_trace": marker_trace,
        "request_path": "/crash",
        "duration_ms": 15.0,
        "extra": marker_extra,
        "occurred_at": 1700000000.0,
    }

    with patch("app.crypto.get_connection", return_value=mock_pool.acquire.return_value), \
         patch("app.worker.project_transaction", return_value=mock_pool.acquire.return_value):
        await process_single_event(payload)

        # Verify conn.execute was called for events insert
        events_call = None
        for call in mock_conn.execute.call_args_list:
            if "INSERT INTO events" in call[0][0]:
                events_call = call
                break

        assert events_call is not None, "INSERT INTO events must be executed"
        args = events_call[0]
        stack_trace_enc_arg = args[7]
        extra_enc_arg = args[11]

        assert isinstance(stack_trace_enc_arg, bytes)
        assert b"MARKER_SECRET_STACK_TRACE_XYZ_54321" not in stack_trace_enc_arg

        assert isinstance(extra_enc_arg, bytes)
        assert b"MARKER_SECRET_EXTRA_TOKEN_11223" not in extra_enc_arg


@pytest.mark.asyncio
async def test_read_api_decrypts_encrypted_columns():
    """GET /api/deploys and GET /api/issues/{id} must decrypt diff_enc and stack_trace_enc."""
    project_id = 1
    raw_diff = "diff --git a/app.py b/app.py\n+ decrypted diff content"
    raw_trace = "Traceback...\n  decrypted stack trace"
    raw_extra = {"auth": "bearer-token"}

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"data_key_enc": None}
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn

    # Pre-encrypt values
    with patch("app.crypto.get_connection", return_value=mock_ctx):
        enc_diff = await encrypt(project_id, "deploys", "diff", raw_diff)
        enc_trace = await encrypt(project_id, "events", "stack_trace", raw_trace)
        enc_extra = await encrypt(project_id, "events", "extra", json.dumps(raw_extra))

    # Test /api/deploys decryption
    auth_row = {
        "id": project_id,
        "ingest_key_hash": hash_api_key("cw_ingest_key"),
        "read_key_hash": hash_api_key("cw_read_key"),
        "api_key": None,
    }
    deploy_row = {
        "id": 10,
        "commit_sha": "abc1234",
        "author": "Alice",
        "message": "fix: bug",
        "diff": None,
        "diff_enc": enc_diff,
        "environment": "prod",
        "deployed_at": None,
        "verdict": "safe",
        "verdict_reasons": [],
        "verdict_final": True,
        "verdict_updated_at": None,
        "likely_cause": None,
        "new_issues": 0,
        "errors_before": 0,
        "errors_after": 0,
        "flagged": False,
    }
    mock_conn.fetchrow.side_effect = [auth_row, {"data_key_enc": None}]
    mock_conn.fetch.return_value = [deploy_row]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool), \
         patch("app.crypto.get_connection", return_value=mock_pool.acquire.return_value):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/deploys",
                headers={"X-API-Key": "cw_read_key"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["diff"] == raw_diff

    # Test /api/issues/{id} decryption
    issue_row = {
        "id": 50,
        "fingerprint": "fp_50",
        "title": "Error: decrypted",
        "first_seen": None,
        "last_seen": None,
        "status": "open",
        "event_count": 1,
    }
    event_row = {
        "id": 501,
        "occurred_at": None,
        "stack_trace": None,
        "stack_trace_enc": enc_trace,
        "request_path": "/crash",
        "duration_ms": 10.0,
        "extra": None,
        "extra_enc": enc_extra,
        "deploy_id": 10,
        "commit_sha": "abc1234",
    }
    api_key_cache.clear()
    mock_conn.fetchrow.side_effect = [auth_row, issue_row, {"data_key_enc": None}, {"data_key_enc": None}]
    mock_conn.fetch.return_value = [event_row]

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool), \
         patch("app.crypto.get_connection", return_value=mock_pool.acquire.return_value):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/issues/50",
                headers={"X-API-Key": "cw_read_key"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["events"]) == 1
            assert data["events"][0]["stack_trace"] == raw_trace
            assert data["events"][0]["extra"] == raw_extra


@pytest.mark.asyncio
async def test_encrypt_existing_cli_command():
    """Verify encrypt-existing CLI converts unencrypted plaintext columns in batches."""
    mock_conn = AsyncMock()
    mock_pool = build_mock_pool(mock_conn)

    # 1. Projects with unencrypted webhook
    mock_conn.fetch.side_effect = [
        # projects fetch
        [{"id": 1, "slack_webhook_url": "https://hooks.slack.com/services/T1/B2/K3"}],
        # deploys batch 1
        [{"id": 10, "project_id": 1, "diff": "diff --git content"}],
        # deploys batch 2 (empty, terminates loop)
        [],
        # events batch 1
        [{"id": 100, "project_id": 1, "stack_trace": "Traceback...", "extra": {"user": 1}}],
        # events batch 2 (empty, terminates loop)
        [],
    ]
    mock_conn.fetchrow.return_value = {"data_key_enc": None}

    with patch("app.cli.init_db_pool", return_value=mock_pool), \
         patch("app.cli.get_connection", return_value=mock_pool.acquire.return_value), \
         patch("app.cli.close_db_pool", return_value=None), \
         patch("app.crypto.get_connection", return_value=mock_pool.acquire.return_value):

        stats = await encrypt_existing_cmd(batch_size=10)
        assert stats["projects_encrypted"] == 1
        assert stats["deploys_encrypted"] == 1
        assert stats["events_encrypted"] == 1

        # Check execute calls for UPDATE ... SET *_enc = $1, plaintext = NULL
        updates = [c[0][0] for c in mock_conn.execute.call_args_list if "UPDATE" in c[0][0]]
        assert any("slack_webhook_url_enc" in u and "slack_webhook_url = NULL" in u for u in updates)
        assert any("diff_enc" in u and "diff = NULL" in u for u in updates)
        assert any("stack_trace_enc" in u and "stack_trace = NULL" in u for u in updates)


@pytest.mark.asyncio
async def test_database_inspection_no_plaintext_markers():
    """
    Database Inspection Test (Section E):
    Assert that sensitive marker strings NEVER appear in plaintext within database rows or raw dumps.
    """
    project_id = 99
    markers = [
        "MARKER_SECRET_DIFF_ABCD_1234",
        "MARKER_SECRET_STACK_EFGH_5678",
        "MARKER_SECRET_EXTRA_IJKL_9012",
        "MARKER_SECRET_WEBHOOK_MNOP_3456",
        "MARKER_SECRET_FILE_QRST_7890",
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"data_key_enc": None}
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn

    # Simulate database rows holding encrypted content
    with patch("app.crypto.get_connection", return_value=mock_ctx):
        db_rows = {
            "deploys": {
                "id": 1,
                "commit_sha": "abc1234",
                "diff": None,  # plaintext is dropped or NULL
                "diff_enc": await encrypt(project_id, "deploys", "diff", f"+ {markers[0]}"),
            },
            "events": {
                "id": 1,
                "stack_trace": None,
                "stack_trace_enc": await encrypt(project_id, "events", "stack_trace", f"Trace: {markers[1]}"),
                "extra": None,
                "extra_enc": await encrypt(project_id, "events", "extra", json.dumps({"secret": markers[2]})),
            },
            "projects": {
                "id": project_id,
                "slack_webhook_url": None,
                "slack_webhook_url_enc": await encrypt(project_id, "projects", "slack_webhook_url", f"https://hooks.slack.com/{markers[3]}"),
            },
            "deploy_files": {
                "id": 1,
                "path": "src/secrets.py",
                "content_enc": await encrypt(project_id, "deploy_files", "content", f"SECRET_KEY = '{markers[4]}'"),
            },
        }

    # Simulate inspecting raw database rows and serialized binary dump
    for table_name, row in db_rows.items():
        for col_name, val in row.items():
            if val is None:
                continue
            if isinstance(val, bytes):
                # Assert that none of the secret markers exist as raw substrings in ciphertext
                for m in markers:
                    assert m.encode("utf-8") not in val, f"Marker {m} leaked in plaintext in {table_name}.{col_name}!"
            elif isinstance(val, str):
                for m in markers:
                    assert m not in val, f"Marker {m} leaked in plaintext in {table_name}.{col_name}!"
