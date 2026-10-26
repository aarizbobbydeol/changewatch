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
import collections
import contextvars
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
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("changewatch")

# Secret scrubbing patterns (Standard library regex)
_AWS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN_PATTERN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SK_TOKEN_PATTERN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{10,}|[rs]k_(?:live|test)_[A-Za-z0-9]{10,}|xox[abprs]-[A-Za-z0-9-]{10,})\b")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.~+/]{10,}=*")
# A secret assigned to a named key, also with a prefix (DB_PASSWORD, client_secret, refresh_token)
# or quotes ("password": "..."). Same pattern as the server's app/scrub.py. A match only starts
# where a word starts and the prefix has no hyphens, so the scan stays linear.
_KV_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_])[A-Za-z0-9_]*?"
    r"(?:password|passwd|pwd|secret(?:[_-]?key)?|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credentials?|auth)"
    r"(?![A-Za-z0-9_])[\"']?\s*[:=]\s*)"
    r"(?P<val>(?![\"']?\[REDACTED_)(?:\"[^\"\n]{0,500}\"|'[^'\n]{0,500}'|[^\s\"',;&)}\]]+))"
)
_TOKEN_LABELS = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?:sk-|[rs]k_(?:live|test)_|xox[abprs]-)[A-Za-z0-9_-]{10,}"), "[REDACTED_SECRET_KEY]"),
)


def _redact_kv(match: "re.Match") -> str:
    val = match.group("val")
    quote = val[0] if val[:1] in ("'", '"') and len(val) >= 2 and val[-1] == val[0] else ""
    inner = val[1:-1] if quote else val
    label = next((name for pattern, name in _TOKEN_LABELS if pattern.match(inner)), "[REDACTED_SECRET]")
    return f"{match.group('prefix')}{quote}{label}{quote}"


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
    scrubbed = _BEARER_PATTERN.sub("Bearer [REDACTED_SECRET]", scrubbed)
    scrubbed = _KV_PATTERN.sub(_redact_kv, scrubbed)

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
# The server's limits for calls, input tokens, output tokens and cost_micros in one ai_usage
# event: one sum past them made it reject the whole batch, error events in it included
_AI_USAGE_LIMITS = (2_147_483_647, 10**12, 10**12, 10**12)


def _json_safe_extra(extra: Dict[str, Any]) -> Dict[str, Any]:
    """`extra` as plain JSON (UUIDs, datetimes, Decimals become strings), within the server's limit."""
    try:
        text = json.dumps(extra, default=str)
    except Exception:
        return {"_unserializable": True}
    if len(text.encode("utf-8")) > _MAX_EXTRA_BYTES:
        return {"_truncated": True, "_bytes": len(text.encode("utf-8"))}
    return json.loads(text)


def _scrub_value(value: Any, depth: int = 0) -> Any:
    """Scrub strings at any depth of dicts, lists and tuples (dicts inside lists too)."""
    if isinstance(value, str):
        return _scrub(value)
    if depth > 20:
        return "[TOO_DEEP]"
    if isinstance(value, dict):
        return {k: _scrub_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v, depth + 1) for v in value]
    return value


def _scrub_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively scrub values in dictionaries."""
    return _scrub_value(data)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would send the API key to the new host (even over plain
    HTTP) and turn the POST into a GET without the events."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status


_OPENER = urllib.request.build_opener(_NoRedirect)


def _reaches_root(name: Any) -> bool:
    """Whether a record of this logger reaches the root logger, i.e. the app's own log output
    (walked up as Logger.callHandlers does): a disabled logger or propagate=False stops it."""
    if not isinstance(name, str) or name.startswith("changewatch"):
        return False  # our own warnings: never a loop
    node = logging.root.manager.loggerDict.get(name)
    if node is None:
        return name == logging.root.name and not logging.root.disabled
    if not isinstance(node, logging.Logger) or node.disabled:
        return False
    while node.parent is not None:
        if not node.propagate:
            return False
        node = node.parent
    return True


class _LogBuffer(logging.Handler):
    """Keeps the process's last log lines in memory, to attach to the next error (capture_logs).

    It is not added to any logger: a log record factory, chained to the one before it, hands it
    each new record that reaches the root logger. A handler on the root logger made a later
    logging.basicConfig() do nothing, hid Python's own output of errors while logging is not
    configured, and logging.config.dictConfig() removed it without a sign.

    Nothing is sent on its own. Lines are cut when recorded and scrubbed only when an error takes
    them, so logging stays cheap. With capture_logged_errors, `logger.exception(...)` (an error
    the app caught and only logged) is also reported as an error.
    """

    def __init__(self, client: "Client", level: int, capacity: int = 50, capture_logged_errors: bool = False):
        super().__init__(level)
        self._client = client
        self._lines: "collections.deque" = collections.deque(maxlen=capacity)
        self._capture_logged_errors = capture_logged_errors
        # The lowest level worth a look: kept lines (capacity 0 keeps none) and logged errors
        never = sys.maxsize
        self._threshold = min(self.level if capacity else never, logging.ERROR if capture_logged_errors else never)
        self._previous_factory = logging.getLogRecordFactory()
        self._factory = self._make_record  # one bound method, to recognise it as ours later
        self._active = True
        logging.setLogRecordFactory(self._factory)

    def _make_record(self, *args: Any, **kwargs: Any) -> logging.LogRecord:
        # Logging only creates a record for a level its logger is enabled for
        record = self._previous_factory(*args, **kwargs)
        try:
            if self._active and (record.levelno or 0) >= self._threshold and _reaches_root(record.name):
                self.handle(record)
        except Exception:
            self._client._internal_errors += 1
        return record

    def detach(self) -> None:
        """Stop looking at log records: put the previous record factory back while ours is still
        the current one. If another library has wrapped ours since, ours only passes records on."""
        self._active = False
        if logging.getLogRecordFactory() is self._factory:
            logging.setLogRecordFactory(self._previous_factory)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._lines.maxlen and record.levelno >= self.level:
                message = record.getMessage()
                if len(message) > 1000:
                    # Cut after the last whole word: a value cut in two (an email, a token) no
                    # longer matches the scrubber
                    message = (message[:1000].rsplit(None, 1) or [""])[0]
                self._lines.append((record.created, record.levelname, record.name, message))
            exc = record.exc_info[1] if record.exc_info else None
            if self._capture_logged_errors and exc is not None and record.levelno >= logging.ERROR:
                self._client._capture(exc, {"logger": record.name[:100]}, None, None, "log")
        except Exception:
            self._client._internal_errors += 1

    def snapshot(self) -> List[Dict[str, Any]]:
        """The buffered lines, oldest first, scrubbed and cut to the server's limits."""
        with self.lock:
            lines = list(self._lines)
        # Scrubbed whole, then cut: cutting first left a value that crossed the cut unredacted
        return [
            {"ts": ts, "level": level[:10], "logger": (_scrub(name[:100]) or "")[:100],
             "message": (_scrub(message) or "")[:500]}
            for ts, level, name, message in lines
        ]


class _Request:
    """The request ASGIMiddleware is handling."""

    __slots__ = ("path", "method", "done")

    def __init__(self, path: str, method: str):
        self.path, self.method, self.done = path, method, False


# Set by ASGIMiddleware at the start of each request, so captures that do not pass a path (a
# logged exception, the app's own capture_exception call) carry the request's path and method.
# Code started for the request sees it too (asyncio tasks and worker threads get a copy of the
# context). It is left in place when the request ends: the server's own report of what the
# request raised (uvicorn logs it) comes right after, in the same context.
_current_request: "contextvars.ContextVar[Optional[_Request]]" = contextvars.ContextVar("changewatch_request", default=None)


def _tb_in_chain(mark: Any, tb: Any) -> bool:
    """Whether traceback entry `mark` is part of traceback `tb`. An exception that propagates only
    gets entries added in front, so this holds while it is still unwinding from `mark`."""
    if mark is None:
        return tb is None
    while tb is not None:
        if tb is mark:
            return True
        tb = tb.tb_next
    return False


def _already_reported(exc: BaseException, source: str, request: Optional[_Request]) -> bool:
    """Whether this capture repeats one already sent, such as an exception logged (with
    capture_logged_errors) and then raised on to the middleware or an exception hook.

    In a request: once per request. A failed shared future or a cached error raises the same
    exception object in every request, and every failing request must still send an event.
    Outside a request: when another kind of capture ("log", "app" for capture_exception, "hook"
    for an exception hook) sent it from this thread and it is still unwinding from there. Two
    captures of the same kind both send: that is what one object raised again in a loop looks like.
    """
    try:
        seen = getattr(exc, "_changewatch_reported", None)
        if not seen:
            return False
        where, tb, by = seen
        if request is not None:
            return where is request
        if where != threading.get_ident() or (by == source and source != "hook"):
            return False
        return _tb_in_chain(tb, exc.__traceback__)
    except Exception:
        return False


def _mark_reported(exc: BaseException, source: str, request: Optional[_Request]) -> None:
    try:
        if request is not None:
            exc._changewatch_reported = (request, None, source)  # type: ignore[attr-defined]
        else:
            exc._changewatch_reported = (threading.get_ident(), exc.__traceback__, source)  # type: ignore[attr-defined]
    except Exception:
        pass  # an exception that takes no attributes is sent again if another hook sees it


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
        capture_logs: bool = False,
        log_level: int = logging.WARNING,
        capture_logged_errors: bool = False,
    ):
        self.disabled = os.getenv("CHANGEWATCH_DISABLED", "0").lower() in ("1", "true", "yes")
        self.api_key = api_key
        # Cut once to the server's limits: an over-long value in any event (heartbeats included)
        # made the server reject the whole batch
        self.release = (release or os.getenv("GIT_SHA", ""))[:64]
        self.environment = (environment or "prod")[:50]
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

        # The app's own AI calls, summed per (model, feature, cost known) and sent once a minute
        self._ai_usage: Dict[Tuple[str, str, bool], List[int]] = {}
        self._last_ai_flush = time.time()

        # The app's recent log lines, attached to errors (opt-in: capture_logs), and exceptions
        # it only logs (opt-in: capture_logged_errors, which works on its own too)
        self._log_handler: Optional[_LogBuffer] = None
        if (capture_logs or capture_logged_errors) and not self.disabled:
            self._log_handler = _LogBuffer(self, level=log_level, capacity=50 if capture_logs else 0,
                                           capture_logged_errors=capture_logged_errors)

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

            atexit.register(self._flush_at_exit)

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
            self._ai_usage = {}
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

    def record_ai_usage(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: Any = None,
        feature: Optional[str] = None,
        calls: int = 1,
    ) -> None:
        """Count one (or `calls`) AI call of the app for cost tracking. Never raises, never blocks.

        The server prices it from its model price list unless `cost_usd` (the exact cost, as a
        number or string) is given. `feature` names what the call is for ("search", "summary").
        """
        if self.disabled:
            return
        try:
            cost_micros = None
            if cost_usd is not None:
                cost_micros = int((Decimal(str(cost_usd)) * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))
            key = (str(model or "unknown")[:100], str(feature or "")[:100], cost_micros is not None)
            added = (max(0, int(calls)), max(0, int(input_tokens or 0)), max(0, int(output_tokens or 0)),
                     max(0, cost_micros or 0))
            with self._lock:
                totals = self._ai_usage.setdefault(key, [0, 0, 0, 0])
                for i, limit in enumerate(_AI_USAGE_LIMITS):
                    totals[i] += added[i]
                    if totals[i] > limit:
                        totals[i] = limit
                        self._internal_errors += 1  # counted: what went past the limit is lost
        except Exception:
            self._internal_errors += 1

    def _flush_ai_usage_locked(self, now: float) -> None:
        """Queue the summed AI usage (called under the lock); what does not fit stays for later."""
        for key in list(self._ai_usage):
            model, feature, has_cost = key
            calls, tokens_in, tokens_out, cost = self._ai_usage[key]
            event: Dict[str, Any] = {
                "event_id": uuid.uuid4().hex, "type": "ai_usage", "model": model, "feature": feature or None,
                "calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
                "release": self.release or None, "environment": self.environment, "occurred_at": now,
            }
            if has_cost:
                event["cost_micros"] = cost
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                return
            del self._ai_usage[key]
        self._last_ai_flush = now

    def _emit_heartbeat_locked(self, now: float):
        """Enqueue heartbeat event if requests > 0 (called under lock)."""
        if self._heartbeat_requests > 0:
            event = {
                "event_id": uuid.uuid4().hex,
                "type": "heartbeat",
                "requests": self._heartbeat_requests,
                "errors": self._heartbeat_errors,
                "release": self.release or None,
                "environment": self.environment,
                "occurred_at": now,
            }
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                return  # keep the counts: the next heartbeat carries them
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
        self._capture(exc, extra, request_path, method, "app")

    def _capture(
        self,
        exc: BaseException,
        extra: Optional[Dict[str, Any]],
        request_path: Optional[str],
        method: Optional[str],
        source: str,
    ):
        """capture_exception, told which kind of capture this is: "app", "log" or "hook"."""
        if self.disabled:
            return
        try:
            request = _current_request.get()
            if _already_reported(exc, source, request):
                return  # e.g. logged with capture_logged_errors, then raised on to a hook
            if request is not None and not request.done:
                request_path = request_path or request.path
                method = method or request.method
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
                "request_path": (_scrub(request_path[:500]) or "")[:500] if request_path else None,
                "release": self.release or None,
                "environment": self.environment,
                "occurred_at": time.time(),
                "extra": clean_extra,
            }
            log_handler = self._log_handler
            if log_handler is not None:
                logs = log_handler.snapshot()
                if logs:
                    event["logs"] = logs
            self._queue.put_nowait(event)
            _mark_reported(exc, source, request)  # only once queued: a dropped one can come again
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
                "request_path": (_scrub(request_path[:500]) or "")[:500],  # markers can be longer than what they replace
                "duration_ms": round(duration_ms, 2),
                "release": self.release or None,
                "environment": self.environment,
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
                if self._ai_usage and time.time() - self._last_ai_flush >= 60.0:
                    with self._lock:
                        self._flush_ai_usage_locked(time.time())

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
            # Stopping (at exit): hand the batch back so the final flush sends it
            self._requeue(batch)

    def _send_batch(self, batch: List[Dict[str, Any]], final: bool = False) -> bool:
        """Send a batch of events via HTTP POST with 3s timeout. Never raises.

        Enforces circuit breaker:
        - If circuit is open, drops send attempt without network cost.
        - After 5 consecutive failures, circuit opens for 60s with exponential backoff up to 10m.
        - HTTP 429/503 pause sending for Retry-After seconds; the batch is re-queued.
        - Network failures re-queue the batch (bounded queue, overflow dropped and counted).
        `final` (at exit) tries once even while paused and keeps nothing for later.
        Returns True when the server accepted the batch.
        """
        if not batch or self.disabled:
            return False

        # Check circuit breaker
        now = time.monotonic()
        if now < self._circuit_open_until and not final:
            return False  # Circuit open, drop batch

        # The server rejects a whole request if one event is older than 24 hours (after a long
        # outage, re-queued events can be): leave those out and count them
        cutoff = time.time() - _MAX_EVENT_AGE_SEC
        fresh = [e for e in batch if e.get("occurred_at", 0) >= cutoff]
        self._dropped_events += len(batch) - len(fresh)
        if not fresh:
            return False
        batch = fresh

        url = f"{self.endpoint}/v1/events"
        payload = json.dumps({"events": batch}, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "User-Agent": "changewatch-python/1.0.1",
        }
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

        try:
            with _OPENER.open(req, timeout=3.0) as resp:
                _ = resp.read()
            # Success: reset circuit breaker
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            self._backoff_seconds = 60.0
            return True
        except urllib.error.HTTPError as exc:
            self._internal_errors += 1
            if 300 <= exc.code < 400:
                logger.warning("ChangeWatch endpoint answered with a redirect (%d); set endpoint to the final URL", exc.code)
                self._record_failure()
            elif exc.code in (429, 503) and not final:
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
            if not final:
                self._requeue(batch)
            self._record_failure()
        return False

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

    def _flush_at_exit(self):
        """At interpreter exit: stop the worker (it hands back the batch it holds), then send
        everything once. Gives up at the first failure so a dead server cannot hold up exit."""
        if self.disabled:
            return
        try:
            with self._lock:
                self._emit_heartbeat_locked(time.time())
                self._flush_ai_usage_locked(time.time())
            self._running = False
            worker = self._worker_thread
            if worker is not None and worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=2.5)
            pending: List[Dict[str, Any]] = []
            while True:
                try:
                    pending.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            for i in range(0, len(pending), 20):
                if not self._send_batch(pending[i : i + 20], final=True):
                    self._dropped_events += len(pending) - i
                    break
        except Exception:
            self._internal_errors += 1

    def stop(self):
        """Stop worker thread and drain remaining queue."""
        self._stop_log_capture()
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)

    def _stop_log_capture(self):
        log_handler, self._log_handler = self._log_handler, None
        if log_handler is not None:
            log_handler.detach()


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
    capture_logs: bool = False,
    log_level: int = logging.WARNING,
    capture_logged_errors: bool = False,
) -> Client:
    """Initialize ChangeWatch SDK, install exception hooks, and return Client.

    capture_logs: attach the app's last 50 log lines (at log_level or above) to each error.
    capture_logged_errors: also report exceptions the app only logs (logger.exception(...)).
    """
    global _client, _orig_sys_excepthook, _orig_threading_excepthook

    if _client is not None:
        # Called again (an app factory, a test per app): the first client's log capture would
        # otherwise stay on for good, and report through that client even after shutdown()
        _client._stop_log_capture()

    client = Client(
        api_key=api_key,
        release=release,
        environment=environment,
        endpoint=endpoint,
        slow_ms=slow_ms,
        allow_insecure=allow_insecure,
        capture_logs=capture_logs,
        log_level=log_level,
        capture_logged_errors=capture_logged_errors,
    )
    _client = client

    if client.disabled:
        return client

    # Install sys.excepthook preserving previous
    if _orig_sys_excepthook is None:
        _orig_sys_excepthook = sys.excepthook

    def _changewatch_sys_excepthook(exc_type, exc_value, exc_traceback):
        if _client and not _client.disabled and exc_value:
            _client._capture(exc_value, None, None, None, "hook")
        if _orig_sys_excepthook:
            _orig_sys_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _changewatch_sys_excepthook

    # Install threading.excepthook preserving previous (Python 3.8+)
    if hasattr(threading, "excepthook"):
        if _orig_threading_excepthook is None:
            _orig_threading_excepthook = threading.excepthook

        def _changewatch_threading_excepthook(args):
            if _client and not _client.disabled and args.exc_value:
                _client._capture(args.exc_value, None, None, None, "hook")
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
        _client._flush_at_exit()  # stops the worker, which hands back its batch, then sends all
        _client.stop()
        _client = None


def capture_exception(exc: BaseException, extra: Optional[Dict[str, Any]] = None):
    """Global helper to capture an exception if ChangeWatch is initialized."""
    if _client is not None and not _client.disabled:
        _client.capture_exception(exc, extra=extra)


def record_ai_usage(model: str, input_tokens: int = 0, output_tokens: int = 0, cost_usd: Any = None,
                    feature: Optional[str] = None, calls: int = 1) -> None:
    """Count one AI call of the app for cost tracking (see Client.record_ai_usage)."""
    if _client is not None and not _client.disabled:
        _client.record_ai_usage(model, input_tokens, output_tokens, cost_usd=cost_usd, feature=feature, calls=calls)


def record_ai_usage_from_response(response: Any, model: Optional[str] = None, feature: Optional[str] = None) -> None:
    """Count an AI call from the response object of the Anthropic or OpenAI SDK (or a dict like
    it): reads `model` and the token counts from `usage`. Never raises.

    Anthropic prompt-cache writes count as input tokens; cache reads (a small fraction of the
    price) are not counted. Pass the exact cost to record_ai_usage(cost_usd=...) when you have it.
    """
    try:
        def field(obj: Any, *names: str) -> Any:
            for name in names:
                value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
                if value is not None:
                    return value
            return None

        usage = field(response, "usage") or {}
        tokens_in = int(field(usage, "input_tokens", "prompt_tokens") or 0) + int(field(usage, "cache_creation_input_tokens") or 0)
        tokens_out = int(field(usage, "output_tokens", "completion_tokens") or 0)
        record_ai_usage(model or field(response, "model") or "unknown", tokens_in, tokens_out, feature=feature)
    except Exception:
        if _client is not None:
            _client._internal_errors += 1


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
        request = _Request(path, method)
        _current_request.set(request)  # not reset at the end: see _current_request

        try:
            await self.app(scope, receive, send)
        except BaseException as exc:
            has_error = True
            active_client._capture(exc, None, path, method, "hook")
            # Must always re-raise original exception unchanged
            raise
        finally:
            request.done = True
            duration_ms = (time.monotonic() - start_time) * 1000.0
            active_client.record_request(has_error=has_error)

            if not has_error and duration_ms >= active_client.slow_ms:
                active_client.capture_slow_request(request_path=path, duration_ms=duration_ms, method=method)
