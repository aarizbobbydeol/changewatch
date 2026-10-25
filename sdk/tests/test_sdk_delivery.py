"""What leaves the SDK, and when: exit flush, redirects, limits and scrubbing."""
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sdk_root = Path(__file__).resolve().parent.parent
if str(sdk_root) not in sys.path:
    sys.path.insert(0, str(sdk_root))

import changewatch  # noqa: E402
from changewatch import Client  # noqa: E402


class Recorder:
    """A local HTTP server that records requests and answers with `status` (and `location`)."""

    def __init__(self, status=200, location=None):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"method": self.command, "headers": dict(self.headers), "body": body})
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_GET = _handle

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def events(self):
        return [e for r in self.requests if r["body"] for e in json.loads(r["body"])["events"]]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def recorder():
    rec = Recorder()
    yield rec
    rec.close()


def _boom():
    try:
        raise RuntimeError("crash right after start")
    except RuntimeError as exc:
        return exc


def test_exit_flush_sends_the_batch_the_worker_is_holding(recorder):
    client = Client(api_key="cw_ingest_test", endpoint=recorder.url)
    client.capture_exception(_boom())
    deadline = time.time() + 2
    while not client._queue.empty() and time.time() < deadline:
        time.sleep(0.01)  # the worker has taken it into its batch (sent only after 2 s)
    client._flush_at_exit()
    assert [e["message"] for e in recorder.events() if e["type"] == "error"] == ["crash right after start"]


def test_exit_flush_gives_up_quickly_when_the_server_is_down():
    client = Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9")  # nothing listens
    client._running = False
    client._worker_thread.join(timeout=3)
    for _ in range(200):
        client.capture_exception(_boom())
    start = time.monotonic()
    client._flush_at_exit()
    assert time.monotonic() - start < 5  # not 10 batches x 3 s timeouts
    assert client._dropped_events >= 200


def test_redirects_are_not_followed_and_the_key_stays_put(recorder):
    redirector = Recorder(status=301, location=f"{recorder.url}/elsewhere")
    try:
        client = Client(api_key="cw_ingest_secret", endpoint=redirector.url)
        client._running = False
        ok = client._send_batch([{"event_id": "e1", "type": "error", "occurred_at": time.time()}])
    finally:
        redirector.close()
    assert ok is False
    assert recorder.requests == []  # the other host never saw the request or the key


def test_long_environment_names_are_cut_the_same_way_everywhere():
    env = "production-eu-central-1-cluster-a-blue-green-canary-2"  # 53 characters
    client = Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9", environment=env)
    client._running = False
    client.capture_exception(_boom())
    client._heartbeat_requests = 3
    with client._lock:
        client._emit_heartbeat_locked(time.time())
    events = [client._queue.get_nowait() for _ in range(2)]
    assert {e["environment"] for e in events} == {env[:50]}


def test_paths_and_nested_extra_are_scrubbed():
    client = Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9")
    client._running = False
    client.capture_exception(_boom(), extra={"recipients": [{"email": "jane.doe@example.com"}], "pair": ("a@b.co", 1)},
                             request_path="/api/users/jane.doe@example.com/reset")
    client.capture_slow_request("/u/jane.doe@example.com", 2000.0)
    text = json.dumps([client._queue.get_nowait(), client._queue.get_nowait()])
    assert "jane.doe@example.com" not in text and "a@b.co" not in text


def test_a_heartbeat_that_does_not_fit_keeps_its_counts():
    client = Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9")
    client._running = False
    client._queue = queue.Queue(maxsize=1)
    client._queue.put_nowait({"filler": True})
    client._heartbeat_requests, client._heartbeat_errors = 40, 2
    with client._lock:
        client._emit_heartbeat_locked(time.time())
    assert (client._heartbeat_requests, client._heartbeat_errors) == (40, 2)


def test_shutdown_sends_what_the_worker_was_holding(recorder):
    changewatch.init(api_key="cw_ingest_test", endpoint=recorder.url)
    changewatch.capture_exception(_boom())
    time.sleep(0.3)
    changewatch.shutdown()
    assert any(e["type"] == "error" for e in recorder.events())
