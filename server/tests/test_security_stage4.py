"""Security Addendum Stage 4 Tests: Read-only guarantees.

Verifies:
1. ReadOnlyGitHubClient only implements GET; any mutating methods raise ReadOnlyViolationError.
2. Codebase-wide static scan: no mutating HTTP verbs (POST, PUT, DELETE, PATCH) target GitHub.
3. Agent analysis and tool queries stay SELECT-only.
4. Action workflows enforce contents: read, persist-credentials: false, continue-on-error: true, and 10s timeout.
"""
from __future__ import annotations

import ast
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.github_client import ReadOnlyGitHubClient, ReadOnlyViolationError


@pytest.mark.asyncio
async def test_github_client_get_async():
    """Verify async GET requests succeed with appropriate headers."""
    client = ReadOnlyGitHubClient(token="ghp_test1234567890")
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"sha": "abc1234", "message": "initial commit"}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await client.get("/repos/acme/project/commits/abc1234")

        assert res["sha"] == "abc1234"
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert args[0] == "https://api.github.com/repos/acme/project/commits/abc1234"
        assert kwargs["headers"]["Authorization"] == "Bearer ghp_test1234567890"
        assert kwargs["headers"]["Accept"] == "application/vnd.github.v3+json"


def test_github_client_get_sync():
    """Verify sync GET requests succeed with appropriate headers."""
    client = ReadOnlyGitHubClient(token="ghp_sync_token")
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"name": "test-repo"}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.Client.get") as mock_get:
        mock_get.return_value = mock_resp
        res = client.get_sync("/repos/acme/test-repo")

        assert res["name"] == "test-repo"
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert args[0] == "https://api.github.com/repos/acme/test-repo"
        assert kwargs["headers"]["Authorization"] == "Bearer ghp_sync_token"


def test_github_client_strictly_blocks_mutating_methods():
    """Verify calling POST, PUT, DELETE, or PATCH raises ReadOnlyViolationError."""
    client = ReadOnlyGitHubClient(token="ghp_test")

    with pytest.raises(ReadOnlyViolationError) as exc_post:
        client.post("repos/acme/test/issues", data={"title": "test"})
    assert "strictly prohibited" in str(exc_post.value)

    with pytest.raises(ReadOnlyViolationError) as exc_put:
        client.put("repos/acme/test/contents/foo.txt", data={"content": "x"})
    assert "strictly prohibited" in str(exc_put.value)

    with pytest.raises(ReadOnlyViolationError) as exc_delete:
        client.delete("repos/acme/test/releases/1")
    assert "strictly prohibited" in str(exc_delete.value)

    with pytest.raises(ReadOnlyViolationError) as exc_patch:
        client.patch("repos/acme/test/pulls/1", data={"state": "closed"})
    assert "strictly prohibited" in str(exc_patch.value)

    for prohibited_verb in ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]:
        with pytest.raises(ReadOnlyViolationError):
            client._assert_read_only(prohibited_verb)


@pytest.mark.asyncio
async def test_github_client_helper_methods():
    """Verify helper methods use GET with expected Accept headers."""
    client = ReadOnlyGitHubClient(token="ghp_test")

    # get_diff
    mock_resp_diff = MagicMock()
    mock_resp_diff.headers = {"content-type": "text/plain"}
    mock_resp_diff.text = "diff --git a/file.py b/file.py"
    mock_resp_diff.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp_diff
        diff = await client.get_diff("acme", "proj", "base1", "head2")
        assert "diff --git" in diff
        assert mock_get.call_args[1]["headers"]["Accept"] == "application/vnd.github.v3.diff"

    # get_file_content
    mock_resp_file = MagicMock()
    mock_resp_file.headers = {"content-type": "text/plain"}
    mock_resp_file.text = "print('hello world')"
    mock_resp_file.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp_file
        content = await client.get_file_content("acme", "proj", "main.py", ref="v1.0")
        assert "print('hello world')" in content
        assert mock_get.call_args[1]["headers"]["Accept"] == "application/vnd.github.v3.raw"
        assert mock_get.call_args[1]["params"] == {"ref": "v1.0"}


def test_ast_static_scan_no_github_mutating_verbs():
    """Assert across all Python files that no mutating requests target GitHub domains."""
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    py_files = list(repo_root.glob("server/**/*.py")) + list(repo_root.glob("sdk/**/*.py")) + list(repo_root.glob("scripts/**/*.py"))

    mutating_methods = {"post", "put", "delete", "patch"}
    github_indicators = {"github.com", "api.github.com"}

    violations = []

    for file_path in py_files:
        if file_path.name == "test_security_stage4.py":
            continue
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                method_name = node.func.attr.lower()
                if method_name in mutating_methods:
                    # Check string arguments for github URLs
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if any(gh in arg.value.lower() for gh in github_indicators):
                                violations.append(f"{file_path}:{node.lineno} calls {method_name} with GitHub URL '{arg.value}'")

    assert not violations, f"Prohibited GitHub mutating calls found: {violations}"


def test_ast_static_scan_agent_analysis_sql_is_select_only():
    """Verify that analysis SQL queries are strictly SELECT-only (read-only)."""
    analysis_file = Path(__file__).resolve().parent.parent / "app" / "analysis.py"
    assert analysis_file.exists()
    content = analysis_file.read_text(encoding="utf-8")

    tree = ast.parse(content)
    mutating_sql_keywords = ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE ", "ALTER "]

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val_upper = node.value.strip().upper()
            if "SELECT " in val_upper:
                for forbidden in mutating_sql_keywords:
                    assert forbidden not in val_upper, (
                        f"Analysis SQL contains mutating statement '{forbidden}': {node.value}"
                    )


def test_action_workflow_lint():
    """Verify action/changewatch-deploy.yml conforms to customer security requirements."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    workflow_path = repo_root / "action" / "changewatch-deploy.yml"
    assert workflow_path.exists(), f"Workflow file {workflow_path} must exist"

    text = workflow_path.read_text(encoding="utf-8")

    # 1. Must enforce contents: read
    assert "contents: read" in text, "Workflow must declare 'permissions: contents: read'"
    assert "contents: write" not in text, "Workflow must NEVER declare write permissions"

    # 2. Must use persist-credentials: false
    assert "persist-credentials: false" in text, "Checkout must specify 'persist-credentials: false'"

    # 3. Must be non-blocking with continue-on-error: true
    assert "continue-on-error: true" in text, "Deploy notification step must specify 'continue-on-error: true'"

    # 4. Must never run git commit or git push
    assert "git commit" not in text, "Action must never run git commit"
    assert "git push" not in text, "Action must never run git push"

    # 5. Must enforce 10s timeout
    assert "--max-time 10" in text, "Curl request must include '--max-time 10'"


def test_composite_action_lint():
    """Verify action/action.yml conforms to customer security requirements."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    action_path = repo_root / "action" / "action.yml"
    assert action_path.exists(), f"Action file {action_path} must exist"

    text = action_path.read_text(encoding="utf-8")

    assert "continue-on-error: true" in text
    assert "git commit" not in text
    assert "git push" not in text
    assert "--max-time 10" in text
