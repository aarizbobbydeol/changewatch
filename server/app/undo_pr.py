"""Draft pull requests that undo a deploy (opt-in, off by default).

The one place ChangeWatch writes to a customer's repository, and only when the team turned it on
for the project with a GitHub token of its own. It then does exactly this, through the GitHub API
(no clone, no push): build a commit on top of the default branch that puts the files the deploy
changed back as they were before it, put that commit on a new branch under `changewatch/`, and
open a DRAFT pull request. It never pushes to an existing branch, never merges, closes, comments or
deletes anything: UndoPullRequestClient refuses any other write. A person reviews and merges.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import httpx

from app.crypto import decrypt_or_plain, encrypt_or_keep
from app.github_client import ReadOnlyViolationError

logger = logging.getLogger("changewatch.undo_pr")

BRANCH_PREFIX = "changewatch/"
MAX_FILES = 100  # a revert this large is for `git revert`, not a generated pull request
# Tests swap in httpx.MockTransport (a stand-in GitHub); None is the real network
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

# owner/name as GitHub allows them; never "." or "..", which a URL would resolve out of repos/
_NAME = r"(?!\.\.?(?:/|$))[A-Za-z0-9_.-]+"
_REPO = rf"repos/{_NAME}/{_NAME}"
_TOKEN = re.compile(r"^[A-Za-z0-9_\-.]{20,255}$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")


class UndoPRError(Exception):
    """A refusal to report to the person who asked, with the HTTP status to answer with."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def check_write(path: str, body: Dict[str, Any]) -> None:
    """The only writes allowed: a tree, a commit, a branch under changewatch/, a draft pull request.
    Anything else is a ReadOnlyViolationError, whatever called it."""
    if re.fullmatch(rf"{_REPO}/git/(?:trees|commits)", path):
        return
    if re.fullmatch(rf"{_REPO}/git/refs", path) and str(body.get("ref", "")).startswith(f"refs/heads/{BRANCH_PREFIX}"):
        return
    if re.fullmatch(rf"{_REPO}/pulls", path) and body.get("draft") is True:
        return
    raise ReadOnlyViolationError(f"Write refused: ChangeWatch may only create a draft revert pull request (not {path})")


class UndoPullRequestClient:
    """GitHub API client for draft revert pull requests: GET, and POST of the writes check_write allows.
    There is no put, patch or delete."""

    def __init__(self, token: str, base_url: str, timeout: float = 15.0):
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ChangeWatch-Undo-PR/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _send(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                    body: Optional[Dict[str, Any]] = None) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=TRANSPORT, follow_redirects=False) as client:
                res = await client.request(method, f"{self.base_url}/{path}", params=params, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise UndoPRError(502, f"Could not reach GitHub ({type(exc).__name__}).") from None
        if res.status_code >= 400:
            try:
                detail = str(res.json().get("message") or "")[:200]
            except ValueError:
                detail = ""
            if res.status_code in (401, 403):
                raise UndoPRError(502, "GitHub refused the token: it needs Contents and Pull requests write access "
                                       f"to this repository{f' ({detail})' if detail else ''}.")
            if res.status_code == 404:
                raise UndoPRError(502, "GitHub cannot find the repository or commit with this token.")
            raise UndoPRError(502, f"GitHub answered HTTP {res.status_code}{f': {detail}' if detail else ''}.")
        return res.json()

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._send("GET", path, params=params)

    async def post(self, path: str, body: Dict[str, Any]) -> Any:
        check_write(path, body)
        return await self._send("POST", path, body=body)


async def is_enabled(conn: asyncpg.Connection, project_id: int) -> bool:
    return bool(await conn.fetchval("SELECT undo_pr_token_enc IS NOT NULL FROM projects WHERE id = $1", project_id))


async def set_token(conn: asyncpg.Connection, project_id: int, token: Optional[str]) -> bool:
    """Turn draft revert pull requests on (with the team's GitHub token, stored encrypted only) or
    off (None). Returns whether they are on."""
    if token is None:
        await conn.execute("UPDATE projects SET undo_pr_token_enc = NULL WHERE id = $1", project_id)
        return False
    token = token.strip()
    if not _TOKEN.match(token):
        raise UndoPRError(422, "That does not look like a GitHub token (letters, digits, - _ . only, 20 to 255 characters).")
    _, token_enc = await encrypt_or_keep(project_id, "projects", "undo_pr_token", token)
    if not token_enc:
        raise UndoPRError(503, "The token could not be stored encrypted, so it was not stored.")
    await conn.execute("UPDATE projects SET undo_pr_token_enc = $1 WHERE id = $2", token_enc, project_id)
    return True


async def opened(conn: asyncpg.Connection, project_id: int, incident_id: int) -> List[Dict[str, Any]]:
    """The draft pull requests already opened for an incident."""
    import json

    rows = await conn.fetch(
        "SELECT detail FROM incident_events WHERE project_id = $1 AND incident_id = $2 AND type = 'undo_pr' ORDER BY at",
        project_id, incident_id,
    )
    out = []
    for r in rows:
        detail = r["detail"] if isinstance(r["detail"], dict) else json.loads(r["detail"] or "{}")
        out.append({k: detail.get(k) for k in ("scope", "url", "number", "branch")})
    return out


def _entries(files: List[Dict[str, Any]], base_tree: Dict[str, Tuple[str, str, str]], only: Optional[str]) -> List[Dict[str, Any]]:
    """Tree entries that put each changed file back as it was at the base commit."""
    entries: List[Dict[str, Any]] = []

    def restore(path: str) -> None:
        if path not in base_tree:
            raise UndoPRError(422, f"{path} is not in the commit before the deploy, so it cannot be put back.")
        mode, kind, sha = base_tree[path]
        entries.append({"path": path, "mode": mode, "type": kind, "sha": sha})

    def remove(path: str) -> None:
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    for f in files:
        path, status = f.get("filename") or "", f.get("status")
        if only is not None and path != only:
            continue
        if only is not None and status not in ("modified", "changed"):
            raise UndoPRError(422, f"{path} was not just modified by the deploy (it was {status}): undo the whole deploy instead.")
        if status in ("modified", "changed", "removed"):
            restore(path)
        elif status in ("added", "copied"):
            remove(path)
        elif status == "renamed":
            remove(path)
            restore(f.get("previous_filename") or "")
    if only is not None and not entries:
        raise UndoPRError(422, f"The deploy did not change {only}.")
    return entries


async def open_undo_pr(conn: asyncpg.Connection, project_id: int, incident_id: int, scope: str,
                       base_url: str, dashboard_link: str) -> Dict[str, Any]:
    """Open (or return the already opened) draft pull request that undoes the incident's deploy
    ("deploy") or only its change to the failing file ("file")."""
    from app.fixes import deploy_undo, fix_suggestions

    # One at a time per incident, so two clicks do not open two pull requests
    await conn.execute("SELECT pg_advisory_xact_lock(720116, $1)", incident_id)
    for pr in await opened(conn, project_id, incident_id):
        if pr["scope"] == scope:
            return {**pr, "already_open": True}

    row = await conn.fetchrow(
        """
        SELECT i.deploy_id, i.title, d.commit_sha, d.repository, d.environment, p.undo_pr_token_enc
        FROM incidents i JOIN deploys d ON d.id = i.deploy_id JOIN projects p ON p.id = i.project_id
        WHERE i.id = $1 AND i.project_id = $2
        """,
        incident_id, project_id,
    )
    if not row:
        raise UndoPRError(404, "Incident not found")
    if not row["undo_pr_token_enc"]:
        raise UndoPRError(409, "Draft revert pull requests are off for this project (turn them on in Settings).")
    repo = row["repository"]
    if not repo or not re.fullmatch(rf"{_NAME}/{_NAME}", repo):
        raise UndoPRError(422, "The deploy did not say which repository it came from (the GitHub Action sends it).")
    sha = (row["commit_sha"] or "").strip().lower()
    if not _SHA.match(sha):
        raise UndoPRError(422, "The deploy's commit is not a git SHA.")
    undo = await deploy_undo(conn, project_id, row["deploy_id"]) or {}
    file_path = None
    if scope == "file":
        file_cmd = next((c for c in (await fix_suggestions(conn, project_id, incident_id) or {}).get("commands", [])
                         if c["kind"] == "revert_file"), None)
        if not file_cmd:
            raise UndoPRError(422, "The failing file was not modified by this deploy, so it cannot be undone on its own.")
        file_path = file_cmd["path"]
    token = await decrypt_or_plain(project_id, "projects", "undo_pr_token", row["undo_pr_token_enc"], None)
    if not token:
        raise UndoPRError(503, "The stored token could not be read; turn draft pull requests off and on again.")
    gh = UndoPullRequestClient(token, base_url)
    r = f"repos/{repo}"

    # The commit before the deploy: the previous deploy in this environment, or the deploy's parent
    base = (undo.get("base") or "").lower()
    if not _SHA.match(base):
        parents = (await gh.get(f"{r}/git/commits/{sha}")).get("parents") or []
        if not parents:
            raise UndoPRError(422, "The deploy's commit has no parent to go back to.")
        base = parents[0]["sha"]
    compare = await gh.get(f"{r}/compare/{base}...{sha}")
    files = compare.get("files") or []
    if len(files) > MAX_FILES:
        raise UndoPRError(422, f"The deploy changed {len(files)} files: too many for a generated pull request. Use git revert.")
    base_tree_sha = (await gh.get(f"{r}/git/commits/{base}"))["tree"]["sha"]
    tree = await gh.get(f"{r}/git/trees/{base_tree_sha}", {"recursive": "1"})
    if tree.get("truncated"):
        raise UndoPRError(422, "The repository is too large to read its file list in one go. Use git revert.")
    base_entries = {e["path"]: (e["mode"], e["type"], e["sha"]) for e in tree.get("tree", []) if e.get("type") in ("blob", "commit")}
    entries = _entries(files, base_entries, file_path)
    if not entries:
        raise UndoPRError(409, "The deploy changed no files, so there is nothing to undo.")

    default_branch = (await gh.get(r))["default_branch"]
    head = (await gh.get(f"{r}/git/ref/heads/{default_branch}"))["object"]["sha"]
    head_tree = (await gh.get(f"{r}/git/commits/{head}"))["tree"]["sha"]
    new_tree = (await gh.post(f"{r}/git/trees", {"base_tree": head_tree, "tree": entries}))["sha"]
    if new_tree == head_tree:
        raise UndoPRError(409, f"Nothing to undo: {default_branch} already has these files as they were before the deploy.")
    what = f"the change to {file_path}" if file_path else f"deploy {sha[:8]}"
    commit = (await gh.post(f"{r}/git/commits", {
        "message": f"Undo {what}\n\nPuts the files back as they were at {base[:12]}, the commit before the deploy.\n"
                   f"Opened as a draft by ChangeWatch for incident #{incident_id}.",
        "tree": new_tree, "parents": [head],
    }))["sha"]
    branch = f"{BRANCH_PREFIX}undo-incident-{incident_id}-{scope}-{int(time.time())}"
    await gh.post(f"{r}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    paths = ", ".join(f"`{e['path']}`" for e in entries[:20]) + (" and more" if len(entries) > 20 else "")
    body = (
        f"ChangeWatch opened this **draft** for incident #{incident_id} ({row['title'] or 'an error'}): {dashboard_link}\n\n"
        f"It puts {paths} back as {'it was' if len(entries) == 1 else 'they were'} at `{base[:12]}`, the commit before "
        f"deploy `{sha[:8]}`, on top of `{default_branch}`. If later commits changed "
        f"{'this file' if len(entries) == 1 else 'these files'} too, those changes are undone as well: check the diff.\n\n"
        "ChangeWatch only opened this pull request. It merges nothing; review it, then merge or close it."
    )
    pr = await gh.post(f"{r}/pulls", {"title": f"Undo {what} (incident #{incident_id})", "head": branch,
                                        "base": default_branch, "body": body, "draft": True})
    result = {"scope": scope, "url": pr.get("html_url"), "number": pr.get("number"), "branch": branch}

    from app.trust.actions import record_incident_event

    await record_incident_event(conn, project_id, incident_id, "undo_pr", actor="manage key", detail=result)
    logger.info("Opened draft revert pull request #%s for incident %d (%s)", result["number"], incident_id, scope)
    return {**result, "already_open": False}
