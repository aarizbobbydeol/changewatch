"""Events are scrubbed on the server too: any HTTP client can send them, not only the SDK."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker import process_event_batch, scrub_event


def test_scrub_event_redacts_text_fields_and_nested_extra():
    ev = {
        "type": "error",
        "message": "login failed for ana@example.com with password=hunter22",
        "stack_trace": 'File "app/auth.py", line 3\n  token = "ghp_' + "a" * 36 + '"',
        "request_path": "/users/ana@example.com",
        "extra": {"headers": {"api_key": "api_key=abcd1234efgh"}, "ips": ["10.1.2.3"], "count": 3},
    }
    scrub_event(ev)
    assert "ana@example.com" not in ev["message"] and "hunter22" not in ev["message"]
    assert "ghp_" not in ev["stack_trace"]
    assert ev["request_path"] == "/users/[REDACTED_EMAIL]"
    assert "abcd1234efgh" not in ev["extra"]["headers"]["api_key"]
    assert ev["extra"]["ips"] == ["[REDACTED_IP]"] and ev["extra"]["count"] == 3


def test_scrub_event_is_idempotent():
    ev = {"message": "password=hunter22 from 10.0.0.1"}
    scrub_event(ev)
    once = dict(ev)
    scrub_event(ev)
    assert ev == once


@pytest.mark.asyncio
async def test_batch_stores_scrubbed_title_and_stack_trace(accept_event_claims):
    mock_conn = AsyncMock()
    accept_event_claims(mock_conn)
    mock_conn.fetchrow.return_value = None
    mock_conn.fetch.return_value = []
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=mock_conn)
    tx.__aexit__ = AsyncMock(return_value=False)

    batch = [{
        "event_id": "s1",
        "project_id": 1,
        "type": "error",
        "exc_type": "PermissionError",
        "message": "denied for ana@example.com",
        "stack_trace": "secret=topsecretvalue",
        "occurred_at": 1_800_000_000.0,
    }]
    encrypted = []

    async def fake_encrypt_or_keep(project_id, table, column, value):
        encrypted.append((column, value))
        return None, b"enc"

    with patch("app.worker.project_transaction", return_value=tx), \
         patch("app.worker.encrypt_or_keep", side_effect=fake_encrypt_or_keep), \
         patch("app.worker.get_redis", side_effect=RuntimeError("no redis")), \
         patch("app.worker.check_incident_for_issue", new=AsyncMock()):
        await process_event_batch(batch)

    issues_call = next(c for c in mock_conn.fetch.call_args_list if "INSERT INTO issues" in c.args[0])
    assert issues_call.args[3] == ["PermissionError: denied for [REDACTED_EMAIL]"]  # titles
    assert ("stack_trace", "secret=[REDACTED_SECRET]") in encrypted
