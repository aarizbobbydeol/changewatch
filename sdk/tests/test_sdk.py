import os
import sys
import time
from unittest.mock import MagicMock, patch
import pytest

# Ensure sdk is in sys.path
from pathlib import Path
sdk_root = Path(__file__).resolve().parent.parent
if str(sdk_root) not in sys.path:
    sys.path.insert(0, str(sdk_root))

import changewatch
from changewatch import Client, _scrub, _scrub_dict


def test_sdk_init_and_env_release(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "test_commit_sha_from_env")
    client = changewatch.init(api_key="test_key", release=None, endpoint="http://localhost:8000")
    assert client.release == "test_commit_sha_from_env"
    assert client.api_key == "test_key"
    assert client.endpoint == "http://localhost:8000"


def test_sdk_secret_scrubbing():
    raw_message = "Error with api_key='secret123' and key AKIA1234567890ABCDEF"
    scrubbed = _scrub(raw_message)
    assert "secret123" not in scrubbed
    assert "AKIA1234567890ABCDEF" not in scrubbed
    assert "[REDACTED_AWS_KEY]" in scrubbed
    assert "[REDACTED_SECRET]" in scrubbed

    extra = {
        "token": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
        "nested": {"sk": "sk-1234567890abcdefghijklmnopqrstuvwxyz"},
    }
    clean_extra = _scrub_dict(extra)
    assert "ghp_" not in clean_extra["token"]
    assert "sk-" not in clean_extra["nested"]["sk"]


def test_sdk_bounded_queue_drops_without_raising():
    """Verify that when queue reaches 1,000 capacity, new events are dropped without raising."""
    client = Client(api_key="test_key", endpoint="http://localhost:9999")
    # Stop background loop for exact queue count inspection
    client._running = False

    # Fill queue to max capacity (1000)
    while not client._queue.full():
        try:
            client._queue.put_nowait({"type": "slow_request", "duration_ms": 2000})
        except Exception:
            break
    assert client._queue.full()

    # Next capture should silently drop without raising queue.Full
    client.capture_slow_request("/path", 2000)
    assert client._queue.qsize() == 1000


def test_sdk_never_raises_on_server_down():
    """Verify SDK _send_batch swallows network errors and never raises."""
    client = Client(api_key="test_key", endpoint="http://127.0.0.1:54321")
    # Port 54321 is not running anything
    batch = [{"type": "error", "message": "test", "occurred_at": time.time()}]
    # Calling _send_batch should complete without exception
    client._send_batch(batch)


@pytest.mark.asyncio
async def test_asgi_middleware_re_raises_and_captures():
    """Verify ASGIMiddleware captures exception and ALWAYS re-raises it."""
    client = Client(api_key="test_key", endpoint="http://localhost:8000")
    client._running = False  # Keep queue local

    async def crashing_app(scope, receive, send):
        raise ZeroDivisionError("Crash inside ASGI app")

    middleware = changewatch.ASGIMiddleware(crashing_app, client=client)

    scope = {"type": "http", "path": "/crash", "method": "GET"}

    # Must re-raise ZeroDivisionError
    with pytest.raises(ZeroDivisionError):
        await middleware(scope, None, None)

    # Event must be queued
    assert not client._queue.empty()
    queued_event = client._queue.get_nowait()
    assert queued_event["type"] == "error"
    assert queued_event["exc_type"] == "ZeroDivisionError"
    assert queued_event["request_path"] == "/crash"


@pytest.mark.asyncio
async def test_asgi_middleware_captures_slow_request():
    """Verify ASGIMiddleware captures slow request when duration >= slow_ms."""
    client = Client(api_key="test_key", slow_ms=50.0, endpoint="http://localhost:8000")
    client._running = False

    async def slow_app(scope, receive, send):
        import asyncio
        await asyncio.sleep(0.06)  # 60ms > 50ms

    middleware = changewatch.ASGIMiddleware(slow_app, client=client)
    scope = {"type": "http", "path": "/slow-query", "method": "POST"}

    await middleware(scope, None, None)

    assert not client._queue.empty()
    event = client._queue.get_nowait()
    assert event["type"] == "slow_request"
    assert event["request_path"] == "/slow-query"
    assert event["duration_ms"] >= 50.0


def test_sdk_traffic_heartbeat():
    """Verify record_request aggregates and emits heartbeat when flush() is called."""
    client = Client(api_key="test_key", endpoint="http://localhost:8000")
    client._running = False

    # Simulate 10 requests, 2 errors
    for _ in range(8):
        client.record_request(has_error=False)
    for _ in range(2):
        client.record_request(has_error=True)

    # Calling flush forces heartbeat emission
    with patch.object(client, "_send_batch") as mock_send:
        client.flush()
        mock_send.assert_called()
        sent_batch = mock_send.call_args[0][0]
        heartbeats = [e for e in sent_batch if e.get("type") == "heartbeat"]
        assert len(heartbeats) == 1
        assert heartbeats[0]["requests"] == 10
        assert heartbeats[0]["errors"] == 2
