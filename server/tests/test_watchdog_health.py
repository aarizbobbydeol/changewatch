"""ChangeWatch watching itself: a dead detector stays down, failing deliveries are noticed early,
operator alerts that fail to send are retried, and tool uptime counts real outages."""
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

import app.system_alerts as sa
from app.analytics.metrics import compute_tool_uptime
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool
from app.watchdog import outbox_delivery_stats, record_status_intervals, run_watchdog_checks


def _healthy_conn():
    conn = AsyncMock()
    conn.fetchval.side_effect = [1, 0]  # SELECT 1, outbox backlog
    conn.fetchrow.return_value = {"delivered": 10, "failed": 0}
    return conn


@pytest.mark.asyncio
async def test_detector_without_heartbeat_is_down_not_degraded():
    # The heartbeat key expires 5 minutes after the detector's last cycle. Calling that
    # "degraded" resolved the operator alert with a false "Recovered" while it was still dead.
    redis = AsyncMock()
    redis.ping.return_value = True
    redis.llen.return_value = 0
    redis.keys.return_value = [b"heartbeat:worker:w1"]
    redis.get.return_value = None
    res = await run_watchdog_checks(conn=_healthy_conn(), redis=redis)
    assert res["components"]["detector"]["status"] == "down"
    assert "detector" in [a["component"] for a in sa.alerts_from_watchdog(res)]


@pytest.mark.asyncio
async def test_failing_deliveries_raise_an_alert_long_before_rows_are_marked_failed():
    conn = AsyncMock()
    conn.fetchval.side_effect = [1, 4]
    conn.fetchrow.return_value = {"delivered": 1, "failed": 4}  # 4 messages failed 3+ times
    redis = AsyncMock()
    redis.ping.return_value = True
    redis.llen.return_value = 0
    redis.keys.return_value = [b"heartbeat:worker:w1"]
    redis.get.return_value = str(time.time())
    res = await run_watchdog_checks(conn=conn, redis=redis)
    assert res["components"]["notifier"]["status"] == "degraded"
    assert any(a["title"] == "Notification deliveries are failing" for a in res["system_alerts"])


class FakeAlertsConn:
    """The system_alerts table with its one-active-alert-per-component rule."""

    def __init__(self):
        self.rows = []

    async def fetchval(self, sql, *args):
        component = args[0]
        active = [r for r in self.rows if r["component"] == component and r["status"] == "active"]
        if sql.lstrip().startswith("INSERT INTO system_alerts"):
            if active:
                return None
            row = {"id": len(self.rows) + 1, "component": component, "title": args[2], "status": "active"}
            self.rows.append(row)
            return row["id"]
        if active:
            active[0]["status"] = "resolved"
            return active[0]["id"]
        return None

    async def fetch(self, sql, *args):
        return [r for r in self.rows if r["status"] == "active"]

    async def execute(self, sql, row_id):
        if sql.startswith("DELETE"):
            self.rows = [r for r in self.rows if r["id"] != row_id]
        else:
            next(r for r in self.rows if r["id"] == row_id)["status"] = "active"


DOWN = {"component": "detector", "severity": "high", "title": "Detector has not run", "detail": ""}


@pytest.fixture
def after_grace(monkeypatch):
    monkeypatch.setattr(sa, "_process_started_at", time.monotonic() - sa.STARTUP_GRACE_SEC - 1)
    monkeypatch.setattr(settings, "SYSTEM_ALERT_WEBHOOK_URL", "https://ops.example/hook")


@pytest.mark.asyncio
async def test_alert_that_failed_to_send_is_sent_on_the_next_cycle(after_grace):
    conn = FakeAlertsConn()
    with patch.object(sa, "send_fallback", new=AsyncMock(side_effect=[False, True])) as send:
        first = await sa.sync_system_alerts(conn, [DOWN])
        second = await sa.sync_system_alerts(conn, [DOWN])
        third = await sa.sync_system_alerts(conn, [DOWN])
    assert (first["raised"], second["raised"], third["raised"]) == ([], ["detector"], [])
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_recovery_that_failed_to_send_is_sent_on_the_next_cycle(after_grace):
    conn = FakeAlertsConn()
    with patch.object(sa, "send_fallback", new=AsyncMock(side_effect=[True, False, True])) as send:
        await sa.sync_system_alerts(conn, [DOWN])
        first = await sa.sync_system_alerts(conn, [])
        second = await sa.sync_system_alerts(conn, [])
    assert (first["resolved"], second["resolved"]) == ([], ["detector"])
    assert send.await_args.args[0].startswith("✅ Recovered")


def test_uptime_counts_open_outages_and_overlaps_once():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    logs = [
        {"status": "down", "started_at": now - timedelta(minutes=60), "ended_at": now - timedelta(minutes=30)},
        {"status": "degraded", "started_at": now - timedelta(minutes=45), "ended_at": now - timedelta(minutes=20)},
        {"status": "down", "started_at": now - timedelta(minutes=10), "ended_at": None},  # still down
    ]
    m = compute_tool_uptime(logs, window_minutes=1440.0, now=now)
    assert m.details["degraded_minutes"] == 50.0  # 60..20 (40 min, overlap counted once) + 10 open
    assert m.value == round((1 - 50 / 1440) * 100, 1)


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def live_db():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    yield
    await close_db_pool()


@pytest.mark.asyncio
async def test_live_status_log_records_intervals(live_db):
    name = f"test-{uuid.uuid4().hex[:8]}"
    t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=30)
    async with get_connection() as conn:
        try:
            for minute, status in ((0, "degraded"), (1, "degraded"), (2, "degraded"), (10, "down"), (25, "ok")):
                await record_status_intervals(conn, {name: {"status": status, "detail": "x"}}, t0 + timedelta(minutes=minute))
            rows = await conn.fetch(
                "SELECT status, started_at, ended_at FROM component_status_log WHERE component = $1 ORDER BY started_at", name
            )
        finally:
            await conn.execute("DELETE FROM component_status_log WHERE component = $1", name)
    assert [(r["status"], r["started_at"] - t0, r["ended_at"] - t0) for r in rows] == [
        ("degraded", timedelta(0), timedelta(minutes=10)),
        ("down", timedelta(minutes=10), timedelta(minutes=25)),
    ]
    assert compute_tool_uptime([dict(r) for r in rows], 1440.0).details["degraded_minutes"] == 25.0


@pytest.mark.asyncio
async def test_live_messages_failing_three_times_count_as_failing(live_db):
    run = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    async with get_connection() as conn:
        before = await outbox_delivery_stats(conn, now)
        try:
            for i, (status, attempts) in enumerate((("pending", 3), ("pending", 5), ("failed", 10), ("pending", 1))):
                await conn.execute(
                    "INSERT INTO outbox (project_id, dedupe_key, payload, attempts, status) VALUES (1, $1, '{}', $2, $3)",
                    f"wd-{run}-{i}", attempts, status,
                )
            after = await outbox_delivery_stats(conn, now + timedelta(seconds=1))
        finally:
            await conn.execute("DELETE FROM outbox WHERE dedupe_key LIKE $1", f"wd-{run}-%")
    assert after[1] - before[1] == 3  # the row with one failed attempt is not "failing" yet
    assert after[0] == before[0]
