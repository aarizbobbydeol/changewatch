"""Milestone 9: Opt-In Telemetry, Privacy Allowlist & Owner View Tests.

Specified in Master Prompt Section 6:
1. Strict allowlist validation: extra fields and forbidden fields raise errors.
2. Zero network calls when telemetry is off (default).
3. Telemetry preview endpoint exposes exact compliant payload.
4. Collector service registers allowlisted payloads and generates aggregates.
5. Owner stats endpoint enforces admin authentication and zero tenant identifiers.
6. Privacy bucketing functions.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from httpx import ASGITransport
import pytest
from pydantic import ValidationError

from app.config import settings
from app.main import app
from app.telemetry import (
    APP_VERSION,
    TelemetryPayload,
    assert_allowlist_compliance,
    bucket_count,
    send_telemetry_report,
)
from collector.server import app as collector_app, reset_collector_store


@pytest.fixture(autouse=True)
def reset_telemetry_config():
    old_enabled = settings.TELEMETRY_ENABLED
    old_collector = settings.TELEMETRY_COLLECTOR_URL
    settings.TELEMETRY_ENABLED = False
    settings.TELEMETRY_COLLECTOR_URL = "http://collector.local"
    yield
    settings.TELEMETRY_ENABLED = old_enabled
    settings.TELEMETRY_COLLECTOR_URL = old_collector


# ==============================================================================
# 1. Allowlist and Forbidden Fields Enforcement
# ==============================================================================
def test_telemetry_allowlist_strict():
    """Verify strictly allowlisted fields pass and extra fields fail."""
    valid_data = {
        "install_id": "inst_123456",
        "version": APP_VERSION,
        "run_mode": "docker",
        "os_family": "linux",
        "ai_provider_type": "rules",
        "channel_types": ["slack"],
        "release_count_bucket": "1-10",
        "incident_count_bucket": "0",
    }
    payload = TelemetryPayload.model_validate(valid_data)
    assert payload.install_id == "inst_123456"
    assert_allowlist_compliance(valid_data)

    # Extra field rejected
    with pytest.raises(ValidationError):
        TelemetryPayload.model_validate({**valid_data, "unrecognized_metric": 42})


@pytest.mark.parametrize("forbidden_field", [
    "repo",
    "repository_name",
    "code",
    "diff_content",
    "file_path",
    "email",
    "ip_address",
    "stack_trace",
    "error_message",
    "user_token",
])
def test_telemetry_forbidden_fields_rejected(forbidden_field: str):
    """Verify that any dictionary containing forbidden privacy keywords fails compliance."""
    data = {
        "install_id": "inst_123456",
        "version": APP_VERSION,
        "run_mode": "docker",
        "os_family": "linux",
        "ai_provider_type": "rules",
        "channel_types": ["slack"],
        "release_count_bucket": "1-10",
        "incident_count_bucket": "0",
        forbidden_field: "sensitive_data",
    }
    with pytest.raises((ValidationError, ValueError)):
        assert_allowlist_compliance(data)


# ==============================================================================
# 2. Zero Network Calls Guarantee When Disabled
# ==============================================================================
@pytest.mark.asyncio
async def test_telemetry_zero_network_when_disabled():
    """Guarantee: when telemetry is disabled, zero network calls occur."""
    settings.TELEMETRY_ENABLED = False
    mock_client = AsyncMock()

    result = await send_telemetry_report(conn_or_pool=None, client=mock_client)
    assert result is False
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_telemetry_network_call_when_enabled():
    """Verify network call is made with allowlisted JSON when telemetry is enabled."""
    settings.TELEMETRY_ENABLED = True
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    with patch("app.telemetry.build_telemetry_payload") as mock_build:
        mock_build.return_value = TelemetryPayload(
            install_id="test_uuid",
            version="0.1.0",
            run_mode="standalone",
            os_family="darwin",
            ai_provider_type="rules",
            channel_types=["slack"],
            release_count_bucket="1-10",
            incident_count_bucket="0",
        )
        result = await send_telemetry_report(conn_or_pool=None, client=mock_client)
        assert result is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://collector.local/collect"
        sent_json = call_args[1]["json"]
        assert sent_json["install_id"] == "test_uuid"
        assert "repo" not in sent_json
        assert "code" not in sent_json


# ==============================================================================
# 3. Privacy Bucketing Logic
# ==============================================================================
def test_bucketing_coarse_privacy():
    assert bucket_count(0) == "0"
    assert bucket_count(-5) == "0"
    assert bucket_count(1) == "1-10"
    assert bucket_count(10) == "1-10"
    assert bucket_count(11) == "11-50"
    assert bucket_count(50) == "11-50"
    assert bucket_count(51) == "51-100"
    assert bucket_count(100) == "51-100"
    assert bucket_count(101) == "100+"
    assert bucket_count(5000) == "100+"


# ==============================================================================
# 4. Collector Service Integration
# ==============================================================================
@pytest.mark.asyncio
async def test_collector_service_flow():
    """Verify independent collector registers reports and returns aggregate stats."""
    reset_collector_store()

    transport = ASGITransport(app=collector_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Healthcheck
        h_res = await client.get("/healthz")
        assert h_res.status_code == 200
        assert h_res.json()["installs_registered"] == 0

        # Send report 1
        p1 = {
            "install_id": "inst_001",
            "version": "0.1.0",
            "run_mode": "docker",
            "os_family": "linux",
            "ai_provider_type": "rules",
            "channel_types": ["slack"],
            "release_count_bucket": "1-10",
            "incident_count_bucket": "0",
        }
        res1 = await client.post("/collect", json=p1)
        assert res1.status_code == 200

        # Send report 2
        p2 = {
            "install_id": "inst_002",
            "version": "0.1.0",
            "run_mode": "standalone",
            "os_family": "darwin",
            "ai_provider_type": "anthropic",
            "channel_types": ["discord", "slack"],
            "release_count_bucket": "11-50",
            "incident_count_bucket": "1-10",
        }
        res2 = await client.post("/collect", json=p2)
        assert res2.status_code == 200

        # Attempt to send extra forbidden field -> 422
        bad_p = {**p1, "code_snippet": "print(1)"}
        res_bad = await client.post("/collect", json=bad_p)
        assert res_bad.status_code == 422

        # Verify aggregate stats
        stats_res = await client.get("/stats")
        assert stats_res.status_code == 200
        stats = stats_res.json()
        assert stats["opted_in_installs"] == 2
        assert stats["active_this_week"] == 2
        assert stats["new_this_week"] == 2
        assert stats["run_mode_mix"] == {"docker": 1, "standalone": 1}
        assert stats["channel_mix"] == {"slack": 2, "discord": 1}
        assert len(stats["privacy_guarantees"]) >= 5


# ==============================================================================
# 5. Server API Endpoints
# ==============================================================================
@pytest.mark.asyncio
async def test_telemetry_preview_and_status_api():
    """Verify preview and status endpoints return allowlist-compliant info."""
    from app.auth import require_read_project_id
    app.dependency_overrides[require_read_project_id] = lambda: 1

    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"X-API-Key": "cw_read_key"}

            # Preview endpoint
            prev_res = await client.get("/v1/telemetry/preview", headers=headers)
            assert prev_res.status_code == 200
            preview = prev_res.json()
            assert "install_id" in preview
            assert "version" in preview
            assert "release_count_bucket" in preview
            # Assert allowlist strictly satisfied
            assert_allowlist_compliance(preview)

            # Status endpoint
            status_res = await client.get("/v1/telemetry/status", headers=headers)
            assert status_res.status_code == 200
            status_data = status_res.json()
            assert status_data["default"] == "off"

            # Opt-in toggle
            opt_res = await client.post("/v1/telemetry/opt-in", json={"enabled": True}, headers=headers)
            assert opt_res.status_code == 200
            assert opt_res.json()["enabled"] is True
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)


@pytest.mark.asyncio
async def test_owner_stats_admin_auth():
    """Verify /v1/owner/stats is protected by require_admin_key."""
    from app.auth import require_admin_key

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthorized without admin key
        unauth_res = await client.get("/v1/owner/stats")
        assert unauth_res.status_code in (401, 403)

        # Authorized with admin key override
        app.dependency_overrides[require_admin_key] = lambda: True
        try:
            auth_res = await client.get("/v1/owner/stats", headers={"X-Admin-Key": "cw_admin_key"})
            assert auth_res.status_code == 200
            data = auth_res.json()
            assert "opted_in_installs" in data
            assert "privacy_guarantees" in data
        finally:
            app.dependency_overrides.pop(require_admin_key, None)

