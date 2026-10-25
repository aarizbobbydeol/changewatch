"""Incident Alert Formatting and Dispatch.

Specified in AI SRE Prompt Stage B3 & B4.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
import asyncpg

from app.config import settings
from app.outbox import queue_notification

logger = logging.getLogger("changewatch.notifiers.incident")


def resolve_team_mentions(
    owners: List[str],
    team_members: List[Dict[str, Any]],
    channel_type: str = "slack",
) -> List[str]:
    """
    Map owner handles/authors/emails to channel mentions using team_members records.
    Unknown people appear as plain text names.
    """
    if not owners:
        return ["Unassigned"]

    mentions: List[str] = []
    for owner in owners:
        owner_clean = owner.lstrip("@").lower()
        matched = False

        for tm in team_members:
            tm_name = tm.get("name", "").lower()
            tm_email = (tm.get("email") or "").lower()
            git_emails = [e.lower() for e in tm.get("git_emails") or []]
            slack_id = tm.get("slack_user_id")
            discord_id = tm.get("discord_user_id")

            if (
                owner_clean == tm_name
                or owner_clean == tm_email
                or owner_clean in git_emails
                or (slack_id and owner_clean == slack_id.lower())
            ):
                if channel_type == "slack" and slack_id:
                    mentions.append(f"<@{slack_id}>")
                    matched = True
                    break
                elif channel_type == "discord" and discord_id:
                    mentions.append(f"<@{discord_id}>")
                    matched = True
                    break
                else:
                    mentions.append(tm.get("name") or owner)
                    matched = True
                    break

        if not matched:
            mentions.append(owner)

    return mentions


def format_incident_instant_message(
    status: str,  # possible | confirmed
    commit_sha: str,
    author: str,
    message: str,
    error_title: str,
    event_count: int,
    where_file: str,
    where_line: int,
    where_function: str,
    in_diff: bool,
    owners: List[str],
    dashboard_url: str,
    triage_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build structured instant alert message without AI (Stage B3).
    """
    is_confirmed = status == "confirmed"
    badge = "🚨 *Confirmed regression*" if is_confirmed else "⚠️ *Possible regression*"
    diff_note = (
        "*(this line was changed in this release)*"
        if in_diff
        else "*(this code was not changed in this release, the cause may be elsewhere)*"
    )

    where_text = f"`{where_file}:{where_line}` in `{where_function}`\n{diff_note}"
    returned = bool(triage_info and triage_info.get("returned"))
    if returned:
        where_text += "\n*This error was resolved before and is back after this deploy.*"
    seen_before = (triage_info or {}).get("seen_before")
    if seen_before:
        where_text += f"\n_{seen_before}_"
    owners_text = ", ".join(owners) if owners else "Unassigned"
    msg_preview = message.splitlines()[0][:120] if message else "No commit message"

    triage_text = ""
    if triage_info:
        prob = triage_info.get("regression_prob")
        sev = triage_info.get("severity")
        cat = triage_info.get("category")
        src = triage_info.get("source", "rules")
        parts = []
        if prob is not None:
            parts.append(f"regression prob: `{prob:.2f}`")
        if sev is not None:
            parts.append(f"severity: `{sev:.1f}`")
        if cat:
            parts.append(f"category: `{cat}`")
        if parts:
            triage_text = f"\n*Triage ({src}):* " + ", ".join(parts)

    slack_blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{'🚨' if is_confirmed else '⚠️'} {status.capitalize()} Regression ({commit_sha[:8]})",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Status:*\n{badge}"},
                {"type": "mrkdwn", "text": f"*Commit:*\n`{commit_sha[:8]}` by {author}"},
                {"type": "mrkdwn", "text": f"*Error:*\n`{error_title[:100]}` ({event_count}x)"},
                {"type": "mrkdwn", "text": f"*Owners:*\n{owners_text}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Where:*\n{where_text}{triage_text}"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Deploy message: _{msg_preview}_ | <{dashboard_url}|View Incident>"}
            ],
        },
    ]

    fallback_text = (
        f"{badge}: {error_title[:80]} ({event_count}x) at {where_file}:{where_line} {diff_note} "
        f"after deploy {commit_sha[:8]} by {author}. "
        + ("This error was resolved before and is back. " if returned else "")
        + (f"{seen_before} " if seen_before else "")
        + f"Owners: {owners_text}. Link: {dashboard_url}"
    )


    return {
        "text": fallback_text,
        "blocks": slack_blocks,
        "details": {
            "title": f"{status.capitalize()} regression after {commit_sha[:8]}",
            "description": f"{error_title[:100]} ({event_count} occurrences)",
            "where": f"{where_file}:{where_line} ({'in diff' if in_diff else 'outside diff'})",
            "owners": owners_text,
            "status": status,
            "link": dashboard_url,
        },
    }


async def dispatch_incident_alert(
    conn: asyncpg.Connection,
    project_id: int,
    incident_id: int,
    status: str,
    commit_sha: str,
    author: str,
    message: str,
    error_title: str,
    event_count: int,
    where_file: str,
    where_line: int,
    where_function: str,
    in_diff: bool,
    raw_owners: List[str],
    triage_info: Optional[Dict[str, Any]] = None,
    first_event_at: Optional[float] = None,
    dedupe_suffix: str = "",
) -> bool:
    """
    Format and enqueue incident alert to all enabled notification channels.
    Respects project.notify_quiet setting. `dedupe_suffix` makes a second alert of the same
    status (a reopened incident) a new message instead of a duplicate of the first.
    """
    # 1. Check project notify_quiet setting
    proj_row = await conn.fetchrow(
        "SELECT name, notify_quiet FROM projects WHERE id = $1",
        project_id,
    )
    if not proj_row:
        return False

    notify_quiet = bool(proj_row["notify_quiet"])
    # If notify_quiet is active, only post for confirmed incidents
    if notify_quiet and status == "possible":
        logger.info(
            "Incident %d (project %d) alert suppressed due to NOTIFY_QUIET=true for status=possible",
            incident_id,
            project_id,
        )
        return False

    # 2. Fetch team members for mention resolution
    member_rows = await conn.fetch(
        "SELECT name, git_emails, slack_user_id, discord_user_id, email FROM team_members WHERE project_id = $1",
        project_id,
    )
    team_members = [dict(r) for r in member_rows]

    # 3. Calculate latency from first matching event
    now_ts = time.time()
    first_alert_ms = int((now_ts - first_event_at) * 1000) if first_event_at else None

    # 4. Fetch enabled notification channels
    channel_rows = await conn.fetch(
        "SELECT id, type, config FROM notification_channels WHERE project_id = $1 AND enabled = true",
        project_id,
    )

    dashboard_url = settings.dashboard_link(f"/incidents/{incident_id}")

    # Record the first alert only: the later "confirmed" alert must not overwrite time-to-alert
    await conn.execute(
        """
        UPDATE incidents
        SET alerted_at = COALESCE(alerted_at, now()), first_alert_ms = COALESCE(first_alert_ms, $1)
        WHERE id = $2 AND project_id = $3
        """,
        first_alert_ms,
        incident_id,
        project_id,
    )

    # If no channels configured, still write to default outbox record for web UI / test inspection
    if not channel_rows:
        resolved_owners = resolve_team_mentions(raw_owners, team_members, channel_type="slack")
        payload = format_incident_instant_message(
            status=status,
            commit_sha=commit_sha,
            author=author,
            message=message,
            error_title=error_title,
            event_count=event_count,
            where_file=where_file,
            where_line=where_line,
            where_function=where_function,
            in_diff=in_diff,
            owners=resolved_owners,
            dashboard_url=dashboard_url,
            triage_info=triage_info,
        )
        dedupe_key = f"incident:{incident_id}:{status}{dedupe_suffix}:default"
        await queue_notification(
            conn=conn,
            project_id=project_id,
            dedupe_key=dedupe_key,
            payload=payload,
            fan_out=False,  # channels were just checked: there are none
        )
        return True

    # 5. Queue into durable outbox for each enabled channel
    for ch in channel_rows:
        ch_id = ch["id"]
        ch_type = ch["type"]
        resolved_owners = resolve_team_mentions(raw_owners, team_members, channel_type=ch_type)

        payload = format_incident_instant_message(
            status=status,
            commit_sha=commit_sha,
            author=author,
            message=message,
            error_title=error_title,
            event_count=event_count,
            where_file=where_file,
            where_line=where_line,
            where_function=where_function,
            in_diff=in_diff,
            owners=resolved_owners,
            dashboard_url=dashboard_url,
            triage_info=triage_info,
        )

        dedupe_key = f"incident:{incident_id}:{status}{dedupe_suffix}:channel:{ch_id}"
        await queue_notification(
            conn=conn,
            project_id=project_id,
            channel_id=ch_id,
            dedupe_key=dedupe_key,
            payload=payload,
        )

    logger.info(
        "Incident %d instant alert queued across %d channels (latency: %s ms)",
        incident_id,
        len(channel_rows),
        first_alert_ms,
    )
    return True
