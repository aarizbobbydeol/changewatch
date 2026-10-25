"""The full story of one incident, in time order (backlog: incident timeline).

Merges what ChangeWatch stored in different places: the deploy, the first and last error, when the
incident opened and was confirmed, every alert delivered (outbox), the AI answer, the deploy's
verdicts, and the audit events (acknowledged, escalated, rated, resolved, reopened). Read-only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

# An alert goes out as one outbox row per channel: its key with this ending removed names the alert
_CHANNEL_SUFFIX = re.compile(r":(?:channel:\d+|default)$")
# The auto-resolve note names who deployed the fix: "likely fixed by deploy 1a2b3c4d by alice"
_FIX_AUTHOR = re.compile(r"^(likely fixed by deploy \S+) by .*$", re.DOTALL)

# Same moment: show the cause before the effect (deployed, then first error, then opened ...)
_ORDER = {
    "deployed": 0, "first_error": 1, "opened": 2, "alert_sent": 3, "investigated": 4, "confirmed": 5,
    "verdict": 6, "escalated": 7, "acked": 8, "assigned": 9, "feedback": 10, "resolved": 11,
    "reopened": 12, "last_error": 13,
}

_ALERT_KIND = {
    "possible": "Possible-regression alert",
    "confirmed": "Confirmed-regression alert",
    "resolved": "Resolved message",
}


def _first_line(text: Optional[str], limit: int = 80) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _event_text(etype: str, actor: str, detail: Dict[str, Any], blameless: bool = False) -> str:
    """One line for an audit event. Blameless (the postmortem): "a responder" instead of anyone's name."""
    if blameless:
        actor = "a responder"
    if etype == "acked":
        return f"Acknowledged by {actor}"
    if etype == "assigned":
        # The actor is who assigned; actions.py records the assignee as "assigned_to"
        return f"Assigned to {actor if blameless else detail.get('assigned_to') or detail.get('assignee') or actor}"
    if etype == "escalated":
        return f"Reminder {detail.get('level', 1)}: still unacknowledged"
    if etype == "resolved":
        if detail.get("auto_resolve"):
            note = detail.get("fix_note") or "no new errors while traffic kept flowing"
            if blameless:
                note = _FIX_AUTHOR.sub(r"\1", note)
            return f"Resolved automatically: {note}"
        return f"Resolved by {actor}"
    if etype == "reopened":
        if detail.get("reason") == "error_returned":
            return "Reopened: the error came back"
        return f"Reopened by {actor}"
    if etype == "feedback":
        label = {"real": "a real problem", "false": "a false alarm", "unclear": "unclear"}.get(detail.get("label"), "rated")
        return f"Rated {label} by {actor}"
    label = etype.replace("_", " ").capitalize()
    return label if blameless else f"{label} ({actor})"


async def build_incident_timeline(
    conn: asyncpg.Connection, project_id: int, incident_id: int, *, blameless: bool = False
) -> Optional[List[Dict[str, Any]]]:
    """[{"at", "event", "detail", "actor"}] in time order, or None if the incident is not this project's.

    Blameless (for the postmortem): no one's name in the text, and no "actor"."""
    inc = await conn.fetchrow(
        """
        SELECT i.id, i.deploy_id, i.issue_id, i.title, i.event_count, i.created_at, i.confirmed_at,
               i.resolved_at, i.first_event_at, i.last_event_at,
               d.commit_sha, d.deployed_at, d.environment, d.message
        FROM incidents i
        JOIN deploys d ON d.id = i.deploy_id AND d.project_id = i.project_id
        WHERE i.id = $1 AND i.project_id = $2
        """,
        incident_id,
        project_id,
    )
    if not inc:
        return None

    items: List[Dict[str, Any]] = []

    def add(at: Optional[datetime], event: str, detail: str, actor: Optional[str] = None) -> None:
        if at is not None:
            items.append({"at": at, "event": event, "detail": detail, **({} if blameless else {"actor": actor})})

    sha = (inc["commit_sha"] or "")[:8]
    message = _first_line(inc["message"])
    add(inc["deployed_at"], "deployed", f"Deploy {sha} to {inc['environment'] or 'prod'}" + (f": {message}" if message else ""))

    first_error = inc["first_event_at"] or await conn.fetchval(
        "SELECT min(occurred_at) FROM events WHERE project_id = $1 AND issue_id = $2 AND deploy_id = $3",
        project_id, inc["issue_id"], inc["deploy_id"],
    )
    add(first_error, "first_error", f"First error: {inc['title'] or 'unknown error'}")
    add(inc["created_at"], "opened", "Incident opened")

    # Alerts: when each one was first delivered and to how many channels, or what happened to it.
    # Each reopen and each resolution sends its own alert (the key carries its time)
    outbox_rows = await conn.fetch(
        """
        SELECT dedupe_key, status, created_at, sent_at
        FROM outbox
        WHERE project_id = $1 AND dedupe_key LIKE $2
        ORDER BY created_at
        """,
        project_id,
        f"incident:{incident_id}:%",
    )
    by_alert: Dict[str, List[Any]] = {}
    for row in outbox_rows:
        by_alert.setdefault(_CHANNEL_SUFFIX.sub("", row["dedupe_key"]), []).append(row)
    for key, rows in by_alert.items():
        parts = key.split(":")
        kind = parts[2] if len(parts) > 2 else "alert"
        if kind == "reopened" or (len(parts) > 3 and parts[3] == "reopened"):
            kind = "reopened_alert"
        label = _ALERT_KIND.get(kind, "Alert" if kind != "reopened_alert" else "Alert: the error is back")
        sent = [r for r in rows if r["status"] == "sent"]
        if sent:
            at = min(r["sent_at"] or r["created_at"] for r in sent)
            add(at, "alert_sent", f"{label} delivered to {len(sent)} channel{'s' if len(sent) != 1 else ''}")
        elif any(r["status"] == "failed" for r in rows):
            add(rows[0]["created_at"], "alert_sent", f"{label} could not be delivered")
        else:
            add(rows[0]["created_at"], "alert_sent", f"{label} waiting to be delivered")

    for inv in await conn.fetch(
        "SELECT status, finished_at FROM investigations WHERE project_id = $1 AND incident_id = $2 ORDER BY id",
        project_id, incident_id,
    ):
        if inv["status"] == "done":
            add(inv["finished_at"], "investigated", "AI investigation finished")
        elif inv["status"] == "partial":
            add(inv["finished_at"], "investigated", "Rules-based explanation sent (AI unavailable)")

    add(inc["confirmed_at"], "confirmed", "Confirmed regression")

    for v in await conn.fetch(
        """
        SELECT a.kind, a.sent_at, da.first_verdict, da.final_verdict, da.verdict
        FROM alerts a
        LEFT JOIN deploy_analysis da ON da.deploy_id = a.deploy_id
        WHERE a.project_id = $1 AND a.deploy_id = $2
        """,
        project_id, inc["deploy_id"],
    ):
        if v["kind"] == "verdict_first":
            verdict = v["first_verdict"] or v["verdict"]
            add(v["sent_at"], "verdict", f"Release verdict: {(verdict or 'unknown').upper()} (first check)")
        elif v["kind"] == "verdict_change":
            verdict = v["final_verdict"] or v["verdict"]
            add(v["sent_at"], "verdict", f"Release verdict changed to {(verdict or 'unknown').upper()} (final check)")
        elif v["kind"] == "verdict_final":
            # Recorded without a message: the final check agreed with the first
            verdict = v["final_verdict"] or v["first_verdict"] or v["verdict"]
            add(v["sent_at"], "verdict", f"Final check: verdict stays {(verdict or 'unknown').upper()}")

    has_resolved_event = False
    for ev in await conn.fetch(
        "SELECT type, actor, detail, at FROM incident_events WHERE project_id = $1 AND incident_id = $2 ORDER BY at, id",
        project_id, incident_id,
    ):
        detail = ev["detail"]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                detail = {}
        detail = detail or {}
        etype = ev["type"]
        has_resolved_event = has_resolved_event or etype == "resolved"
        add(ev["at"], etype, _event_text(etype, ev["actor"] or "someone", detail, blameless), ev["actor"])

    if inc["resolved_at"] and not has_resolved_event:
        add(inc["resolved_at"], "resolved", "Resolved")
    if inc["last_event_at"] and (first_error is None or inc["last_event_at"] > first_error):
        add(inc["last_event_at"], "last_error", f"Last error seen ({inc['event_count'] or 0} in total)")

    items.sort(key=lambda it: (it["at"], _ORDER.get(it["event"], 50)))
    return [{**it, "at": it["at"].astimezone(timezone.utc).isoformat()} for it in items]
