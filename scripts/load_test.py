#!/usr/bin/env python3
"""
ChangeWatch High-Throughput Load Testing Utility (Phase 1 Stage 6).

Generates synthetic application events, slow requests, and heartbeats at scale,
sends them asynchronously in batches to /v1/events using httpx.AsyncClient,
and collects comprehensive throughput, latency percentile, and status metrics.
"""

import argparse
import asyncio
from dataclasses import dataclass, field
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional
import uuid
import httpx

logger = logging.getLogger(__name__)

ERROR_TYPES = [
    ("ValueError", "Invalid user identifier or session format"),
    ("KeyError", "Missing required configuration parameter 'auth_secret'"),
    ("ZeroDivisionError", "Division by zero in pricing discount calculation"),
    ("ConnectionError", "Database connection pool exhausted after 30s timeout"),
    ("TimeoutError", "Upstream payment gateway timed out on charge request"),
    ("RuntimeError", "Worker thread failed to acquire release lock"),
]

REQUEST_PATHS = [
    "/api/v1/checkout",
    "/api/v1/users/login",
    "/api/v1/orders/create",
    "/api/v1/cart/items",
    "/api/v1/products/search",
    "/api/v1/billing/invoices",
    "/api/v1/inventory/sync",
]


def generate_synthetic_event(
    event_type: str = "error",
    release: str = "v1.0.0",
    occurred_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Generate a single synthetic event dictionary conforming to EventItem schema."""
    if occurred_at is None:
        occurred_at = time.time()

    event_id = uuid.uuid4().hex

    if event_type == "heartbeat":
        return {
            "event_id": event_id,
            "type": "heartbeat",
            "occurred_at": occurred_at,
            "environment": "prod",
            "release": release,
            "requests": random.randint(50, 250),
            "errors": random.randint(0, 5),
            "extra": {"generator": "load_test"},
        }

    if event_type == "slow_request":
        path = random.choice(REQUEST_PATHS)
        duration_ms = round(random.uniform(1000.0, 3500.0), 2)
        return {
            "event_id": event_id,
            "type": "slow_request",
            "occurred_at": occurred_at,
            "environment": "prod",
            "release": release,
            "request_path": path,
            "duration_ms": duration_ms,
            "extra": {"method": "GET", "generator": "load_test"},
        }

    # Default: "error"
    exc_type, exc_msg = random.choice(ERROR_TYPES)
    path = random.choice(REQUEST_PATHS)
    duration_ms = round(random.uniform(15.0, 450.0), 2)
    stack_trace = (
        f"Traceback (most recent call last):\n"
        f'  File "/app/services/api.py", line 142, in handle_request\n'
        f"    return process_{path.split('/')[-1]}(payload)\n"
        f'  File "/app/services/worker.py", line 89, in execute\n'
        f"    raise {exc_type}('{exc_msg}')\n"
        f"{exc_type}: {exc_msg}"
    )

    return {
        "event_id": event_id,
        "type": "error",
        "occurred_at": occurred_at,
        "environment": "prod",
        "release": release,
        "exc_type": exc_type,
        "message": f"{exc_type}: {exc_msg}",
        "stack_trace": stack_trace,
        "request_path": path,
        "duration_ms": duration_ms,
        "extra": {"user_id": random.randint(1000, 9999), "generator": "load_test"},
    }


def generate_batch(
    batch_size: int = 20,
    heartbeat_ratio: float = 0.05,
    slow_request_ratio: float = 0.15,
    release: str = "v1.0.0",
) -> List[Dict[str, Any]]:
    """Generate a batch of synthetic events (capped between 1 and 100)."""
    batch_size = max(1, min(batch_size, 100))
    events = []
    now = time.time()

    for _ in range(batch_size):
        r = random.random()
        if r < heartbeat_ratio:
            etype = "heartbeat"
        elif r < heartbeat_ratio + slow_request_ratio:
            etype = "slow_request"
        else:
            etype = "error"

        events.append(generate_synthetic_event(event_type=etype, release=release, occurred_at=now))

    return events


def calculate_percentiles(latencies_ms: List[float]) -> Dict[str, float]:
    """Calculate p50, p95, p99, min, max, and avg request latencies in milliseconds."""
    if not latencies_ms:
        return {
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "min": 0.0,
            "max": 0.0,
            "avg": 0.0,
        }

    s = sorted(latencies_ms)
    n = len(s)

    def get_p(p: float) -> float:
        idx = int(round(p * (n - 1)))
        return s[max(0, min(idx, n - 1))]

    return {
        "p50": round(get_p(0.50), 2),
        "p95": round(get_p(0.95), 2),
        "p99": round(get_p(0.99), 2),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "avg": round(sum(s) / n, 2),
    }


@dataclass
class LoadTestMetrics:
    target_rate: int = 1000
    batch_size: int = 20
    planned_duration: float = 30.0
    actual_duration: float = 0.0
    events_sent: int = 0
    events_accepted: int = 0
    events_rate_limited: int = 0
    events_failed: int = 0
    requests_sent: int = 0
    requests_accepted: int = 0
    requests_rate_limited: int = 0
    requests_failed: int = 0
    latencies_ms: List[float] = field(default_factory=list)

    @property
    def effective_throughput_accepted(self) -> float:
        """Effective accepted throughput (events/sec achieved)."""
        if self.actual_duration <= 0:
            return 0.0
        return round(self.events_accepted / self.actual_duration, 2)

    @property
    def effective_throughput_sent(self) -> float:
        """Effective sent throughput (events/sec attempted)."""
        if self.actual_duration <= 0:
            return 0.0
        return round(self.events_sent / self.actual_duration, 2)

    def percentiles(self) -> Dict[str, float]:
        return calculate_percentiles(self.latencies_ms)

    def format_report(self, url: str) -> str:
        pct = self.percentiles()
        total_sent = self.events_sent
        acc_pct = (self.events_accepted / total_sent * 100.0) if total_sent > 0 else 0.0
        rl_pct = (self.events_rate_limited / total_sent * 100.0) if total_sent > 0 else 0.0
        fail_pct = (self.events_failed / total_sent * 100.0) if total_sent > 0 else 0.0

        lines = [
            "=" * 60,
            "               ChangeWatch Load Test Report",
            "=" * 60,
            f"Target URL:             {url}",
            f"Target Rate:            {self.target_rate} events/sec",
            f"Batch Size:             {self.batch_size} events/request",
            f"Planned Duration:       {self.planned_duration:.2f} s",
            f"Actual Duration:        {self.actual_duration:.2f} s",
            "-" * 60,
            f"Total Events Sent:      {self.events_sent:,}",
            f"  - Accepted (200):     {self.events_accepted:,} ({acc_pct:.1f}%)",
            f"  - Rate-Limited (429): {self.events_rate_limited:,} ({rl_pct:.1f}%)",
            f"  - Failed (5xx/Err):   {self.events_failed:,} ({fail_pct:.1f}%)",
            "-" * 60,
            f"Total HTTP Requests:    {self.requests_sent:,}",
            f"  - 200 OK:             {self.requests_accepted:,}",
            f"  - 429 Too Many Req:   {self.requests_rate_limited:,}",
            f"  - 5xx / Network Err:  {self.requests_failed:,}",
            "-" * 60,
            "Latency Percentiles (ms):",
            f"  - p50:                {pct['p50']:.2f} ms",
            f"  - p95:                {pct['p95']:.2f} ms",
            f"  - p99:                {pct['p99']:.2f} ms",
            f"  - min / avg / max:    {pct['min']:.2f} / {pct['avg']:.2f} / {pct['max']:.2f} ms",
            "-" * 60,
            f"Effective Throughput:   {self.effective_throughput_accepted:,.2f} events/sec (accepted)",
            f"                        {self.effective_throughput_sent:,.2f} events/sec (sent)",
            "=" * 60,
        ]
        return "\n".join(lines)


async def _execute_batch_request(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    headers: Dict[str, str],
    batch: List[Dict[str, Any]],
    metrics: LoadTestMetrics,
) -> None:
    """Send one batch of events to /v1/events and record response stats."""
    count = len(batch)
    metrics.requests_sent += 1
    metrics.events_sent += count
    endpoint = f"{url.rstrip('/')}/v1/events"
    payload = {"events": batch}

    try:
        async with semaphore:
            t0 = time.monotonic()
            try:
                resp = await client.post(endpoint, json=payload, headers=headers)
                latency_ms = (time.monotonic() - t0) * 1000.0
                metrics.latencies_ms.append(latency_ms)

                if 200 <= resp.status_code < 300:
                    metrics.requests_accepted += 1
                    try:
                        data = resp.json()
                        metrics.events_accepted += data.get("accepted", count)
                    except Exception:
                        metrics.events_accepted += count
                elif resp.status_code == 429:
                    metrics.requests_rate_limited += 1
                    metrics.events_rate_limited += count
                else:
                    metrics.requests_failed += 1
                    metrics.events_failed += count
            except Exception:
                latency_ms = (time.monotonic() - t0) * 1000.0
                metrics.latencies_ms.append(latency_ms)
                metrics.requests_failed += 1
                metrics.events_failed += count
    except Exception:
        # If acquiring semaphore fails or is cancelled
        metrics.requests_failed += 1
        metrics.events_failed += count


async def run_load_test(
    url: str = "http://localhost:8000",
    key: str = "cw_test_key_123",
    rate: int = 1000,
    duration: float = 30.0,
    batch_size: int = 20,
    concurrency: int = 50,
    release: str = "v1.0.0",
    heartbeat_ratio: float = 0.05,
    slow_request_ratio: float = 0.15,
    client: Optional[httpx.AsyncClient] = None,
) -> LoadTestMetrics:
    """
    Run an asynchronous high-throughput load test against ChangeWatch.

    - rate: Target events per second.
    - duration: Run duration in seconds.
    - batch_size: Events per HTTP request (clamped to 1..100).
    - concurrency: Max concurrent requests allowed in flight.
    """
    if rate <= 0:
        raise ValueError("rate must be greater than 0")
    if duration < 0:
        raise ValueError("duration must be non-negative")
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")

    batch_size = max(1, min(batch_size, 100))
    metrics = LoadTestMetrics(
        target_rate=rate,
        batch_size=batch_size,
        planned_duration=float(duration),
    )

    headers = {
        "X-API-Key": key,
        "Content-Type": "application/json",
    }

    # Calculate batches per second needed to achieve target event rate
    batches_per_sec = max(1.0, float(rate) / float(batch_size))
    interval = 1.0 / batches_per_sec
    concurrency_limit = max(concurrency, min(int(batches_per_sec * 2), 200))
    semaphore = asyncio.Semaphore(concurrency_limit)

    async def _runner(active_client: httpx.AsyncClient):
        start_time = time.monotonic()
        deadline = start_time + duration
        next_schedule = start_time
        active_tasks = set()

        while time.monotonic() < deadline:
            batch = generate_batch(
                batch_size=batch_size,
                heartbeat_ratio=heartbeat_ratio,
                slow_request_ratio=slow_request_ratio,
                release=release,
            )
            task = asyncio.create_task(
                _execute_batch_request(
                    client=active_client,
                    semaphore=semaphore,
                    url=url,
                    headers=headers,
                    batch=batch,
                    metrics=metrics,
                )
            )
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

            next_schedule += interval
            sleep_time = next_schedule - time.monotonic()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                # Cooperative yield so in-flight tasks can execute HTTP I/O
                await asyncio.sleep(0)
                # Cap runaway drift if scheduler falls significantly behind
                if next_schedule < time.monotonic() - (interval * 5):
                    next_schedule = time.monotonic()

        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        metrics.actual_duration = time.monotonic() - start_time

    if client is not None:
        await _runner(client)
    else:
        limits = httpx.Limits(
            max_connections=concurrency_limit * 2,
            max_keepalive_connections=concurrency_limit,
        )
        timeout = httpx.Timeout(15.0, connect=5.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as new_client:
            await _runner(new_client)

    return metrics


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="ChangeWatch High-Throughput Load Testing Utility (Stage 6)"
    )
    parser.add_argument(
        "--url",
        default=os.getenv("CHANGEWATCH_URL", "http://localhost:8000"),
        help="ChangeWatch server URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--key",
        default=os.getenv("CHANGEWATCH_API_KEY", "cw_test_key_123"),
        help="ChangeWatch API Ingest Key",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=1000,
        help="Target events per second (default: 1000)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Test duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Events per HTTP request batch (default: 20, max: 100)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Max concurrent HTTP requests (default: 50)",
    )
    parser.add_argument(
        "--heartbeat-ratio",
        type=float,
        default=0.05,
        help="Ratio of heartbeat events in batch (default: 0.05)",
    )
    parser.add_argument(
        "--slow-ratio",
        type=float,
        default=0.15,
        help="Ratio of slow request events in batch (default: 0.15)",
    )
    parser.add_argument(
        "--release",
        default="v1.0.0",
        help="Release tag for synthetic events (default: v1.0.0)",
    )
    return parser.parse_args(args)


def main():
    args = parse_args()
    print("\nStarting ChangeWatch Load Test...")
    print(f"Target:      {args.url}")
    print(f"Rate:        {args.rate} events/sec")
    print(f"Batch Size:  {min(max(1, args.batch_size), 100)}")
    print(f"Duration:    {args.duration} s\n")

    metrics = asyncio.run(
        run_load_test(
            url=args.url,
            key=args.key,
            rate=args.rate,
            duration=args.duration,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            release=args.release,
            heartbeat_ratio=args.heartbeat_ratio,
            slow_request_ratio=args.slow_ratio,
        )
    )

    print("\n" + metrics.format_report(args.url) + "\n")


if __name__ == "__main__":
    main()
