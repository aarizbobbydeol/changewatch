"""ChangeWatch SDK Overhead & Memory Budget Benchmark.

Measures:
1. Added latency per request (p50, p95, p99 in milliseconds).
   Budget: Must stay under 1.0 ms at p95.
2. Memory footprint (peak extra RAM in megabytes).
   Budget: Must stay under 20 MB.
"""
from __future__ import annotations

import asyncio
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List

# Ensure SDK is in sys.path
sdk_dir = Path(__file__).resolve().parent.parent
if str(sdk_dir) not in sys.path:
    sys.path.insert(0, str(sdk_dir))

from changewatch import ASGIMiddleware, Client


async def _dummy_asgi_app(scope, receive, send):
    """Minimal ASGI application representing a microsecond endpoint."""
    return None


def calculate_percentiles(values: List[float]) -> Dict[str, float]:
    """Compute p50, p95, p99 percentiles from a list of measurements."""
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "avg": 0.0}
    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def _perc(p: float) -> float:
        idx = min(int(n * p), n - 1)
        return round(sorted_vals[idx], 4)

    return {
        "p50": _perc(0.50),
        "p95": _perc(0.95),
        "p99": _perc(0.99),
        "max": round(sorted_vals[-1], 4),
        "avg": round(sum(sorted_vals) / n, 4),
    }


def run_overhead_benchmark(iterations: int = 5000) -> Dict[str, float]:
    """Run baseline vs ChangeWatch ASGI middleware benchmark."""
    # 1. Measure baseline
    scope = {"type": "http", "path": "/api/bench", "method": "GET"}

    async def _run_baseline():
        times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            await _dummy_asgi_app(scope, None, None)
            t1 = time.perf_counter()
            times.append((t1 - t0) * 1000.0)  # ms
        return times

    baseline_times = asyncio.run(_run_baseline())

    # 2. Measure with ChangeWatch SDK Active
    tracemalloc.start()
    client = Client(
        api_key="cw_bench_key",
        endpoint="http://localhost:8000",
        allow_insecure=True,
    )
    # Stop background thread from sending out to non-existent server during bench
    client._running = False
    middleware = ASGIMiddleware(_dummy_asgi_app, client=client)

    async def _run_sdk():
        times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            await middleware(scope, None, None)
            t1 = time.perf_counter()
            times.append((t1 - t0) * 1000.0)  # ms
        return times

    sdk_times = asyncio.run(_run_sdk())
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mem_mb = peak_mem / (1024.0 * 1024.0)

    # 3. Calculate added overhead
    added_times = [max(0.0, s - b) for s, b in zip(sdk_times, baseline_times)]
    stats = calculate_percentiles(added_times)
    stats["peak_memory_mb"] = round(peak_mem_mb, 2)
    stats["iterations"] = iterations

    return stats


def main():
    print("=" * 60)
    print("      ChangeWatch SDK Overhead & Memory Budget Benchmark      ")
    print("=" * 60)
    stats = run_overhead_benchmark(iterations=10000)
    print(f"Iterations:             {int(stats['iterations'])}")
    print(f"Added Latency p50:      {stats['p50']:.4f} ms")
    print(f"Added Latency p95:      {stats['p95']:.4f} ms  (Budget: < 1.0 ms)")
    print(f"Added Latency p99:      {stats['p99']:.4f} ms")
    print(f"Peak Added Memory:      {stats['peak_memory_mb']:.2f} MB  (Budget: < 20 MB)")
    print("-" * 60)

    # Enforce hard budgets
    if stats["p95"] >= 1.0:
        print(f"FAILED: p95 latency {stats['p95']} ms exceeded 1.0 ms budget!")
        sys.exit(1)

    if stats["peak_memory_mb"] >= 20.0:
        print(f"FAILED: Peak memory {stats['peak_memory_mb']} MB exceeded 20.0 MB budget!")
        sys.exit(1)

    print("PASSED: All safety and performance budgets satisfied.")
    print("=" * 60)


if __name__ == "__main__":
    main()
