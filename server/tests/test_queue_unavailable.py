"""When the event queue (Redis) is down, ingest answers 503 + Retry-After and the SDK keeps the events."""
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.main import QUEUE_RETRY_AFTER_SEC, app

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))
from changewatch import Client  # noqa: E402

EVENT = {"type": "error", "exc_type": "KeyError", "message": "boom"}


@pytest.mark.asyncio
async def test_ingest_returns_503_with_retry_after_when_queue_is_down():
    mock_redis = AsyncMock()
    mock_redis.get.return_value = "1"
    mock_redis.llen.return_value = 0
    pipe = MagicMock()
    pipe.execute = AsyncMock(side_effect=ConnectionError("Error connecting to redis://:hunter2@redis:6379/0"))
    mock_redis.pipeline = MagicMock(return_value=pipe)

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.main.get_redis", return_value=mock_redis), \
         patch("app.main.check_rate_limit", new_callable=AsyncMock, return_value=(True, 1, 600)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/events",
                headers={"X-API-Key": "cw_test_key_123"},
                json={"events": [{**EVENT, "occurred_at": time.time()}]},
            )

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == str(QUEUE_RETRY_AFTER_SEC)
    assert "hunter2" not in resp.text
    mock_redis.pipeline.assert_called_with(transaction=True)


class _Server:
    """Tiny local HTTP server answering every POST with a fixed status and headers."""

    def __init__(self, status, headers=None):
        outer = self
        self.posts = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.posts += 1
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def _client(endpoint):
    client = Client(api_key="cw_ingest_test", endpoint=endpoint, environment="prod")
    client.stop()  # drive _send_batch directly, without the background thread
    if client._worker_thread is not None:
        client._worker_thread.join(timeout=5)
        assert not client._worker_thread.is_alive()
    return client


def test_sdk_keeps_events_and_pauses_for_retry_after_on_503():
    server = _Server(503, {"Retry-After": "120"})
    try:
        client = _client(server.url)
        batch = [{**EVENT, "occurred_at": time.time()} for _ in range(3)]
        client._send_batch(batch)

        assert server.posts == 1
        assert client._queue.qsize() == 3  # nothing lost
        pause = client._circuit_open_until - time.monotonic()
        assert 110 < pause <= 120
    finally:
        server.close()


def test_sdk_does_not_retry_a_rejected_batch():
    server = _Server(422)
    try:
        client = _client(server.url)
        client._send_batch([{**EVENT, "occurred_at": time.time()}])
        assert client._queue.qsize() == 0  # a 4xx means this batch is bad: retrying would loop forever
        assert client._circuit_open_until == 0.0
    finally:
        server.close()


def test_sdk_requeues_on_network_failure_within_bounds():
    client = _client("http://127.0.0.1:9")  # nothing listens on the discard port
    client._queue.maxsize = 2
    client._send_batch([{**EVENT, "occurred_at": time.time()} for _ in range(3)])
    assert client._queue.qsize() == 2
    assert client._dropped_events == 1


def test_retry_after_is_clamped():
    client = _client("http://127.0.0.1:9")
    assert client._retry_after_seconds({"Retry-After": "99999"}) == 600.0
    assert client._retry_after_seconds({"Retry-After": "0"}) == 1.0
    assert client._retry_after_seconds({"Retry-After": "soon"}) == 30.0
    assert client._retry_after_seconds({}) == 30.0
