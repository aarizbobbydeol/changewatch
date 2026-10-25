"""Incident Lifecycle and Event-Driven Trigger for ChangeWatch.

Handles:
- Event-driven incident opening (no polling, p95 < 5s)
- Fast-path checking against critical routes or event threshold (>=3 in 60s)
- Resolution of error frame, diff range mapping, and owners
- Typed decisions triage integration (rules-first, sub-second timeout)
- Upgrade from 'possible' to 'confirmed' (>=10 events or rollback verdict)
- Auto-resolution after 30 minutes of silence
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import asyncpg

from app.crypto import decrypt_or_plain
from app.config import settings
from app.decisions import (
    build_decision_state,
    decide_with_fallback,
    get_triage_questions,
    persist_decisions,
    Q_REGRESSION,
    Q_SEVERITY,
    Q_CATEGORY,
)
from app.notifiers.incident_alert import dispatch_incident_alert
from app.sre_prep import (
    frame_in_diff,
    innermost_project_frame,
    is_project_frame,
    parse_stack_trace,
    resolve_owners,
)

logger = logging.getLogger("changewatch.incidents")


def _same_file(frame_path: str, repo_path: str) -> bool:
    """True when a stack-frame path and a repo-relative path name the same file."""
    frame_path = frame_path.replace("\\", "/")
    repo_path = repo_path.replace("\\", "/").lstrip("/")
    return frame_path == repo_path or frame_path.endswith("/" + repo_path)


def _repo_relative(frames: List[Any], frame: Any, deploy_paths: List[str]) -> Optional[str]:
    """The frame's path inside the repository, or None. The server directory the repository is
    deployed at is taken from the frames that name a changed file; the longest match wins."""
    best: Optional[Tuple[str, int]] = None  # (server root, length of the matched repo path)
    for f in frames:
        fp = (f.file or "").replace("\\", "/")
        for rp in deploy_paths:
            rp = (rp or "").replace("\\", "/").lstrip("/")
            if not rp:
                continue
            if fp == rp:
                root = ""
            elif fp.endswith("/" + rp):
                root = fp[: len(fp) - len(rp)]
            else:
                continue
            if best is None or len(rp) > best[1]:
                best = (root, len(rp))
    if best is None:
        return None
    fpath = (frame.file or "").replace("\\", "/")
    root = best[0]
    if root and fpath.startswith(root):
        return fpath[len(root):]
    if not root and not fpath.startswith("/"):
        return fpath
    return None


async def meter_decision_call(
    conn: asyncpg.Connection, project_id: int, incident_id: int, answers: Dict[str, Any]
) -> bool:
    """Meter one hosted decision call (brief §11 `decision_call`); rules answers are free.

    Runs in a savepoint and swallows errors: metering must never roll back or delay the
    instant alert that follows (safety floor).
    """
    if not any(getattr(ans, "source", "rules") == "jev" for ans in answers.values()):
        return False
    try:
        from app.metering import Meter, record_usage

        async with conn.transaction():
            await record_usage(
                conn,
                project_id=project_id,
                meter=Meter.DECISION_CALL,
                units=1,
                ref_type="incident",
                ref_id=incident_id,
                idempotency_key=f"decision_call:incident:{incident_id}",
            )
        return True
    except Exception as exc:
        logger.warning("Failed metering decision call for incident %d: %s", incident_id, exc)
        return False


async def _returned_after_resolution(
    conn: asyncpg.Connection, project_id: int, issue_id: int, deployed_at: datetime
) -> bool:
    """True when the issue's last incident was resolved before this deploy and the error did
    not occur again between that resolution and the deploy."""
    resolved_at = await conn.fetchval(
        """
        SELECT max(resolved_at) FROM incidents
        WHERE project_id = $1 AND issue_id = $2 AND status = 'resolved' AND resolved_at <= $3
        """,
        project_id,
        issue_id,
        deployed_at,
    )
    if resolved_at is None:
        return False
    still_open = await conn.fetchval(
        "SELECT 1 FROM incidents WHERE project_id = $1 AND issue_id = $2 AND status != 'resolved' LIMIT 1",
        project_id,
        issue_id,
    )
    if still_open:
        return False
    seen_since = await conn.fetchval(
        """
        SELECT 1 FROM events
        WHERE project_id = $1 AND issue_id = $2 AND occurred_at > $3 AND occurred_at < $4
        LIMIT 1
        """,
        project_id,
        issue_id,
        resolved_at,
        deployed_at,
    )
    return seen_since is None


async def check_and_trigger_incident(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
    issue_id: int,
    payload: Dict[str, Any],
    occurred_at_dt: datetime,
    first_event_dt: Optional[datetime] = None,
    new_events: int = 1,
    investigation_jobs: Optional[List[Dict[str, Any]]] = None,
) -> Optional[int]:
    """
    Event-driven check called immediately after inserting error events; `new_events` is how many
    events of this issue and deploy arrived since the last check.
    Target latency: < 50ms for check + dispatch.

    With `investigation_jobs`, the AI investigation job is appended there for the caller to queue
    after its transaction commits (the investigator cannot see an uncommitted incident).
    """
    # 1. Fetch deploy metadata (deployed_at, commit_sha, author, message, codeowners)
    dep_row = await conn.fetchrow(
        """
        SELECT deployed_at, commit_sha, author, message, codeowners, codeowners_enc
        FROM deploys
        WHERE id = $1 AND project_id = $2
        """,
        deploy_id,
        project_id,
    )
    if not dep_row:
        return None

    deployed_at = dep_row["deployed_at"]
    if not deployed_at:
        return None

    # Never open incidents for deploys older than INCIDENT_MAX_DEPLOY_AGE_MIN (default 120m)
    max_deploy_age_min = getattr(settings, "INCIDENT_MAX_DEPLOY_AGE_MIN", 120)
    now_dt = datetime.now(timezone.utc)
    if (now_dt - deployed_at).total_seconds() > (max_deploy_age_min * 60):
        return None

    # 2. Check issue first_seen: must be after or at deployed_at (new regression)
    issue_row = await conn.fetchrow(
        "SELECT title, fingerprint, first_seen, event_count FROM issues WHERE id = $1 AND project_id = $2",
        issue_id,
        project_id,
    )
    if not issue_row:
        return None

    first_seen = issue_row["first_seen"]
    returned = False
    if first_seen and first_seen < deployed_at - timedelta(seconds=10):
        # An issue from an earlier release is only this deploy's regression if it had been
        # resolved and stayed quiet until this deploy: then it is back ("returned")
        returned = await _returned_after_resolution(conn, project_id, issue_id, deployed_at)
        if not returned:
            return None

    # 3. Check trigger conditions: critical routes or >= INSTANT_MIN_EVENTS in INSTANT_WINDOW_SEC
    proj_row = await conn.fetchrow(
        "SELECT critical_routes FROM projects WHERE id = $1",
        project_id,
    )
    raw_critical = proj_row["critical_routes"] if proj_row else []
    critical_routes = raw_critical if isinstance(raw_critical, list) else json.loads(raw_critical or "[]")

    request_path = payload.get("request_path") or ""
    is_critical_route = any(request_path.startswith(prefix) for prefix in critical_routes if prefix)

    min_events = getattr(settings, "INSTANT_MIN_EVENTS", 3)
    window_sec = getattr(settings, "INSTANT_WINDOW_SEC", 60)

    # Check recent event count in the last window_sec
    window_start = now_dt - timedelta(seconds=window_sec)
    recent_cnt_row = await conn.fetchrow(
        """
        SELECT COUNT(id) AS cnt
        FROM events
        WHERE project_id = $1 AND issue_id = $2 AND occurred_at >= $3
        """,
        project_id,
        issue_id,
        window_start,
    )
    recent_count = int(recent_cnt_row["cnt"]) if recent_cnt_row else 1

    should_open = is_critical_route or (recent_count >= min_events)

    # A mute rule (fingerprint, path prefix or exception type) silences alerts; mutes expire
    from app.trust.calm import is_issue_muted
    muted, mute_reason = await is_issue_muted(
        conn, project_id, issue_row["fingerprint"] or "", request_path, payload.get("exc_type") or ""
    )
    if muted:
        logger.info("Issue %d is muted (%s): no alert", issue_id, mute_reason)

    # 4. Check if incident already exists
    existing = await conn.fetchrow(
        """
        SELECT id, status, event_count, first_event_at, resolved_at
        FROM incidents
        WHERE deploy_id = $1 AND issue_id = $2 AND project_id = $3
        """,
        deploy_id,
        issue_id,
        project_id,
    )

    if existing:
        inc_id = int(existing["id"])
        current_status = existing["status"]
        cur_count = int(existing["event_count"]) + new_events

        # Check upgrade condition: possible -> confirmed at >= CONFIRM_MIN_EVENTS (default 10)
        confirm_min = getattr(settings, "CONFIRM_MIN_EVENTS", 10)
        if current_status == "resolved" and should_open and not muted:
            # It was resolved (often automatically) and is firing again: reopen and tell the team
            await conn.execute(
                """
                UPDATE incidents
                SET status = 'confirmed', confirmed_at = COALESCE(confirmed_at, now()), resolved_at = NULL,
                    event_count = $1, last_event_at = $2
                WHERE id = $3 AND project_id = $4
                """,
                cur_count,
                occurred_at_dt,
                inc_id,
                project_id,
            )
            logger.info("Reopened incident %d: the error is back after it was resolved", inc_id)
            from app.trust.actions import record_incident_event

            await record_incident_event(
                conn, project_id, inc_id, "reopened", actor="changewatch",
                detail={"reason": "error_returned", "events_last_minute": recent_count},
                occurred_at=occurred_at_dt,
            )
            inc_full = await conn.fetchrow(
                "SELECT title, where_file, where_line, where_function, in_diff, owners FROM incidents WHERE id = $1",
                inc_id,
            )
            resolved_at = existing["resolved_at"]
            await dispatch_incident_alert(
                conn=conn,
                project_id=project_id,
                incident_id=inc_id,
                status="confirmed",
                commit_sha=dep_row["commit_sha"],
                author=dep_row["author"] or "Unknown",
                message=dep_row["message"] or "",
                error_title=inc_full["title"] or "Error",
                event_count=recent_count,
                where_file=inc_full["where_file"] or "unknown",
                where_line=inc_full["where_line"] or 0,
                where_function=inc_full["where_function"] or "unknown",
                in_diff=bool(inc_full["in_diff"]),
                raw_owners=json.loads(inc_full["owners"]) if inc_full["owners"] else [],
                triage_info={"returned": True},
                # A new message, not a repeat of the first "confirmed" alert (outbox dedupe)
                dedupe_suffix=f":reopened:{int(resolved_at.timestamp()) if resolved_at else 0}",
            )
        elif current_status == "possible" and cur_count >= confirm_min and not muted:
            await conn.execute(
                """
                UPDATE incidents
                SET status = 'confirmed', confirmed_at = now(), event_count = $1, last_event_at = $2
                WHERE id = $3 AND project_id = $4
                """,
                cur_count,
                occurred_at_dt,
                inc_id,
                project_id,
            )
            logger.info("Upgraded incident %d to CONFIRMED (event_count=%d)", inc_id, cur_count)
            # Re-dispatch confirmed alert
            # Fetch details for dispatch
            inc_full = await conn.fetchrow(
                "SELECT title, where_file, where_line, where_function, in_diff, owners FROM incidents WHERE id = $1",
                inc_id,
            )
            owners_list = json.loads(inc_full["owners"]) if inc_full and inc_full["owners"] else []
            first_event_at_ts = existing["first_event_at"].timestamp() if existing["first_event_at"] else None

            await dispatch_incident_alert(
                conn=conn,
                project_id=project_id,
                incident_id=inc_id,
                status="confirmed",
                commit_sha=dep_row["commit_sha"],
                author=dep_row["author"] or "Unknown",
                message=dep_row["message"] or "",
                error_title=inc_full["title"] or "Error",
                event_count=cur_count,
                where_file=inc_full["where_file"] or "unknown",
                where_line=inc_full["where_line"] or 0,
                where_function=inc_full["where_function"] or "unknown",
                in_diff=bool(inc_full["in_diff"]),
                raw_owners=owners_list,
                first_event_at=first_event_at_ts,
            )
        else:
            await conn.execute(
                "UPDATE incidents SET event_count = event_count + $3, last_event_at = $1 WHERE id = $2",
                occurred_at_dt,
                inc_id,
                new_events,
            )
        return inc_id

    if not should_open:
        return None

    # 5. Open new incident
    raw_trace = payload.get("stack_trace") or ""
    frames = parse_stack_trace(raw_trace)

    # Fetch deploy files to check in_diff and resolve owners
    df_rows = await conn.fetch(
        "SELECT path, status, added_ranges, authors FROM deploy_files WHERE deploy_id = $1 AND project_id = $2",
        deploy_id,
        project_id,
    )
    deploy_paths = [r["path"] for r in df_rows]

    frame = innermost_project_frame(frames)
    if frame is not None and not is_project_frame(frame):
        # Every frame looks like a library (the app is installed as a package): the innermost
        # frame in a file this deploy changed is the better guess
        frame = next((f for f in reversed(frames) if any(_same_file(f.file, p) for p in deploy_paths)), frame)
    where_file = frame.file if frame else "unknown"
    where_line = frame.line if frame else 0
    where_function = frame.function if frame else "unknown"

    # Stack frames carry server paths (/srv/app/app/billing.py); CODEOWNERS rules and deploy files
    # are repository paths. The repository's place on the server is learned from any frame that
    # names a changed file, so even an unchanged file gets its repository path and owners.
    rel_path = _repo_relative(frames, frame, deploy_paths) if frame else None
    deploy_files = []
    file_authors = []
    repo_path: Optional[str] = None
    for r in df_rows:
        ranges = r["added_ranges"]
        parsed_ranges = ranges if isinstance(ranges, list) else json.loads(ranges or "[]")
        df_dict = {
            "path": r["path"],
            "status": r["status"],
            "added_ranges": parsed_ranges,
        }
        deploy_files.append(df_dict)
        if rel_path is not None:
            matched = r["path"].replace("\\", "/").lstrip("/") == rel_path
        else:
            # The longest matching path wins, so a same-named file elsewhere does not
            matched = bool(where_file) and _same_file(where_file, r["path"]) and (
                repo_path is None or len(r["path"]) > len(repo_path))
        if matched:
            repo_path = r["path"]
            auths = r["authors"]
            file_authors = auths if isinstance(auths, list) else json.loads(auths or "[]")

    in_diff = frame_in_diff(frame, deploy_files) if frame else False
    codeowners_content = await decrypt_or_plain(
        project_id, "deploys", "codeowners", dep_row.get("codeowners_enc"), dep_row.get("codeowners")
    )
    owners = resolve_owners(repo_path or rel_path or where_file, codeowners_content, file_authors)
    if repo_path or rel_path:
        # Store the repo path, not the server path: alerts read "app/billing.py:42", hotspots group
        # by repo directory, and the investigator's exact-path file lookups find the file
        where_file = repo_path or rel_path

    # 6. Run Typed Decisions Triage (sub-second rules/fake fallback)
    triage_info: Dict[str, Any] = {}
    try:
        # Code goes to the decision provider only with DECISION_SEND_CODE: 20 lines around the frame
        code_excerpt: Optional[str] = None
        if settings.DECISION_SEND_CODE and repo_path and where_line:
            file_row = await conn.fetchrow(
                "SELECT content, content_enc FROM deploy_files WHERE deploy_id = $1 AND project_id = $2 AND path = $3",
                deploy_id,
                project_id,
                repo_path,
            )
            if file_row:
                text = await decrypt_or_plain(
                    project_id, "deploy_files", "content", file_row.get("content_enc"), file_row.get("content")
                )
                if text:
                    lines = text.splitlines()
                    start = max(0, where_line - 11)
                    code_excerpt = "\n".join(lines[start:start + 20])
        decision_state = build_decision_state(
            error_title=issue_row["title"],
            exc_type=payload.get("exc_type") or "Error",
            frames=[{"file": f.file, "line": f.line, "function": f.function} for f in frames],
            request_path=request_path,
            event_count=recent_count,
            in_diff=in_diff,
            is_critical=is_critical_route,
            commit_message=dep_row["message"],
            code_excerpt=code_excerpt,
            send_code=settings.DECISION_SEND_CODE,
        )
        triage_questions = get_triage_questions(owner_candidates=owners)
        t_start = time.monotonic()
        answers = await decide_with_fallback(decision_state, triage_questions, timeout_ms=800)
        t_latency = int((time.monotonic() - t_start) * 1000)

        reg_ans = answers.get(Q_REGRESSION)
        sev_ans = answers.get(Q_SEVERITY)
        cat_ans = answers.get(Q_CATEGORY)

        triage_info = {
            "regression_prob": reg_ans.probability if reg_ans else 0.5,
            "severity": sev_ans.score if sev_ans else 2.0,
            "category": cat_ans.choice if cat_ans else "logic_bug",
            "source": reg_ans.source if reg_ans else "rules",
        }
    except Exception as exc:
        logger.warning("Typed decision triage failed (continuing safely): %s", exc)
        answers = None
        decision_state = None
        t_latency = 0

    if returned:
        triage_info["returned"] = True
    category_str = triage_info.get("category", "logic_bug")

    # 7. Insert new incident record
    inc_row = await conn.fetchrow(
        """
        INSERT INTO incidents (
            project_id, deploy_id, issue_id, status, title,
            where_file, where_line, where_function, in_diff, owners,
            event_count, created_at, first_event_at, last_event_at, category
        )
        VALUES ($1, $2, $3, 'possible', $4, $5, $6, $7, $8, $9, $10, now(), $11, $12, $13)
        ON CONFLICT (deploy_id, issue_id) DO UPDATE SET
            event_count = incidents.event_count + $14,
            last_event_at = EXCLUDED.last_event_at
        RETURNING id, (xmax = 0) AS is_new
        """,
        project_id,
        deploy_id,
        issue_id,
        issue_row["title"],
        where_file,
        where_line,
        where_function,
        in_diff,
        json.dumps(owners),
        recent_count,
        first_event_dt or occurred_at_dt,
        occurred_at_dt,
        category_str,
        new_events,
    )
    if not inc_row:
        return None

    incident_id = int(inc_row["id"])
    is_new = bool(inc_row["is_new"])

    if not is_new:
        return incident_id

    # 8. Persist decisions if available (savepoint: a failure must not abort the transaction)
    if answers and decision_state:
        try:
            async with conn.transaction():
                await persist_decisions(
                    conn=conn,
                    project_id=project_id,
                    incident_id=incident_id,
                    state=decision_state,
                    answers=answers,
                    latency_ms=t_latency,
                )
        except Exception as exc:
            logger.warning("Failed persisting decisions for incident %d: %s", incident_id, exc)

        await meter_decision_call(conn, project_id, incident_id, answers)

    if muted:
        return incident_id

    # Memory: the closest past incident like this one and how it ended, for one line in the alert
    try:
        async with conn.transaction():
            from app.trust.memory import find_similar_incidents, seen_before_line

            similar = await find_similar_incidents(
                conn, project_id, exclude_incident_id=incident_id, issue_id=issue_id, where_file=where_file,
                where_function=where_function, title=issue_row["title"], before=now_dt, limit=1,
            )
            line = seen_before_line(similar)
            if line:
                triage_info["seen_before"] = line
    except Exception as exc:
        logger.warning("Incident %d: past incidents could not be checked: %s", incident_id, exc)

    # Runbooks: the team's steps for this kind of error, one line in the alert
    try:
        async with conn.transaction():
            from app.runbooks import find_runbooks, runbook_line

            line = runbook_line(await find_runbooks(
                conn, project_id, title=issue_row["title"], where_file=where_file, issue_id=issue_id,
                since=now_dt - timedelta(hours=1), limit=1,
            ))
            if line:
                triage_info["runbook"] = line
    except Exception as exc:
        logger.warning("Incident %d: runbooks could not be checked: %s", incident_id, exc)

    # 9. Dispatch instant alert
    await dispatch_incident_alert(
        conn=conn,
        project_id=project_id,
        incident_id=incident_id,
        status="possible",
        commit_sha=dep_row["commit_sha"],
        author=dep_row["author"] or "Unknown",
        message=dep_row["message"] or "",
        error_title=issue_row["title"],
        event_count=recent_count,
        where_file=where_file,
        where_line=where_line,
        where_function=where_function,
        in_diff=in_diff,
        raw_owners=owners,
        triage_info=triage_info,
        first_event_at=(first_event_dt or occurred_at_dt).timestamp(),
    )

    # 10. Enqueue AI investigation job
    job = {"project_id": project_id, "incident_id": incident_id, "deep_mode": False}
    if investigation_jobs is not None:
        investigation_jobs.append(job)
    else:
        await queue_investigations([job])

    return incident_id


async def queue_investigations(jobs: List[Dict[str, Any]]) -> None:
    """Queue AI investigation jobs; call only once their incidents are committed."""
    if not jobs:
        return
    try:
        from app.queue import get_redis
        redis_client = get_redis()
        if redis_client:
            for job in jobs:
                await redis_client.lpush("investigations", json.dumps(job))
    except Exception as q_exc:
        logger.warning("Investigation queue push failed: %s", q_exc)


async def auto_resolve_stale_incidents(conn: asyncpg.Connection, resolve_after_min: int = 30) -> int:
    """
    Mark incidents 'resolved' when no events for that issue arrive for resolve_after_min.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=resolve_after_min)
    result = await conn.execute(
        """
        UPDATE incidents
        SET status = 'resolved', resolved_at = now()
        WHERE status != 'resolved' AND last_event_at <= $1
        """,
        cutoff,
    )
    # Result format: "UPDATE N"
    parts = result.split()
    count = int(parts[-1]) if len(parts) >= 2 and parts[-1].isdigit() else 0
    if count > 0:
        logger.info("Auto-resolved %d incidents inactive for %d min", count, resolve_after_min)
    return count
