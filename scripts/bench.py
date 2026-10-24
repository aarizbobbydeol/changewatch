#!/usr/bin/env python3
"""ChangeWatch Performance Benchmark Suite.

Executes standardized scenarios:
1. steady: steady 500 events/s for 60s
2. crash_loop: burst of 5,000 events/s for 30s with a single fingerprint
3. distinct_burst: burst with 1,000 distinct fingerprints
4. mixed: mixed traffic with heartbeats and slow requests

Saves results to benchmarks/<timestamp>.json and supports side-by-side run comparisons.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx

# Ensure load_test from scripts is importable
scripts_dir = Path(__file__).resolve().parent
root_dir = scripts_dir.parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from load_test import generate_batch, calculate_percentiles, LoadTestMetrics


SCENARIOS = {
    "steady": {
        "rate": 500,
        "duration": 10.0,  # Default for test runs; full runs use 60.0
        "batch_size": 20,
        "concurrency": 25,
        "fingerprints": 10,
        "heartbeat_ratio": 0.05,
        "slow_ratio": 0.10,
        "description": "Steady traffic: 500 events/s with balanced distribution",
    },
    "crash_loop": {
        "rate": 5000,
        "duration": 10.0,  # Default for test runs; full runs use 30.0
        "batch_size": 50,
        "concurrency": 100,
        "fingerprints": 1,
        "heartbeat_ratio": 0.0,
        "slow_ratio": 0.0,
        "description": "Crash loop burst: 5,000 events/s with single identical fingerprint",
    },
    "distinct_burst": {
        "rate": 3000,
        "duration": 10.0,
        "batch_size": 50,
        "concurrency": 60,
        "fingerprints": 1000,
        "heartbeat_ratio": 0.0,
        "slow_ratio": 0.05,
        "description": "High entropy burst: 3,000 events/s across 1,000 distinct fingerprints",
    },
    "mixed": {
        "rate": 1000,
        "duration": 10.0,
        "batch_size": 20,
        "concurrency": 50,
        "fingerprints": 50,
        "heartbeat_ratio": 0.10,
        "slow_ratio": 0.15,
        "description": "Mixed realistic workload with heartbeats and slow requests",
    },
}


async def run_scenario(
    name: str,
    cfg: Dict[str, Any],
    url: str,
    api_key: str,
    duration_override: Optional[float] = None,
    mock_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run an individual benchmark scenario and collect performance metrics."""
    duration = duration_override if duration_override is not None else cfg["duration"]
    rate = cfg["rate"]
    batch_size = cfg["batch_size"]
    concurrency = cfg["concurrency"]
    fingerprints = cfg["fingerprints"]
    heartbeat_ratio = cfg["heartbeat_ratio"]
    slow_ratio = cfg["slow_ratio"]

    metrics = LoadTestMetrics()
    semaphore = asyncio.Semaphore(concurrency)

    # Pre-generate sample fingerprints to simulate target distribution
    fps = [f"err_class_fp_{i:04d}" for i in range(max(1, fingerprints))]

    async def _send_batch(client: httpx.AsyncClient, batch: List[Dict[str, Any]]):
        async with semaphore:
            payload = {"events": batch}
            headers = {"Content-Type": "application/json", "X-API-Key": api_key}
            count = len(batch)
            metrics.requests_sent += 1
            metrics.events_sent += count
            t0 = time.monotonic()
            try:
                resp = await client.post(f"{url}/v1/events", json=payload, headers=headers)
                latency_ms = (time.monotonic() - t0) * 1000.0
                metrics.latencies_ms.append(latency_ms)

                if resp.status_code == 200:
                    metrics.requests_accepted += 1
                    try:
                        data = resp.json()
                        if isinstance(data, dict) and isinstance(data.get("accepted"), (int, float)):
                            metrics.events_accepted += int(data["accepted"])
                        else:
                            metrics.events_accepted += count
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

    client = mock_client or httpx.AsyncClient(timeout=10.0)
    should_close_client = mock_client is None

    total_events_target = int(rate * duration)
    total_batches = max(1, total_events_target // batch_size)
    batch_interval = duration / total_batches

    start_time = time.monotonic()
    tasks = []

    try:
        for b_idx in range(total_batches):
            # Pick fingerprint from set
            fp_sample = fps[b_idx % len(fps)]
            batch = generate_batch(
                batch_size=batch_size,
                release="v1.0.0",
                heartbeat_ratio=heartbeat_ratio,
                slow_request_ratio=slow_ratio,
            )

            # Tag error events with selected fingerprint
            for item in batch:
                if item.get("type") == "error":
                    item["exc_type"] = fp_sample

            tasks.append(asyncio.create_task(_send_batch(client, batch)))

            expected_time = start_time + (b_idx + 1) * batch_interval
            sleep_duration = expected_time - time.monotonic()
            if sleep_duration > 0.001:
                await asyncio.sleep(sleep_duration)
            else:
                await asyncio.sleep(0)

        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if should_close_client:
            await client.aclose()

    metrics.actual_duration = time.monotonic() - start_time
    latencies = calculate_percentiles(metrics.latencies_ms)
    throughput = round(metrics.events_accepted / metrics.actual_duration, 2) if metrics.actual_duration > 0 else 0.0

    return {
        "scenario": name,
        "description": cfg["description"],
        "target_rate": rate,
        "batch_size": batch_size,
        "planned_duration": duration,
        "actual_duration": round(metrics.actual_duration, 2),
        "total_events_sent": metrics.events_sent,
        "accepted_events": metrics.events_accepted,
        "rate_limited_events": metrics.events_rate_limited,
        "failed_events": metrics.events_failed,
        "throughput_events_sec": throughput,
        "latencies_ms": latencies,
    }


def compare_runs(file1: Path, file2: Path):
    """Print a side-by-side comparative table between two benchmark run JSON files."""
    data1 = json.loads(file1.read_text(encoding="utf-8"))
    data2 = json.loads(file2.read_text(encoding="utf-8"))

    print("\n" + "=" * 90)
    print(f"  Benchmark Comparison: {file1.name} (Baseline) vs {file2.name} (Optimized)")
    print("=" * 90)

    scenarios1 = {s["scenario"]: s for s in data1.get("scenarios", [])}
    scenarios2 = {s["scenario"]: s for s in data2.get("scenarios", [])}

    all_keys = sorted(set(scenarios1.keys()) | set(scenarios2.keys()))

    for key in all_keys:
        s1 = scenarios1.get(key)
        s2 = scenarios2.get(key)
        print(f"\nScenario: {key.upper()}")
        print("-" * 90)
        print(f"{'Metric':<30} | {'Baseline':<18} | {'Optimized':<18} | {'Delta':<15}")
        print("-" * 90)

        if not s1 or not s2:
            print("Scenario missing in one of the runs")
            continue

        metrics_to_compare = [
            ("Throughput (events/s)", s1["throughput_events_sec"], s2["throughput_events_sec"], True),
            ("Latency p50 (ms)", s1["latencies_ms"]["p50"], s2["latencies_ms"]["p50"], False),
            ("Latency p95 (ms)", s1["latencies_ms"]["p95"], s2["latencies_ms"]["p95"], False),
            ("Latency p99 (ms)", s1["latencies_ms"]["p99"], s2["latencies_ms"]["p99"], False),
            ("Accepted Events", s1["accepted_events"], s2["accepted_events"], True),
            ("Failed Events", s1["failed_events"], s2["failed_events"], False),
        ]

        for label, val1, val2, higher_is_better in metrics_to_compare:
            if val1 == 0:
                pct = 0.0
            else:
                pct = ((val2 - val1) / val1) * 100.0

            sign = "+" if pct > 0 else ""
            delta_str = f"{sign}{pct:.1f}%"

            # Color coding
            if abs(pct) < 1.0:
                verdict = delta_str
            elif (pct > 0 and higher_is_better) or (pct < 0 and not higher_is_better):
                verdict = f"\033[92m{delta_str} (Faster)\033[0m"
            else:
                verdict = f"\033[91m{delta_str} (Slower)\033[0m"

            print(f"{label:<30} | {val1:<18.2f} | {val2:<18.2f} | {verdict:<15}")

    print("=" * 90 + "\n")


async def run_benchmark_suite(
    scenarios: List[str],
    url: str,
    api_key: str,
    duration: Optional[float] = None,
    save: bool = True,
    mock_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute scenarios and save results."""
    results = []
    print("\n" + "=" * 70)
    print("         ChangeWatch Performance Benchmark Suite Runner         ")
    print("=" * 70)

    for sc_name in scenarios:
        if sc_name not in SCENARIOS:
            print(f"Warning: Unknown scenario '{sc_name}', skipping")
            continue

        cfg = SCENARIOS[sc_name]
        print(f"\nRunning Scenario: {sc_name.upper()} ({cfg['description']})...")
        res = await run_scenario(sc_name, cfg, url, api_key, duration, mock_client=mock_client)
        results.append(res)
        print(f"  -> Throughput:   {res['throughput_events_sec']:.2f} events/sec")
        print(f"  -> Latency p50:  {res['latencies_ms']['p50']:.2f} ms")
        print(f"  -> Latency p95:  {res['latencies_ms']['p95']:.2f} ms")
        print(f"  -> Accepted:     {res['accepted_events']}/{res['total_events_sent']}")

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "scenarios": results,
    }

    if save:
        bench_dir = root_dir / "benchmarks"
        bench_dir.mkdir(exist_ok=True)
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_file = bench_dir / f"bench_{ts_slug}.json"
        out_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n[Saved benchmark results to {out_file}]")
        report["output_file"] = str(out_file)

    print("=" * 70 + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description="ChangeWatch Performance Benchmarking")
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()) + ["all"], default="all")
    parser.add_argument("--url", default="http://localhost:8000", help="ChangeWatch Ingest API URL")
    parser.add_argument("--key", default="cw_test_key_123", help="Ingest API Key")
    parser.add_argument("--duration", type=float, default=None, help="Duration override per scenario")
    parser.add_argument("--no-save", action="store_true", help="Do not save output to benchmarks/")
    parser.add_argument("--output", type=str, default=None, help="Explicit output file path for results")
    parser.add_argument("--mock", action="store_true", help="Run benchmark with simulated mock client")
    parser.add_argument("--compare", nargs=2, metavar=("RUN1", "RUN2"), help="Compare two benchmark JSON files")

    args = parser.parse_args()

    if args.compare:
        compare_runs(Path(args.compare[0]), Path(args.compare[1]))
        sys.exit(0)

    mock_client = None
    if args.mock:
        from unittest.mock import AsyncMock, MagicMock
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"accepted": None}

        async def _mock_post(*a, **kw):
            await asyncio.sleep(0.003)  # 3ms simulated roundtrip
            return mock_resp

        mock_client.post.side_effect = _mock_post

    scenarios = list(SCENARIOS.keys()) if args.scenario == "all" else [args.scenario]
    report = asyncio.run(
        run_benchmark_suite(
            scenarios=scenarios,
            url=args.url,
            api_key=args.key,
            duration=args.duration,
            save=not args.no_save,
            mock_client=mock_client,
        )
    )

    if args.output and not args.no_save:
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[Saved benchmark results to {out_p}]")


if __name__ == "__main__":
    main()
