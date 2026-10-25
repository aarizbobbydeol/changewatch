"""Memory of past incidents (backlog item): when something breaks, what happened the last times
something like it broke, and how it ended.

Similar means: the same error group (the error is back), or an error in the same function or file.
The exception type only breaks ties (a KeyError somewhere else is not "the same problem"). Looks at
the last 180 days of this project only. Read-only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from app.crypto import decrypt_or_plain

MEMORY_DAYS = 180
STRONG_MATCH = 60  # the same error, or the same function: worth a line in the alert


def _minutes(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


async def find_similar_incidents(
    conn: asyncpg.Connection,
    project_id: int,
    *,
    exclude_incident_id: int = 0,
    issue_id: Optional[int] = None,
    where_file: Optional[str] = None,
    where_function: Optional[str] = None,
    title: Optional[str] = None,
    before: Optional[datetime] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Past incidents like this one, best match first, each with how it was explained and resolved."""
    exc_type = (title or "").split(":", 1)[0].strip()
    file_ = (where_file or "").strip()
    func = (where_function or "").strip()
    if func in ("unknown", "<module>"):
        func = ""
    rows = await conn.fetch(
        """
        SELECT i.id, i.title, i.status, i.where_file, i.where_line, i.created_at, i.resolved_at,
               i.first_event_at, i.event_count, d.commit_sha,
               (CASE WHEN i.issue_id = $3 THEN 100 ELSE 0 END
                + CASE WHEN $4 <> '' AND i.where_file = $4
                       THEN CASE WHEN $5 <> '' AND i.where_function = $5 THEN 60 ELSE 40 END ELSE 0 END
                + CASE WHEN $6 <> '' AND split_part(i.title, ':', 1) = $6 THEN 5 ELSE 0 END) AS score
        FROM incidents i
        JOIN deploys d ON d.id = i.deploy_id AND d.project_id = i.project_id
        WHERE i.project_id = $1 AND i.id <> $2
          AND i.created_at < $7 AND i.created_at >= $7 - make_interval(days => $8)
          AND (i.issue_id = $3 OR ($4 <> '' AND i.where_file = $4))
        ORDER BY score DESC, i.created_at DESC
        LIMIT $9
        """,
        project_id,
        exclude_incident_id,
        issue_id or 0,
        file_,
        func,
        exc_type,
        before or datetime.now(timezone.utc),
        MEMORY_DAYS,
        max(1, min(limit, 10)),
    )
    similar: List[Dict[str, Any]] = []
    for r in rows:
        score = int(r["score"])
        match = "the same error" if score >= 100 else "the same function" if score >= 60 else "the same file"
        resolved = await conn.fetchrow(
            """
            SELECT actor, detail FROM incident_events
            WHERE project_id = $1 AND incident_id = $2 AND type = 'resolved'
            ORDER BY at DESC LIMIT 1
            """,
            project_id, r["id"],
        )
        resolution = None
        if resolved:
            detail = resolved["detail"]
            detail = json.loads(detail) if isinstance(detail, str) else (detail or {})
            if detail.get("auto_resolve"):
                resolution = f"resolved automatically ({detail.get('fix_note') or 'the errors stopped'})"
            else:
                resolution = "resolved by the team"
        elif r["status"] == "resolved":
            resolution = "resolved"
        rated = await conn.fetchval(
            """
            SELECT label FROM alert_feedback
            WHERE project_id = $1 AND incident_id = $2 AND label IN ('real', 'false')
            ORDER BY created_at DESC LIMIT 1
            """,
            project_id, r["id"],
        )
        inv = await conn.fetchrow(
            """
            SELECT result, result_enc FROM investigations
            WHERE project_id = $1 AND incident_id = $2 AND status = 'done'
            ORDER BY id DESC LIMIT 1
            """,
            project_id, r["id"],
        )
        cause = None
        if inv:
            result = await decrypt_or_plain(project_id, "investigations", "result", inv.get("result_enc"), inv.get("result"))
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    result = {}
            cause = (result or {}).get("cause") if isinstance(result, dict) else None
        started = r["first_event_at"] or r["created_at"]
        similar.append({
            "id": r["id"],
            "title": r["title"],
            "when": r["created_at"].astimezone(timezone.utc).isoformat(),
            "match": match,
            "score": score,
            "where": f"{r['where_file']}:{r['where_line']}" if r["where_file"] else None,
            "deploy": (r["commit_sha"] or "")[:8],
            "status": r["status"],
            "resolution": resolution,
            "resolved_after": _minutes((r["resolved_at"] - started).total_seconds()) if r["resolved_at"] and started else None,
            "rated": {"real": "a real problem", "false": "a false alarm"}.get(rated),
            "cause": (cause or "")[:300] or None,
        })
    return similar


def seen_before_line(similar: List[Dict[str, Any]]) -> Optional[str]:
    """One line for the alert about the closest strong match, or None."""
    best = next((s for s in similar if s["score"] >= STRONG_MATCH), None)
    if not best:
        return None
    parts = [f"Seen before: incident #{best['id']} on {best['when'][:10]} ({best['match']})"]
    if best["resolution"]:
        parts.append(best["resolution"] + (f" after {best['resolved_after']}" if best["resolved_after"] else ""))
    if best["rated"] == "a false alarm":
        parts.append("that one was rated a false alarm")
    return ", ".join(parts) + "."
