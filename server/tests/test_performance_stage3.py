"""Performance Addendum Stage 3 Tests: Concurrency Model & Process Supervisor.

Verifies:
1. Worker process and API worker resolution with 'auto' bounds.
2. Worker supervisor restarts terminated worker processes without losing state.
3. Memory limit watchdog triggers clean worker termination above threshold.
4. Detector advisory lock (pg_try_advisory_lock) prevents dual-active detector instances.
5. Database pool sizing per process (DB_POOL_PER_PROCESS).
6. /healthz endpoint reports queue_depth, dropped_count, and worker_count.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from app.config import resolve_api_workers, resolve_worker_processes, settings
from app.db import init_db_pool
from app.detector import DETECTOR_ADVISORY_LOCK_ID, release_detector_lock, try_acquire_detector_lock
from app.main import app
from app.worker import WorkerSupervisor, check_memory_limit


def test_resolve_worker_processes():
    """Verify resolve_worker_processes calculates auto based on CPU count with [1, 8] clamp."""
    with patch("os.cpu_count", return_value=16):
        assert resolve_worker_processes("auto") == 8

    with patch("os.cpu_count", return_value=4):
        assert resolve_worker_processes("auto") == 3

    with patch("os.cpu_count", return_value=1):
        assert resolve_worker_processes("auto") == 1

    assert resolve_worker_processes("4") == 4
    assert resolve_worker_processes("1") == 1


def test_resolve_api_workers():
    """Verify resolve_api_workers calculates auto clamped between 2 and 4."""
    with patch("os.cpu_count", return_value=8):
        assert resolve_api_workers("auto") == 4

    with patch("os.cpu_count", return_value=2):
        assert resolve_api_workers("auto") == 2

    assert resolve_api_workers("3") == 3


def test_worker_supervisor_process_management_and_restart():
    """Verify WorkerSupervisor detects terminated workers and launches replacements."""
    supervisor = WorkerSupervisor(num_processes=2)

    mock_proc_1 = MagicMock()
    mock_proc_1.is_alive.return_value = True
    mock_proc_1.pid = 1101

    mock_proc_2 = MagicMock()
    mock_proc_2.is_alive.return_value = False  # Terminated/crashed
    mock_proc_2.pid = 1102
    mock_proc_2.exitcode = 1

    replacement_proc = MagicMock()
    replacement_proc.is_alive.return_value = True
    replacement_proc.pid = 1103

    supervisor.workers = {0: mock_proc_1, 1: mock_proc_2}

    with patch.object(supervisor, "_start_worker", return_value=replacement_proc) as mock_start:
        restarted = supervisor.check_and_restart()
        assert restarted == 1
        mock_start.assert_called_once_with(1)
        assert supervisor.workers[1] == replacement_proc


def test_worker_supervisor_stop_all():
    """Verify stop_all cleanly terminates all child worker processes."""
    supervisor = WorkerSupervisor(num_processes=2)
    mock_p1 = MagicMock()
    mock_p1.is_alive.side_effect = [True, False]
    mock_p2 = MagicMock()
    mock_p2.is_alive.side_effect = [True, False]

    supervisor.workers = {0: mock_p1, 1: mock_p2}
    supervisor.stop_all(timeout=0.1)

    assert not supervisor.is_running
    mock_p1.terminate.assert_called_once()
    mock_p2.terminate.assert_called_once()


def test_check_memory_limit():
    """Verify check_memory_limit flags when usage exceeds MAX_MEMORY_MB."""
    with patch("app.worker.MAX_MEMORY_MB", 256):
        # Mock high memory: 500 MB
        mock_rusage = MagicMock()
        if sys.platform == "darwin":
            mock_rusage.ru_maxrss = 500 * 1024 * 1024
        else:
            mock_rusage.ru_maxrss = 500 * 1024

        with patch("resource.getrusage", return_value=mock_rusage):
            assert check_memory_limit() is True

        # Mock low memory: 100 MB
        if sys.platform == "darwin":
            mock_rusage.ru_maxrss = 100 * 1024 * 1024
        else:
            mock_rusage.ru_maxrss = 100 * 1024

        with patch("resource.getrusage", return_value=mock_rusage):
            assert check_memory_limit() is False


@pytest.mark.asyncio
async def test_detector_advisory_lock():
    """Verify detector advisory lock handles leader acquisition and conflict."""
    mock_conn = AsyncMock()

    # 1. Lock acquired successfully
    mock_conn.fetchrow.return_value = {"acquired": True}
    acquired = await try_acquire_detector_lock(mock_conn)
    assert acquired is True
    mock_conn.fetchrow.assert_called_with("SELECT pg_try_advisory_lock($1) AS acquired", DETECTOR_ADVISORY_LOCK_ID)

    # 2. Lock already held by another instance
    mock_conn.fetchrow.return_value = {"acquired": False}
    acquired_second = await try_acquire_detector_lock(mock_conn)
    assert acquired_second is False

    # 3. Release lock
    await release_detector_lock(mock_conn)
    mock_conn.execute.assert_called_with("SELECT pg_advisory_unlock($1)", DETECTOR_ADVISORY_LOCK_ID)


@pytest.mark.asyncio
async def test_db_pool_per_process_sizing():
    """Verify init_db_pool bounds max_size to DB_POOL_PER_PROCESS."""
    with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create_pool:
        mock_create_pool.return_value = MagicMock()
        with patch.object(settings, "DB_POOL_PER_PROCESS", 12):
            await init_db_pool(dsn="postgresql://localhost/test")
            kwargs = mock_create_pool.call_args[1]
            assert kwargs["max_size"] == 12
            assert kwargs["min_size"] == 2


@pytest.mark.asyncio
async def test_healthz_reports_queue_metrics():
    """Verify /healthz returns queue depth, dropped count, and worker count."""
    mock_redis = AsyncMock()
    mock_redis.llen.return_value = 42
    mock_redis.get.return_value = b"5"
    mock_redis.scan.return_value = (0, [b"events:processing:worker_1", b"events:processing:worker_2"])

    with patch("app.main.check_db_health", return_value=True), \
         patch("app.main.check_redis_health", return_value=True), \
         patch("app.main.get_redis", return_value=mock_redis):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200
            data = resp.json()

            assert data["status"] == "ok"
            assert data["postgres"] == "ok"
            assert data["redis"] == "ok"
            assert data["queue_depth"] == 42
            assert data["dropped_count"] == 5
            assert data["worker_count"] == 2
