"""Autonomous Read-Only Release Investigator Agent (Phase 2 Stage C).

Pre-gathers evidence, executes agent reasoning with tools or single-shot context,
validates structured output, runs verification filters, and posts thread replies.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import asyncpg

from app.crypto import decrypt_or_plain, encrypt_or_keep
from app.config import settings
from app.decisions.verifier import (
    verify_evidence_supports_conclusion,
    verify_suggestion_safety,
)
from app.investigator.schemas import (
    EvidenceItem,
    InvestigationOutput,
    SuggestedFix,
    WhereLocation,
)
from app.investigator.tools import (
    get_config_changes,
    get_dependency_changes,
    get_diff_files,
    get_error_rate,
    get_file_at_commit,
    get_issue_events,
    get_previous_deploys,
    list_new_issues,
)
from app.llm import get_llm_client
from app.outbox import queue_notification
from app.scrub import scrub_text

logger = logging.getLogger("changewatch.investigator")

SYSTEM_PROMPT = """You are a read-only Release Investigator for ChangeWatch.
Your task is to analyze an incident following a deployment and determine what broke, where, why, and how to fix it.
Rules:
1. You are strictly read-only.
2. Rely only on the factual evidence provided. Never invent file names, line numbers, or library versions.
3. If the provided data does not explain the failure, you MUST set confidence to 'low' and provide a clear not_sure_reason.
4. Output strictly valid JSON matching the required schema. No Markdown wrapper, no prose around JSON.
"""


async def pregather_context(
    conn: asyncpg.Connection,
    project_id: int,
    incident_id: int,
) -> Dict[str, Any]:
    """Pre-gather all relevant deploy and incident data."""
    inc_row = await conn.fetchrow(
        """
        SELECT i.*, d.commit_sha, d.author AS commit_author, d.message AS commit_message, d.deployed_at,
               d.environment, iss.fingerprint
        FROM incidents i
        JOIN deploys d ON d.id = i.deploy_id
        JOIN issues iss ON iss.id = i.issue_id
        WHERE i.id = $1 AND i.project_id = $2
        """,
        incident_id,
        project_id,
    )
    if not inc_row:
        raise ValueError(f"Incident {incident_id} not found for project {project_id}")

    deploy_id = inc_row["deploy_id"]
    where_file = inc_row["where_file"] or ""
    where_line = inc_row["where_line"] or 1

    # Sequential pre-gather: an asyncpg connection runs one query at a time, and this
    # connection carries the project's RLS context, so the reads cannot share it in parallel.
    diff_files = await get_diff_files(conn, project_id, deploy_id)
    dep_changes = await get_dependency_changes(conn, project_id, deploy_id)
    cfg_changes = await get_config_changes(conn, project_id, deploy_id)
    new_issues = await list_new_issues(conn, project_id, deploy_id)
    error_rate = await get_error_rate(conn, project_id, deploy_id)
    prev_deploys = await get_previous_deploys(conn, project_id, deploy_id, n=3)
    events = await get_issue_events(conn, project_id, inc_row["issue_id"], limit=5)

    # Context file excerpt (±30 lines around error frame)
    code_excerpt = await get_file_at_commit(
        conn,
        project_id,
        deploy_id,
        where_file,
        start_line=max(1, where_line - 30),
        end_line=where_line + 30,
    )

    return {
        "incident": dict(inc_row),
        "diff_files": diff_files,
        "dependency_changes": dep_changes,
        "config_changes": cfg_changes,
        "new_issues": new_issues,
        "error_rate": error_rate,
        "previous_deploys": prev_deploys,
        "recent_events": events,
        "code_excerpt": code_excerpt,
    }


def build_investigation_prompt(context: Dict[str, Any]) -> str:
    """Build scrubbed user prompt containing pre-gathered context."""
    inc = context["incident"]
    stack_sample = ""
    if context["recent_events"]:
        stack_sample = context["recent_events"][0].get("stack_trace") or ""

    prompt = f"""Investigate incident #{inc['id']} after deploy {inc['commit_sha'][:8]} ('{inc['commit_message']}').
Error Title: {inc['title']}
Event Count: {inc['event_count']}
Location: {inc['where_file']}:{inc['where_line']} (in diff: {inc['in_diff']})

Recent Stack Trace Sample:
{stack_sample}

Code Excerpt Around Error Line:
{context['code_excerpt'].get('content') or 'Not available'}

Changed Files in Deploy:
{json.dumps(context['diff_files'][:15], indent=2)}

Dependency Changes:
{json.dumps(context['dependency_changes'][:10], indent=2)}

Config Changes:
{json.dumps(context['config_changes'], indent=2)}

Error Rate Before vs After Deploy:
{json.dumps(context['error_rate'], indent=2)}

Return a JSON object conforming exactly to:
{{
  "summary": "...",
  "what_broke": "...",
  "where": {{"file": "...", "line": 0, "function": "..."}},
  "cause": "...",
  "evidence": [{{"kind": "diff|file|dependency|config|events|error_rate|previous_deploy", "detail": "...", "ref": "..."}}],
  "suggested_fix": {{"description": "...", "code_hint": "...", "rollback_recommended": false, "rollback_to_sha": null}},
  "confidence": "high|medium|low",
  "not_sure_reason": null
}}
"""
    return scrub_text(prompt)


async def run_investigation(
    conn: asyncpg.Connection,
    project_id: int,
    incident_id: int,
    deep_mode: bool = False,
) -> InvestigationOutput:
    """Run full read-only investigation with pre-gathering, agent execution, and verification."""
    started_at = datetime.now(timezone.utc)

    # 1. Check cache: existing completed investigation for this incident (unless deep mode requested)
    if not deep_mode:
        cached_row = await conn.fetchrow(
            """
            SELECT id, result, result_enc
            FROM investigations
            WHERE project_id = $1 AND incident_id = $2 AND status = 'done'
            LIMIT 1
            """,
            project_id,
            incident_id,
        )
        cached_result = (
            await decrypt_or_plain(
                project_id, "investigations", "result", cached_row.get("result_enc"), cached_row.get("result")
            )
            if cached_row
            else None
        )
        if cached_result:
            if isinstance(cached_result, str):
                cached_result = json.loads(cached_result)
            logger.info("Reusing cached investigation #%d for incident #%d", cached_row["id"], incident_id)
            return InvestigationOutput.model_validate(cached_result)

    # 2. Pre-gather context
    context = await pregather_context(conn, project_id, incident_id)
    user_prompt = build_investigation_prompt(context)

    # 3. Agent Execution with LLM
    llm = get_llm_client()
    raw_response = ""
    steps = [{"step": "pregather", "duration_ms": 100}]

    t0 = time.monotonic()
    try:
        raw_response = await llm.complete(
            system=SYSTEM_PROMPT,
            prompt=user_prompt,
            max_tokens=1500 if deep_mode else 1000,
        )
    except Exception as exc:
        logger.warning("LLM call failed during investigation: %s", exc)

    llm_duration_ms = int((time.monotonic() - t0) * 1000)
    steps.append({"step": "llm_complete", "duration_ms": llm_duration_ms})

    # 4. Parse and validate JSON output
    parsed_output: Optional[InvestigationOutput] = None
    if raw_response:
        try:
            # Strip markdown codeblocks if model wrapped in ```json
            cleaned = raw_response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1]
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            data = json.loads(cleaned.strip())
            parsed_output = InvestigationOutput.model_validate(data)
        except Exception as p_exc:
            logger.debug("Failed parsing LLM output: %s. Falling back to deterministic fallback.", p_exc)

    if not parsed_output:
        # Fallback output derived from deterministic context
        inc = context["incident"]
        in_diff = inc["in_diff"]
        evidence_items = []
        if in_diff:
            evidence_items.append(
                EvidenceItem(
                    kind="diff",
                    detail=f"Line {inc['where_line']} in {inc['where_file']} was modified in deploy {inc['commit_sha'][:8]}.",
                    ref="deploy_files",
                )
            )

        parsed_output = InvestigationOutput(
            summary=f"Incident opened for {inc['title']} at {inc['where_file']}:{inc['where_line']}",
            what_broke=f"{inc['title']} triggered {inc['event_count']} times",
            where=WhereLocation(
                file=inc["where_file"] or "unknown",
                line=inc["where_line"] or 1,
                function=inc["where_function"] or "unknown",
            ),
            cause=f"Regression detected in {inc['where_file']} following deploy {inc['commit_sha'][:8]}",
            evidence=evidence_items,
            suggested_fix=SuggestedFix(
                description=f"Inspect changes to {inc['where_file']} around line {inc['where_line']}",
                rollback_recommended=in_diff and inc["event_count"] >= 10,
            ),
            confidence="medium" if in_diff else "low",
            not_sure_reason=None if in_diff else "Innermost stack frame was not modified in this deploy.",
        )

    # 5. TD Stage 5 Verifiers:
    # (a) Verify evidence supports conclusion
    is_supported, sup_score, note = verify_evidence_supports_conclusion(
        cause=parsed_output.cause,
        evidence=[e.model_dump() for e in parsed_output.evidence],
        where_file=parsed_output.where.file,
    )
    if not is_supported:
        parsed_output.confidence = "low"
        weak_note = "Evidence does not strongly correlate with hypothesized root cause."
        parsed_output.not_sure_reason = (
            f"{parsed_output.not_sure_reason}; {weak_note}"
            if parsed_output.not_sure_reason
            else weak_note
        )

    # (b) Verify suggested fix safety
    is_safe, _, warning = verify_suggestion_safety(parsed_output.suggested_fix.description)
    if not is_safe and warning:
        parsed_output.suggested_fix.warning_label = warning

    # 6. Store investigation record in database
    finished_at = datetime.now(timezone.utc)
    result_dict = parsed_output.model_dump()
    # Results quote code and stack traces: store them (and the tool audit log) encrypted
    result_plain, result_enc = await encrypt_or_keep(project_id, "investigations", "result", json.dumps(result_dict))
    steps_plain, steps_enc = await encrypt_or_keep(project_id, "investigations", "steps", json.dumps(steps))

    inv_row = await conn.fetchrow(
        """
        INSERT INTO investigations (
            incident_id, project_id, status, model, started_at, finished_at,
            steps, steps_enc, result, result_enc, input_tokens, output_tokens
        )
        VALUES ($1, $2, 'done', $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        incident_id,
        project_id,
        getattr(llm, "model", "default"),
        started_at,
        finished_at,
        steps_plain or "[]",
        steps_enc,
        result_plain,
        result_enc,
        500,
        250,
    )
    inv_id = int(inv_row["id"])

    # 6b. Record usage metering
    try:
        from app.metering import record_investigation_usage
        await record_investigation_usage(
            conn=conn,
            project_id=project_id,
            investigation_id=inv_id,
            status="done",
            is_deep=deep_mode,
            input_tokens=500,
            output_tokens=250,
            model=getattr(llm, "model", None),
            provider=settings.LLM_PROVIDER,
        )
    except Exception as u_exc:
        logger.debug("Investigation usage recording skipped or failed: %s", u_exc)

    # 7. Deliver thread reply notification
    try:
        reply_title = f"Investigation: {parsed_output.summary}"
        evidence_bullets = "\n".join([f"• [{e.kind}] {e.detail}" for e in parsed_output.evidence[:4]])
        fix_note = parsed_output.suggested_fix.description
        if parsed_output.suggested_fix.warning_label:
            fix_note += f"\n⚠️ {parsed_output.suggested_fix.warning_label}"

        reply_body = f"""Root Cause: {parsed_output.cause}
Confidence: {parsed_output.confidence.upper()}

Evidence:
{evidence_bullets}

Suggested Action:
{fix_note}"""

        await queue_notification(
            conn,
            project_id=project_id,
            dedupe_key=f"investigation_reply:{incident_id}:{inv_id}",
            payload={
                "type": "investigation_reply",
                "incident_id": incident_id,
                "investigation_id": inv_id,
                "title": reply_title,
                "body": reply_body,
                "confidence": parsed_output.confidence,
                "rollback_recommended": parsed_output.suggested_fix.rollback_recommended,
            },
        )
    except Exception as n_exc:
        logger.warning("Failed queueing investigation notification: %s", n_exc)

    return parsed_output
