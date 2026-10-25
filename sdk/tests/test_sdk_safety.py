"""Security Addendum Stage 5 Tests: SDK Safety Budget.

Verifies:
1. Fork safety (os.register_at_fork restores background thread and queue).
2. Circuit breaker (trips after 5 failures, exponential backoff up to 10m).
3. Kill switch (CHANGEWATCH_DISABLED=1 makes SDK a zero-overhead no-op).
4. Strict HTTPS enforcement (http:// rejected unless localhost or allow_insecure=True).
5. Exception hooks and shutdown cleanup (restores sys.excepthook and threading.excepthook).
6. Overhead budget benchmark (p95 < 1.0 ms, memory < 20 MB).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

# Ensure sdk is in sys.path
sdk_root = Path(__file__).resolve().parent.parent
if str(sdk_root) not in sys.path:
    sys.path.insert(0, str(sdk_root))

from changewatch import Client, init, shutdown
import importlib.util
_bench_spec = importlib.util.spec_from_file_location("sdk_bench_overhead", sdk_root / "bench" / "overhead.py")
_bench_mod = importlib.util.module_from_spec(_bench_spec)
_bench_spec.loader.exec_module(_bench_mod)
run_overhead_benchmark = _bench_mod.run_overhead_benchmark


def test_https_enforcement_rejects_insecure_remote_http():
    """Verify SDK refuses http:// endpoints for non-localhost without allow_insecure=True."""
    with pytest.raises(ValueError) as exc:
        Client(api_key="cw_test", endpoint="http://api.changewatch.io")
    assert "Insecure endpoint" in str(exc.value)
    assert "requires HTTPS" in str(exc.value)


def test_https_enforcement_allows_localhost_and_127_0_0_1():
    """Verify SDK allows http:// endpoints on localhost and 127.0.0.1."""
    client_local = Client(api_key="cw_test", endpoint="http://localhost:8000")
    client_local._running = False
    assert client_local.endpoint == "http://localhost:8000"

    client_ip = Client(api_key="cw_test", endpoint="http://127.0.0.1:8000")
    client_ip._running = False
    assert client_ip.endpoint == "http://127.0.0.1:8000"


def test_https_enforcement_allows_insecure_when_explicitly_passed():
    """Verify SDK allows http:// on remote hosts when allow_insecure=True is passed."""
    client = Client(
        api_key="cw_test",
        endpoint="http://remote.internal.dev:8000",
        allow_insecure=True,
    )
    client._running = False
    assert client.endpoint == "http://remote.internal.dev:8000"


def test_kill_switch_makes_sdk_zero_overhead_noop(monkeypatch):
    """Verify CHANGEWATCH_DISABLED=1 disables SDK completely."""
    monkeypatch.setenv("CHANGEWATCH_DISABLED", "1")
    client = init(api_key="cw_test", endpoint="https://api.changewatch.io")

    assert client.disabled is True
    # Background worker thread must NOT be started
    assert client._worker_thread is None

    # Operations must be immediate no-ops
    client.record_request(has_error=False)
    client.capture_exception(RuntimeError("ignored"))
    client.capture_slow_request("/path", 2500)
    assert client._queue.empty()

    shutdown()


def test_sdk_shutdown_restores_original_hooks():
    """Verify shutdown() restores original sys and threading excepthooks."""
    orig_sys = sys.excepthook
    orig_threading = getattr(threading, "excepthook", None)

    init(api_key="cw_test", endpoint="http://localhost:8000")
    # Verify hooks changed
    assert sys.excepthook != orig_sys

    shutdown()
    # Verify hooks restored
    assert sys.excepthook == orig_sys
    if orig_threading is not None:
        assert threading.excepthook == orig_threading


def test_circuit_breaker_trips_after_5_failures():
    """Verify circuit breaker opens after 5 failures and prevents further send attempts."""
    client = Client(api_key="cw_test", endpoint="http://localhost:8000")
    client._running = False

    batch = [{"type": "error", "message": "test", "occurred_at": time.time()}]

    with patch("changewatch._OPENER.open", side_effect=Exception("Connection refused")) as mock_urlopen:
        # Send 4 times: consecutive_failures increases, circuit still closed
        for i in range(1, 5):
            client._send_batch(batch)
            assert client._consecutive_failures == i
            assert client._circuit_open_until == 0.0

        assert mock_urlopen.call_count == 4

        # 5th failure trips the circuit breaker
        client._send_batch(batch)
        assert client._consecutive_failures == 5
        assert client._circuit_open_until > time.monotonic()
        assert mock_urlopen.call_count == 5

        # 6th call while circuit is open should drop batch without calling urlopen
        client._send_batch(batch)
        assert mock_urlopen.call_count == 5  # No new network call made!

    # Simulate circuit timeout expiring
    client._circuit_open_until = time.monotonic() - 1.0

    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"status": "ok"}'
    mock_resp.__enter__.return_value = mock_resp

    with patch("changewatch._OPENER.open", return_value=mock_resp) as mock_urlopen_success:
        client._send_batch(batch)
        assert mock_urlopen_success.call_count == 1
        # Circuit breaker reset on success
        assert client._consecutive_failures == 0
        assert client._circuit_open_until == 0.0


def test_sdk_fork_safety_reinitializes_worker_thread():
    """Verify fork handler resets queue and restarts worker thread in child process."""
    client = Client(api_key="cw_test", endpoint="http://localhost:8000")
    old_thread = client._worker_thread

    # Queue some events in parent
    client.record_request()
    assert client._heartbeat_requests == 1

    # Simulate child process after fork
    client._reinit_after_fork()

    assert client._worker_thread is not old_thread
    assert client._worker_thread.is_alive()
    assert client._heartbeat_requests == 0
    assert client._queue.empty()

    client.stop()


def test_overhead_budget_benchmark():
    """Verify SDK meets strict overhead budget: p95 < 1.0 ms and memory < 20 MB."""
    stats = run_overhead_benchmark(iterations=3000)
    assert stats["p95"] < 1.0, f"p95 overhead {stats['p95']} ms exceeded 1.0 ms budget"
    assert stats["peak_memory_mb"] < 20.0, f"Memory {stats['peak_memory_mb']} MB exceeded 20 MB budget"


def _sent_events(mock_urlopen):
    import json as _json
    out = []
    for call in mock_urlopen.call_args_list:
        out += _json.loads(call.args[0].data)["events"]
    return out


def test_unserializable_extra_does_not_stop_sending():
    """A UUID in extra once made every later send fail and the batch grow forever."""
    import uuid

    client = Client(api_key="cw_test", endpoint="http://localhost:8000")
    ok = MagicMock()
    ok.__enter__.return_value.read.return_value = b"{}"
    with patch("changewatch._OPENER.open", return_value=ok) as mock_urlopen:
        for tag, extra in (("bad", {"order_id": uuid.uuid4(), "when": object()}), ("after", {"n": 1})):
            try:
                raise ValueError(tag)
            except ValueError as e:
                client.capture_exception(e, extra=extra)
        deadline = time.time() + 6
        def mine():
            return [e for e in _sent_events(mock_urlopen) if e.get("message") in ("bad", "after")]

        while time.time() < deadline and len(mine()) < 2:
            time.sleep(0.1)
        sent = mine()
    assert [e["message"] for e in sent] == ["bad", "after"]
    assert isinstance(sent[0]["extra"]["order_id"], str)


def test_extra_is_capped_and_old_events_are_left_out():
    import changewatch

    client = Client(api_key="cw_test", endpoint="http://localhost:8000")
    big = changewatch._json_safe_extra({"blob": "x" * 20000})
    assert big == {"_truncated": True, "_bytes": len('{"blob": "' + "x" * 20000 + '"}')}

    ok = MagicMock()
    ok.__enter__.return_value.read.return_value = b"{}"
    old = {"event_id": "old", "type": "error", "occurred_at": time.time() - 25 * 3600}
    new = {"event_id": "new", "type": "error", "occurred_at": time.time()}
    before = client._dropped_events
    with patch("changewatch._OPENER.open", return_value=ok) as mock_urlopen:
        client._send_batch([old, new])
    assert [e["event_id"] for e in _sent_events(mock_urlopen)] == ["new"]
    assert client._dropped_events == before + 1


def test_scrubbing_time_is_bounded_for_hostile_exception_text():
    """Text echoed into an exception (user input) must not stall the customer's app."""
    import changewatch

    client = Client(api_key="cw_test", endpoint="http://localhost:8000")
    start = time.perf_counter()
    try:
        raise ValueError("a-" * 200000)
    except ValueError as e:
        client.capture_exception(e)
    assert time.perf_counter() - start < 0.2
    assert len(changewatch._scrub("a." * 20000)) == 40000
