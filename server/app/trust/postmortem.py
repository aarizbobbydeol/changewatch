"""A postmortem draft for one incident, written from what ChangeWatch recorded (backlog item).

No AI call: every sentence comes from stored facts (deploy, errors, traffic, alerts, verdicts, the
investigation result, the audit events). Blameless: CODEOWNERS teams are named, people are not
(the commit author is left out). Nothing is sent anywhere; the draft is returned to the caller.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from app.crypto import decrypt_or_plain
from app.trust.timeline import build_incident_timeline


def _duration(seconds: Optional[float]) -> str:
    """'45 s', '12 min', '3 h 5 min', '2 d 4 h' (whole units, no rounding up)."""
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def _utc(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "unknown"


async def build_postmortem(conn: asyncpg.Connection, project_id: int, incident_id: int) -> Optional[Dict[str, Any]]:
    """{"markdown", "sections"} for the incident, or None if it is not this project's."""
    inc = await conn.fetchrow(
        """
        SELECT i.id, i.deploy_id, i.issue_id, i.status, i.title, i.where_file, i.where_line, i.where_function,
               i.in_diff, i.owners, i.event_count, i.category, i.created_at, i.confirmed_at, i.resolved_at,
               i.first_event_at, i.last_event_at, i.first_alert_ms,
               d.commit_sha, d.environment, d.deployed_at, d.message
        FROM incidents i
        JOIN deploys d ON d.id = i.deploy_id AND d.project_id = i.project_id
        WHERE i.id = $1 AND i.project_id = $2
        """,
        incident_id,
        project_id,
    )
    if not inc:
        return None
    timeline = await build_incident_timeline(conn, project_id, incident_id) or []

    sha = (inc["commit_sha"] or "")[:8]
    env = inc["environment"] or "prod"
    where = f"{inc['where_file']}:{inc['where_line']}" if inc["where_file"] else "an unknown location"
    owners = inc["owners"] if isinstance(inc["owners"], list) else json.loads(inc["owners"] or "[]")
    first_error = inc["first_event_at"] or inc["created_at"]
    last_error = inc["last_event_at"] or first_error
    resolved_at = inc["resolved_at"]

    # Impact: errors, duration, routes, and the error rate from the app's own request counts
    routes = await conn.fetch(
        """
        SELECT request_path, count(*) AS n FROM events
        WHERE project_id = $1 AND issue_id = $2 AND deploy_id = $3 AND request_path IS NOT NULL
        GROUP BY request_path ORDER BY n DESC LIMIT 3
        """,
        project_id, inc["issue_id"], inc["deploy_id"],
    )
    traffic = None
    if first_error and last_error:
        traffic = await conn.fetchrow(
            """
            SELECT coalesce(sum(requests), 0) AS requests, coalesce(sum(errors), 0) AS errors,
                   max(CASE WHEN requests > 0 THEN errors * 1000 / requests END) AS peak_per_mille
            FROM traffic
            WHERE project_id = $1 AND environment = $2 AND bucket >= $3 AND bucket <= $4
            """,
            project_id, env, first_error.replace(second=0, microsecond=0), last_error + timedelta(minutes=1),
        )

    # Cause and fix: the latest investigation, clearly labelled as AI or rules-based
    inv = await conn.fetchrow(
        """
        SELECT status, result, result_enc FROM investigations
        WHERE project_id = $1 AND incident_id = $2 AND status IN ('done', 'partial')
        ORDER BY id DESC LIMIT 1
        """,
        project_id, incident_id,
    )
    finding: Dict[str, Any] = {}
    if inv:
        raw = await decrypt_or_plain(project_id, "investigations", "result", inv.get("result_enc"), inv.get("result"))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        finding = raw if isinstance(raw, dict) else {}

    resolved_by = next((t for t in reversed(timeline) if t["event"] == "resolved"), None)
    verdicts = [t["detail"] for t in timeline if t["event"] == "verdict"]
    alert_seconds = inc["first_alert_ms"] / 1000 if inc["first_alert_ms"] is not None else None

    status_text = {"resolved": "Resolved", "confirmed": "Ongoing (confirmed)", "possible": "Ongoing (possible)"}.get(
        inc["status"], inc["status"])
    summary = (
        f"Deploy `{sha}` to {env} on {_utc(inc['deployed_at'])} was followed by `{inc['title']}` at `{where}`"
        + (" (a line changed in this release)" if inc["in_diff"] else " (code this release did not change)")
        + f". {inc['event_count'] or 0} errors over {_duration((last_error - first_error).total_seconds()) if first_error and last_error else 'an unknown time'}."
        + (f" ChangeWatch alerted {_duration(alert_seconds)} after the first error." if alert_seconds is not None else "")
        + (f" Resolved {_duration((resolved_at - first_error).total_seconds())} after the first error." if resolved_at and first_error else "")
    )

    impact: List[str] = [
        f"Errors: {inc['event_count'] or 0}",
        f"First error: {_utc(first_error)}",
        f"Last error: {_utc(last_error)}",
    ]
    if traffic and traffic["requests"]:
        rate = traffic["errors"] * 100 / traffic["requests"]
        impact.append(
            f"Requests during the incident: {traffic['requests']}, of which {traffic['errors']} failed ({rate:.1f}%);"
            f" worst minute {int(traffic['peak_per_mille'] or 0) / 10:.1f}% failed"
        )
    if routes:
        impact.append("Most affected routes: " + ", ".join(f"`{r['request_path']}` ({r['n']})" for r in routes))
    from app.costs import incident_cost

    cost = await incident_cost(conn, project_id, incident_id)
    if cost and cost["estimated_impact"]:
        impact.append(f"Estimated impact: {cost['estimated_impact']} ({cost['failed_requests']} failed requests at "
                      f"{cost['value_per_failed_request']} each, the project's setting)")
    if cost and cost["investigation_cost_micros"]:
        impact.append(f"AI investigation cost: {cost['investigation_cost']}")

    detection: List[str] = []
    if alert_seconds is not None:
        detection.append(f"Time from the first error to the alert: {_duration(alert_seconds)}")
    if inc["confirmed_at"] and first_error:
        detection.append(f"Confirmed after {_duration((inc['confirmed_at'] - first_error).total_seconds())}")
    detection.extend(verdicts)

    cause_lines: List[str] = []
    if finding:
        source = "AI investigation" if inv and inv["status"] == "done" else "Rules-based analysis (the AI was unavailable)"
        cause_lines.append(f"_{source}, confidence {finding.get('confidence', 'unknown')}. Check it before publishing._")
        if finding.get("cause"):
            cause_lines.append("")
            cause_lines.append(str(finding["cause"]))
        evidence = finding.get("evidence") or []
        if evidence:
            cause_lines.append("")
            cause_lines.append("Evidence:")
            cause_lines.extend(f"- [{e.get('kind', 'evidence')}] {e.get('detail', '')}" for e in evidence[:5] if isinstance(e, dict))
    else:
        cause_lines.append("_No investigation result yet. Describe the root cause here._")

    if resolved_by:
        resolution = resolved_by["detail"]
    elif inc["status"] == "resolved":
        resolution = "Resolved."
    else:
        resolution = "Not resolved yet."

    actions: List[str] = []
    fix = (finding.get("suggested_fix") or {}) if finding else {}
    if fix.get("description"):
        actions.append(f"Fix: {fix['description']}")
    exc = (inc["title"] or "Error").split(":", 1)[0]
    if inc["in_diff"]:
        actions.append(f"Add a test that covers `{where}` ({inc['where_function'] or 'this code'}) for the case that raised {exc}.")
    else:
        actions.append(f"Find out why `{inc['where_file'] or 'this code'}` failed although the deploy did not change it (data, configuration, a dependency).")
    if alert_seconds is not None and alert_seconds > 300:
        actions.append("Alerting took over 5 minutes: check that the ChangeWatch SDK runs in every process of the app.")
    if any("ROLLBACK" in v for v in verdicts) and resolved_at and first_error and (resolved_at - first_error) > timedelta(minutes=30):
        actions.append("The verdict said ROLLBACK: consider rolling back automatically on it (GET /v1/verdict in the deploy pipeline).")
    if not owners:
        actions.append(f"Add a CODEOWNERS rule for `{inc['where_file'] or 'this path'}` so the next alert reaches its team.")

    # The team's runbook for this error, or an action item to write one
    from app.runbooks import exception_type, find_runbooks

    runbooks = await find_runbooks(
        conn, project_id, title=inc["title"], where_file=inc["where_file"], issue_id=inc["issue_id"],
        since=(first_error or inc["created_at"]) - timedelta(hours=1), limit=1,
    )
    if runbooks:
        resolution += f"\n\nRunbook for this error: {runbooks[0]['title']}" + (f" ({runbooks[0]['url']})" if runbooks[0]["url"] else "")
    else:
        actions.append(f"Write a runbook for `{exception_type(inc['title']) or 'this error'}` in `{inc['where_file'] or 'this code'}`"
                       " (Settings → Runbooks), so the next alert links to the steps.")

    # Times as HH:MM:SS on the first day, with the date when the incident ran past midnight
    day0 = timeline[0]["at"][:10] if timeline else ""
    timeline_lines = [
        f"- {t['at'][11:19]} {t['detail']}" if t["at"][:10] == day0 else f"- {t['at'][:10]} {t['at'][11:19]} {t['detail']}"
        for t in timeline
    ]

    md: List[str] = [
        f"# Postmortem (draft): {inc['title']}",
        "",
        f"**Status:** {status_text} · **Environment:** {env} · **Deploy:** `{sha}` · "
        f"**Owners:** {', '.join(owners) if owners else 'none in CODEOWNERS'} · **Category:** {inc['category'] or 'unknown'}",
        "",
        "> Draft generated by ChangeWatch from its own records. It is blameless: it names teams from",
        "> CODEOWNERS, not people. Review and edit before sharing.",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Impact",
        "",
        *[f"- {line}" for line in impact],
        "",
        "## Detection",
        "",
        *([f"- {line}" for line in detection] or ["- No alert or verdict recorded."]),
        "",
        "## Timeline (UTC)",
        "",
        *([f"All times on {day0} unless a date is shown.", ""] if day0 else []),
        *(timeline_lines or ["- Nothing recorded."]),
        "",
        "## Root cause",
        "",
        *cause_lines,
        "",
        "## Resolution",
        "",
        resolution,
        "",
        "## Action items",
        "",
        *[f"- [ ] {a}" for a in actions],
        "- [ ] Review this draft and add what ChangeWatch could not know (customer impact, decisions taken).",
        "",
    ]
    return {
        "incident_id": incident_id,
        "draft": True,
        "markdown": "\n".join(md),
        "sections": {
            "summary": summary,
            "impact": impact,
            "detection": detection,
            "timeline": timeline,
            "root_cause": cause_lines,
            "resolution": resolution,
            "action_items": actions,
        },
    }
