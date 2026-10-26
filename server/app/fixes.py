"""Revert and fix suggestions (backlog): the exact commands to undo a deploy, or only the failing
file, and the AI's suggested change, as text the team copies. Suggest only: ChangeWatch never
runs them and never writes to the repository (the read-only promise).
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any, Dict, List, Optional

import asyncpg

from app.crypto import decrypt_or_plain

# Only a real commit SHA goes into a command: a deploy's "commit_sha" is whatever the client sent
# (a release name, or text built to run something when pasted into a shell)
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
# A release name is shown only when it is plain: other text could bring its own formatting, or a
# fake command, into the alert line that holds the real ones
_NAME = re.compile(r"[A-Za-z0-9._/-]{1,64}")
NOTE = ("Suggestions only: ChangeWatch never runs these. It writes to your repository only when you open a draft "
        "pull request here, if the project turned that on. Check before using.")


def _sha(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    return value if _SHA.match(value) else None


def _short(release: str) -> Optional[str]:
    """A SHA cut to 12 characters; a plain release name as sent; None for any other text."""
    if _sha(release):
        return release.strip()[:12]
    return release if _NAME.fullmatch(release) else None


async def deploy_undo(conn: asyncpg.Connection, project_id: int, deploy_id: int) -> Optional[Dict[str, Any]]:
    """How to undo a whole deploy: redeploy what ran before it, or revert its commits. None if the
    deploy is not this project's."""
    # The previous deploy runs other code: the same commit sent again (a second region, a re-run)
    # is not something to roll back to
    dep = await conn.fetchrow(
        """
        SELECT d.commit_sha, d.environment,
               (SELECT p.commit_sha FROM deploys p WHERE p.project_id = d.project_id
                  AND p.environment IS NOT DISTINCT FROM d.environment AND p.deployed_at < d.deployed_at
                  AND lower(p.commit_sha) <> lower(d.commit_sha)
                ORDER BY p.deployed_at DESC, p.id DESC LIMIT 1) AS previous_sha
        FROM deploys d WHERE d.id = $1 AND d.project_id = $2
        """,
        deploy_id, project_id,
    )
    if not dep:
        return None
    sha, prev = _sha(dep["commit_sha"]), _sha(dep["previous_sha"])
    env = dep["environment"] or "prod"
    commands: List[Dict[str, str]] = []
    if sha and prev and prev != sha:
        commands.append({"kind": "check", "label": "See which commits this deploy added",
                         "command": f"git log --oneline {prev}..{sha}"})
        # A merged pull request is a merge commit, which git revert stops at: skip merges, their
        # commits are in the range and are undone one by one
        commands.append({"kind": "revert", "label": "Revert every commit of this deploy",
                         "command": f"git revert --no-edit --no-merges {prev}..{sha}",
                         "explain": f"Undoes the commits after the previous {env} deploy ({prev[:8]}), newest first, "
                                    "as new commits. Merge commits are skipped; the commits they merged are undone instead."})
    elif sha:
        commands.append({"kind": "revert", "label": "Revert this deploy's commit",
                         "command": f"git revert --no-edit {sha}",
                         "explain": f"Undoes {sha[:8]} as a new commit. For a merge commit use git revert -m 1 {sha} instead."})
    rollback_to = None
    if dep["previous_sha"]:
        name = _short(dep["previous_sha"])
        rollback_to = {"sha": dep["previous_sha"],
                       "text": f"Redeploy {name}, the last {env} deploy before this one." if name
                       else f"Redeploy the last {env} deploy before this one."}
    return {
        "commit_sha": dep["commit_sha"],
        "rollback_to": rollback_to,
        "commands": commands,
        "base": prev or (f"{sha}^" if sha else None),
        "no_commands_reason": None if sha else "The deploy's commit is not a git SHA, so no git commands are suggested.",
    }


def undo_line(undo: Optional[Dict[str, Any]]) -> Optional[str]:
    """One line for a ROLLBACK verdict alert."""
    if not undo:
        return None
    revert = next((c["command"] for c in undo["commands"] if c["kind"] == "revert"), None)
    parts = []
    if undo.get("rollback_to"):
        name = _short(undo["rollback_to"]["sha"])
        parts.append(f"redeploy {name}" if name else "redeploy the previous release")
    if revert:
        parts.append(f"`{revert}`")
    return ("Undo: " + " or ".join(parts) + " (suggestions; ChangeWatch runs nothing)") if parts else None


async def fix_suggestions(conn: asyncpg.Connection, project_id: int, incident_id: int) -> Optional[Dict[str, Any]]:
    """For an incident: undo the deploy, undo only the failing file, and the AI's suggested fix."""
    inc = await conn.fetchrow(
        "SELECT deploy_id, where_file FROM incidents WHERE id = $1 AND project_id = $2", incident_id, project_id
    )
    if not inc:
        return None
    undo = await deploy_undo(conn, project_id, inc["deploy_id"]) or {"commands": [], "base": None, "rollback_to": None}
    commands = list(undo["commands"])

    # Only the failing file, when this deploy modified it: put it back as it was before the deploy
    # (an added or renamed file did not exist there under this path)
    where = (inc["where_file"] or "").replace("\\", "/")
    if where and undo["base"]:
        files = await conn.fetch(
            "SELECT path, status FROM deploy_files WHERE deploy_id = $1 AND project_id = $2", inc["deploy_id"], project_id
        )
        matches = [f for f in files if f["path"] and (where == f["path"].lstrip("/") or where.endswith("/" + f["path"].lstrip("/")))]
        best = max(matches, key=lambda f: len(f["path"]), default=None)
        if best and (best["status"] or "modified") == "modified":
            path = best["path"].lstrip("/")
            commands.append({"kind": "revert_file", "label": f"Undo only the change to {path}", "path": path,
                             "command": f"git restore --source={undo['base']} -- {shlex.quote(path)}",
                             "explain": "Puts this one file back as it was before the deploy and keeps the rest; "
                                        "review it, then commit."})

    # The AI's suggested fix from the latest investigation
    ai_fix = None
    inv = await conn.fetchrow(
        """
        SELECT result, result_enc FROM investigations
        WHERE project_id = $1 AND incident_id = $2 AND status IN ('done', 'partial')
        ORDER BY id DESC LIMIT 1
        """,
        project_id, incident_id,
    )
    if inv:
        raw = await decrypt_or_plain(project_id, "investigations", "result", inv.get("result_enc"), inv.get("result"))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        fix = (raw or {}).get("suggested_fix") if isinstance(raw, dict) else None
        if isinstance(fix, dict) and (fix.get("description") or fix.get("code_hint")):
            ai_fix = {"description": fix.get("description"), "code_hint": fix.get("code_hint"),
                      "rollback_recommended": bool(fix.get("rollback_recommended")), "warning": fix.get("warning_label")}

    # Draft revert pull requests, when the project turned them on (app/undo_pr.py)
    from app.undo_pr import is_enabled, opened

    pull_requests = {"enabled": await is_enabled(conn, project_id), "opened": await opened(conn, project_id, incident_id)}
    return {"rollback_to": undo.get("rollback_to"), "commands": commands, "ai_fix": ai_fix,
            "no_commands_reason": undo.get("no_commands_reason"), "note": NOTE, "pull_requests": pull_requests}
