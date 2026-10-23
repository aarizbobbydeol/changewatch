import json
import pytest
from app.sre_prep import (
    StackFrame,
    parse_stack_trace,
    innermost_project_frame,
    frame_in_diff,
    resolve_owners,
    classify_file,
)
from app.schemas import DeployCreate, DeployFileItem


SAMPLE_TRACEBACK = """Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/starlette/routing.py", line 68, in app
    await response(scope, receive, send)
  File "/app/server/main.py", line 42, in handle_request
    result = process_data(payload)
  File "/app/services/processor.py", line 108, in process_data
    return items[key]
KeyError: 'foo'
"""


def test_parse_stack_trace():
    frames = parse_stack_trace(SAMPLE_TRACEBACK)
    assert len(frames) == 3
    assert frames[0].file == "/usr/local/lib/python3.12/site-packages/starlette/routing.py"
    assert frames[0].line == 68
    assert frames[0].function == "app"
    assert frames[0].code == "await response(scope, receive, send)"

    assert frames[1].file == "/app/server/main.py"
    assert frames[1].line == 42
    assert frames[1].function == "handle_request"

    assert frames[2].file == "/app/services/processor.py"
    assert frames[2].line == 108
    assert frames[2].function == "process_data"
    assert frames[2].code == "return items[key]"


def test_innermost_project_frame():
    frames = parse_stack_trace(SAMPLE_TRACEBACK)
    innermost = innermost_project_frame(frames)
    assert innermost is not None
    # /app/services/processor.py line 108 is the deepest non-site-packages frame
    assert innermost.file == "/app/services/processor.py"
    assert innermost.line == 108
    assert innermost.function == "process_data"


def test_innermost_project_frame_skips_stdlib():
    trace = """Traceback (most recent call last):
  File "/app/views.py", line 15, in get_item
    return json.loads(val)
  File "/usr/lib/python3.12/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
"""
    frames = parse_stack_trace(trace)
    innermost = innermost_project_frame(frames)
    assert innermost is not None
    # Should skip stdlib json/__init__.py and pick views.py
    assert innermost.file == "/app/views.py"
    assert innermost.line == 15


def test_frame_in_diff_exact_and_tolerance():
    frame = StackFrame(file="services/processor.py", line=108, function="process_data")

    # Inside range [105, 115]
    deploy_files = [
        {"path": "services/processor.py", "added_ranges": [[105, 115]]}
    ]
    assert frame_in_diff(frame, deploy_files) is True

    # Boundary test: line 108 with range [111, 120] -> diff is 3 lines -> within +/- 3 lines tolerance
    deploy_files = [
        {"path": "services/processor.py", "added_ranges": [[111, 120]]}
    ]
    assert frame_in_diff(frame, deploy_files) is True

    # Boundary test: line 108 with range [100, 105] -> diff is 3 lines -> within +/- 3 lines tolerance
    deploy_files = [
        {"path": "services/processor.py", "added_ranges": [[100, 105]]}
    ]
    assert frame_in_diff(frame, deploy_files) is True

    # Outside tolerance: line 108 with range [112, 120] (diff 4 lines)
    deploy_files = [
        {"path": "services/processor.py", "added_ranges": [[112, 120]]}
    ]
    assert frame_in_diff(frame, deploy_files) is False

    # Different file
    deploy_files = [
        {"path": "services/other.py", "added_ranges": [[105, 115]]}
    ]
    assert frame_in_diff(frame, deploy_files) is False


def test_resolve_owners_codeowners_priority():
    codeowners = """
    * @core-team
    services/* @backend-team
    services/processor.py @sre-team @lead
    """
    owners = resolve_owners(
        path="services/processor.py",
        codeowners_content=codeowners,
        authors=["alice", "bob"],
    )
    # Last matching rule wins
    assert owners == ["@sre-team", "@lead"]

    # Partial match
    owners = resolve_owners(
        path="services/other.py",
        codeowners_content=codeowners,
        authors=["alice"],
    )
    assert owners == ["@backend-team"]


def test_resolve_owners_fallback_to_authors():
    owners = resolve_owners(
        path="unknown/path.py",
        codeowners_content="",
        authors=["alice", "bob"],
    )
    assert owners == ["alice", "bob"]

    owners_none = resolve_owners(
        path="unknown/path.py",
        codeowners_content=None,
        authors=None,
    )
    assert owners_none == []


def test_classify_file():
    # Dependencies
    assert classify_file("requirements.txt") == (True, False)
    assert classify_file("requirements-prod.txt") == (True, False)
    assert classify_file("pyproject.toml") == (True, True)  # both dep and toml cfg
    assert classify_file("uv.lock") == (True, False)
    assert classify_file("package.json") == (True, False)

    # Configs
    assert classify_file("Dockerfile") == (False, True)
    assert classify_file("Dockerfile.dev") == (False, True)
    assert classify_file("docker-compose.yml") == (False, True)
    assert classify_file("config/settings.yaml") == (False, True)
    assert classify_file(".env.example") == (False, True)
    assert classify_file("migrations/001_init.sql") == (False, True)
    assert classify_file("alembic/env.py") == (False, True)

    # Regular app code
    assert classify_file("app/main.py") == (False, False)
    assert classify_file("services/payment.py") == (False, False)


def test_deploy_create_schema_validation():
    payload = DeployCreate(
        commit_sha="abc1234567890",
        author="alice",
        message="feat: improve processor",
        diff="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n",
        codeowners="* @team",
        files=[
            DeployFileItem(
                path="services/processor.py",
                status="modified",
                added_ranges=[[10, 20]],
                authors=["alice", "bob"],
                content="def process(): pass",
            )
        ],
    )
    assert payload.commit_sha == "abc1234567890"
    assert payload.codeowners == "* @team"
    assert len(payload.files) == 1
    assert payload.files[0].path == "services/processor.py"
    assert payload.files[0].added_ranges == [[10, 20]]


@pytest.mark.asyncio
async def test_deploy_hook_with_files_and_codeowners():
    from unittest.mock import AsyncMock, MagicMock, patch
    import httpx
    from app.main import app

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 202}
    mock_conn.execute = AsyncMock()

    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    mock_pool_acquire = MagicMock()
    mock_pool_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool_acquire.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_pool_acquire

    mock_redis = AsyncMock()
    mock_redis.get.return_value = "1"  # project_id 1

    with patch("app.auth.get_redis", return_value=mock_redis), \
         patch("app.db.get_db_pool", return_value=mock_pool), \
         patch("app.auth.require_ingest_project_id", return_value=1):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/deploys",
                headers={"X-API-Key": "cw_live_key_test"},
                json={
                    "commit_sha": "deploy_sha_12345",
                    "author": "charlie",
                    "message": "fix: update db connector",
                    "diff": "--- a/db.py\n+++ b/db.py\n",
                    "codeowners": "* @core-team\ndb.py @data-team",
                    "files": [
                        {
                            "path": "db.py",
                            "status": "modified",
                            "added_ranges": [[5, 12]],
                            "authors": ["charlie"],
                            "content": "def connect(): return None",
                        },
                        {
                            "path": "requirements.txt",
                            "status": "modified",
                            "added_ranges": [[1, 2]],
                            "authors": ["charlie"],
                            "content": "asyncpg==0.29.0\n",
                        }
                    ],
                },
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["deploy_id"] == 202

    # Verify deploy row insert included codeowners
    insert_call = next(
        call for call in mock_conn.fetchrow.call_args_list
        if "INSERT INTO deploys" in call[0][0]
    )
    sql = insert_call[0][0]
    assert "codeowners" in sql
    assert "* @core-team\ndb.py @data-team" in insert_call[0]



    # Verify deploy_files inserts occurred
    assert mock_conn.execute.call_count >= 2
    execute_calls = mock_conn.execute.call_args_list
    # First file is db.py
    assert any("deploy_files" in str(call) and "db.py" in str(call) for call in execute_calls)
    # Second file is requirements.txt
    assert any("deploy_files" in str(call) and "requirements.txt" in str(call) for call in execute_calls)

