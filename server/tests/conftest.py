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
from app.cache import api_key_cache, deploy_lookup_cache, issue_lookup_cache, codeowners_cache


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



