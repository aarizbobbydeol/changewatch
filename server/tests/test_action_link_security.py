"""Signed action links and Slack interactions cannot be forged or used for XSS."""
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import httpx
import pytest

import app.trust.actions as actions
from app.crypto import get_master_key_bytes
from app.main import app


def test_link_secret_is_derived_not_the_master_key(monkeypatch):
    monkeypatch.setattr(actions.settings, "LINK_SIGNING_SECRET", "")
    secret = actions._get_link_secret()
    assert secret != get_master_key_bytes()
    assert secret != b"changewatch-link-default-secret"
    monkeypatch.setattr(actions.settings, "LINK_SIGNING_SECRET", "explicit-secret")
    assert actions._get_link_secret() == b"explicit-secret"


def test_token_signed_with_old_default_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(actions.settings, "LINK_SIGNING_SECRET", "")
    import base64

    payload = f"12:resolve:1:{int(time.time()) + 3600}"
    sig = hmac.new(b"changewatch-link-default-secret", payload.encode(), hashlib.sha256).hexdigest()
    forged = base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()
    valid, _, err = actions.verify_signed_action_token(forged)
    assert valid is False and "signature" in err


@pytest.mark.asyncio
async def test_invalid_link_page_escapes_reflected_input():
    import base64

    evil = base64.urlsafe_b64encode(b"<script>alert(1)</script>:ack:1:9:x").decode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/a/{evil}")
    assert resp.status_code == 400
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def _slack_form(action_value):
    payload = {"user": {"username": "mallory"}, "actions": [{"value": action_value}]}
    return urlencode({"payload": json.dumps(payload)}).encode()


@pytest.mark.asyncio
async def test_slack_interactions_disabled_without_signing_secret(monkeypatch):
    monkeypatch.setattr(actions.settings, "SLACK_SIGNING_SECRET", "")
    with patch("app.trust.actions.execute_incident_action", new=AsyncMock()) as execute:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/integrations/slack/interactions",
                content=_slack_form("resolve:5:2"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    assert resp.status_code == 404
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_interactions_reject_bad_signature(monkeypatch):
    monkeypatch.setattr(actions.settings, "SLACK_SIGNING_SECRET", "slack-secret")
    with patch("app.trust.actions.execute_incident_action", new=AsyncMock()) as execute:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/integrations/slack/interactions",
                content=_slack_form("resolve:5:2"),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Slack-Request-Timestamp": str(int(time.time())),
                    "X-Slack-Signature": "v0=" + "0" * 64,
                },
            )
    assert resp.status_code == 403
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_interactions_accept_valid_signature(monkeypatch):
    monkeypatch.setattr(actions.settings, "SLACK_SIGNING_SECRET", "slack-secret")
    body = _slack_form("ack:5:2")
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"slack-secret", f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()

    tx_conn = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=tx_conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch("app.trust.actions.execute_incident_action", new=AsyncMock()) as execute, \
         patch("app.main.project_transaction", return_value=ctx):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/integrations/slack/interactions",
                content=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Slack-Request-Timestamp": ts,
                    "X-Slack-Signature": sig,
                },
            )
    assert resp.status_code == 200
    execute.assert_awaited_once()
    assert execute.await_args.args[1:4] == (2, 5, "ack")
