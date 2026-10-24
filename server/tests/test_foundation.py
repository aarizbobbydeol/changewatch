from unittest.mock import AsyncMock, patch
import httpx
import pytest
from app.auth import get_project_id_by_api_key
from app.db import project_transaction
from app.main import app


@pytest.mark.asyncio
async def test_healthz_all_healthy():
    """Test /healthz returns 200 when both Postgres and Redis are healthy."""
    with patch("app.main.check_db_health", new_callable=AsyncMock) as mock_db, \
         patch("app.main.check_redis_health", new_callable=AsyncMock) as mock_redis:
        mock_db.return_value = True
        mock_redis.return_value = True

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["postgres"] == "ok"
            assert data["redis"] == "ok"


@pytest.mark.asyncio
async def test_healthz_postgres_down():
    """Test /healthz returns 503 when Postgres is unreachable."""
    with patch("app.main.check_db_health", new_callable=AsyncMock) as mock_db, \
         patch("app.main.check_redis_health", new_callable=AsyncMock) as mock_redis:
        mock_db.return_value = False
        mock_redis.return_value = True

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "error"
            assert data["postgres"] == "unreachable"
            assert data["redis"] == "ok"


@pytest.mark.asyncio
async def test_healthz_redis_down():
    """Test /healthz returns 503 when Redis is unreachable."""
    with patch("app.main.check_db_health", new_callable=AsyncMock) as mock_db, \
         patch("app.main.check_redis_health", new_callable=AsyncMock) as mock_redis:
        mock_db.return_value = True
        mock_redis.return_value = False

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "error"
            assert data["postgres"] == "ok"
            assert data["redis"] == "unreachable"


@pytest.mark.asyncio
async def test_auth_missing_header():
    """Test request without X-API-Key returns 401."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/auth/verify")
        assert resp.status_code == 401
        assert "Missing X-API-Key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_auth_invalid_key():
    """Test request with invalid X-API-Key returns 401."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None  # Cache miss

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None  # Key not in DB

    mock_db_ctx = AsyncMock()
    mock_db_ctx.__aenter__.return_value = mock_conn

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.auth.get_connection", return_value=mock_db_ctx):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/auth/verify", headers={"X-API-Key": "invalid_random_key"}
            )
            assert resp.status_code == 401
            assert "Invalid API key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_auth_valid_key_cache_miss_then_hit():
    """Test valid API key fetches from DB on miss, then uses Redis cache."""
    mock_redis = AsyncMock()
    # First lookup: cache miss
    mock_redis.get.return_value = None

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "name": "my-test-app"}

    mock_db_ctx = AsyncMock()
    mock_db_ctx.__aenter__.return_value = mock_conn

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.auth.get_connection", return_value=mock_db_ctx):

        # First call: hits DB and sets cache
        pid1 = await get_project_id_by_api_key("cw_test_key_123")
        assert pid1 == 1
        mock_conn.fetchrow.assert_called_once()
        mock_redis.setex.assert_called_once_with("apikey:cw_test_key_123", 60, "1")

        # Second call: simulated cache hit
        mock_redis.get.return_value = "1"
        pid2 = await get_project_id_by_api_key("cw_test_key_123")
        assert pid2 == 1
        # DB fetchrow count should still be 1 (not called again)
        assert mock_conn.fetchrow.call_count == 1


@pytest.mark.asyncio
async def test_auth_verify_endpoint_success():
    """Test /v1/auth/verify returns 200 and project_id for valid key."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = "42"

    with patch("app.auth.get_redis", return_value=mock_redis):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/auth/verify", headers={"X-API-Key": "valid_key"}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["project_id"] == 42


@pytest.mark.asyncio
async def test_rls_sql_isolation_unit():
    """
    Unit test verifying that project_transaction invokes
    set_config('app.project_id', ...) inside a database transaction.
    """
    from unittest.mock import MagicMock

    mock_conn = AsyncMock()
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_pool_acquire = MagicMock()
    mock_pool_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_pool_acquire

    with patch("app.db.get_db_pool", return_value=mock_pool):
        async with project_transaction(99) as conn:
            assert conn == mock_conn

        mock_conn.execute.assert_called_once_with(
            "SELECT set_config('app.project_id', $1, true)", "99"
        )
