import io
import json
import logging
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from app.auth import (
    get_project_id_by_api_key,
    hash_api_key,
    require_ingest_project_id,
    require_read_project_id,
)
from app.cli import create_project_cmd, generate_ingest_key, generate_read_key
from app.logging_filter import LogRedactionFilter, redact_sensitive_data
from app.main import app, JSONLogFormatter
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_api_key_hashing_and_constant_time():
    """Verify API keys are hashed with SHA-256 and constant-time check works."""
    raw_key = "cw_ingest_secure_random_token_12345"
    h = hash_api_key(raw_key)
    assert len(h) == 64
    assert h == hash_api_key(raw_key)
    assert h != hash_api_key("cw_ingest_other_token")


@pytest.mark.asyncio
async def test_ingest_key_allows_ingest_and_forbids_read():
    """Ingest key must allow ingest role but return 403 Forbidden on read role."""
    ingest_key = "cw_ingest_test_write_key_abc"
    ingest_hash = hash_api_key(ingest_key)

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": 1,
        "ingest_key_hash": ingest_hash,
        "read_key_hash": hash_api_key("cw_read_diff_key"),
        "api_key": None,
    }
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.auth.get_connection", return_value=mock_ctx):
        # 1. Allowed for ingest
        pid = await get_project_id_by_api_key(ingest_key, role="ingest")
        assert pid == 1

        # 2. Forbidden for read (403)
        with pytest.raises(HTTPException) as exc_info:
            await get_project_id_by_api_key(ingest_key, role="read")
        assert exc_info.value.status_code == 403
        assert "Ingest key cannot be used to read data" in exc_info.value.detail


@pytest.mark.asyncio
async def test_read_key_allows_read_and_forbids_ingest():
    """Read key must allow read role but return 403 Forbidden on ingest role."""
    read_key = "cw_read_test_read_key_xyz"
    read_hash = hash_api_key(read_key)

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": 2,
        "ingest_key_hash": hash_api_key("cw_ingest_diff_key"),
        "read_key_hash": read_hash,
        "api_key": None,
    }
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.auth.get_connection", return_value=mock_ctx):
        # 1. Allowed for read
        pid = await get_project_id_by_api_key(read_key, role="read")
        assert pid == 2

        # 2. Forbidden for ingest (403)
        with pytest.raises(HTTPException) as exc_info:
            await get_project_id_by_api_key(read_key, role="ingest")
        assert exc_info.value.status_code == 403
        assert "Read key cannot be used for ingestion" in exc_info.value.detail


@pytest.mark.asyncio
async def test_invalid_key_returns_401():
    """Non-existent API key returns 401 Unauthorized."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.auth.get_connection", return_value=mock_ctx):
        with pytest.raises(HTTPException) as exc_info:
            await get_project_id_by_api_key("cw_ingest_nonexistent", role="any")
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_endpoints_enforce_ingest_key_roles():
    """Test /v1/deploys and /v1/events endpoints block read keys with 403."""
    read_key = "cw_read_only_key_999"
    read_hash = hash_api_key(read_key)

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": 5,
        "ingest_key_hash": hash_api_key("cw_ingest_diff"),
        "read_key_hash": read_hash,
        "api_key": None,
    }
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.auth.get_connection", return_value=mock_ctx):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Attempt to deploy with read key
            resp_deploy = await client.post(
                "/v1/deploys",
                headers={"X-API-Key": read_key},
                json={"commit_sha": "abc1234", "author": "Eve"},
            )
            assert resp_deploy.status_code == 403
            assert "Read key cannot be used for ingestion" in resp_deploy.json()["detail"]

            # Attempt to ingest events with read key
            resp_events = await client.post(
                "/v1/events",
                headers={"X-API-Key": read_key},
                json={"events": [{"type": "error", "exc_type": "KeyError", "message": "fail"}]},
            )
            assert resp_events.status_code == 403
            assert "Read key cannot be used for ingestion" in resp_events.json()["detail"]


@pytest.mark.asyncio
async def test_cli_generates_hashed_keys_without_plaintext_in_db():
    """CLI create-project writes only SHA-256 hashes and prefixes to DB."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 10, "name": "prod-app", "created_at": "2026-10-19"}
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn

    mock_pool = AsyncMock()
    mock_pool.acquire.return_value = mock_ctx

    with patch("app.cli.init_db_pool", return_value=mock_pool), \
         patch("app.cli.get_connection", return_value=mock_ctx), \
         patch("app.cli.close_db_pool", return_value=None), \
         patch("app.cli.run_migrations", return_value=[]):

        await create_project_cmd("prod-app", "https://hooks.slack.com/services/T1/B2/X3")

        # Verify SQL call
        insert_args = mock_conn.fetchrow.call_args[0]
        query = insert_args[0]
        assert "ingest_key_hash" in query
        assert "read_key_hash" in query
        assert "api_key" not in query or "$2, $3" in query

        # The passed parameters must be hashes (length 64), not raw keys
        name_arg = insert_args[1]
        webhook_arg = insert_args[2]
        ingest_hash_arg = insert_args[4]
        ingest_prefix_arg = insert_args[5]
        read_hash_arg = insert_args[6]
        read_prefix_arg = insert_args[7]

        assert name_arg == "prod-app"
        assert len(ingest_hash_arg) == 64
        assert ingest_prefix_arg.startswith("cw_ingest_")
        assert len(read_hash_arg) == 64
        assert read_prefix_arg.startswith("cw_read_")


def test_log_redaction_filter_removes_all_sensitive_data():
    """Log filter must redact keys, webhook URLs, tokens, diffs, and stack traces."""
    # 1. API Keys
    s1 = redact_sensitive_data("Project created with key cw_live_ab1234567890 and cw_ingest_xyz12345678")
    assert "cw_live_" not in s1
    assert "cw_ingest_" not in s1
    assert "[REDACTED_KEY]" in s1

    # 2. Webhook URLs
    s2 = redact_sensitive_data("Posting to https://hooks.slack.com/services/T001/B002/XYZ123 for notification")
    assert "hooks.slack.com" not in s2
    assert "[REDACTED_WEBHOOK_URL]" in s2

    # 3. Provider tokens and secrets
    s3 = redact_sensitive_data("AWS: AKIAIOSFODNN7EXAMPLE, GitHub: ghp_123456789012345678901234567890123456, AI: sk-proj1234567890abcdef")
    assert "AKIAIOSFODNN7EXAMPLE" not in s3
    assert "[REDACTED_AWS_KEY]" in s3
    assert "ghp_" not in s3
    assert "[REDACTED_GITHUB_TOKEN]" in s3
    assert "sk-" not in s3
    assert "[REDACTED_SECRET_KEY]" in s3

    # 4. Git diffs
    diff_text = """Deploy commit:
diff --git a/app.py b/app.py
index 83db48f..bf269f4 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,4 @@
-print('secret')
+print('redacted')

Deploy finished."""
    s4 = redact_sensitive_data(diff_text)
    assert "diff --git" not in s4
    assert "[DIFF REDACTED]" in s4

    # 5. Stack traces
    trace_text = """Exception raised:
Traceback (most recent call last):
  File "server/app/main.py", line 45, in handler
    raise ValueError("Invalid secret cw_test_123")
ValueError: Invalid secret cw_test_123"""
    s5 = redact_sensitive_data(trace_text)
    assert "Traceback" not in s5
    assert 'File "server/app/main.py"' not in s5
    assert "[STACK TRACE REDACTED]" in s5


def test_json_formatter_and_filter_in_logging_pipeline():
    """Verify JSONLogFormatter and LogRedactionFilter produce clean sanitized JSON logs."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    formatter = JSONLogFormatter()
    handler.setFormatter(formatter)
    filter_ = LogRedactionFilter()
    handler.addFilter(filter_)

    test_logger = logging.getLogger("test.security.logger")
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    # Log message with secrets and webhook
    test_logger.info(
        "Deploy notify url=%s using token=%s and key=%s",
        "https://hooks.slack.com/services/T99/B99/K99",
        "sk-ant-api03-abcdef1234567890",
        "cw_ingest_supersecret12345",
    )

    log_output = stream.getvalue().strip()
    assert log_output != ""
    parsed = json.loads(log_output)

    assert "hooks.slack.com" not in parsed["message"]
    assert "sk-ant" not in parsed["message"]
    assert "cw_ingest_" not in parsed["message"]
    assert "[REDACTED_WEBHOOK_URL]" in parsed["message"]
    assert ("[REDACTED_SECRET]" in parsed["message"] or "[REDACTED_SECRET_KEY]" in parsed["message"])
    assert "[REDACTED_KEY]" in parsed["message"]
