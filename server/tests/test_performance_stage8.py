"""Tests for Performance Addendum Stage 8:
Final Benchmark Comparison Table & Suite Verification.
"""
import json
from pathlib import Path
import pytest
from scripts.bench import compare_runs, run_benchmark_suite, SCENARIOS


def test_baseline_and_optimized_benchmark_files_exist():
    """Verify baseline.json and optimized.json benchmark artifacts exist."""
    bench_dir = Path(__file__).resolve().parent.parent.parent / "benchmarks"
    baseline = bench_dir / "baseline.json"
    optimized = bench_dir / "optimized.json"

    assert baseline.exists(), "benchmarks/baseline.json must exist"
    assert optimized.exists(), "benchmarks/optimized.json must exist"

    base_data = json.loads(baseline.read_text(encoding="utf-8"))
    opt_data = json.loads(optimized.read_text(encoding="utf-8"))

    assert len(base_data["scenarios"]) == 4
    assert len(opt_data["scenarios"]) == 4

    # Verify all scenarios have 0 failures and sub-5ms latencies
    for sc in opt_data["scenarios"]:
        assert sc["failed_events"] == 0
        assert sc["latencies_ms"]["p95"] < 5.0, f"Scenario {sc['scenario']} p95 latency must be < 5.0ms"


def test_compare_runs_output(capsys):
    """Verify compare_runs computes deltas and prints comparative report."""
    bench_dir = Path(__file__).resolve().parent.parent.parent / "benchmarks"
    baseline = bench_dir / "baseline.json"
    optimized = bench_dir / "optimized.json"

    compare_runs(baseline, optimized)
    captured = capsys.readouterr()

    assert "Benchmark Comparison" in captured.out
    assert "CRASH_LOOP" in captured.out
    assert "DISTINCT_BURST" in captured.out
    assert "MIXED" in captured.out
    assert "STEADY" in captured.out
