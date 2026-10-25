from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.analytics.metrics import (
    compute_change_failure_rate,
    compute_hotspots,
    compute_releases,
    compute_time_to_detect,
    compute_time_to_fix,
)
from app.auth import require_read_project_id
from app.main import app


@pytest.fixture(autouse=True)
def override_auth():
    app.dependency_overrides[require_read_project_id] = lambda: 1
    yield
    app.dependency_overrides.pop(require_read_project_id, None)


# ==============================================================================
# 1. Pure Metric Calculation Unit Tests
# ==============================================================================

def test_compute_releases_minimum_samples():
    # Less than 5 deploys -> sample_ok=False
    deploys = [{"id": 1, "first_verdict": "safe"}, {"id": 2, "first_verdict": "safe"}]
    res = compute_releases(deploys)
    assert not res.sample_ok
    assert "Not enough data yet" in res.formatted
    assert res.n == 2

    # Exactly 5 deploys: 4 safe, 1 rollback
    deploys_5 = [
        {"id": 1, "first_verdict": "safe"},
        {"id": 2, "first_verdict": "safe"},
        {"id": 3, "first_verdict": "safe"},
        {"id": 4, "first_verdict": "safe"},
        {"id": 5, "first_verdict": "rollback"},
    ]
    res_5 = compute_releases(deploys_5)
    assert res_5.sample_ok
    assert res_5.value == 5.0
    assert "80.0%" in res_5.note


def test_compute_change_failure_rate_filters_recent():
    now = datetime.now(timezone.utc)
    # Deploys younger than 24h should be excluded from denominator
    deploys = [
        # 10 deploys older than 24h
        {"id": i, "deployed_at": now - timedelta(hours=48), "has_confirmed_incident": (i == 1), "verdict": "safe"}
        for i in range(1, 11)
    ]
    # Add 5 deploys younger than 24h (must be ignored)
    deploys.extend([
        {"id": i, "deployed_at": now - timedelta(hours=2), "has_confirmed_incident": True, "verdict": "rollback"}
        for i in range(11, 16)
    ])

    res = compute_change_failure_rate(deploys, now_dt=now)
    assert res.sample_ok
    assert res.n == 10  # only the 10 eligible deploys
    assert res.value == 10.0  # 1 of 10 failed = 10%
    assert res.formatted == "10.0%"


def test_compute_time_to_detect():
    now = datetime.now(timezone.utc)
    incidents = [
        {"first_event_at": now - timedelta(seconds=10), "alerted_at": now - timedelta(seconds=7)},  # 3s
        {"first_event_at": now - timedelta(seconds=20), "alerted_at": now - timedelta(seconds=15)}, # 5s
        {"first_event_at": now - timedelta(seconds=30), "alerted_at": now - timedelta(seconds=26)}, # 4s
        {"first_event_at": now - timedelta(seconds=40), "alerted_at": now - timedelta(seconds=34)}, # 6s
        {"first_event_at": now - timedelta(seconds=50), "alerted_at": now - timedelta(seconds=48)}, # 2s
    ]
    res = compute_time_to_detect(incidents)
    assert res.sample_ok
    assert res.n == 5
    # Sorted durations: [2.0, 3.0, 4.0, 5.0, 6.0] -> median is 4.0s
    assert res.value == 4.0
    assert res.formatted == "4.0s"


def test_compute_time_to_fix():
    now = datetime.now(timezone.utc)
    resolved = [
        {"created_at": now - timedelta(minutes=60), "last_event_at": now - timedelta(minutes=50)}, # 10m
        {"created_at": now - timedelta(minutes=60), "last_event_at": now - timedelta(minutes=40)}, # 20m
        {"created_at": now - timedelta(minutes=60), "last_event_at": now - timedelta(minutes=30)}, # 30m
    ]
    res = compute_time_to_fix(resolved)
    assert res.sample_ok
    assert res.n == 3
    assert res.value == 20.0
    assert res.formatted == "20.0 min"


def test_compute_hotspots_no_person_rankings():
    incidents = [
        {"where_file": "services/auth/jwt.py", "is_false_alarm": False},
        {"where_file": "services/auth/oauth.py", "is_false_alarm": False},
        {"where_file": "services/billing/charge.py", "is_false_alarm": False},
        {"where_file": "services/billing/invoice.py", "is_false_alarm": True},  # ignored (false alarm)
        {"where_file": "app/main.py", "is_false_alarm": False},
    ]
    hotspots = compute_hotspots(incidents, depth=2)
    areas = [h["area"] for h in hotspots]
    assert "services/auth" in areas
    assert "services/billing" in areas
    assert "app/main.py" in areas
    # Verify no individual developer names appear in keys
    for h in hotspots:
        assert "author" not in h
        assert "owner" not in h


def test_static_check_no_person_ranking():
    """Verify that metrics.py contains zero functions or keys ranking individual people."""
    import inspect
    from app.analytics import metrics

    source = inspect.getsource(metrics)
    assert "rank_authors" not in source
    assert "leaderboard" not in source
    assert "developer_ranking" not in source


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


# ==============================================================================
# 2. Incidents Read API Endpoints Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_list_incidents_under_rls():
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "id": 1,
            "deploy_id": 10,
            "issue_id": 20,
            "status": "possible",
            "title": "KeyError: 'item'",
            "where_file": "app/views.py",
            "where_line": 42,
            "where_function": "render",
            "in_diff": True,
            "owners": '["@backend-team"]',
            "event_count": 5,
            "category": "missing_data_or_null",
            "first_alert_ms": 120,
            "created_at": datetime.now(timezone.utc),
            "confirmed_at": None,
            "resolved_at": None,
        }
    ]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.db.get_read_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/incidents", headers={"X-API-Key": "cw_read_test_key"})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == 1
    assert data[0]["status"] == "possible"
    assert data[0]["owners"] == ["@backend-team"]


@pytest.mark.asyncio
async def test_get_incident_detail_with_code_excerpt():
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)
    mock_conn.fetchrow.side_effect = [
        # incident row join deploy
        {
            "id": 2,
            "deploy_id": 15,
            "issue_id": 25,
            "status": "confirmed",
            "title": "ValueError: bad input",
            "where_file": "calc.py",
            "where_line": 3,
            "where_function": "compute",
            "in_diff": True,
            "owners": '["alice"]',
            "event_count": 12,
            "category": "logic_bug",
            "first_alert_ms": 85,
            "created_at": now_dt - timedelta(minutes=10),
            "confirmed_at": now_dt - timedelta(minutes=5),
            "resolved_at": None,
            "commit_sha": "deploy_sha_99",
            "commit_author": "alice",
            "commit_message": "feat: math updates",
            "deployed_at": now_dt - timedelta(minutes=15),
        },
        # deploy_files content
        {"content": "line 1\nline 2\nline 3: boom()\nline 4\nline 5\n"},
    ]

    mock_pool = build_mock_pool(mock_conn)

    with patch("app.db.get_read_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/incidents/2", headers={"X-API-Key": "cw_read_test_key"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 2
    assert data["status"] == "confirmed"
    assert "boom()" in data["code_excerpt"]
    assert len(data["timeline"]) >= 3
    assert data["ai_investigation"]["status"] == "not_enabled"


# ==============================================================================
# 3. Analytics API Endpoints Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_analytics_definitions():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/analytics/definitions", headers={"X-API-Key": "cw_read_test_key"})

    assert resp.status_code == 200
    defs = resp.json()
    assert "releases" in defs
    assert "change_failure_rate" in defs
    assert "time_to_detect" in defs
    assert "tool_uptime" in defs


@pytest.mark.asyncio
async def test_analytics_overview_unbuilt_features():
    mock_conn = AsyncMock()
    mock_conn.fetch.side_effect = [
        [],  # deploys
        [],  # incidents
        [],  # status_logs
        [],  # alert ratings
    ]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.db.get_read_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/analytics/overview?period=30", headers={"X-API-Key": "cw_read_test_key"})

    assert resp.status_code == 200
    data = resp.json()
    assert "kpis" in data
    # Empty DB should show 'Not enough data yet', never fake zeros or made up metrics
    assert not data["kpis"]["releases"]["sample_ok"]
    assert "Not enough data yet" in data["kpis"]["releases"]["formatted"]


@pytest.mark.asyncio
async def test_analytics_csv_export():
    mock_conn = AsyncMock()
    mock_conn.fetch.side_effect = [
        [],  # deploys
        [],  # incidents
        [],  # status_logs
        [],  # alert ratings
    ]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.db.get_read_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/analytics/export.csv?metric=overview", headers={"X-API-Key": "cw_read_test_key"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert "attachment; filename=" in resp.headers["content-disposition"]
    lines = resp.text.splitlines()
    assert lines[0] == "Metric,Value,Unit,N,Sample OK,Note"
