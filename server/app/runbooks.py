"""Runbooks (backlog): the steps a team wrote for a kind of error, matched by the error type, the
failing file and the route, and shown where the responder looks first: the alert, the incident
page, the AI investigation and the postmortem. They live in ChangeWatch only (nothing is read
from or written to the repository) and the steps are stored encrypted.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncpg
from pydantic import BaseModel, Field, field_validator, model_validator

from app.crypto import decrypt_or_plain, encrypt_or_keep

MAX_RUNBOOKS = 100
# A link to the team's own page; no spaces or characters that would break a chat link
_URL = re.compile(r"^https://[^\s<>|\"'`]+$")


class RunbookIn(BaseModel):
    """A runbook as the team writes it. At least one of the three matchers is required; when
    several are set, all must match."""

    title: str = Field(..., min_length=1, max_length=200)
    steps: str = Field("", max_length=5000)
    match_exception: Optional[str] = Field(None, max_length=200, description="Error type, e.g. ZeroDivisionError")
    match_path: Optional[str] = Field(None, max_length=300, description="Failing file, e.g. app/billing/* or app/billing/")
    match_route: Optional[str] = Field(None, max_length=300, description="Request path, e.g. /checkout*")
    url: Optional[str] = Field(None, max_length=500, description="Link to the team's own runbook page (https)")

    @field_validator("title", "steps", "match_exception", "match_path", "match_route", "url", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
        return value

    @field_validator("match_exception", "match_path", "match_route", "url")
    @classmethod
    def _empty_is_none(cls, value: Optional[str]) -> Optional[str]:
        return value or None

    @field_validator("title")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("the title must be one line")
        return value

    @field_validator("url")
    @classmethod
    def _https(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _URL.match(value):
            raise ValueError("the link must start with https:// and contain no spaces")
        return value

    @model_validator(mode="after")
    def _a_matcher(self) -> "RunbookIn":
        if not (self.match_exception or self.match_path or self.match_route):
            raise ValueError("set at least one of match_exception, match_path or match_route")
        return self


def exception_type(title: Optional[str]) -> str:
    """'ZeroDivisionError' from 'ZeroDivisionError: division by zero'."""
    return (title or "").split(":", 1)[0].strip()


def _glob(pattern: str) -> str:
    """A pattern ending in '/' means everything under it."""
    return pattern + "*" if pattern.endswith("/") else pattern


def _specificity(pattern: str) -> int:
    return sum(ch not in "*?[]" for ch in pattern)


def _path_matches(path: str, pattern: str) -> bool:
    """Repository-relative patterns also match an absolute path from a stack trace
    ('app/billing.py' matches '/srv/app/billing.py')."""
    pattern = _glob(pattern)
    if fnmatchcase(path, pattern):
        return True
    return not pattern.startswith(("*", "/")) and fnmatchcase(path, "*/" + pattern)


def match_runbook(runbook: Any, exc_type: str, path: Optional[str], routes: Sequence[str]) -> Optional[Tuple[int, List[str]]]:
    """(score, what matched) when every matcher the runbook sets matches, else None. An error
    type match counts most; longer patterns beat shorter ones."""
    matched: List[str] = []
    score = 0
    want = runbook["match_exception"]
    if want:
        if not exc_type or not (exc_type == want or exc_type.endswith("." + want) or want.endswith("." + exc_type)):
            return None
        matched.append("error type")
        score += 100
    if runbook["match_path"]:
        if not path or not _path_matches(path, runbook["match_path"]):
            return None
        matched.append("file")
        score += 10 + _specificity(runbook["match_path"])
    if runbook["match_route"]:
        pattern = _glob(runbook["match_route"])
        if not any(fnmatchcase(r, pattern) for r in routes):
            return None
        matched.append("route")
        score += 10 + _specificity(runbook["match_route"])
    return (score, matched) if matched else None


async def _routes(conn: asyncpg.Connection, project_id: int, issue_id: int, since: datetime) -> List[str]:
    """The request paths this error was seen on, most frequent first, without query strings."""
    rows = await conn.fetch(
        """
        SELECT request_path, count(*) AS n FROM events
        WHERE project_id = $1 AND issue_id = $2 AND occurred_at >= $3 AND request_path IS NOT NULL
        GROUP BY request_path ORDER BY n DESC LIMIT 10
        """,
        project_id, issue_id, since,
    )
    return [r["request_path"].split("?", 1)[0] for r in rows]


async def _steps(project_id: int, row: Any) -> str:
    return await decrypt_or_plain(project_id, "runbooks", "steps", row["steps_enc"], row["steps"]) or ""


async def find_runbooks(conn: asyncpg.Connection, project_id: int, *, title: Optional[str], where_file: Optional[str],
                        issue_id: Optional[int] = None, since: Optional[datetime] = None,
                        routes: Optional[Sequence[str]] = None, limit: int = 3) -> List[Dict[str, Any]]:
    """The project's runbooks that match this error, best first, with their steps."""
    rows = await conn.fetch(
        """
        SELECT id, title, match_exception, match_path, match_route, steps, steps_enc, url
        FROM runbooks WHERE project_id = $1 ORDER BY id LIMIT $2
        """,
        project_id, MAX_RUNBOOKS,
    )
    if not rows:
        return []
    if routes is None:
        routes = []
        if issue_id and any(r["match_route"] for r in rows):
            routes = await _routes(conn, project_id, issue_id, since or datetime.now(timezone.utc) - timedelta(days=1))
    exc_type = exception_type(title)
    scored = []
    for row in rows:
        hit = match_runbook(row, exc_type, where_file, routes)
        if hit:
            scored.append((hit[0], row, hit[1]))
    scored.sort(key=lambda s: (-s[0], s[1]["id"]))
    return [
        {"id": row["id"], "title": row["title"], "steps": await _steps(project_id, row), "url": row["url"],
         "matched_on": matched}
        for _, row, matched in scored[:limit]
    ]


def runbook_line(runbooks: List[Dict[str, Any]]) -> Optional[str]:
    """One line for the alert: the best runbook, with its link when it has one."""
    if not runbooks:
        return None
    best = runbooks[0]
    return f"Runbook: {best['title']}" + (f" ({best['url']})" if best.get("url") else " (steps on the incident page)")


def _out(row: Any, steps: str) -> Dict[str, Any]:
    return {
        "id": row["id"], "title": row["title"], "steps": steps, "match_exception": row["match_exception"],
        "match_path": row["match_path"], "match_route": row["match_route"], "url": row["url"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def list_runbooks(conn: asyncpg.Connection, project_id: int) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, title, match_exception, match_path, match_route, steps, steps_enc, url, updated_at
        FROM runbooks WHERE project_id = $1 ORDER BY id
        """,
        project_id,
    )
    return [_out(r, await _steps(project_id, r)) for r in rows]


async def create_runbook(conn: asyncpg.Connection, project_id: int, data: RunbookIn) -> Optional[Dict[str, Any]]:
    """The new runbook, or None when the project already has MAX_RUNBOOKS."""
    # One writer at a time per project, so two requests cannot both pass the limit
    await conn.execute("SELECT pg_advisory_xact_lock(720114, $1)", project_id)
    if await conn.fetchval("SELECT count(*) FROM runbooks WHERE project_id = $1", project_id) >= MAX_RUNBOOKS:
        return None
    steps, steps_enc = await encrypt_or_keep(project_id, "runbooks", "steps", data.steps)
    row = await conn.fetchrow(
        """
        INSERT INTO runbooks (project_id, title, match_exception, match_path, match_route, steps, steps_enc, url)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id, title, match_exception, match_path, match_route, url, updated_at
        """,
        project_id, data.title, data.match_exception, data.match_path, data.match_route, steps, steps_enc, data.url,
    )
    return _out(row, data.steps)


async def update_runbook(conn: asyncpg.Connection, project_id: int, runbook_id: int, data: RunbookIn) -> Optional[Dict[str, Any]]:
    """The updated runbook, or None when it is not this project's."""
    steps, steps_enc = await encrypt_or_keep(project_id, "runbooks", "steps", data.steps)
    row = await conn.fetchrow(
        """
        UPDATE runbooks SET title = $3, match_exception = $4, match_path = $5, match_route = $6,
               steps = $7, steps_enc = $8, url = $9, updated_at = now()
        WHERE id = $1 AND project_id = $2
        RETURNING id, title, match_exception, match_path, match_route, url, updated_at
        """,
        runbook_id, project_id, data.title, data.match_exception, data.match_path, data.match_route,
        steps, steps_enc, data.url,
    )
    return _out(row, data.steps) if row else None


async def delete_runbook(conn: asyncpg.Connection, project_id: int, runbook_id: int) -> bool:
    status = await conn.execute("DELETE FROM runbooks WHERE id = $1 AND project_id = $2", runbook_id, project_id)
    return status.endswith(" 1")
