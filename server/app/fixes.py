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
NOTE = "Suggestions only: ChangeWatch never runs these and never writes to your repository. Check before using."


def _sha(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    return value if _SHA.match(value) else None


def _short(release: str) -> str:
    """A SHA cut to 12 characters; a release name as sent."""
    return release[:12] if _sha(release) else release


async def deploy_undo(conn: asyncpg.Connection, project_id: int, deploy_id: int) -> Optional[Dict[str, Any]]:
    """How to undo a whole deploy: redeploy what ran before it, or revert its commits. None if the
    deploy is not this project's."""
    dep = await conn.fetchrow(
        """
        SELECT d.commit_sha, d.environment,
               (SELECT p.commit_sha FROM deploys p WHERE p.project_id = d.project_id
                  AND p.environment IS NOT DISTINCT FROM d.environment AND p.deployed_at < d.deployed_at
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
        commands.append({"kind": "revert", "label": "Revert every commit of this deploy",
                         "command": f"git revert --no-edit {prev}..{sha}",
                         "explain": f"Undoes the commits after the previous {env} deploy ({prev[:8]}), newest first, "
                                    "as new commits. A merge commit in the range needs git revert -m 1 <sha> instead."})
    elif sha:
        commands.append({"kind": "revert", "label": "Revert this deploy's commit",
                         "command": f"git revert --no-edit {sha}",
                         "explain": f"Undoes {sha[:8]} as a new commit. For a merge commit use git revert -m 1 {sha} instead."})
    return {
        "commit_sha": dep["commit_sha"],
        "rollback_to": {"sha": dep["previous_sha"], "text": f"Redeploy {_short(dep['previous_sha'])}, the last {env} deploy before this one."}
        if dep["previous_sha"] and dep["previous_sha"] != dep["commit_sha"] else None,
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
        parts.append(f"redeploy {_short(undo['rollback_to']['sha'])}")
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
            commands.append({"kind": "revert_file", "label": f"Undo only the change to {path}",
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

    return {"rollback_to": undo.get("rollback_to"), "commands": commands, "ai_fix": ai_fix,
            "no_commands_reason": undo.get("no_commands_reason"), "note": NOTE}
