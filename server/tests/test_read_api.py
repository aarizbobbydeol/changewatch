import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from app.auth import hash_api_key
from app.cache import api_key_cache
from app.main import app


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


@pytest.fixture
def auth_keys():
    ingest_key = "cw_ingest_test_write_key_123"
    read_key = "cw_read_test_read_key_456"
    return {
        "ingest_key": ingest_key,
        "ingest_hash": hash_api_key(ingest_key),
        "read_key": read_key,
        "read_hash": hash_api_key(read_key),
    }


@pytest.mark.asyncio
async def test_ingest_key_receives_403_on_read_endpoints(auth_keys):
    """Ingest key must receive HTTP 403 Forbidden on all Read API endpoints."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"X-API-Key": auth_keys["ingest_key"]}

            # 1. /v1/verdict
            r1 = await client.get("/v1/verdict?sha=abc1234", headers=headers)
            assert r1.status_code == 403
            assert "Ingest key cannot be used to read data" in r1.json()["detail"]

            # 2. /api/deploys
            r2 = await client.get("/api/deploys", headers=headers)
            assert r2.status_code == 403
            assert "Ingest key cannot be used to read data" in r2.json()["detail"]

            # 3. /api/issues
            r3 = await client.get("/api/issues", headers=headers)
            assert r3.status_code == 403
            assert "Ingest key cannot be used to read data" in r3.json()["detail"]

            # 4. /api/issues/{id}
            r4 = await client.get("/api/issues/10", headers=headers)
            assert r4.status_code == 403
            assert "Ingest key cannot be used to read data" in r4.json()["detail"]

            # 5. /api/timeline
            r5 = await client.get("/api/timeline", headers=headers)
            assert r5.status_code == 403
            assert "Ingest key cannot be used to read data" in r5.json()["detail"]


@pytest.mark.asyncio
async def test_missing_and_invalid_api_key_returns_401():
    """Missing or invalid API key returns 401 Unauthorized."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Missing header
            r1 = await client.get("/v1/verdict?sha=abc1234")
            assert r1.status_code == 401
            assert "Missing X-API-Key header" in r1.json()["detail"]

            # Invalid key
            r2 = await client.get("/api/deploys", headers={"X-API-Key": "invalid_key"})
            assert r2.status_code == 401
            assert "Invalid API key" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_read_verdict_success(auth_keys):
    """Read key can fetch verdict and deploy analysis for a commit SHA."""
    now_dt = datetime.now(timezone.utc)
    mock_conn = AsyncMock()

    # 1st fetchrow: Auth check
    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    # 2nd fetchrow: Verdict query
    verdict_row = {
        "deploy_id": 42,
        "commit_sha": "abc1234",
        "environment": "prod",
        "author": "Alice",
        "message": "feat: critical checkout service",
        "deployed_at": now_dt,
        "verdict": "safe",
        "verdict_reasons": json.dumps(["traffic observed, error rates normal"]),
        "verdict_final": True,
        "verdict_updated_at": now_dt,
        "likely_cause": None,
        "new_issues": 0,
        "errors_before": 2,
        "errors_after": 1,
        "flagged": False,
    }
    mock_conn.fetchrow.side_effect = [auth_row, verdict_row]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/verdict?sha=abc1234&environment=prod",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["deploy_id"] == 42
            assert data["commit_sha"] == "abc1234"
            assert data["environment"] == "prod"
            assert data["author"] == "Alice"
            assert data["verdict"] == "safe"
            assert data["verdict_final"] is True
            assert "traffic observed" in data["verdict_reasons"][0]
            assert data["new_issues"] == 0


@pytest.mark.asyncio
async def test_read_verdict_not_found(auth_keys):
    """Unknown commit SHA returns HTTP 404 Not Found."""
    mock_conn = AsyncMock()
    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_conn.fetchrow.side_effect = [auth_row, None]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/verdict?sha=nonexistent_sha",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 404
            assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_list_deploys(auth_keys):
    """Read key can list deploys for authenticated project."""
    now_dt = datetime.now(timezone.utc)
    mock_conn = AsyncMock()

    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_conn.fetchrow.return_value = auth_row

    deploy_rows = [
        {
            "id": 10,
            "commit_sha": "sha_10",
            "author": "Alice",
            "message": "fix: auth bug",
            "diff": "+ fixed",
            "environment": "prod",
            "deployed_at": now_dt,
            "verdict": "rollback",
            "verdict_reasons": ["20 new issues appeared"],
            "verdict_final": True,
            "verdict_updated_at": now_dt,
            "likely_cause": "KeyError in auth.py",
            "new_issues": 3,
            "errors_before": 0,
            "errors_after": 25,
            "flagged": True,
        },
        {
            "id": 9,
            "commit_sha": "sha_9",
            "author": "Bob",
            "message": "feat: UI",
            "diff": "+ ui",
            "environment": "prod",
            "deployed_at": now_dt - timedelta(hours=1),
            "verdict": "safe",
            "verdict_reasons": [],
            "verdict_final": True,
            "verdict_updated_at": now_dt - timedelta(hours=1),
            "likely_cause": None,
            "new_issues": 0,
            "errors_before": 0,
            "errors_after": 0,
            "flagged": False,
        },
    ]
    mock_conn.fetch.return_value = deploy_rows
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/deploys?limit=20",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 2
            assert data[0]["id"] == 10
            assert data[0]["verdict"] == "rollback"
            assert data[0]["likely_cause"] == "KeyError in auth.py"
            assert data[1]["id"] == 9
            assert data[1]["verdict"] == "safe"


@pytest.mark.asyncio
async def test_list_issues_and_issue_details(auth_keys):
    """Read key can list issues and fetch issue details with event trace."""
    now_dt = datetime.now(timezone.utc)
    mock_conn = AsyncMock()

    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    # List issues
    mock_conn.fetchrow.return_value = auth_row
    mock_conn.fetch.return_value = [
        {
            "id": 100,
            "fingerprint": "fp_hash_100",
            "title": "ZeroDivisionError: integer division or modulo by zero",
            "first_seen": now_dt - timedelta(hours=2),
            "last_seen": now_dt,
            "status": "open",
            "event_count": 15,
        }
    ]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/issues?limit=10",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 200
            issues = resp.json()
            assert len(issues) == 1
            assert issues[0]["id"] == 100
            assert issues[0]["event_count"] == 15
            assert "ZeroDivisionError" in issues[0]["title"]

    # Issue details by ID
    issue_row = {
        "id": 100,
        "fingerprint": "fp_hash_100",
        "title": "ZeroDivisionError: division by zero",
        "first_seen": now_dt - timedelta(hours=2),
        "last_seen": now_dt,
        "status": "open",
        "event_count": 15,
    }
    event_rows = [
        {
            "id": 5001,
            "occurred_at": now_dt,
            "stack_trace": "Traceback...\nZeroDivisionError: division by zero",
            "request_path": "/crash",
            "duration_ms": 12.5,
            "extra": {"user_id": 42},
            "deploy_id": 42,
            "commit_sha": "abc1234",
        }
    ]
    api_key_cache.clear()
    mock_conn.fetchrow.side_effect = [auth_row, issue_row]
    mock_conn.fetch.side_effect = [event_rows]

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/issues/100",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 200
            detail = resp.json()
            assert detail["id"] == 100
            assert len(detail["events"]) == 1
            assert detail["events"][0]["request_path"] == "/crash"
            assert detail["events"][0]["commit_sha"] == "abc1234"


@pytest.mark.asyncio
async def test_tenant_isolation_rls(auth_keys):
    """
    Tenant Isolation Verification:
    Project A read key CANNOT read Project B's issue or deploys.
    Returns 404 when attempting to access Project B's issue or deploy.
    """
    proj_a_read_key = "cw_read_proj_a_secret_key_111"
    proj_a_hash = hash_api_key(proj_a_read_key)

    mock_conn = AsyncMock()

    # Auth returns project_id = 1 (Project A)
    auth_row = {
        "id": 1,
        "ingest_key_hash": hash_api_key("cw_ingest_proj_a"),
        "read_key_hash": proj_a_hash,
        "api_key": None,
    }
    # When querying issue 999 (which belongs to Project B), RLS / project_id filter returns None
    mock_conn.fetchrow.side_effect = [auth_row, None]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Project A tries to view Project B's issue -> 404
            resp = await client.get(
                "/api/issues/999",
                headers={"X-API-Key": proj_a_read_key},
            )
            assert resp.status_code == 404
            assert "Issue 999 not found" in resp.json()["detail"]

    # Project A tries to view Project B's commit sha -> 404
    api_key_cache.clear()
    mock_conn.fetchrow.side_effect = [auth_row, None]
    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp_verdict = await client.get(
                "/v1/verdict?sha=sha_of_project_b",
                headers={"X-API-Key": proj_a_read_key},
            )
            assert resp_verdict.status_code == 404
            assert "not found for this project" in resp_verdict.json()["detail"]


@pytest.mark.asyncio
async def test_timeline_endpoint(auth_keys):
    """Test /api/timeline aggregates traffic heartbeats and deploys into 5-minute buckets."""
    raw_now = datetime.now(timezone.utc)
    # Align to 5-minute boundary so test expectations match bucket keys
    now_dt = raw_now.replace(minute=(raw_now.minute // 5) * 5, second=0, microsecond=0)
    b1 = now_dt - timedelta(minutes=10)
    b2 = now_dt - timedelta(minutes=5)


    mock_conn = AsyncMock()
    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_conn.fetchrow.return_value = auth_row

    traffic_rows = [
        {"bucket": b1, "requests": 50, "errors": 1},
        {"bucket": b1 + timedelta(minutes=1), "requests": 60, "errors": 0},
        {"bucket": b2, "requests": 40, "errors": 5},
    ]
    event_rows = [
        {"occurred_at": b2 + timedelta(minutes=2)},
    ]
    deploy_rows = [
        {
            "id": 1,
            "commit_sha": "abc1234",
            "environment": "prod",
            "deployed_at": b1,
            "verdict": "safe",
        }
    ]

    mock_conn.fetch.side_effect = [traffic_rows, event_rows, deploy_rows]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1. Default list format
            resp = await client.get(
                "/api/timeline?hours=24",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) >= 2

            # Bucket b1 should have aggregated requests = 110, errors = 1, and 1 deploy
            bucket_b1 = next(item for item in data if item["bucket"].startswith(b1.strftime("%Y-%m-%dT%H:%M")))
            assert bucket_b1["requests"] == 110
            assert bucket_b1["errors"] == 1
            assert len(bucket_b1["deploys"]) == 1
            assert bucket_b1["deploys"][0]["commit_sha"] == "abc1234"

            # 2. Dict format
            mock_conn.fetchrow.return_value = auth_row
            mock_conn.fetch.side_effect = [traffic_rows, event_rows, deploy_rows]
            resp_dict = await client.get(
                "/api/timeline?hours=24&format=dict",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp_dict.status_code == 200
            dict_data = resp_dict.json()
            assert "timeline" in dict_data
            assert "deploys" in dict_data
            assert len(dict_data["deploys"]) == 1


@pytest.mark.asyncio
async def test_empty_timeline_returns_empty_collections(auth_keys):
    """When no traffic, events, or deploys exist, timeline returns empty list or dict."""
    mock_conn = AsyncMock()
    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_conn.fetchrow.return_value = auth_row
    mock_conn.fetch.side_effect = [[], [], []]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/timeline?hours=6",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 200
            assert resp.json() == []

            mock_conn.fetchrow.return_value = auth_row
            mock_conn.fetch.side_effect = [[], [], []]
            resp_dict = await client.get(
                "/api/timeline?hours=6&format=dict",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp_dict.status_code == 200
            data = resp_dict.json()
            assert data["timeline"] == []
            assert data["deploys"] == []


@pytest.mark.asyncio
async def test_invalid_query_parameters(auth_keys):
    """Validation bounds reject out-of-range parameters with HTTP 422."""
    mock_conn = AsyncMock()
    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_conn.fetchrow.return_value = auth_row
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # deploys limit < 1
            r1 = await client.get(
                "/api/deploys?limit=0",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert r1.status_code == 422

            # deploys limit > 200
            r2 = await client.get(
                "/api/deploys?limit=500",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert r2.status_code == 422

            # timeline hours > 168 (7 days max)
            r3 = await client.get(
                "/api/timeline?hours=300",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert r3.status_code == 422

            # non-integer issue ID
            r4 = await client.get(
                "/api/issues/not-a-number",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert r4.status_code == 422

            # invalid timeline format
            r5 = await client.get(
                "/api/timeline?format=invalid_format",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert r5.status_code == 422


@pytest.mark.asyncio
async def test_cors_preflight_and_headers():
    """CORS middleware handles OPTIONS preflight and attaches CORS headers."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Preflight OPTIONS request
        resp = await client.options(
            "/api/deploys",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert resp.headers.get("access-control-allow-credentials") == "true"
        assert "GET" in resp.headers.get("access-control-allow-methods", "")


@pytest.mark.asyncio
async def test_verdict_pending_and_unanalyzed_defaults_to_no_data(auth_keys):
    """Deploy without analysis row or with 'pending' verdict maps verdict to 'no_data'."""
    now_dt = datetime.now(timezone.utc)
    mock_conn = AsyncMock()

    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }

    # Case 1: Deploy exists, but deploy_analysis row is None
    unanalyzed_row = {
        "deploy_id": 99,
        "commit_sha": "def5678",
        "environment": "prod",
        "author": "Charlie",
        "message": "fix: pipeline",
        "deployed_at": now_dt,
        "verdict": None,
        "verdict_reasons": None,
        "verdict_final": None,
        "verdict_updated_at": None,
        "likely_cause": None,
        "new_issues": None,
        "errors_before": None,
        "errors_after": None,
        "flagged": None,
    }
    mock_conn.fetchrow.side_effect = [auth_row, unanalyzed_row]
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/verdict?sha=def5678&environment=prod",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["verdict"] == "no_data"
            assert data["verdict_reasons"] == []
            assert data["verdict_final"] is False
            assert data["new_issues"] == 0

    # Case 2: Deploy has analysis row with verdict = 'pending' (DB column default)
    pending_row = {
        "deploy_id": 100,
        "commit_sha": "ghi9012",
        "environment": "prod",
        "author": "Dave",
        "message": "feat: analytics",
        "deployed_at": now_dt,
        "verdict": "pending",
        "verdict_reasons": json.dumps([]),
        "verdict_final": False,
        "verdict_updated_at": None,
        "likely_cause": None,
        "new_issues": 0,
        "errors_before": 0,
        "errors_after": 0,
        "flagged": False,
    }
    api_key_cache.clear()
    mock_conn.fetchrow.side_effect = [auth_row, pending_row]
    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/verdict?sha=ghi9012&environment=prod",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert resp.status_code == 200
            assert resp.json()["verdict"] == "no_data"


@pytest.mark.asyncio
async def test_read_endpoints_filtering_and_rls_sql(auth_keys):
    """Test filtering parameters and verification that RLS context is set on queries."""
    mock_conn = AsyncMock()
    auth_row = {
        "id": 1,
        "ingest_key_hash": auth_keys["ingest_hash"],
        "read_key_hash": auth_keys["read_hash"],
        "api_key": None,
    }
    mock_conn.fetchrow.return_value = auth_row
    mock_conn.fetch.return_value = []
    mock_pool = build_mock_pool(mock_conn)

    with patch("app.auth.get_redis", return_value=None), \
         patch("app.db.get_db_pool", return_value=mock_pool):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1. /api/deploys with environment filter
            r1 = await client.get(
                "/api/deploys?limit=10&environment=staging",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert r1.status_code == 200
            # Confirm RLS set_config was called with project_id = 1
            mock_conn.execute.assert_called_with("SELECT set_config('app.project_id', $1, true)", "1")
            # Confirm fetch query received project_id 1 and 'staging' filter
            fetch_call_args = mock_conn.fetch.call_args[0]
            assert fetch_call_args[1] == 1  # project_id
            assert fetch_call_args[2] == "staging"  # environment
            assert fetch_call_args[3] == 10  # limit

            # 2. /api/issues with status filter
            mock_conn.fetchrow.return_value = auth_row
            mock_conn.fetch.return_value = []
            r2 = await client.get(
                "/api/issues?limit=25&status=resolved",
                headers={"X-API-Key": auth_keys["read_key"]},
            )
            assert r2.status_code == 200
            issue_call_args = mock_conn.fetch.call_args[0]
            assert issue_call_args[1] == 1  # project_id
            assert issue_call_args[2] == "resolved"  # status
            assert issue_call_args[3] == 25  # limit


