"""Read-only tools for ChangeWatch AI SRE Investigator (Phase 2 Stage C2).

Guarantees:
1. Every tool is strictly SELECT-only (read-only). No INSERT, UPDATE, DELETE, or DROP.
2. Every database query is tenant-scoped by project_id and subject to Row-Level Security.
3. Size caps and row limits are enforced on all queries and outputs.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional
import asyncpg

from app.crypto import decrypt_or_plain
from app.config import settings

logger = logging.getLogger("changewatch.investigator.tools")


async def get_diff_files(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
) -> List[Dict[str, Any]]:
    """Return list of changed files for a deploy with added line ranges (capped at 50 files)."""
    rows = await conn.fetch(
        """
        SELECT path, status, added_ranges, is_dependency_file, is_config_file
        FROM deploy_files
        WHERE project_id = $1 AND deploy_id = $2
        ORDER BY path ASC
        LIMIT 50
        """,
        project_id,
        deploy_id,
    )
    result = []
    for r in rows:
        ranges = r["added_ranges"]
        if isinstance(ranges, str):
            try:
                ranges = json.loads(ranges)
            except Exception:
                ranges = []
        result.append({
            "path": r["path"],
            "status": r["status"],
            "added_ranges": ranges,
            "is_dependency_file": r["is_dependency_file"],
            "is_config_file": r["is_config_file"],
        })
    return result


async def get_file_at_commit(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetch file content at deploy commit from deploy_files.
    If content is None (e.g. ephemeral mode) and GITHUB_READ_TOKEN is set, fetch read-only via GitHub client.
    """
    row = await conn.fetchrow(
        """
        SELECT content, content_enc
        FROM deploy_files
        WHERE project_id = $1 AND deploy_id = $2 AND path = $3
        LIMIT 1
        """,
        project_id,
        deploy_id,
        path,
    )
    content = (
        await decrypt_or_plain(project_id, "deploy_files", "content", row.get("content_enc"), row.get("content"))
        if row
        else None
    )

    # Ephemeral or missing fallback: if GITHUB_READ_TOKEN configured
    if content is None and settings.GITHUB_READ_TOKEN:
        try:
            from app.github_client import get_file_content_readonly
            dep_row = await conn.fetchrow(
                "SELECT commit_sha FROM deploys WHERE id = $1 AND project_id = $2",
                deploy_id,
                project_id,
            )
            if dep_row and dep_row["commit_sha"]:
                content = await get_file_content_readonly(
                    commit_sha=dep_row["commit_sha"],
                    file_path=path,
                )
        except Exception as exc:
            logger.debug("GitHub fallback read failed for %s: %s", path, exc)

    if content is None:
        return {"path": path, "available": False, "lines": [], "content": None}

    lines = content.splitlines()
    total_lines = len(lines)
    s = max(1, start_line or 1)
    e = min(total_lines, end_line or total_lines)

    sliced = lines[s - 1 : e]
    return {
        "path": path,
        "available": True,
        "start_line": s,
        "end_line": e,
        "total_lines": total_lines,
        "lines": sliced[:100],  # cap at 100 lines
        "content": "\n".join(sliced[:100]),
    }


async def get_dependency_changes(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
) -> List[Dict[str, Any]]:
    """Parse added or modified dependency files to extract package changes."""
    rows = await conn.fetch(
        """
        SELECT path, status, content, content_enc
        FROM deploy_files
        WHERE project_id = $1 AND deploy_id = $2 AND is_dependency_file = true
        LIMIT 10
        """,
        project_id,
        deploy_id,
    )
    changes = []
    for r in rows:
        path = r["path"]
        content = await decrypt_or_plain(project_id, "deploy_files", "content", r.get("content_enc"), r.get("content")) or ""
        # Parse requirements.txt style lines
        if "requirements" in path or path.endswith(".txt"):
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"^([a-zA-Z0-9_\-]+)([=><~]+.*)?$", line)
                if match:
                    pkg = match.group(1)
                    ver = match.group(2) or "any"
                    changes.append({"file": path, "package": pkg, "spec": ver, "change_type": r["status"]})
        elif "package.json" in path:
            try:
                pkg_json = json.loads(content)
                deps = {**pkg_json.get("dependencies", {}), **pkg_json.get("devDependencies", {})}
                for pkg, ver in list(deps.items())[:50]:
                    changes.append({"file": path, "package": pkg, "spec": ver, "change_type": r["status"]})
            except Exception:
                pass
        elif "pyproject.toml" in path:
            for line in content.splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    parts = line.split("=", 1)
                    pkg = parts[0].strip().strip('"').strip("'")
                    if pkg and not pkg.startswith("["):
                        changes.append({"file": path, "package": pkg, "spec": parts[1].strip(), "change_type": r["status"]})
    return changes[:50]


async def get_config_changes(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
) -> Dict[str, Any]:
    """Extract changed config, Docker, migration files and new environment variable names."""
    rows = await conn.fetch(
        """
        SELECT path, status, content, content_enc
        FROM deploy_files
        WHERE project_id = $1 AND deploy_id = $2
          AND (is_config_file = true OR path LIKE '%docker%' OR path LIKE '%env%' OR path LIKE '%migration%')
        LIMIT 20
        """,
        project_id,
        deploy_id,
    )
    env_vars_found = set()
    changed_configs = []

    for r in rows:
        changed_configs.append({"path": r["path"], "status": r["status"]})
        content = await decrypt_or_plain(project_id, "deploy_files", "content", r.get("content_enc"), r.get("content")) or ""
        # Search for os.environ, os.getenv, process.env, settings.
        for match in re.finditer(r"(?:os\.(?:environ\[|getenv\()|process\.env\.)[\"']([A-Z0-9_]+)[\"']", content):
            env_vars_found.add(match.group(1))

    return {
        "config_files": changed_configs,
        "referenced_env_vars": sorted(list(env_vars_found))[:20],
    }


async def list_new_issues(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
) -> List[Dict[str, Any]]:
    """List new issues first seen after the deploy timestamp (capped at 20)."""
    rows = await conn.fetch(
        """
        SELECT i.id, i.fingerprint, i.title, i.status, i.event_count, i.first_seen
        FROM issues i
        JOIN deploys d ON d.id = $2 AND d.project_id = $1
        WHERE i.project_id = $1 AND i.first_seen >= d.deployed_at
        ORDER BY i.event_count DESC
        LIMIT 20
        """,
        project_id,
        deploy_id,
    )
    return [dict(r) for r in rows]


async def get_issue_events(
    conn: asyncpg.Connection,
    project_id: int,
    issue_id: int,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Retrieve recent event stack traces and request paths for an issue."""
    rows = await conn.fetch(
        """
        SELECT id, occurred_at, request_path, duration_ms, stack_trace, stack_trace_enc
        FROM events
        WHERE project_id = $1 AND issue_id = $2
        ORDER BY occurred_at DESC
        LIMIT $3
        """,
        project_id,
        issue_id,
        min(limit, 20),
    )
    events = []
    for r in rows:
        trace = await decrypt_or_plain(project_id, "events", "stack_trace", r.get("stack_trace_enc"), r.get("stack_trace"))
        events.append(
            {
                "id": r["id"],
                "occurred_at": r["occurred_at"].isoformat() if r.get("occurred_at") else None,
                "request_path": r.get("request_path"),
                "duration_ms": r.get("duration_ms"),
                "stack_trace": (trace or "")[:2000],
            }
        )
    return events


async def get_error_rate(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
) -> Dict[str, Any]:
    """Calculate error rate before and after deploy from traffic table."""
    dep = await conn.fetchrow(
        "SELECT deployed_at, environment FROM deploys WHERE id = $1 AND project_id = $2",
        deploy_id,
        project_id,
    )
    if not dep or not dep["deployed_at"]:
        return {"before": {"requests": 0, "errors": 0, "rate": 0.0}, "after": {"requests": 0, "errors": 0, "rate": 0.0}}

    dep_at = dep["deployed_at"]
    # 30 min before, 30 min after
    before_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(requests), 0) AS reqs, COALESCE(SUM(errors), 0) AS errs
        FROM traffic
        WHERE project_id = $1 AND environment = $4 AND bucket >= $2 AND bucket < $3
        """,
        project_id,
        dep_at - timedelta(minutes=30),
        dep_at,
        dep.get("environment") or "prod",
    )
    after_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(requests), 0) AS reqs, COALESCE(SUM(errors), 0) AS errs
        FROM traffic
        WHERE project_id = $1 AND environment = $4 AND bucket >= $2 AND bucket <= $3
        """,
        project_id,
        dep_at,
        dep_at + timedelta(minutes=30),
        dep.get("environment") or "prod",
    )

    b_reqs = int(before_row["reqs"]) if before_row else 0
    b_errs = int(before_row["errs"]) if before_row else 0
    a_reqs = int(after_row["reqs"]) if after_row else 0
    a_errs = int(after_row["errs"]) if after_row else 0

    return {
        "before": {"requests": b_reqs, "errors": b_errs, "rate": (b_errs / b_reqs) if b_reqs > 0 else 0.0},
        "after": {"requests": a_reqs, "errors": a_errs, "rate": (a_errs / a_reqs) if a_reqs > 0 else 0.0},
    }


async def get_previous_deploys(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
    n: int = 3,
) -> List[Dict[str, Any]]:
    """Retrieve up to n prior deploys before the specified deploy."""
    dep = await conn.fetchrow(
        "SELECT deployed_at FROM deploys WHERE id = $1 AND project_id = $2",
        deploy_id,
        project_id,
    )
    if not dep or not dep["deployed_at"]:
        return []

    rows = await conn.fetch(
        """
        SELECT id, commit_sha, author, message, environment, deployed_at
        FROM deploys
        WHERE project_id = $1 AND deployed_at < $2
        ORDER BY deployed_at DESC
        LIMIT $3
        """,
        project_id,
        dep["deployed_at"],
        min(n, 5),
    )
    return [
        {
            "id": r["id"],
            "commit_sha": r["commit_sha"],
            "author": r["author"],
            "message": r["message"],
            "deployed_at": r["deployed_at"].isoformat() if r["deployed_at"] else None,
        }
        for r in rows
    ]


async def find_similar_issues(
    conn: asyncpg.Connection,
    project_id: int,
    fingerprint: str,
) -> List[Dict[str, Any]]:
    """Find other occurrences of this fingerprint or similar issues."""
    rows = await conn.fetch(
        """
        SELECT id, fingerprint, title, status, event_count, first_seen, last_seen
        FROM issues
        WHERE project_id = $1 AND fingerprint = $2
        LIMIT 5
        """,
        project_id,
        fingerprint,
    )
    return [dict(r) for r in rows]
