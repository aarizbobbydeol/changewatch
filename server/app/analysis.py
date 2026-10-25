import logging
from typing import Any, Dict, List, Optional
import asyncpg
from app.crypto import decrypt_or_plain
from app.llm import get_llm_client
from app.outbox import queue_notification
from app.scrub import scrub_text

logger = logging.getLogger("changewatch.analysis")


async def generate_likely_cause(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
) -> Optional[str]:
    """
    Generate AI root-cause analysis for a flagged deploy using configured LLMClient.
    Scrubs all secrets from diff and traces before calling the LLM.
    Returns short explanation under 80 words, or None if LLM is unavailable.
    """
    # Check budget before running optional AI investigation (Usage Metering Addendum Section E)
    from app.metering import Meter, can_spend
    allowed, reason = await can_spend(conn, project_id, Meter.AI_INVESTIGATION)
    if not allowed:
        # Not stored as the "likely cause": it is not one, and it would stay after the budget resets
        logger.info("AI investigation skipped for deploy %d (project %d): %s", deploy_id, project_id, reason)
        return None

    # 1. Fetch deploy information
    dep_row = await conn.fetchrow(
        """
        SELECT commit_sha, author, message, diff, diff_enc, deployed_at
        FROM deploys
        WHERE id = $1 AND project_id = $2
        """,
        deploy_id,
        project_id,
    )
    if not dep_row:
        return None

    commit_sha = dep_row["commit_sha"]
    author = dep_row["author"] or "Unknown"
    message = dep_row["message"] or ""
    raw_diff = await decrypt_or_plain(project_id, "deploys", "diff", dep_row.get("diff_enc"), dep_row.get("diff")) or ""
    deployed_at = dep_row["deployed_at"]

    # 2. Fetch top 3 issues with event counts and sample stack traces
    issues = await conn.fetch(
        """
        SELECT i.title, COUNT(e.id) AS count,
               s.stack_trace AS sample_trace, s.stack_trace_enc AS sample_trace_enc
        FROM issues i
        JOIN events e ON e.issue_id = i.id
        LEFT JOIN LATERAL (
          SELECT e2.stack_trace, e2.stack_trace_enc
          FROM events e2
          WHERE e2.issue_id = i.id AND (e2.stack_trace IS NOT NULL OR e2.stack_trace_enc IS NOT NULL)
          ORDER BY e2.occurred_at DESC
          LIMIT 1
        ) s ON true
        WHERE e.project_id = $1 AND (e.deploy_id = $2 OR e.occurred_at >= $3)
        GROUP BY i.id, i.title, s.stack_trace, s.stack_trace_enc
        ORDER BY count DESC
        LIMIT 3
        """,
        project_id,
        deploy_id,
        deployed_at,
    )

    clean_diff = scrub_text(raw_diff[:15000])

    # Format issues section
    issues_text = ""
    for idx, iss in enumerate(issues, start=1):
        clean_title = scrub_text(iss["title"] or "Error")
        sample_trace = await decrypt_or_plain(
            project_id, "events", "stack_trace", iss.get("sample_trace_enc"), iss.get("sample_trace")
        )
        clean_trace = scrub_text((sample_trace or "")[:1500])
        issues_text += f"\n--- Error #{idx} ({iss['count']} events): {clean_title} ---\n{clean_trace}\n"

    system_prompt = (
        "You are an automated release SRE. In under 80 words, answer: "
        "(1) which change in the git diff most likely caused the errors, naming the specific file or function; "
        "(2) one suggested fix. "
        "If the diff does not explain the errors, clearly say so instead of guessing."
    )

    user_prompt = (
        f"Deploy commit: {commit_sha[:8]}\n"
        f"Author: {author}\n"
        f"Message: {message}\n\n"
        f"Git Diff:\n{clean_diff}\n\n"
        f"Top Errors since deploy:\n{issues_text}"
    )

    try:
        llm = get_llm_client()
        answer = await llm.complete(
            system=system_prompt,
            prompt=user_prompt,
            # Room for the model's thinking as well as the short answer
            max_tokens=4000,
        )
        return answer.strip() if answer else None
    except Exception as exc:
        logger.warning(
            "LLM analysis failed for deploy %d (AI explanation will be omitted): %s",
            deploy_id,
            exc,
        )
        return None


def format_slack_blocks(
    project_name: str,
    verdict: str,
    reasons: List[str],
    commit_sha: str,
    author: str,
    message: str,
    likely_cause: Optional[str] = None,
    top_errors: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build Slack block kit message structure for release verdict alerts."""
    emoji_map = {
        "safe": "✅",
        "watch": "⚠️",
        "rollback": "🚨",
        "no_data": "❓",
    }
    emoji = emoji_map.get(verdict.lower(), "📢")
    header_title = f"{emoji} Release Verdict: {verdict.upper()} ({project_name})"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_title, "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Commit:*\n`{commit_sha[:8]}`"},
                {"type": "mrkdwn", "text": f"*Author:*\n{author}"},
                {"type": "mrkdwn", "text": f"*Message:*\n{message[:150]}"},
                {"type": "mrkdwn", "text": f"*Status:*\n*{verdict.upper()}*"},
            ],
        },
    ]

    # Reasons section
    if reasons:
        reasons_md = "\n".join(f"• {r}" for r in reasons)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reasons:*\n{reasons_md}"},
        })

    # AI Likely cause section
    if likely_cause:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Root Cause Analysis (AI):*\n{likely_cause}"},
        })

    # Top errors
    if top_errors:
        err_lines = [f"• `{e['count']}x` {e['title'][:80]}" for e in top_errors]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Top Errors:*\n" + "\n".join(err_lines)},
        })

    fallback_text = f"{emoji} [{project_name}] Deploy {commit_sha[:8]} verdict: {verdict.upper()}"
    return {"text": fallback_text, "blocks": blocks}


async def dispatch_verdict_alert(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: int,
    verdict: str,
    reasons: List[str],
    kind: str,
    likely_cause: Optional[str] = None,
) -> bool:
    """
    Queue a verdict alert into the durable outbox and record in alerts table.
    Ensures idempotency via UNIQUE (deploy_id, kind).
    """
    # 1. Fetch project name and deploy metadata
    row = await conn.fetchrow(
        """
        SELECT p.name AS project_name, d.commit_sha, d.author, d.message
        FROM projects p
        JOIN deploys d ON d.project_id = p.id
        WHERE p.id = $1 AND d.id = $2
        """,
        project_id,
        deploy_id,
    )
    if not row:
        return False

    project_name = row["project_name"]
    commit_sha = row["commit_sha"]
    author = row["author"] or "Unknown"
    message = row["message"] or ""

    # 2. Fetch top errors summary
    err_rows = await conn.fetch(
        """
        SELECT i.title, COUNT(e.id) AS count
        FROM issues i
        JOIN events e ON e.issue_id = i.id
        WHERE e.deploy_id = $1
        GROUP BY i.id, i.title
        ORDER BY count DESC
        LIMIT 3
        """,
        deploy_id,
    )
    top_errors = [{"title": r["title"] or "Error", "count": r["count"]} for r in err_rows]

    # 3. Format Slack payload
    payload = format_slack_blocks(
        project_name=project_name,
        verdict=verdict,
        reasons=reasons,
        commit_sha=commit_sha,
        author=author,
        message=message,
        likely_cause=likely_cause,
        top_errors=top_errors,
    )

    # The deploy's risk review (rules, plus AI when available): its most serious finding
    try:
        async with conn.transaction():
            from app.review import get_deploy_review, review_line

            line = review_line(await get_deploy_review(conn, project_id, deploy_id))
        if line:
            payload["text"] = f"{payload.get('text', '')} {line}".strip()
            payload.setdefault("blocks", []).append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{line}*"}})
    except Exception as exc:
        logger.warning("Deploy %d: risk review not added to the verdict: %s", deploy_id, exc)

    # On ROLLBACK: how to undo it, as text to copy (ChangeWatch runs nothing)
    if verdict == "rollback":
        try:
            async with conn.transaction():
                from app.fixes import deploy_undo, undo_line

                line = undo_line(await deploy_undo(conn, project_id, deploy_id))
            if line:
                payload["text"] = f"{payload.get('text', '')} {line}".strip()
                payload.setdefault("blocks", []).append({"type": "section", "text": {"type": "mrkdwn", "text": line}})
        except Exception as exc:
            logger.warning("Deploy %d: undo commands not added to the verdict: %s", deploy_id, exc)

    dedupe_key = f"alert:{deploy_id}:{kind}"

    # 4. Insert into alerts table (unique constraint deploy_id, kind)
    alert_row = await conn.fetchrow(
        """
        INSERT INTO alerts (project_id, deploy_id, channel, kind)
        VALUES ($1, $2, 'slack', $3)
        ON CONFLICT (deploy_id, kind) DO NOTHING
        RETURNING id
        """,
        project_id,
        deploy_id,
        kind,
    )
    if not alert_row:
        logger.debug("Alert already exists for deploy %d kind %s; skipping", deploy_id, kind)
        return False

    # 5. Queue into durable outbox for reliable delivery
    queued = await queue_notification(
        conn=conn,
        project_id=project_id,
        dedupe_key=dedupe_key,
        payload=payload,
    )
    return queued
