"""
Automated tests for Phase 1 Stage 6: Hardening & Load Testing.
- Retention Cleanup Service (server/app/retention.py)
- Retention CLI Command (server/app/cli.py)
- High-Throughput Load Test Utility (scripts/load_test.py)
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import httpx

from app.config import settings
from app.retention import _cleanup_with_conn, _parse_delete_count, cleanup_old_records
from app.cli import cleanup_retention_cmd, main as cli_main
from app.schemas import EventItem
from scripts.load_test import (
    calculate_percentiles,
    generate_batch,
    generate_synthetic_event,
    LoadTestMetrics,
    parse_args,
    run_load_test,
)


# ---------------------------------------------------------------------------
# 1. Retention Cleanup Service Tests
# ---------------------------------------------------------------------------


def test_parse_delete_count():
    """Verify parsing of asyncpg command status strings and integer fallbacks."""
    assert _parse_delete_count("DELETE 5000") == 5000
    assert _parse_delete_count("DELETE 0") == 0
    assert _parse_delete_count("DELETE 1") == 1
    assert _parse_delete_count("UPDATE 42") == 42
    assert _parse_delete_count(100) == 100
    assert _parse_delete_count(None) == 0
    assert _parse_delete_count("") == 0
    assert _parse_delete_count("INVALID") == 0


@pytest.mark.asyncio
async def test_cleanup_old_records_multiple_batches():
    """
    Verify cleanup_old_records executes batch deletions in a loop
    until fewer than batch_size records are deleted.
    """
    mock_conn = AsyncMock()

    # Scenario: 12,000 expired events deleted in batches of 5,000:
    # Batch 1: 5,000
    # Batch 2: 5,000
    # Batch 3: 2,000 (< 5,000 -> loop breaks)
    # Traffic query: 150 rows deleted
    mock_conn.execute.side_effect = [
        "DELETE 5000",
        "DELETE 5000",
        "DELETE 2000",
        "DELETE 150",
    ]

    stats = await cleanup_old_records(mock_conn, retention_days=30, batch_size=5000)

    assert stats == {
        "events_deleted": 12000,
        "traffic_deleted": 150,
        "batches": 3,
    }

    # Verify 4 calls were made: 3 for events, 1 for traffic
    assert mock_conn.execute.call_count == 4

    # Verify event query uses subquery/CTE with limit
    event_query_call = mock_conn.execute.call_args_list[0]
    sql_text = event_query_call[0][0]
    cutoff_arg = event_query_call[0][1]
    batch_arg = event_query_call[0][2]

    assert "DELETE FROM events" in sql_text
    assert "occurred_at < $1" in sql_text
    assert "ORDER BY id ASC" in sql_text
    assert "LIMIT $2" in sql_text
    assert batch_arg == 5000

    # Cutoff should be approximately 30 days ago in UTC
    expected_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    assert abs((cutoff_arg - expected_cutoff).total_seconds()) < 5.0

    # Verify traffic query uses bucket cutoff
    traffic_query_call = mock_conn.execute.call_args_list[3]
    traffic_sql = traffic_query_call[0][0]
    assert "DELETE FROM traffic" in traffic_sql
    assert "bucket < $1" in traffic_sql


@pytest.mark.asyncio
async def test_cleanup_old_records_zero_expired():
    """Verify cleanup returns 0 deleted and 0 batches when no expired rows exist."""
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = [
        "DELETE 0",  # First event batch: 0 deleted -> break
        "DELETE 0",  # Traffic: 0 deleted
    ]

    stats = await cleanup_old_records(mock_conn, retention_days=14, batch_size=1000)

    assert stats == {
        "events_deleted": 0,
        "traffic_deleted": 0,
        "batches": 0,
    }
    assert mock_conn.execute.call_count == 2


@pytest.mark.asyncio
async def test_cleanup_old_records_default_retention_days():
    """Verify retention_days defaults to settings.RETENTION_DAYS (30) when None."""
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = ["DELETE 0", "DELETE 0"]

    with patch.object(settings, "RETENTION_DAYS", 45):
        stats = await cleanup_old_records(mock_conn, retention_days=None, batch_size=5000)

    assert stats["events_deleted"] == 0
    cutoff_arg = mock_conn.execute.call_args_list[0][0][1]
    expected_cutoff = datetime.now(timezone.utc) - timedelta(days=45)
    assert abs((cutoff_arg - expected_cutoff).total_seconds()) < 5.0


@pytest.mark.asyncio
async def test_cleanup_old_records_with_pool():
    """Verify cleanup_old_records properly acquires and releases a connection when pool is passed."""
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = ["DELETE 10", "DELETE 2"]

    mock_pool_acquire = MagicMock()
    mock_pool_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_pool_acquire

    stats = await cleanup_old_records(mock_pool, retention_days=7, batch_size=100)

    assert stats["events_deleted"] == 10
    assert stats["traffic_deleted"] == 2
    assert stats["batches"] == 1
    mock_pool.acquire.assert_called_once()
    mock_pool_acquire.__aenter__.assert_called_once()
    mock_pool_acquire.__aexit__.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_old_records_none_fetches_global_pool():
    """Verify cleanup_old_records falls back to get_db_pool() when pool_or_conn is None."""
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = ["DELETE 0", "DELETE 0"]

    mock_pool_acquire = MagicMock()
    mock_pool_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_pool_acquire

    with patch("app.retention.get_db_pool", return_value=mock_pool):
        stats = await cleanup_old_records(None, retention_days=30, batch_size=5000)

    assert stats["events_deleted"] == 0
    mock_pool.acquire.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_old_records_validation():
    """Verify input validation errors on negative retention or invalid batch sizes."""
    mock_conn = AsyncMock()

    with pytest.raises(ValueError, match="retention_days must be non-negative"):
        await cleanup_old_records(mock_conn, retention_days=-5)

    with pytest.raises(ValueError, match="batch_size must be greater than 0"):
        await cleanup_old_records(mock_conn, retention_days=30, batch_size=0)

    with pytest.raises(ValueError, match="batch_size must be greater than 0"):
        await cleanup_old_records(mock_conn, retention_days=30, batch_size=-100)


# ---------------------------------------------------------------------------
# 2. Retention CLI Command Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_retention_cli_cmd(capsys):
    """Verify cleanup_retention_cmd initializes DB pool, runs cleanup, and closes pool."""
    mock_pool = MagicMock()
    fake_stats = {"events_deleted": 8500, "traffic_deleted": 42, "batches": 2}

    with patch("app.cli.init_db_pool", new_callable=AsyncMock) as mock_init, \
         patch("app.cli.close_db_pool", new_callable=AsyncMock) as mock_close, \
         patch("app.cli.cleanup_old_records", new_callable=AsyncMock) as mock_cleanup:
        mock_init.return_value = mock_pool
        mock_cleanup.return_value = fake_stats

        res = await cleanup_retention_cmd(days=14, batch_size=2500)

        assert res == fake_stats
        mock_init.assert_called_once()
        mock_cleanup.assert_called_once_with(mock_pool, retention_days=14, batch_size=2500)
        mock_close.assert_called_once()

    captured = capsys.readouterr()
    assert "ChangeWatch Retention Cleanup Completed" in captured.out
    assert "Retention Window:  14 days" in captured.out
    assert "Events Deleted:    8500" in captured.out
    assert "Traffic Deleted:   42" in captured.out
    assert "Batches Executed:  2" in captured.out


def test_cli_subcommand_dispatch():
    """Verify CLI parser dispatches cleanup-retention subcommand correctly."""
    test_args = ["cli.py", "cleanup-retention", "--days", "7", "--batch-size", "1000"]
    with patch("sys.argv", test_args), \
         patch("app.cli.cleanup_retention_cmd", new_callable=AsyncMock) as mock_cmd:
        cli_main()
        mock_cmd.assert_called_once_with(7, 1000)


# ---------------------------------------------------------------------------
# 3. High-Throughput Load Test Utility Tests
# ---------------------------------------------------------------------------


def test_generate_synthetic_event():
    """Verify synthetic event generator produces valid schema conforming events."""
    now = time.time() if False else datetime.now(timezone.utc).timestamp()

    # 1. Error event
    err_event = generate_synthetic_event(event_type="error", release="v2.1.0", occurred_at=now)
    assert err_event["type"] == "error"
    assert err_event["release"] == "v2.1.0"
    assert err_event["exc_type"] is not None
    assert "Traceback" in err_event["stack_trace"]
    assert err_event["request_path"].startswith("/api/v1/")
    # Validate against Pydantic EventItem
    validated_err = EventItem(**err_event)
    assert validated_err.event_id == err_event["event_id"]

    # 2. Slow request event
    slow_event = generate_synthetic_event(event_type="slow_request", release="v2.1.0", occurred_at=now)
    assert slow_event["type"] == "slow_request"
    assert slow_event["duration_ms"] >= 1000.0
    validated_slow = EventItem(**slow_event)
    assert validated_slow.type == "slow_request"

    # 3. Heartbeat event
    hb_event = generate_synthetic_event(event_type="heartbeat", release="v2.1.0", occurred_at=now)
    assert hb_event["type"] == "heartbeat"
    assert hb_event["requests"] >= 0
    assert hb_event["errors"] >= 0
    validated_hb = EventItem(**hb_event)
    assert validated_hb.type == "heartbeat"


def test_generate_batch():
    """Verify batch generator handles size constraints and contains diverse event types."""
    batch = generate_batch(batch_size=25, heartbeat_ratio=0.2, slow_request_ratio=0.2)
    assert len(batch) == 25

    # Verify each event conforms to EventItem
    for item in batch:
        EventItem(**item)

    # Clamping tests
    large_batch = generate_batch(batch_size=250)
    assert len(large_batch) == 100  # Capped at max 100

    small_batch = generate_batch(batch_size=-5)
    assert len(small_batch) == 1  # Clamped to min 1


def test_calculate_percentiles():
    """Verify latency percentile calculations for empty, uniform, and skewed distributions."""
    # Empty list
    empty_res = calculate_percentiles([])
    assert empty_res == {"p50": 0.0, "p95": 0.0, "p99": 0.0, "min": 0.0, "max": 0.0, "avg": 0.0}

    # Single value
    single_res = calculate_percentiles([42.5])
    assert single_res["p50"] == 42.5
    assert single_res["p95"] == 42.5
    assert single_res["p99"] == 42.5
    assert single_res["min"] == 42.5
    assert single_res["max"] == 42.5
    assert single_res["avg"] == 42.5

    # 100 linear values: 1.0 to 100.0
    latencies = [float(i) for i in range(1, 101)]
    res = calculate_percentiles(latencies)
    assert res["min"] == 1.0
    assert res["max"] == 100.0
    assert res["avg"] == 50.5
    assert 49.0 <= res["p50"] <= 51.0
    assert 94.0 <= res["p95"] <= 96.0
    assert 98.0 <= res["p99"] <= 100.0


def test_load_test_metrics_calculation():
    """Verify LoadTestMetrics throughput and report formatting."""
    metrics = LoadTestMetrics(
        target_rate=1000,
        batch_size=20,
        planned_duration=10.0,
        actual_duration=10.0,
        events_sent=10000,
        events_accepted=9500,
        events_rate_limited=400,
        events_failed=100,
        requests_sent=500,
        requests_accepted=475,
        requests_rate_limited=20,
        requests_failed=5,
        latencies_ms=[12.0, 18.5, 25.0, 45.0, 80.0],
    )

    assert metrics.effective_throughput_accepted == 950.0
    assert metrics.effective_throughput_sent == 1000.0

    report = metrics.format_report("http://localhost:8000")
    assert "ChangeWatch Load Test Report" in report
    assert "Total Events Sent:      10,000" in report
    assert "Accepted (200):     9,500" in report
    assert "Rate-Limited (429): 400" in report
    assert "Failed (5xx/Err):   100" in report
    assert "Effective Throughput:   950.00 events/sec (accepted)" in report


@pytest.mark.asyncio
async def test_run_load_test_with_mock_client():
    """Verify run_load_test executes async requests and records accepted/429/failed counts."""
    # Define an HTTP transport handler simulating responses:
    # Requests 1..4: HTTP 200 {"accepted": 20}
    # Request 5: HTTP 429 Too Many Requests
    # Request 6: HTTP 500 Internal Server Error
    req_counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal req_counter
        req_counter += 1
        if req_counter <= 4:
            return httpx.Response(200, json={"accepted": 20})
        elif req_counter == 5:
            return httpx.Response(429, json={"detail": "Rate limit exceeded"})
        else:
            return httpx.Response(500, json={"detail": "Internal error"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        metrics = await run_load_test(
            url="http://testserver",
            key="cw_test_key_123",
            rate=60,
            duration=0.1,  # Short duration for test
            batch_size=10,
            client=mock_client,
        )

    assert metrics.requests_sent >= 1
    assert metrics.events_sent >= 10
    assert len(metrics.latencies_ms) == metrics.requests_sent
    assert metrics.actual_duration > 0


def test_load_test_argparse():
    """Verify CLI argument parsing for load test utility."""
    args = parse_args(["--url", "http://prod-api:8000", "--key", "cw_ingest_abc", "--rate", "2500", "--duration", "45", "--batch-size", "50"])
    assert args.url == "http://prod-api:8000"
    assert args.key == "cw_ingest_abc"
    assert args.rate == 2500
    assert args.duration == 45
    assert args.batch_size == 50


@pytest.mark.asyncio
async def test_run_load_test_network_exception_handling():
    """Verify run_load_test handles network errors gracefully without crashing."""
    def handler(request: httpx.Request):
        raise httpx.ConnectError("Connection refused by testserver")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        metrics = await run_load_test(
            url="http://badserver",
            key="cw_test_key_123",
            rate=50,
            duration=0.08,
            batch_size=10,
            client=mock_client,
        )

    assert metrics.requests_failed >= 1
    assert metrics.events_failed >= 10
    assert metrics.requests_accepted == 0
    assert metrics.events_accepted == 0


def test_load_test_metrics_zero_duration():
    """Verify metrics handle zero duration without division by zero errors."""
    m = LoadTestMetrics(actual_duration=0.0, events_accepted=0, events_sent=0)
    assert m.effective_throughput_accepted == 0.0
    assert m.effective_throughput_sent == 0.0


@pytest.mark.asyncio
async def test_cleanup_old_records_zero_retention_days():
    """Verify retention_days=0 is allowed and uses current time as cutoff."""
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = ["DELETE 5", "DELETE 2"]

    stats = await cleanup_old_records(mock_conn, retention_days=0, batch_size=100)
    assert stats["events_deleted"] == 5
    assert stats["traffic_deleted"] == 2
    assert stats["batches"] == 1
    cutoff_arg = mock_conn.execute.call_args_list[0][0][1]
    now = datetime.now(timezone.utc)
    assert abs((cutoff_arg - now).total_seconds()) < 5.0


@pytest.mark.asyncio
async def test_cleanup_old_records_deletes_expired_preserves_active():
    """
    Verify retention batch deletion deletes expired records in batches
    WITHOUT deleting active records, for both events and traffic tables.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    # Simulated events table: 12 expired records, 6 active records
    simulated_events = [
        {"id": i, "occurred_at": cutoff - timedelta(days=i + 1)} for i in range(1, 13)
    ] + [
        {"id": 100 + i, "occurred_at": cutoff + timedelta(days=i + 1)} for i in range(1, 7)
    ]

    # Simulated traffic table: 4 expired rows, 3 active rows
    simulated_traffic = [
        {"bucket": cutoff - timedelta(days=i + 1), "requests": 100, "errors": 0} for i in range(1, 5)
    ] + [
        {"bucket": cutoff + timedelta(days=i + 1), "requests": 200, "errors": 1} for i in range(1, 4)
    ]

    mock_conn = AsyncMock()

    async def mock_execute(query, *args):
        nonlocal simulated_events, simulated_traffic
        if "DELETE FROM events" in query:
            cutoff_param, batch_param = args[0], args[1]
            # Find expired events sorted by id
            matching = [e for e in simulated_events if e["occurred_at"] < cutoff_param]
            matching.sort(key=lambda x: x["id"])
            to_delete = matching[:batch_param]
            to_delete_ids = {e["id"] for e in to_delete}
            simulated_events = [e for e in simulated_events if e["id"] not in to_delete_ids]
            return f"DELETE {len(to_delete)}"
        elif "DELETE FROM traffic" in query:
            cutoff_param = args[0]
            matching = [t for t in simulated_traffic if t["bucket"] < cutoff_param]
            simulated_traffic = [t for t in simulated_traffic if t["bucket"] >= cutoff_param]
            return f"DELETE {len(matching)}"
        return "DELETE 0"

    mock_conn.execute.side_effect = mock_execute

    # Run cleanup with batch_size=5 (12 expired / 5 = batches of 5, 5, 2)
    stats = await cleanup_old_records(mock_conn, retention_days=30, batch_size=5)

    # 1. Summary stats verification
    assert stats["events_deleted"] == 12
    assert stats["traffic_deleted"] == 4
    assert stats["batches"] == 3

    # 2. Verify all 6 active events are strictly preserved
    assert len(simulated_events) == 6
    for active_event in simulated_events:
        assert active_event["occurred_at"] >= cutoff
        assert active_event["id"] >= 100

    # 3. Verify all 3 active traffic records are strictly preserved
    assert len(simulated_traffic) == 3
    for active_traffic in simulated_traffic:
        assert active_traffic["bucket"] >= cutoff


@pytest.mark.asyncio
async def test_cleanup_old_records_pool_db_error_propagates():
    """Verify database errors during cleanup_old_records with pool propagate without masking."""
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = RuntimeError("PostgreSQL disk space exhausted")

    mock_pool_acquire = MagicMock()
    mock_pool_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_pool_acquire

    with pytest.raises(RuntimeError, match="PostgreSQL disk space exhausted"):
        await cleanup_old_records(mock_pool, retention_days=30, batch_size=5000)


@pytest.mark.asyncio
async def test_cleanup_retention_cli_cmd_defaults(capsys):
    """Verify cleanup_retention_cmd runs cleanly with default arguments (days=None, batch_size=5000)."""
    mock_pool = MagicMock()
    fake_stats = {"events_deleted": 0, "traffic_deleted": 0, "batches": 0}

    with patch("app.cli.init_db_pool", new_callable=AsyncMock) as mock_init, \
         patch("app.cli.close_db_pool", new_callable=AsyncMock) as mock_close, \
         patch("app.cli.cleanup_old_records", new_callable=AsyncMock) as mock_cleanup:
        mock_init.return_value = mock_pool
        mock_cleanup.return_value = fake_stats

        res = await cleanup_retention_cmd()

        assert res == fake_stats
        mock_init.assert_called_once()
        mock_cleanup.assert_called_once_with(mock_pool, retention_days=None, batch_size=5000)
        mock_close.assert_called_once()

    captured = capsys.readouterr()
    assert "ChangeWatch Retention Cleanup Completed" in captured.out
    assert "Retention Window:  30 days" in captured.out
    assert "Events Deleted:    0" in captured.out


def test_cli_subcommand_dispatch_defaults():
    """Verify CLI parser dispatches cleanup-retention subcommand with defaults."""
    test_args = ["cli.py", "cleanup-retention"]
    with patch("sys.argv", test_args), \
         patch("app.cli.cleanup_retention_cmd", new_callable=AsyncMock) as mock_cmd:
        cli_main()
        mock_cmd.assert_called_once_with(None, 5000)


def test_generate_batch_pure_ratios():
    """Verify generate_batch produces pure distributions with explicit ratios."""
    # Pure heartbeats
    hb_batch = generate_batch(batch_size=15, heartbeat_ratio=1.0)
    assert len(hb_batch) == 15
    assert all(e["type"] == "heartbeat" for e in hb_batch)

    # Pure slow requests
    slow_batch = generate_batch(batch_size=15, heartbeat_ratio=0.0, slow_request_ratio=1.0)
    assert len(slow_batch) == 15
    assert all(e["type"] == "slow_request" for e in slow_batch)


@pytest.mark.asyncio
async def test_run_load_test_input_validation():
    """Verify run_load_test validates rate, duration, and batch_size parameters."""
    with pytest.raises(ValueError, match="rate must be greater than 0"):
        await run_load_test(rate=0)

    with pytest.raises(ValueError, match="rate must be greater than 0"):
        await run_load_test(rate=-10)

    with pytest.raises(ValueError, match="duration must be non-negative"):
        await run_load_test(duration=-1.0)

    with pytest.raises(ValueError, match="batch_size must be greater than 0"):
        await run_load_test(batch_size=0)


def test_load_test_argparse_ratios():
    """Verify CLI parser handles ratio flags and release strings."""
    args = parse_args([
        "--rate", "500",
        "--heartbeat-ratio", "0.02",
        "--slow-ratio", "0.10",
        "--release", "v3.0.0-rc1",
    ])
    assert args.rate == 500
    assert args.heartbeat_ratio == 0.02
    assert args.slow_ratio == 0.10
    assert args.release == "v3.0.0-rc1"

