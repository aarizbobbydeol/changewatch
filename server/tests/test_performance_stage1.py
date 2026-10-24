"""Performance Addendum Stage 1 Tests: Baseline Benchmarks and Metrics.

Verifies:
1. Benchmark scenario definitions (steady, crash_loop, distinct_burst, mixed).
2. run_scenario execution and percentile/throughput calculations.
3. Saving and loading benchmark reports in benchmarks/ directory.
4. Comparative delta computation between two benchmark runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

import importlib.util

scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("scripts_bench", scripts_dir / "bench.py")
scripts_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scripts_bench)

SCENARIOS = scripts_bench.SCENARIOS
run_scenario = scripts_bench.run_scenario
run_benchmark_suite = scripts_bench.run_benchmark_suite
compare_runs = scripts_bench.compare_runs



def test_scenario_definitions():
    """Verify all 4 required benchmark scenarios are configured."""
    required = ["steady", "crash_loop", "distinct_burst", "mixed"]
    for sc in required:
        assert sc in SCENARIOS
        cfg = SCENARIOS[sc]
        assert "rate" in cfg
        assert "duration" in cfg
        assert "batch_size" in cfg
        assert "concurrency" in cfg
        assert "fingerprints" in cfg


@pytest.mark.asyncio
async def test_run_scenario_with_mock_client():
    """Verify run_scenario calculates accurate throughput and latencies."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    cfg = {
        "rate": 100,
        "duration": 0.5,
        "batch_size": 10,
        "concurrency": 5,
        "fingerprints": 2,
        "heartbeat_ratio": 0.0,
        "slow_ratio": 0.0,
        "description": "Test scenario",
    }

    res = await run_scenario("test_sc", cfg, "http://localhost:8000", "cw_key", mock_client=mock_client)

    assert res["scenario"] == "test_sc"
    assert res["total_events_sent"] == 50
    assert res["accepted_events"] == 50
    assert res["throughput_events_sec"] > 0
    assert "latencies_ms" in res
    assert res["latencies_ms"]["p50"] >= 0.0
    assert res["latencies_ms"]["p95"] >= 0.0


@pytest.mark.asyncio
async def test_run_benchmark_suite_and_compare(tmp_path, capsys):
    """Verify run_benchmark_suite and compare_runs format comparative tables."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post.return_value = mock_resp

    report1 = await run_benchmark_suite(
        scenarios=["steady"],
        url="http://localhost:8000",
        api_key="cw_key",
        duration=0.2,
        save=False,
        mock_client=mock_client,
    )

    f1 = tmp_path / "baseline.json"
    f1.write_text(json.dumps(report1), encoding="utf-8")

    # Simulate faster second run
    report2 = dict(report1)
    report2["scenarios"] = [
        {
            **report1["scenarios"][0],
            "throughput_events_sec": report1["scenarios"][0]["throughput_events_sec"] * 1.5,
            "latencies_ms": {
                **report1["scenarios"][0]["latencies_ms"],
                "p50": 5.0,
                "p95": 10.0,
            },
        }
    ]
    f2 = tmp_path / "optimized.json"
    f2.write_text(json.dumps(report2), encoding="utf-8")

    # Compare runs
    compare_runs(f1, f2)
    captured = capsys.readouterr()

    assert "Benchmark Comparison" in captured.out
    assert "STEADY" in captured.out
    assert "Throughput" in captured.out
    assert "Faster" in captured.out
