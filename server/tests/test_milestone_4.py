from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from app.auth import require_read_project_id
from app.config import settings
from app.detector import evaluate_deploy
from app.main import app
from app.sources import (
    check_dead_mans_switch,
    is_in_idle_hours,
    record_source_activity,
)
from app.watchdog import reap_stuck_queue_items, run_watchdog_checks


def build_mock_pool(mock_conn: AsyncMock):
    """Helper to mock asyncpg pool and transactions."""
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_acquire = MagicMock()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_acquire
    return mock_pool


@pytest.fixture(autouse=True)
def override_auth():
    app.dependency_overrides[require_read_project_id] = lambda: 1
    yield
    app.dependency_overrides.pop(require_read_project_id, None)


# ==============================================================================
# 1. Dead-Man's Switch Unit Tests (TRUST Stage 2)
# ==============================================================================

@pytest.mark.asyncio
async def test_record_source_activity_and_gap_adaptation():
    """Verify heartbeat recording and exponential moving average gap adaptation."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    prev_dt = now_dt - timedelta(seconds=120)

    # 1. Existing source with typical gap 60s, previous heartbeat 120s ago
    mock_conn.fetchrow.return_value = {
        "id": 1,
        "status": "active",
        "last_heartbeat_at": prev_dt,
        "typical_gap_sec": 60,
        "silent_since": None,
    }

    res = await record_source_activity(
        conn=mock_conn,
        project_id=1,
        environment="prod",
        app_name="web",
        is_heartbeat=True,
        occurred_at=now_dt,
    )

    assert res["status"] == "active"
    assert not res["recovered"]
    # 0.8 * 60 + 0.2 * 120 = 48 + 24 = 72s
    assert res["typical_gap_sec"] == 72
    mock_conn.execute.assert_called_once()


@pytest.mark.asyncio
async def test_source_recovers_from_silent_state():
    """Verify that when a silent source sends a heartbeat, it recovers and queues notification."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)

    # Source is currently silent
    mock_conn.fetchrow.side_effect = [
        {
            "id": 2,
            "status": "silent",
            "last_heartbeat_at": now_dt - timedelta(minutes=20),
            "typical_gap_sec": 60,
            "silent_since": now_dt - timedelta(minutes=10),
        },
        # outbox insert return
        {"id": 101},
    ]

    res = await record_source_activity(
        conn=mock_conn,
        project_id=1,
        environment="prod",
        app_name="payment-service",
        is_heartbeat=True,
        occurred_at=now_dt,
    )

    assert res["status"] == "active"
    assert res["recovered"] is True


def test_idle_hours_schedule():
    """Verify scheduled idle hours (nights, weekends)."""
    # Wednesday 14:00 UTC (active)
    wed_afternoon = datetime(2026, 10, 21, 14, 0, 0, tzinfo=timezone.utc)
    schedule = {"weekdays": [0, 1, 2, 3, 4], "start_hour": 8, "end_hour": 20}
    assert not is_in_idle_hours(schedule, wed_afternoon)

    # Wednesday 03:00 UTC (night -> idle)
    wed_night = datetime(2026, 10, 21, 3, 0, 0, tzinfo=timezone.utc)
    assert is_in_idle_hours(schedule, wed_night)

    # Sunday 14:00 UTC (weekend -> idle)
    sun_afternoon = datetime(2026, 10, 25, 14, 0, 0, tzinfo=timezone.utc)
    assert is_in_idle_hours(schedule, sun_afternoon)


@pytest.mark.asyncio
async def test_dead_mans_switch_triggers_silent_alert():
    """Verify that a source inactive past threshold is marked silent and triggers alert."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    last_hb = now_dt - timedelta(minutes=15)  # 15 min ago (exceeds 10 min default)

    mock_conn.fetch.return_value = [
        {
            "id": 5,
            "project_id": 1,
            "environment": "prod",
            "app_name": "worker-proc",
            "status": "active",
            "last_heartbeat_at": last_hb,
            "typical_gap_sec": 60,
            "expected_hours": None,
            "silence_alerts": True,
        }
    ]
    mock_conn.fetchrow.return_value = {"id": 999}  # outbox id

    silent_sources = await check_dead_mans_switch(
        conn=mock_conn,
        silent_after_min=10,
        now_dt=now_dt,
    )

    assert len(silent_sources) == 1
    assert silent_sources[0]["app_name"] == "worker-proc"
    assert silent_sources[0]["silent_min"] >= 15
    # Assert UPDATE sources SET status = 'silent' was run
    mock_conn.execute.assert_any_call(
        """
                UPDATE sources
                SET status = 'silent', silent_since = $1, updated_at = now()
                WHERE id = $2
                """,
        now_dt,
        5,
    )


@pytest.mark.asyncio
async def test_verdict_becomes_no_data_when_source_is_silent():
    """Trust Addendum A2 rule: Silence is never safe. Verdict is no_data when source is silent."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    deploy = {
        "id": 10,
        "deployed_at": now_dt - timedelta(minutes=12),
        "environment": "prod",
    }

    # is_source_silent returns True
    mock_conn._source_silent = True

    verdict, reasons, is_final, is_flagged = await evaluate_deploy(
        conn=mock_conn,
        project_id=1,
        deploy=deploy,
        now_dt=now_dt,
    )

    assert verdict == "no_data"
    assert any("Source is silent" in r for r in reasons)
    assert not is_flagged


# ==============================================================================
# 2. Watchdog & Status API Tests (TRUST Stage 3)
# ==============================================================================

@pytest.mark.asyncio
async def test_watchdog_detects_detector_lag_and_stale_verdicts():
    """Watchdog marks detector down and verdicts stale when detector lag > STALE_AFTER_SEC."""
    mock_conn = AsyncMock()
    mock_conn.fetchval.side_effect = [
        1,   # SELECT 1 (Postgres ok)
        0,   # outbox backlog (0 pending)
    ]
    mock_conn.fetchrow.return_value = {"delivered": 10, "failed": 0}

    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True
    mock_redis.llen.return_value = 5
    mock_redis.keys.return_value = [b"heartbeat:worker:uuid-1"]
    # Detector last ran 300 seconds ago (> STALE_AFTER_SEC 180s)
    now_ts = datetime.now(timezone.utc).timestamp()
    mock_redis.get.return_value = str(now_ts - 300)

    res = await run_watchdog_checks(conn=mock_conn, redis=mock_redis)

    assert res["overall_status"] == "degraded"
    assert res["verdicts_fresh"] is False
    assert res["components"]["detector"]["status"] == "down"
    assert len(res["system_alerts"]) >= 1
    assert res["system_alerts"][0]["component"] == "detector"


@pytest.mark.asyncio
async def test_watchdog_reaps_stuck_queue_items():
    """Verify reliable queue reaper returns stuck items to 'events' queue."""
    mock_redis = AsyncMock()
    mock_redis.keys.return_value = [b"events:processing:worker-dead"]
    mock_redis.llen.return_value = 2
    # Stuck item timestamp is 120s ago (> 60s timeout)
    now_ts = datetime.now(timezone.utc).timestamp()
    mock_redis.get.return_value = str(now_ts - 120)
    mock_redis.rpoplpush.side_effect = [b"event1", b"event2"]

    reaped = await reap_stuck_queue_items(mock_redis)
    assert reaped == 2
    assert mock_redis.rpoplpush.call_count == 2


@pytest.mark.asyncio
async def test_get_system_status_api_endpoint():
    """Test GET /v1/status returns all components, sources, and queue lag."""
    mock_conn = AsyncMock()
    mock_conn.fetchval.side_effect = [
        1,  # SELECT 1
        0,  # outbox backlog
    ]
    mock_conn.fetchrow.side_effect = [
        {"delivered": 5, "failed": 0},  # outbox 15m
    ]
    mock_conn.fetch.return_value = [
        {
            "environment": "prod",
            "app_name": "api-gateway",
            "status": "active",
            "last_heartbeat_at": datetime.now(timezone.utc),
            "last_event_at": None,
            "typical_gap_sec": 60,
            "silent_since": None,
            "silence_alerts": True,
        }
    ]
    mock_pool = build_mock_pool(mock_conn)

    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True
    mock_redis.llen.return_value = 3
    mock_redis.keys.return_value = [b"heartbeat:worker:1"]
    mock_redis.get.return_value = str(datetime.now(timezone.utc).timestamp() - 10)
    # Dropped events are counted per project and day in Redis (30 days x 2 reasons)
    mock_redis.mget.return_value = [b"12"] + [None] * 59

    with patch("app.db.get_read_db_pool", return_value=mock_pool), \
         patch("app.main.get_redis", return_value=mock_redis):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/status", headers={"X-API-Key": "cw_read_test_key"})

    assert resp.status_code == 200
    data = resp.json()
    assert "overall_status" in data
    assert "components" in data
    assert "database" in data["components"]
    assert "queue" in data["components"]
    assert "detector" in data["components"]
    assert len(data["sources"]) == 1
    assert data["sources"][0]["app_name"] == "api-gateway"
    assert data["dropped_events"] == 12


# ==============================================================================
# 3. Ephemeral Code Mode Tests (TD Stage 3)
# ==============================================================================

@pytest.mark.asyncio
async def test_ephemeral_code_mode_stores_no_diff_or_file_content():
    """In CODE_MODE=ephemeral, server stores no diff and no file content."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        {"id": 42},
        # price_book query for usage metering
        {"version": 1, "pricing_mode": "cost_plus", "unit_price_micros": 1000, "margin_multiplier": 1.1, "min_unit_price_micros": 100},
    ]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.db.get_db_pool", return_value=mock_pool), \
         patch.object(settings, "CODE_MODE", "ephemeral"):

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "commit_sha": "ephemeral_sha_123",
                "environment": "prod",
                "diff": "diff --git a/test.py b/test.py\n+secret_code_line()",
                "message": "fix: security patch",
                "files": [
                    {
                        "path": "app/secret.py",
                        "status": "modified",
                        "content": "def secret_code_line(): return 'super_secret'",
                        "added_ranges": [[1, 2]],
                        "authors": ["alice"],
                    }
                ],
            }
            # Dependency override for require_ingest_project_id
            from app.auth import require_ingest_project_id
            app.dependency_overrides[require_ingest_project_id] = lambda: 1

            resp = await client.post(
                "/v1/deploys",
                json=payload,
                headers={"X-API-Key": "cw_live_test_key"},
            )

            app.dependency_overrides.pop(require_ingest_project_id, None)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Assert INSERT into deploys had diff=None, diff_enc=None
    deploy_call_args = mock_conn.fetchrow.call_args_list[0][0]
    # Parameter order: query, project_id, commit_sha, author, message, diff, diff_enc, environment, codeowners
    assert deploy_call_args[5] is None  # diff is None
    assert deploy_call_args[6] is None  # diff_enc is None

    # Assert INSERT into deploy_files had content=None
    insert_file_call = None
    for call in mock_conn.execute.call_args_list:
        if "deploy_files" in str(call[0][0]):
            insert_file_call = call[0]
            break
    assert insert_file_call is not None
    # Values: query, deploy_id, project_id, path, status, added_ranges, authors, content, is_dep, is_cfg
    assert insert_file_call[7] is None  # content is None!
    # But metadata IS stored
    assert insert_file_call[3] == "app/secret.py"
    assert "alice" in insert_file_call[6]
