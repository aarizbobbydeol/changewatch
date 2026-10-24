"""ChangeWatch Zero-Dependency Python SDK.

Observes application health, captures exceptions and slow requests, and emits
minute-level traffic heartbeats with hard performance and safety guarantees:
- Never raises into host app (all public calls wrapped, errors counted)
- Never alters app behavior or inspects request/response bodies
- Never blocks (bounded queue, background daemon thread)
- Fork-safe (os.register_at_fork restores background thread after fork)
- Circuit breaker (trips after 5 consecutive failures, exponential backoff up to 10m)
- Kill switch (CHANGEWATCH_DISABLED=1 makes SDK a zero-overhead no-op)
- Strict HTTPS enforcement (http:// rejected unless localhost or allow_insecure=True)
- Clean shutdown (restores excepthooks, drains worker)
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("changewatch")

# Secret scrubbing patterns (Standard library regex)
_AWS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN_PATTERN = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b")
_SK_TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")
_KV_PATTERN = re.compile(
    r"""(?i)(?P<prefix>\b(?:api[_-]?key|secret|token|password|auth[_-]?token|access[_-]?token)\b\s*[:=]\s*["']?)(?P<val>[^\s"',;]+)(?P<suffix>["']?)"""
)

# PII patterns
# The lookbehind lets a match start only where a run of local-part characters starts. Without it,
# every position inside a long run like "a-a-a-..." was tried again: quadratic, seconds per 40 KB.
_EMAIL_PATTERN = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4_PATTERN = re.compile(r"\b(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b")
_IPV6_PATTERN = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]{10,}\b")
_CARD_PATTERN = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")


def _scrub(text: Optional[str]) -> Optional[str]:
    """Scrub sensitive keys, tokens, and PII from strings."""
    if not text:
        return text
    scrubbed = _AWS_KEY_PATTERN.sub("[REDACTED_AWS_KEY]", text)
    scrubbed = _GITHUB_TOKEN_PATTERN.sub("[REDACTED_GITHUB_TOKEN]", scrubbed)
    scrubbed = _SK_TOKEN_PATTERN.sub("[REDACTED_SECRET_KEY]", scrubbed)
    scrubbed = _KV_PATTERN.sub(r"\g<prefix>[REDACTED_SECRET]\g<suffix>", scrubbed)

    # PII Scrubbing (SCRUB_PII=true by default)
    if os.getenv("SCRUB_PII", "1").lower() in ("1", "true", "yes"):
        scrubbed = _JWT_PATTERN.sub("[REDACTED_JWT]", scrubbed)
        scrubbed = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", scrubbed)
        scrubbed = _IPV4_PATTERN.sub("[REDACTED_IP]", scrubbed)
        scrubbed = _IPV6_PATTERN.sub("[REDACTED_IP]", scrubbed)
        scrubbed = _CARD_PATTERN.sub("[REDACTED_CARD]", scrubbed)

    return scrubbed



_MAX_EXTRA_BYTES = 10000  # the server rejects an event whose extra is over 10 KB
_MAX_EVENT_AGE_SEC = 23 * 3600  # the server rejects events older than 24 hours


def _json_safe_extra(extra: Dict[str, Any]) -> Dict[str, Any]:
    """`extra` as plain JSON (UUIDs, datetimes, Decimals become strings), within the server's limit."""
    try:
        text = json.dumps(extra, default=str)
    except Exception:
        return {"_unserializable": True}
    if len(text.encode("utf-8")) > _MAX_EXTRA_BYTES:
        return {"_truncated": True, "_bytes": len(text.encode("utf-8"))}
    return json.loads(text)


def _scrub_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively scrub values in dictionaries."""
    clean = {}
    for k, v in data.items():
        if isinstance(v, str):
            clean[k] = _scrub(v)
        elif isinstance(v, dict):
            clean[k] = _scrub_dict(v)
        elif isinstance(v, list):
            clean[k] = [_scrub(x) if isinstance(x, str) else x for x in v]
        else:
            clean[k] = v
    return clean


class Client:
    """ChangeWatch SDK Client managing event batching, circuit breaking, and delivery."""

    def __init__(
        self,
        api_key: str,
        release: Optional[str] = None,
        environment: str = "prod",
        endpoint: Optional[str] = None,
        slow_ms: float = 1500.0,
        allow_insecure: bool = False,
    ):
        self.disabled = os.getenv("CHANGEWATCH_DISABLED", "0").lower() in ("1", "true", "yes")
        self.api_key = api_key
        self.release = release or os.getenv("GIT_SHA", "")
        self.environment = environment
        self.slow_ms = float(slow_ms)
        self.allow_insecure = allow_insecure or (os.getenv("ALLOW_INSECURE_HTTP", "0").lower() in ("1", "true", "yes"))

        raw_endpoint = (endpoint or os.getenv("CHANGEWATCH_ENDPOINT", "http://localhost:8000")).rstrip("/")
        self.endpoint = self._validate_and_format_endpoint(raw_endpoint)

        # Internal failure counter (swallowed errors)
        self._internal_errors = 0

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._backoff_seconds = 60.0
        self._max_backoff_seconds = 600.0  # 10 minutes
        self._dropped_events = 0  # events lost because the bounded queue was full

        # Threading and queue state
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._running = not self.disabled
        self._lock = threading.Lock()

        # Heartbeat counters
        self._heartbeat_requests = 0
        self._heartbeat_errors = 0
        self._last_heartbeat_time = time.time()

        # Background worker thread (only started if not disabled)
        self._worker_thread: Optional[threading.Thread] = None
        if not self.disabled:
            self._start_worker_thread()

            # Register at-fork handler for preloaded server environments (Gunicorn, uWSGI)
            if hasattr(os, "register_at_fork"):
                try:
                    os.register_at_fork(after_in_child=self._reinit_after_fork)
                except Exception:
                    pass

            atexit.register(self.flush)

    def _validate_and_format_endpoint(self, endpoint: str) -> str:
        """Validate HTTPS requirement unless localhost or explicitly allowed."""
        parsed = urllib.parse.urlparse(endpoint)
        hostname = (parsed.hostname or "").lower()
        scheme = (parsed.scheme or "").lower()

        is_localhost = hostname in ("localhost", "127.0.0.1", "::1", "testserver")

        if scheme == "http" and not is_localhost and not self.allow_insecure:
            raise ValueError(
                f"ChangeWatch security violation: Insecure endpoint '{endpoint}' is prohibited. "
                "ChangeWatch requires HTTPS for production endpoints. Use https:// or set allow_insecure=True."
            )

        if scheme == "http" and not is_localhost and self.allow_insecure:
            logger.warning("ChangeWatch warning: Insecure HTTP connection explicitly allowed for %s", endpoint)

        return endpoint

    def _start_worker_thread(self):
        """Start or restart daemon worker thread."""
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="changewatch-worker"
        )
        self._worker_thread.start()

    def _reinit_after_fork(self):
        """Fork-safety handler: reinitializes lock, queue, and background thread in child process."""
        if self.disabled:
            return
        try:
            self._lock = threading.Lock()
            # Drain or re-create child queue
            self._queue = queue.Queue(maxsize=1000)
            self._heartbeat_requests = 0
            self._heartbeat_errors = 0
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            self._start_worker_thread()
        except Exception:
            self._internal_errors += 1

    def record_request(self, has_error: bool = False):
        """Record a request for the 60-second traffic heartbeat (non-blocking, never raises)."""
        if self.disabled:
            return
        try:
            with self._lock:
                self._heartbeat_requests += 1
                if has_error:
                    self._heartbeat_errors += 1

                now = time.time()
                if now - self._last_heartbeat_time >= 60.0:
                    self._emit_heartbeat_locked(now)
        except Exception:
            self._internal_errors += 1

    def _emit_heartbeat_locked(self, now: float):
        """Enqueue heartbeat event if requests > 0 (called under lock)."""
        if self._heartbeat_requests > 0:
            event = {
                "event_id": uuid.uuid4().hex,
                "type": "heartbeat",
                "requests": self._heartbeat_requests,
                "errors": self._heartbeat_errors,
                "release": self.release,
                "environment": self.environment,
                "occurred_at": now,
            }
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._dropped_events += 1
            self._heartbeat_requests = 0
            self._heartbeat_errors = 0
            self._last_heartbeat_time = now

    def capture_exception(
        self,
        exc: BaseException,
        extra: Optional[Dict[str, Any]] = None,
        request_path: Optional[str] = None,
        method: Optional[str] = None,
    ):
        """Capture an unhandled or manual exception (non-blocking, never raises)."""
        if self.disabled:
            return
        try:
            exc_type = type(exc).__name__[:200]
            # Cut to the server's limits (with room for redaction markers) before scrubbing, so
            # scrubbing time stays bounded however large the exception text is
            message = _scrub(str(exc)[:4000])
            tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            stack_trace = _scrub(tb_str[-40000:])

            clean_extra = _json_safe_extra(_scrub_dict(extra) if extra else {})
            if method:
                clean_extra["method"] = str(method)[:20]

            event = {
                "event_id": uuid.uuid4().hex,
                "type": "error",
                "exc_type": exc_type,
                "message": (message or "")[:2000],
                "stack_trace": (stack_trace or "")[-20000:],
                "request_path": (request_path or "")[:500] if request_path else None,
                "release": self.release[:64] if self.release else None,
                "environment": self.environment[:20],
                "occurred_at": time.time(),
                "extra": clean_extra,
            }
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped_events += 1
        except Exception:
            self._internal_errors += 1

    def capture_slow_request(self, request_path: str, duration_ms: float, method: Optional[str] = None):
        """Capture a slow request exceeding slow_ms threshold (non-blocking, never raises)."""
        if self.disabled:
            return
        try:
            extra = {"method": method} if method else {}
            event = {
                "event_id": uuid.uuid4().hex,
                "type": "slow_request",
                "request_path": request_path[:500],
                "duration_ms": round(duration_ms, 2),
                "release": self.release[:64] if self.release else None,
                "environment": self.environment[:20],
                "occurred_at": time.time(),
                "extra": extra,
            }
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped_events += 1
        except Exception:
            self._internal_errors += 1

    def _worker_loop(self):
        """Background thread worker pulling events, batching up to 20 or 2 seconds."""
        batch: List[Dict[str, Any]] = []
        last_flush = time.monotonic()

        while self._running:
            try:
                # While paused (circuit open / server asked to retry later) leave events queued
                pause = self._circuit_open_until - time.monotonic()
                if pause > 0:
                    time.sleep(min(0.5, pause))
                    continue

                elapsed = time.monotonic() - last_flush
                timeout = max(0.05, 2.0 - elapsed)

                try:
                    event = self._queue.get(timeout=timeout)
                    batch.append(event)
                except queue.Empty:
                    pass

                now = time.monotonic()
                if len(batch) >= 20 or (batch and (now - last_flush) >= 2.0):
                    try:
                        self._send_batch(batch)
                    finally:
                        # Never retry the same list here: a batch that cannot be sent must not
                        # block every later event (and grow without bound)
                        batch = []
                        last_flush = now

            except Exception:
                self._internal_errors += 1

        if batch:
            self._send_batch(batch)

    def _send_batch(self, batch: List[Dict[str, Any]]):
        """Send a batch of events via HTTP POST with 3s timeout. Never raises.

        Enforces circuit breaker:
        - If circuit is open, drops send attempt without network cost.
        - After 5 consecutive failures, circuit opens for 60s with exponential backoff up to 10m.
        - HTTP 429/503 pause sending for Retry-After seconds; the batch is re-queued.
        - Network failures re-queue the batch (bounded queue, overflow dropped and counted).
        """
        if not batch or self.disabled:
            return

        # Check circuit breaker
        now = time.monotonic()
        if now < self._circuit_open_until:
            return  # Circuit open, drop batch

        # The server rejects a whole request if one event is older than 24 hours (after a long
        # outage, re-queued events can be): leave those out and count them
        cutoff = time.time() - _MAX_EVENT_AGE_SEC
        fresh = [e for e in batch if e.get("occurred_at", 0) >= cutoff]
        self._dropped_events += len(batch) - len(fresh)
        if not fresh:
            return
        batch = fresh

        url = f"{self.endpoint}/v1/events"
        payload = json.dumps({"events": batch}, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "User-Agent": "changewatch-python/0.1.0",
        }
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                _ = resp.read()
            # Success: reset circuit breaker
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            self._backoff_seconds = 60.0
        except urllib.error.HTTPError as exc:
            self._internal_errors += 1
            if exc.code in (429, 503):
                # The server asked us to retry later (e.g. its queue is down): keep the
                # events and pause sending for Retry-After seconds
                self._circuit_open_until = time.monotonic() + self._retry_after_seconds(exc.headers)
                self._requeue(batch)
            else:
                # Other HTTP errors mean this batch itself was rejected; do not retry it
                self._record_failure()
        except Exception:
            # Network failure: the events are still good, keep them for the next attempt
            self._internal_errors += 1
            self._requeue(batch)
            self._record_failure()

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            # Trip circuit breaker
            self._circuit_open_until = time.monotonic() + self._backoff_seconds
            self._backoff_seconds = min(self._max_backoff_seconds, self._backoff_seconds * 2.0)

    def _retry_after_seconds(self, headers: Any) -> float:
        """Seconds from a Retry-After header (1 s to the 10 min backoff cap; 30 s if absent)."""
        try:
            value = float(headers.get("Retry-After")) if headers is not None else 30.0
        except (TypeError, ValueError):
            value = 30.0
        return max(1.0, min(self._max_backoff_seconds, value))

    def _requeue(self, batch: List[Dict[str, Any]]):
        """Put unsent events back; the queue stays bounded, overflow is counted and dropped."""
        for event in batch:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._dropped_events += 1

    def flush(self):
        """Flush pending events and final heartbeats on exit (never raises)."""
        if self.disabled:
            return
        try:
            with self._lock:
                self._emit_heartbeat_locked(time.time())

            events_to_send: List[Dict[str, Any]] = []
            while not self._queue.empty():
                try:
                    events_to_send.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            if events_to_send:
                for i in range(0, len(events_to_send), 20):
                    self._send_batch(events_to_send[i : i + 20])
        except Exception:
            self._internal_errors += 1

    def stop(self):
        """Stop worker thread and drain remaining queue."""
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)


# Global state
_client: Optional[Client] = None
_orig_sys_excepthook = None
_orig_threading_excepthook = None


def init(
    api_key: str,
    release: Optional[str] = None,
    environment: str = "prod",
    endpoint: Optional[str] = None,
    slow_ms: float = 1500.0,
    allow_insecure: bool = False,
) -> Client:
    """Initialize ChangeWatch SDK, install exception hooks, and return Client."""
    global _client, _orig_sys_excepthook, _orig_threading_excepthook

    client = Client(
        api_key=api_key,
        release=release,
        environment=environment,
        endpoint=endpoint,
        slow_ms=slow_ms,
        allow_insecure=allow_insecure,
    )
    _client = client

    if client.disabled:
        return client

    # Install sys.excepthook preserving previous
    if _orig_sys_excepthook is None:
        _orig_sys_excepthook = sys.excepthook

    def _changewatch_sys_excepthook(exc_type, exc_value, exc_traceback):
        if _client and not _client.disabled and exc_value:
            _client.capture_exception(exc_value)
        if _orig_sys_excepthook:
            _orig_sys_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _changewatch_sys_excepthook

    # Install threading.excepthook preserving previous (Python 3.8+)
    if hasattr(threading, "excepthook"):
        if _orig_threading_excepthook is None:
            _orig_threading_excepthook = threading.excepthook

        def _changewatch_threading_excepthook(args):
            if _client and not _client.disabled and args.exc_value:
                _client.capture_exception(args.exc_value)
            if _orig_threading_excepthook:
                _orig_threading_excepthook(args)

        threading.excepthook = _changewatch_threading_excepthook

    return client


def shutdown():
    """Cleanly shutdown ChangeWatch, uninstall global hooks, and drain worker."""
    global _client, _orig_sys_excepthook, _orig_threading_excepthook

    if _orig_sys_excepthook is not None:
        sys.excepthook = _orig_sys_excepthook
        _orig_sys_excepthook = None

    if _orig_threading_excepthook is not None and hasattr(threading, "excepthook"):
        threading.excepthook = _orig_threading_excepthook
        _orig_threading_excepthook = None

    if _client is not None:
        _client.flush()
        _client.stop()
        _client = None


def capture_exception(exc: BaseException, extra: Optional[Dict[str, Any]] = None):
    """Global helper to capture an exception if ChangeWatch is initialized."""
    if _client is not None and not _client.disabled:
        _client.capture_exception(exc, extra=extra)


class ASGIMiddleware:
    """ASGI Middleware for FastAPI/Starlette frameworks.

    Guarantees:
    - Only observes duration and exceptions.
    - Never reads or modifies request bodies or response bodies.
    - Always re-raises original exceptions unchanged into host application.
    - Completely no-op if client is uninitialized or disabled.
    """

    def __init__(self, app: Any, client: Optional[Client] = None):
        self.app = app
        self.client = client

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        active_client = self.client or _client
        if active_client is None or active_client.disabled:
            await self.app(scope, receive, send)
            return

        start_time = time.monotonic()
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        has_error = False

        try:
            await self.app(scope, receive, send)
        except BaseException as exc:
            has_error = True
            active_client.capture_exception(exc, request_path=path, method=method)
            # Must always re-raise original exception unchanged
            raise
        finally:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            active_client.record_request(has_error=has_error)

            if not has_error and duration_ms >= active_client.slow_ms:
                active_client.capture_slow_request(request_path=path, duration_ms=duration_ms, method=method)
