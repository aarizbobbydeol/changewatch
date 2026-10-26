"""Unit tests for Usage Metering Addendum Stage 4:
Usage API, forecast, cost estimate, CSV export, budget endpoints, and admin margin aggregation.
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch
import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import require_manage_project_id, require_read_project_id
from app.config import settings
from app.main import app
from app.metering import PriceBookEntry


@pytest.fixture
def sample_price_book():
    return {
        "release_monitored": PriceBookEntry(
            version=1,
            meter="release_monitored",
            pricing_mode="fixed",
            unit_price_micros=20000,
            margin_multiplier=1.0,
            min_unit_price_micros=0,
            free_units_per_month=200,
        ),
        "ai_investigation": PriceBookEntry(
            version=1,
            meter="ai_investigation",
            pricing_mode="cost_plus",
            unit_price_micros=0,
            margin_multiplier=1.30,
            min_unit_price_micros=5000,
            free_units_per_month=20,
        ),
        "stored_events": PriceBookEntry(
            version=1,
            meter="stored_events",
            pricing_mode="fixed",
            unit_price_micros=2,
            margin_multiplier=1.0,
            min_unit_price_micros=0,
            free_units_per_month=100000,
        ),
    }


@pytest.mark.asyncio
async def test_usage_breakdown_by_meter_day_and_tenant_isolation():
    """Verify GET /v1/usage grouping by meter and day, and strict tenant isolation."""
    # Mock database rows for Project 1
    mock_rows_meter = [
        {
            "meter": "release_monitored",
            "units": 10,
            "free_units_applied": 10,
            "our_cost_micros": 0,
            "billed_micros": 0,
        },
        {
            "meter": "ai_investigation",
            "units": 5,
            "free_units_applied": 5,
            "our_cost_micros": 30000,
            "billed_micros": 0,
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = mock_rows_meter

    app.dependency_overrides[require_read_project_id] = lambda: 1

    try:
        with patch("app.main.get_connection") as mock_get_conn:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_ctx.__aexit__.return_value = None
            mock_get_conn.return_value = mock_ctx

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                # 1. Group by meter
                resp = await ac.get("/v1/usage?group=meter&from=2026-06-01&to=2026-06-30")
                assert resp.status_code == 200
                data = resp.json()
                assert data["project_id"] == 1
                assert data["group"] == "meter"
                assert len(data["items"]) == 2
                assert data["totals"]["units"] == 15
                assert data["totals"]["free_units_applied"] == 15

                # Verify query checked project_id = 1
                query_args = mock_conn.fetch.call_args[0]
                assert query_args[1] == 1  # project_id
                assert query_args[2] == date(2026, 6, 1)
                assert query_args[3] == date(2026, 6, 30)

                # 2. Group by day
                mock_rows_day = [
                    {
                        "day": date(2026, 6, 15),
                        "units": 5,
                        "free_units_applied": 5,
                        "our_cost_micros": 0,
                        "billed_micros": 0,
                    }
                ]
                mock_conn.fetch.return_value = mock_rows_day
                resp_day = await ac.get("/v1/usage?group=day&from=2026-06-01&to=2026-06-30")
                assert resp_day.status_code == 200
                data_day = resp_day.json()
                assert data_day["group"] == "day"
                assert data_day["items"][0]["day"] == "2026-06-15"
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)


@pytest.mark.asyncio
async def test_usage_forecast_not_enough_data_and_linear_projection():
    """Verify GET /v1/usage/forecast returns 'not_enough_data' when < 3 days,
    and accurate linear projection when >= 3 days.
    """
    mock_conn = AsyncMock()
    app.dependency_overrides[require_read_project_id] = lambda: 1

    try:
        with patch("app.main.get_connection") as mock_get_conn:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_ctx.__aexit__.return_value = None
            mock_get_conn.return_value = mock_ctx

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                # Case 1: Only 1 day of data on day 2 of month -> not_enough_data
                mock_conn.fetch.return_value = [{"daily_billed": 100000}]
                with patch("app.main.datetime") as mock_dt:
                    mock_dt.now.return_value = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
                    resp_short = await ac.get("/v1/usage/forecast")
                    assert resp_short.status_code == 200
                    data_short = resp_short.json()
                    assert data_short["status"] == "not_enough_data"
                    assert "3 days of data" in data_short["message"]

                # Case 2: 5 days of data on June 10 (30-day month)
                # Spent $10.00 (10,000,000 micros) over 10 elapsed days -> $1.00/day
                # Projected = 1,000,000 * 30 = $30.00 (30,000,000 micros)
                mock_conn.fetch.return_value = [
                    {"daily_billed": 2000000},
                    {"daily_billed": 2000000},
                    {"daily_billed": 2000000},
                    {"daily_billed": 2000000},
                    {"daily_billed": 2000000},
                ]
                with patch("app.main.datetime") as mock_dt:
                    mock_dt.now.return_value = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
                    resp_proj = await ac.get("/v1/usage/forecast")
                    assert resp_proj.status_code == 200
                    data_proj = resp_proj.json()
                    assert data_proj["status"] == "ok"
                    assert data_proj["current_spent_micros"] == 10000000
                    assert data_proj["daily_rate_micros"] == 1000000
                    assert data_proj["projected_month_end_micros"] == 30000000
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)


@pytest.mark.asyncio
async def test_usage_estimate_with_remaining_free_allowance(sample_price_book):
    """Verify GET /v1/usage/estimate calculates billable cost after free allowance."""
    mock_conn = AsyncMock()
    app.dependency_overrides[require_read_project_id] = lambda: 1

    try:
        with patch("app.main.get_connection") as mock_get_conn, \
             patch("app.main.get_active_price_book", return_value=sample_price_book):
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_ctx.__aexit__.return_value = None
            mock_get_conn.return_value = mock_ctx

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                # 1. 10 free AI investigations used so far this month out of 20
                mock_conn.fetchval.return_value = 10

                # Estimate count=5: all 5 are free (remaining free = 10)
                resp1 = await ac.get("/v1/usage/estimate?meter=ai_investigation&count=5")
                assert resp1.status_code == 200
                d1 = resp1.json()
                assert d1["free_applicable"] == 5
                assert d1["billable_units"] == 0
                assert d1["estimated_billed_micros"] == 0

                # Estimate count=15: 10 free, 5 billable
                # 5 billable investigations * ~25,350 micros = 126,750 micros
                resp2 = await ac.get("/v1/usage/estimate?meter=ai_investigation&count=15")
                assert resp2.status_code == 200
                d2 = resp2.json()
                assert d2["free_applicable"] == 10
                assert d2["billable_units"] == 5
                assert d2["estimated_billed_micros"] > 0

                # 2. Fixed meter: release_monitored ($0.02 = 20,000 micros)
                # 200 free used already
                mock_conn.fetchval.return_value = 200
                resp3 = await ac.get("/v1/usage/estimate?meter=release_monitored&count=10")
                assert resp3.status_code == 200
                d3 = resp3.json()
                assert d3["free_applicable"] == 0
                assert d3["billable_units"] == 10
                assert d3["estimated_billed_micros"] == 10 * 20000  # 200,000 micros
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)


@pytest.mark.asyncio
async def test_usage_csv_export():
    """Verify GET /v1/usage/export.csv returns RFC-4180 CSV format."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "day": date(2026, 6, 1),
            "meter": "release_monitored",
            "units": 10,
            "free_units_applied": 10,
            "our_cost_micros": 0,
            "billed_micros": 0,
        },
        {
            "day": date(2026, 6, 2),
            "meter": "ai_investigation",
            "units": 1,
            "free_units_applied": 0,
            "our_cost_micros": 19500,
            "billed_micros": 25350,
        },
    ]

    app.dependency_overrides[require_read_project_id] = lambda: 1

    try:
        with patch("app.main.get_connection") as mock_get_conn:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_ctx.__aexit__.return_value = None
            mock_get_conn.return_value = mock_ctx

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/v1/usage/export.csv?from=2026-06-01&to=2026-06-30")
                assert resp.status_code == 200
                assert "text/csv" in resp.headers["content-type"]
                lines = resp.text.strip().split("\n")
                assert lines[0] == "day,meter,units,free_units_applied,our_cost_micros,billed_micros"
                assert "2026-06-01,release_monitored,10,10,0,0" in lines[1]
                assert "2026-06-02,ai_investigation,1,0,19500,25350" in lines[2]
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)


@pytest.mark.asyncio
async def test_budget_get_and_put():
    """Verify GET /v1/budget and PUT /v1/budget endpoint handlers."""
    mock_conn = AsyncMock()
    app.dependency_overrides[require_read_project_id] = lambda: 1
    app.dependency_overrides[require_manage_project_id] = lambda: 1  # changing the budget

    try:
        with patch("app.main.get_connection") as mock_get_conn, \
             patch("app.main.project_transaction") as mock_proj_tx:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_ctx.__aexit__.return_value = None
            mock_get_conn.return_value = mock_ctx
            mock_proj_tx.return_value = mock_ctx

            # 1. GET /v1/budget
            with patch("app.main.get_project_budget", return_value={
                "project_id": 1,
                "soft_limit_micros": 5000000,
                "hard_limit_micros": 10000000,
                "warn_at": [50, 80, 100],
                "current_spent_micros": 2000000,
                "extras_paused": False,
            }):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    resp_get = await ac.get("/v1/budget")
                    assert resp_get.status_code == 200
                    assert resp_get.json()["soft_limit_micros"] == 5000000

            # 2. PUT /v1/budget
            with patch("app.main.set_project_budget", return_value={
                "project_id": 1,
                "soft_limit_micros": 8000000,
                "hard_limit_micros": 15000000,
                "warn_at": [50, 75, 90],
            }):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    resp_put = await ac.put(
                        "/v1/budget",
                        json={
                            "soft_limit_micros": 8000000,
                            "hard_limit_micros": 15000000,
                            "warn_at": [50, 75, 90],
                        },
                    )
                    assert resp_put.status_code == 200
                    data_put = resp_put.json()
                    assert data_put["hard_limit_micros"] == 15000000
                    assert data_put["warn_at"] == [50, 75, 90]
    finally:
        app.dependency_overrides.pop(require_read_project_id, None)
        app.dependency_overrides.pop(require_manage_project_id, None)


@pytest.mark.asyncio
async def test_admin_margin_endpoint_permissions_and_privacy():
    """Verify GET /v1/admin/margin:
    - Rejects callers without the admin key with 401 (403 only when admin endpoints are disabled).
    - Exposes strictly aggregate metrics with NO project identifiers or customer data.
    """
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "meter": "release_monitored",
            "units": 500,
            "free_units_applied": 200,
            "our_cost_micros": 0,
            "billed_micros": 6000000,
        },
        {
            "meter": "ai_investigation",
            "units": 100,
            "free_units_applied": 50,
            "our_cost_micros": 1000000,
            "billed_micros": 1300000,
        },
    ]

    # 1. Unauthenticated or wrong key -> 403 Forbidden
    with patch.object(settings, "ADMIN_API_KEY", "super_secret_admin_key"):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp_no_auth = await ac.get("/v1/admin/margin")
            assert resp_no_auth.status_code == 401

            resp_bad_key = await ac.get(
                "/v1/admin/margin", headers={"X-Admin-Key": "wrong_key"}
            )
            assert resp_bad_key.status_code == 401

    # 2. Valid admin key -> returns aggregates with 0 tenant IDs
    with patch.object(settings, "ADMIN_API_KEY", "super_secret_admin_key"), \
         patch("app.main.get_connection") as mock_get_conn:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_ctx.__aexit__.return_value = None
        mock_get_conn.return_value = mock_ctx

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp_admin = await ac.get(
                "/v1/admin/margin?from=2026-06-01&to=2026-06-30",
                headers={"X-Admin-Key": "super_secret_admin_key"},
            )
            assert resp_admin.status_code == 200
            data = resp_admin.json()

            # Verify response schema contains totals and meters
            assert "meters" in data
            assert "totals" in data
            assert len(data["meters"]) == 2
            assert data["totals"]["units"] == 600
            assert data["totals"]["our_cost_micros"] == 1000000
            assert data["totals"]["billed_micros"] == 7300000
            assert data["totals"]["margin_micros"] == 6300000

            # PRIVACY GUARANTEE: Response string must NOT contain "project_id" or project identifiers
            raw_text = resp_admin.text
            assert "project_id" not in raw_text
            assert "tenant" not in raw_text


@pytest.mark.asyncio
async def test_admin_endpoints_are_disabled_without_an_admin_key_configured():
    """With ADMIN_API_KEY unset the admin endpoints are off (403), whatever key is sent."""
    with patch.object(settings, "ADMIN_API_KEY", ""):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/v1/admin/margin", headers={"X-Admin-Key": "anything"})
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"]
