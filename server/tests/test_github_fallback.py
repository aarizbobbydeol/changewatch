"""Ephemeral mode: the investigator reads a changed file from GitHub (read-only) at the deploy's commit."""
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.github_client import ReadOnlyGitHubClient
from app.investigator.tools import get_file_at_commit


@pytest.mark.asyncio
async def test_file_is_read_from_github_at_the_deploy_commit_and_scrubbed(monkeypatch):
    monkeypatch.setattr(settings, "GITHUB_READ_TOKEN", "ghs_test_token")
    conn = AsyncMock()

    async def fetchrow(query, *args):
        if "FROM deploy_files" in query:
            return None  # ephemeral mode: no stored content
        if "FROM deploys" in query:
            return {"commit_sha": "abc123", "repository": "acme/shop"}
        return None

    conn.fetchrow.side_effect = fetchrow
    remote = "line 1\npassword=hunter22\nline 3\n"
    with patch.object(ReadOnlyGitHubClient, "get_file_content", new=AsyncMock(return_value=remote)) as get_file:
        result = await get_file_at_commit(conn, project_id=1, deploy_id=9, path="app/billing.py", start_line=1, end_line=3)

    get_file.assert_awaited_once_with("acme", "shop", "app/billing.py", ref="abc123")
    assert result["available"] is True
    assert "hunter22" not in result["content"]


def test_deploy_repository_must_look_like_owner_slash_name():
    from pydantic import ValidationError

    from app.schemas import DeployCreate

    assert DeployCreate(commit_sha="a", repository="acme/shop").repository == "acme/shop"
    for bad in ("acme", "acme/shop/x", "../../etc", "acme/shop?x=1"):
        with pytest.raises(ValidationError):
            DeployCreate(commit_sha="a", repository=bad)
