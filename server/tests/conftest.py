import sys
from pathlib import Path

# Add server and sdk directories to sys.path
server_dir = Path(__file__).resolve().parent.parent
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

root_dir = server_dir.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

sdk_dir = root_dir / "sdk"
if str(sdk_dir) not in sys.path:
    sys.path.insert(0, str(sdk_dir))


import pytest
from unittest.mock import DEFAULT
from app.cache import api_key_cache, deploy_lookup_cache, issue_lookup_cache, codeowners_cache


@pytest.fixture(autouse=True)
def reset_tenant_role_flag():
    """detect_tenant_role sets a process-wide flag; keep it from leaking between tests."""
    import app.db

    app.db._switch_to_tenant_role = False
    app.db._tenant_role_pending = False
    yield
    app.db._switch_to_tenant_role = False
    app.db._tenant_role_pending = False


@pytest.fixture(autouse=True)
def reset_in_memory_caches():
    api_key_cache.clear()
    deploy_lookup_cache.clear()
    issue_lookup_cache.clear()
    codeowners_cache.clear()
    yield
    api_key_cache.clear()
    deploy_lookup_cache.clear()
    issue_lookup_cache.clear()
    codeowners_cache.clear()





@pytest.fixture
def accept_event_claims():
    """Make a mocked connection treat every event_id as new in worker.claim_new_events.

    Other fetch calls keep returning the mock's return_value.
    """
    def _apply(conn):
        def _fetch(query, *args, **kwargs):
            if "INSERT INTO ingest_dedupe" in query:
                return [{"event_id": eid} for eid in args[1]]
            return DEFAULT

        conn.fetch.side_effect = _fetch
        return conn

    return _apply
