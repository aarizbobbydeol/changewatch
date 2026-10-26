"""ChangeWatch Read-Only GitHub Client.

Hard guarantee: ChangeWatch NEVER writes to, modifies, commits to, or comments on
customer repositories. This client strictly permits HTTP GET requests. Any invocation
or attempt to use POST, PUT, DELETE, PATCH, or other mutating HTTP verbs will immediately
raise ReadOnlyViolationError.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import quote
import httpx

logger = logging.getLogger(__name__)


class ReadOnlyViolationError(RuntimeError):
    """Raised whenever a non-GET or mutating request is attempted against GitHub."""
    pass


_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")


def _repo(owner: str, repo: str) -> str:
    """'repos/owner/repo', refusing names a URL would resolve out of it ('.' or '..')."""
    for name in (owner, repo):
        if not _NAME.match(name or "") or name in (".", ".."):
            raise ValueError("GitHub owner and repository must be plain names")
    return f"/repos/{owner}/{repo}"


def _file_path(path: str) -> str:
    """A repository file path, each part percent-encoded. A '.' or '..' part is refused: the URL
    would otherwise resolve to another repository's file (a stack-trace path is sent by the app)."""
    parts = (path or "").replace("\\", "/").strip("/").split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ValueError("Not a plain path inside the repository")
    return "/".join(quote(p, safe="") for p in parts)


class ReadOnlyGitHubClient:
    """Strictly read-only client for interacting with the GitHub API."""

    def __init__(
        self,
        token: Optional[str] = None,
        base_url: str = "https://api.github.com",
        timeout: float = 10.0,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self, custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ChangeWatch-ReadOnly-Client/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def _assert_read_only(self, method: str) -> None:
        if method.upper() != "GET":
            raise ReadOnlyViolationError(
                f"Security violation: Method '{method}' is strictly prohibited. "
                "ChangeWatch guarantees read-only access to customer code and repositories."
            )

    async def get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Execute a read-only GET request."""
        self._assert_read_only("GET")
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=self._headers(headers))
            resp.raise_for_status()
            if "application/json" in resp.headers.get("content-type", ""):
                return resp.json()
            return resp.text

    def get_sync(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Execute a synchronous read-only GET request."""
        self._assert_read_only("GET")
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, params=params, headers=self._headers(headers))
            resp.raise_for_status()
            if "application/json" in resp.headers.get("content-type", ""):
                return resp.json()
            return resp.text

    # Explicit prohibited mutating methods that immediately raise ReadOnlyViolationError
    def post(self, *args: Any, **kwargs: Any) -> None:
        raise ReadOnlyViolationError("POST is strictly prohibited on GitHub client (read-only guarantee)")

    def put(self, *args: Any, **kwargs: Any) -> None:
        raise ReadOnlyViolationError("PUT is strictly prohibited on GitHub client (read-only guarantee)")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise ReadOnlyViolationError("DELETE is strictly prohibited on GitHub client (read-only guarantee)")

    def patch(self, *args: Any, **kwargs: Any) -> None:
        raise ReadOnlyViolationError("PATCH is strictly prohibited on GitHub client (read-only guarantee)")

    async def get_commit(self, owner: str, repo: str, sha: str) -> Dict[str, Any]:
        """Fetch commit details (read-only)."""
        return await self.get(f"{_repo(owner, repo)}/commits/{quote(sha, safe='')}")

    async def get_diff(self, owner: str, repo: str, base: str, head: str) -> str:
        """Fetch comparison diff between base and head (read-only)."""
        headers = {"Accept": "application/vnd.github.v3.diff"}
        res = await self.get(f"{_repo(owner, repo)}/compare/{quote(base, safe='')}...{quote(head, safe='')}", headers=headers)
        return str(res)

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str = "main") -> str:
        """Fetch raw file content from a repository (read-only)."""
        headers = {"Accept": "application/vnd.github.v3.raw"}
        if not _REF.match(ref or "") or ".." in ref:
            raise ValueError("Not a plain git ref")
        res = await self.get(f"{_repo(owner, repo)}/contents/{_file_path(path)}", params={"ref": ref}, headers=headers)
        return str(res)
