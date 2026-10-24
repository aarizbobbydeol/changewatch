"""Demo traffic: a pretend online shop that deploys on a schedule and sometimes ships a bug.

It talks to ChangeWatch only through the public API (POST /v1/deploys and /v1/events), exactly
like a real app with the SDK and the GitHub Action, so a fresh install shows deploys, verdicts,
instant alerts, incidents and analytics within minutes.

It first sends --history-min minutes of past traffic, so the first deploy has a baseline.
Schedule: a healthy deploy at start, a bad deploy after --first-bad-after-min, its fix after
--fix-after-min, then a deploy every --deploy-every-min in the cycle healthy, healthy, bad, fix.
Each bad deploy changes one line and that line starts failing on a share of requests.

    python -m app.demo --api http://api:8000 --ingest-key cw_ingest_...      (runs until stopped)
    python -m app.demo --once ...                                              (one healthy → bad → fix story)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("changewatch.demo")

CODEOWNERS = (
    "* @demo-shop/core\n"
    "app/billing.py @demo-shop/payments\n"
    "app/orders.py @demo-shop/checkout\n"
    "app/search.py @demo-shop/discovery\n"
)
AUTHORS = ["alex@demo-shop.example", "sam@demo-shop.example", "kim@demo-shop.example", "lee@demo-shop.example"]


@dataclass(frozen=True)
class Bug:
    path: str
    line: int
    function: str
    caller: Tuple[str, int, str, str]  # file, line, function, code
    good_code: str
    bad_code: str
    exc_type: str
    message: str
    endpoint: str
    deploy_message: str
    fix_message: str
    failure_rate: float  # share of requests that fail while the bug is live


BUGS = [
    Bug(
        path="app/billing.py", line=42, function="total_cents",
        caller=("api/orders.py", 18, "checkout", "total = total_cents(order)"),
        good_code="    return sum(i.price for i in order.items)",
        bad_code="    return sum(i.price for i in order.items) / order.discount",
        exc_type="ZeroDivisionError", message="division by zero", endpoint="/checkout",
        deploy_message="Apply discount to totals", fix_message="Revert discount division in totals",
        failure_rate=0.08,
    ),
    Bug(
        path="app/orders.py", line=88, function="shipping_label",
        caller=("api/orders.py", 41, "create_order", "label = shipping_label(payload)"),
        good_code='    address = payload.get("shipping_address") or payload["billing_address"]',
        bad_code='    address = payload["shipping_address"]',
        exc_type="KeyError", message="'shipping_address'", endpoint="/orders",
        deploy_message="Simplify shipping address lookup", fix_message="Fall back to billing address again",
        failure_rate=0.05,
    ),
    Bug(
        path="app/search.py", line=57, function="normalize_query",
        caller=("api/search.py", 12, "search", "q = normalize_query(request.query)"),
        good_code="    return (query or \"\").strip().lower()",
        bad_code="    return query.strip().lower()",
        exc_type="AttributeError", message="'NoneType' object has no attribute 'strip'", endpoint="/search",
        deploy_message="Tidy search query normalisation", fix_message="Handle empty search queries",
        failure_rate=0.06,
    ),
]

HEALTHY_CHANGES = [
    ("templates/home.html", "Update homepage banner copy"),
    ("app/search.py", "Boost in-stock items in search ranking"),
    ("app/emails.py", "Shorter order confirmation email"),
    ("static/styles.css", "Larger tap targets on mobile"),
    ("app/recommendations.py", "Cache recommendations for 5 minutes"),
]


def file_content(bug: Bug, code: str) -> str:
    """A plausible source file with `code` on exactly line `bug.line`."""
    head = [f'"""{bug.path} (demo shop)."""', "import logging", "", "logger = logging.getLogger(__name__)", ""]
    body = [f"def {bug.function}(*args):", '    """Demo function."""']
    filler = ["" if i % 4 == 0 else f"# {bug.path} line {len(head) + i + 1}" for i in range(bug.line - 1 - len(head) - len(body))]
    lines = head + filler + body + [code, ""]
    assert lines[bug.line - 1] == code
    return "\n".join(lines)


def diff_for(path: str, line: int, old: str, new: str) -> str:
    return f"diff --git a/{path} b/{path}\n@@ -{line},1 +{line},1 @@\n-{old}\n+{new}\n"


def stack_trace(bug: Bug, code: str) -> str:
    caller_file, caller_line, caller_fn, caller_code = bug.caller
    return (
        "Traceback (most recent call last):\n"
        f'  File "/srv/app/{caller_file}", line {caller_line}, in {caller_fn}\n'
        f"    {caller_code}\n"
        f'  File "/srv/app/{bug.path}", line {bug.line}, in {bug.function}\n'
        f"    {code.strip()}\n"
        f"{bug.exc_type}: {bug.message}\n"
    )


# A known, long-standing error: it exists before and after every deploy, so it never counts as new.
BACKGROUND_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/app/inventory.py", line 23, in stock_level\n'
    "    return warehouse.fetch(sku, timeout=2)\n"
    "TimeoutError: warehouse API timed out after 2s\n"
)


@dataclass
class LiveDeploy:
    sha: str
    kind: str  # healthy, bad, fix
    bug: Optional[Bug] = None


@dataclass
class DemoShop:
    """State machine for the demo. `tick(now)` returns the API calls to make at time `now`."""

    rng: random.Random
    environment: str = "prod"
    requests_per_min: int = 450
    first_bad_after_sec: float = 300
    fix_after_sec: float = 600
    deploy_every_sec: float = 1800
    heartbeat_sec: float = 60
    once: bool = False
    current: Optional[LiveDeploy] = None
    next_deploy_at: float = 0.0
    finished_at: Optional[float] = None
    deploy_index: int = 0
    _kinds: List[str] = field(default_factory=lambda: ["healthy", "bad", "fix"])
    _bug_index: int = 0
    _last_tick: Optional[float] = None
    _hb_started: Optional[float] = None
    _hb_requests: int = 0
    _hb_errors: int = 0

    def _next_kind(self) -> str:
        if not self._kinds:
            self._kinds = ["healthy", "healthy", "bad", "fix"]
        return self._kinds.pop(0)

    def _deploy(self, now: float) -> Dict[str, Any]:
        kind = self._next_kind()
        sha = uuid.UUID(int=self.rng.getrandbits(128)).hex[:12]
        author = self.rng.choice(AUTHORS)
        if kind == "bad":
            bug = BUGS[self._bug_index % len(BUGS)]
            self._bug_index += 1
            body = self._deploy_body(sha, author, bug.deploy_message, bug, bug.good_code, bug.bad_code)
            self.current = LiveDeploy(sha, kind, bug)
            self.next_deploy_at = now + self.fix_after_sec
        elif kind == "fix" and self.current and self.current.bug:
            bug = self.current.bug
            body = self._deploy_body(sha, author, bug.fix_message, bug, bug.bad_code, bug.good_code)
            self.current = LiveDeploy(sha, kind)
            self.next_deploy_at = now + self.deploy_every_sec
            if self.once:
                self.finished_at = now + 2 * self.heartbeat_sec
        else:
            path, message = self.rng.choice(HEALTHY_CHANGES)
            line = self.rng.randint(10, 80)
            body = {
                "commit_sha": sha, "author": author, "message": message, "environment": self.environment,
                "diff": diff_for(path, line, "# before", "# after"), "codeowners": CODEOWNERS,
                "files": [{"path": path, "status": "modified", "added_ranges": [[line, line]], "authors": [author]}],
            }
            self.current = LiveDeploy(sha, "healthy")
            # The first bad deploy comes sooner so a new install shows an alert within minutes
            first = self.deploy_index == 0
            self.next_deploy_at = now + (self.first_bad_after_sec if first else self.deploy_every_sec)
        self.deploy_index += 1
        logger.info("Deploy %s (%s): %s", sha, kind, body["message"])
        return body

    def _deploy_body(self, sha: str, author: str, message: str, bug: Bug, old: str, new: str) -> Dict[str, Any]:
        return {
            "commit_sha": sha, "author": author, "message": message, "environment": self.environment,
            "diff": diff_for(bug.path, bug.line, old, new), "codeowners": CODEOWNERS,
            "files": [{
                "path": bug.path, "status": "modified", "added_ranges": [[bug.line, bug.line]],
                "authors": [author], "content": file_content(bug, new),
            }],
        }

    def _event(self, now: float, **fields: Any) -> Dict[str, Any]:
        return {"event_id": uuid.UUID(int=self.rng.getrandbits(128)).hex, "release": self.current.sha,
                "environment": self.environment, "occurred_at": now, **fields}

    def tick(self, now: float) -> List[Tuple[str, Dict[str, Any]]]:
        calls: List[Tuple[str, Dict[str, Any]]] = []
        if self.finished_at is not None and now >= self.finished_at:
            return calls
        if self.current is None or now >= self.next_deploy_at:
            if self.current is not None:
                calls.extend(self._flush_heartbeat(now))
            calls.append(("deploy", self._deploy(now)))
            self._last_tick = now
            self._hb_started = now
            return calls

        elapsed = now - (self._last_tick or now)
        self._last_tick = now
        requests = self.rng.randint(int(self.requests_per_min * 0.8), int(self.requests_per_min * 1.2))
        requests = max(1, round(requests * elapsed / 60))
        events: List[Dict[str, Any]] = []

        if self.rng.random() < elapsed / 60:  # about one background error a minute
            events.append(self._event(now, type="error", exc_type="TimeoutError",
                                      message="warehouse API timed out after 2s", stack_trace=BACKGROUND_TRACE,
                                      request_path="/product"))
        bug = self.current.bug
        if bug:
            failures = max(1, round(requests * bug.failure_rate))
            trace = stack_trace(bug, bug.bad_code)
            events += [
                self._event(now - self.rng.random() * elapsed, type="error", exc_type=bug.exc_type,
                            message=bug.message, stack_trace=trace, request_path=bug.endpoint)
                for _ in range(failures)
            ]
        if self.rng.random() < 0.3:
            events.append(self._event(now, type="slow_request", request_path="/search",
                                      duration_ms=float(self.rng.randint(1600, 3200))))

        self._hb_requests += requests
        self._hb_errors += sum(1 for e in events if e["type"] == "error")
        for start in range(0, len(events), 100):  # the API accepts 100 events per request
            calls.append(("events", {"events": events[start:start + 100]}))
        if now - (self._hb_started or now) >= self.heartbeat_sec:
            calls.extend(self._flush_heartbeat(now))
        return calls

    def history(self, now: float, minutes: int) -> List[Tuple[str, Dict[str, Any]]]:
        """Traffic for the `minutes` before `now`, so the first deploy has a baseline and the
        long-standing background error is already known (the API accepts events up to 24 h old)."""
        events: List[Dict[str, Any]] = []
        for m in range(minutes, 0, -1):
            start = now - m * 60
            requests = self.rng.randint(int(self.requests_per_min * 0.8), int(self.requests_per_min * 1.2))
            errors = 1 if self.rng.random() < 0.9 else 0
            base = {"environment": self.environment}
            events += [
                {**base, "event_id": uuid.UUID(int=self.rng.getrandbits(128)).hex, "occurred_at": start + 20,
                 "type": "error", "exc_type": "TimeoutError", "message": "warehouse API timed out after 2s",
                 "stack_trace": BACKGROUND_TRACE, "request_path": "/product"}
                for _ in range(errors)
            ]
            if self.rng.random() < 0.8:
                events.append({**base, "event_id": uuid.UUID(int=self.rng.getrandbits(128)).hex,
                               "occurred_at": start + 40, "type": "slow_request", "request_path": "/search",
                               "duration_ms": float(self.rng.randint(1600, 3200))})
            events.append({**base, "event_id": uuid.UUID(int=self.rng.getrandbits(128)).hex,
                           "occurred_at": start + 59, "type": "heartbeat", "requests": requests, "errors": errors})
        return [("events", {"events": events[i:i + 100]}) for i in range(0, len(events), 100)]

    def _flush_heartbeat(self, now: float) -> List[Tuple[str, Dict[str, Any]]]:
        self._hb_started = now
        if self._hb_requests <= 0:
            return []
        hb = self._event(now, type="heartbeat", requests=self._hb_requests, errors=self._hb_errors)
        self._hb_requests = self._hb_errors = 0
        return [("events", {"events": [hb]})]


def post(api: str, key: str, path: str, body: Dict[str, Any]) -> Optional[int]:
    req = urllib.request.Request(
        f"{api.rstrip('/')}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        logger.warning("%s returned HTTP %d", path, exc.code)
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("%s failed: %s", path, type(exc).__name__)
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Send demo deploys and traffic to ChangeWatch.")
    parser.add_argument("--api", default=os.getenv("DEMO_API_URL", "http://api:8000"))
    parser.add_argument("--ingest-key", default=os.getenv("DEMO_INGEST_KEY", ""))
    parser.add_argument("--environment", default=os.getenv("DEMO_ENVIRONMENT", "prod"))
    parser.add_argument("--requests-per-min", type=int, default=int(os.getenv("DEMO_REQUESTS_PER_MIN", "450")))
    parser.add_argument("--first-bad-after-min", type=float, default=float(os.getenv("DEMO_FIRST_BAD_AFTER_MIN", "5")))
    parser.add_argument("--fix-after-min", type=float, default=float(os.getenv("DEMO_FIX_AFTER_MIN", "10")))
    parser.add_argument("--deploy-every-min", type=float, default=float(os.getenv("DEMO_DEPLOY_EVERY_MIN", "30")))
    parser.add_argument("--history-min", type=int, default=int(os.getenv("DEMO_HISTORY_MIN", "30")),
                        help="minutes of past traffic to send before the first deploy (0 to skip)")
    parser.add_argument("--tick-sec", type=float, default=10.0)
    parser.add_argument("--once", action="store_true", help="one healthy → bad → fix story, then exit")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [demo] %(message)s")
    if not args.ingest_key.startswith("cw_"):
        logger.error("Set DEMO_INGEST_KEY (or --ingest-key) to a project's ingest key; see docs/DEMO.md")
        return 2

    shop = DemoShop(
        rng=random.Random(args.seed), environment=args.environment, requests_per_min=args.requests_per_min,
        first_bad_after_sec=args.first_bad_after_min * 60, fix_after_sec=args.fix_after_min * 60,
        deploy_every_sec=args.deploy_every_min * 60, once=args.once,
    )
    stopping = False

    def _stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("Sending demo traffic to %s (environment %s)", args.api, args.environment)
    if args.history_min > 0:
        for _, body in shop.history(time.time(), min(args.history_min, 1380)):
            post(args.api, args.ingest_key, "/v1/events", body)
        logger.info("Sent %d minutes of past traffic", args.history_min)

    while not stopping:
        now = time.time()
        for kind, body in shop.tick(now):
            post(args.api, args.ingest_key, "/v1/deploys" if kind == "deploy" else "/v1/events", body)
        if shop.finished_at is not None and now >= shop.finished_at:
            logger.info("Demo story finished")
            break
        deadline = time.monotonic() + args.tick_sec
        while not stopping and time.monotonic() < deadline:
            time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
