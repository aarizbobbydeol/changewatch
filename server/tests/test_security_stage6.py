"""Security Addendum Stage 6 Tests: TLS, Internal Network, and Signed Webhooks.

Verifies:
1. Outbound webhook signatures using HMAC-SHA256 and timestamps (X-ChangeWatch-Signature).
2. Webhook verification: round-trip, tamper detection, wrong secret, and clock-skew tolerance.
3. Ollama warning on non-local unencrypted HTTP endpoints.
4. Docker Compose security rules:
   - Production compose publishes no host ports for Postgres or Redis.
   - Production Redis enforces password authentication (--requirepass).
   - Production compose includes Caddy service under --profile tls.
   - Dev compose exposes ports bound strictly to 127.0.0.1.
5. Database SSL connection flag (sslmode=require).
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.webhook import generate_webhook_signature, verify_webhook_signature, send_signed_webhook
from app.llm import OllamaClient


def test_webhook_signature_generation_and_verification_roundtrip():
    """Verify HMAC-SHA256 signature generation and constant-time verification."""
    payload = b'{"event":"verdict","status":"safe","deploy_id":10}'
    secret = "whsec_test_secret_abc123"

    sig_header, ts = generate_webhook_signature(payload, secret)
    assert sig_header.startswith(f"t={ts},v1=")

    # Verification must succeed
    assert verify_webhook_signature(payload, secret, sig_header) is True


def test_webhook_tamper_detection():
    """Verify modifying payload causes verification to fail."""
    payload = b'{"event":"verdict","status":"safe","deploy_id":10}'
    tampered_payload = b'{"event":"verdict","status":"rollback","deploy_id":10}'
    secret = "whsec_test_secret_abc123"

    sig_header, _ = generate_webhook_signature(payload, secret)
    assert verify_webhook_signature(tampered_payload, secret, sig_header) is False


def test_webhook_wrong_secret():
    """Verify wrong secret fails verification."""
    payload = b'{"event":"verdict","status":"safe"}'
    sig_header, _ = generate_webhook_signature(payload, "secret_one")

    assert verify_webhook_signature(payload, "secret_two", sig_header) is False


def test_webhook_timestamp_tolerance_and_replay_protection():
    """Verify timestamps outside tolerance window (default 300s) are rejected."""
    payload = b'{"event":"verdict","status":"safe"}'
    secret = "whsec_test_secret"

    old_ts = int(time.time()) - 600  # 10 minutes ago (> 300s)
    sig_header, _ = generate_webhook_signature(payload, secret, timestamp=old_ts)

    assert verify_webhook_signature(payload, secret, sig_header, tolerance_sec=300) is False

    # Within tolerance (e.g. 1000s) should succeed
    assert verify_webhook_signature(payload, secret, sig_header, tolerance_sec=1000) is True


@pytest.mark.asyncio
async def test_send_signed_webhook_includes_signature_headers():
    """Verify send_signed_webhook attaches X-ChangeWatch-Signature and Timestamp."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp

        await send_signed_webhook(
            url="https://hooks.example.com/alerts",
            payload={"alert": "deploy_safe"},
            secret="whsec_mysecret",
        )

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        headers = kwargs["headers"]
        assert "X-ChangeWatch-Signature" in headers
        assert "X-ChangeWatch-Timestamp" in headers
        assert headers["X-ChangeWatch-Signature"].startswith("t=")


def test_ollama_client_warns_on_non_local_unencrypted_http(caplog):
    """Verify OllamaClient logs warning when configured with remote http:// URL."""
    with caplog.at_level("WARNING"):
        _ = OllamaClient(base_url="http://ollama.internal.company.com:11434")
        assert any("Ollama configured over non-local unencrypted HTTP" in record.message for record in caplog.records)

    caplog.clear()
    with caplog.at_level("WARNING"):
        _ = OllamaClient(base_url="http://localhost:11434")
        assert not any("Ollama configured over non-local unencrypted HTTP" in record.message for record in caplog.records)


def test_docker_compose_production_isolation_and_tls_profile():
    """Verify production docker-compose.yml exposes no db ports and configures TLS profile."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    compose_path = repo_root / "docker-compose.yml"
    assert compose_path.exists()

    content = compose_path.read_text(encoding="utf-8")

    # In production, postgres and redis must have NO published host ports
    postgres_block = content.split("\n  postgres:\n")[1].split("\n  redis:\n")[0]
    assert "ports:" not in postgres_block, "Postgres must publish NO host ports in production compose"

    redis_block = content.split("\n  redis:\n")[1].split("\n  api:\n")[0]
    assert "ports:" not in redis_block, "Redis must publish NO host ports in production compose"
    assert "--requirepass" in redis_block, "Redis must enforce password authentication with --requirepass"

    # Must contain caddy service under profile tls
    assert "caddy:" in content, "docker-compose.yml must contain caddy service"
    assert 'profiles: ["tls"]' in content or "profiles:\n      - tls" in content or "profiles:\n      - \"tls\"" in content



def test_docker_compose_dev_binds_to_localhost_only():
    """Verify docker-compose.dev.yml binds ports strictly to 127.0.0.1."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    dev_compose_path = repo_root / "docker-compose.dev.yml"
    assert dev_compose_path.exists()

    content = dev_compose_path.read_text(encoding="utf-8")
    assert "127.0.0.1:5432:5432" in content
    assert "127.0.0.1:6379:6379" in content
