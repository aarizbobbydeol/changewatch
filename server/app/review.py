"""A risk review of each deploy's diff (backlog: AI code review), shown inside ChangeWatch only.

Rules first (no AI, no cost): risky added lines (division by a value that may be zero, swallowed
exceptions, eval, SQL built from strings, TLS checks turned off, a committed secret), removed
guard checks, and risky files (migrations, dependencies, very large releases). Then, when an AI
provider is configured and the budget allows, the model adds its findings. Read-only: it reads
the stored diff and never writes to the repository or to GitHub.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple

import asyncpg
from pydantic import BaseModel, Field

from app.crypto import decrypt_or_plain, encrypt_or_keep
from app.scrub import scrub_text
from app.sre_prep import classify_file

logger = logging.getLogger("changewatch.review")

_SEVERITY = {"low": 0, "medium": 1, "high": 2}
_CODE = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java", ".kt", ".php", ".cs")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_STRING = re.compile(r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")

# (severity, pattern on an added line with strings and comments removed, message)
_LINE_RULES: List[Tuple[str, re.Pattern, str]] = [
    ("medium", re.compile(r"[\w)\]]\s*//?\s*(?!\d)[A-Za-z_][\w.]*"), "Divides by a value that may be zero"),
    ("medium", re.compile(r"^\s*except\s*:|^\s*except\s+(?:Base)?Exception\b[^:]*:\s*pass\b"), "Swallows every exception"),
    ("high", re.compile(r"\b(?:eval|exec)\s*\("), "Runs code built at runtime (eval/exec)"),
    ("high", re.compile(r"\b(?:verify\s*=\s*False|rejectUnauthorized\s*:\s*false)"), "Turns off TLS certificate checks"),
]
_RAW_RULES: List[Tuple[str, re.Pattern, str]] = [
    ("high", re.compile(r"""f["'][^"']*\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^"']*\{""", re.I), "Builds SQL from an f-string (injection risk)"),
    ("high", re.compile(r"""["'][^"']*\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^"']*["']\s*\.format\(""", re.I), "Builds SQL with .format() (injection risk)"),
    ("high", re.compile(r"\[REDACTED_[A-Z_]+\]"), "A secret was committed (ChangeWatch removed it from its copy: rotate it)"),
    ("low", re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b"), "Leaves a TODO/FIXME in released code"),
]
# "Path(base) / name" joins paths, it does not divide
_PATH_JOIN = re.compile(r"(?i)(?:\bPath\(|\w*(?:path|dir|root|base|folder|home|url|uri)\w*\)?\s*/)")
_GUARD = re.compile(r"^\s*(?:if|elif|guard|assert)\b.*(?:\bis None\b|\bis not None\b|\bnot\s+\w|==\s*0\b|!=\s*0\b|\blen\()")


class ReviewFinding(BaseModel):
    file: str = Field(..., max_length=300)
    line: Optional[int] = None
    severity: Literal["low", "medium", "high"]
    issue: str = Field(..., max_length=300)
    why: Optional[str] = Field(None, max_length=500)
    source: Literal["rules", "ai"] = "rules"


class AIReview(BaseModel):
    risk: Literal["low", "medium", "high"]
    summary: str = Field(..., max_length=600)
    findings: List[ReviewFinding] = Field(default_factory=list, max_length=8)
    tests_to_add: List[str] = Field(default_factory=list, max_length=5)


def _code_only(line: str, path: str) -> str:
    """The line without string contents and comments, so '/' in a URL or '#' text is not code."""
    line = _STRING.sub('""', line)
    marker = "#" if path.endswith((".py", ".rb")) else "//"
    return line.split(marker, 1)[0]


def _diff_lines(diff: str) -> Iterator[Tuple[str, str, Optional[int], str]]:
    """(path, '+' or '-', new-side line number for '+', text) for every changed line."""
    path, new_line = "", None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split(" b/", 1)
            path, new_line = (parts[1].strip() if len(parts) == 2 else ""), None
            continue
        match = _HUNK.match(raw)
        if match:
            new_line = int(match.group(2))
            continue
        if new_line is None or raw.startswith(("+++", "---")):
            continue
        if raw.startswith("+"):
            yield path, "+", new_line, raw[1:]
            new_line += 1
        elif raw.startswith("-"):
            yield path, "-", None, raw[1:]
        elif not raw.startswith("\\"):
            new_line += 1


def rules_review(diff: str, paths: List[str]) -> List[Dict[str, Any]]:
    """Findings from fixed rules, most severe first (at most 10)."""
    findings: List[Dict[str, Any]] = []
    added_by_file: Dict[str, int] = {}
    removed_guards: Dict[str, List[str]] = {}
    added_guards: Dict[str, List[str]] = {}
    for path, sign, line_no, text in _diff_lines(diff or ""):
        if sign == "+":
            added_by_file[path] = added_by_file.get(path, 0) + 1
        is_code = path.endswith(_CODE)
        if sign == "-" and is_code and _GUARD.match(text):
            removed_guards.setdefault(path, []).append(text.strip())
            continue
        if sign != "+":
            continue
        if is_code and _GUARD.match(text):
            added_guards.setdefault(path, []).append(text.strip())
        for severity, pattern, message in _RAW_RULES:
            if pattern.search(text):
                findings.append({"file": path, "line": line_no, "severity": severity, "issue": message, "source": "rules"})
        if is_code:
            code = _code_only(text, path)
            for severity, pattern, message in _LINE_RULES:
                if message.startswith("Divides") and _PATH_JOIN.search(code):
                    continue
                if pattern.search(code):
                    findings.append({"file": path, "line": line_no, "severity": severity, "issue": message, "source": "rules"})
    for path, guards in removed_guards.items():
        kept = set(added_guards.get(path, []))
        for guard in guards:
            if guard not in kept:
                findings.append({"file": path, "line": None, "severity": "medium", "source": "rules",
                                 "issue": f"Removes a check: `{guard[:120]}`"})
    for path in paths:
        lower = path.lower()
        is_dep, _ = classify_file(path)
        if ("migration" in lower or "alembic" in lower) and lower.endswith(_CODE + (".sql",)):
            findings.append({"file": path, "line": None, "severity": "medium", "source": "rules",
                             "issue": "Changes the database schema: check it works with the code still running during the rollout"})
        elif is_dep:
            findings.append({"file": path, "line": None, "severity": "low", "source": "rules", "issue": "Changes dependencies"})
    total_added = sum(added_by_file.values())
    if total_added > 500:
        findings.append({"file": "(release)", "line": None, "severity": "medium", "source": "rules",
                         "issue": f"Large release: {total_added} added lines are harder to review and to roll back"})
    findings.sort(key=lambda f: -_SEVERITY[f["severity"]])
    return findings[:10]


def _risk(findings: List[Dict[str, Any]]) -> str:
    return max((f["severity"] for f in findings), key=lambda s: _SEVERITY[s], default="low")


SYSTEM_PROMPT = """You review the diff of a release that was just deployed, for production risk.
You are read-only: you only report. Point at concrete added or removed lines (file and new line
number) that could fail in production: crashes, wrong results, data loss, security, performance.
No style comments. If nothing is risky, say so with risk "low" and no findings.
Output only JSON: {"risk": "low|medium|high", "summary": "...", "findings": [{"file": "...",
"line": 0, "severity": "low|medium|high", "issue": "...", "why": "..."}], "tests_to_add": ["..."]}
"""


async def _ai_review(conn: asyncpg.Connection, project_id: int, deploy_id: int, diff: str, message: str,
                     paths: List[str]) -> Tuple[Optional[AIReview], Optional[str], int, int]:
    """The model's review, or None (no provider, no budget, a failure or an unusable answer)."""
    from app.config import settings
    from app.llm import get_llm_client
    from app.metering import can_spend

    if (settings.LLM_PROVIDER or "").lower() == "fake" or settings.DRY_RUN_LLM:
        return None, None, 0, 0
    allowed, _ = await can_spend(conn, project_id, "ai_review")
    if not allowed:
        return None, None, 0, 0
    llm = get_llm_client()
    prompt = (f"Commit message: {message[:500]}\nChanged files: {json.dumps(paths[:50])}\n\nDiff:\n{diff[:15000]}"
              + ("\n[diff cut to fit]" if len(diff) > 15000 else ""))
    try:
        raw = await llm.complete(system=SYSTEM_PROMPT, prompt=scrub_text(prompt), max_tokens=6000)
    except Exception as exc:
        logger.warning("AI review of deploy %d failed: %s", deploy_id, exc)
        return None, getattr(llm, "model", None), 0, 0
    usage = getattr(llm, "last_usage", None)
    tokens_in, tokens_out = usage if isinstance(usage, tuple) and len(usage) == 2 else (0, 0)
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        review = AIReview.model_validate(json.loads(text))
    except Exception:
        return None, getattr(llm, "model", None), tokens_in, tokens_out
    for finding in review.findings:
        finding.source = "ai"
    return review, getattr(llm, "model", None), tokens_in, tokens_out


async def run_deploy_review(conn: asyncpg.Connection, project_id: int, deploy_id: int) -> Optional[Dict[str, Any]]:
    """Review a deploy and store the result (encrypted). Returns the result, or None if the
    deploy is not this project's."""
    dep = await conn.fetchrow(
        "SELECT diff, diff_enc, message FROM deploys WHERE id = $1 AND project_id = $2", deploy_id, project_id
    )
    if not dep:
        return None
    diff = await decrypt_or_plain(project_id, "deploys", "diff", dep.get("diff_enc"), dep.get("diff")) or ""
    paths = [r["path"] for r in await conn.fetch(
        "SELECT path FROM deploy_files WHERE deploy_id = $1 AND project_id = $2 ORDER BY path", deploy_id, project_id)]
    findings = rules_review(diff, paths)
    ai, model, tokens_in, tokens_out = await _ai_review(conn, project_id, deploy_id, diff, dep["message"] or "", paths) \
        if diff else (None, None, 0, 0)
    summary = None
    tests: List[str] = []
    if ai:
        findings = sorted(findings + [f.model_dump() for f in ai.findings], key=lambda f: -_SEVERITY[f["severity"]])[:12]
        summary, tests = ai.summary, ai.tests_to_add
    risk = max(_risk(findings), ai.risk if ai else "low", key=lambda s: _SEVERITY[s])
    result = {"risk": risk, "summary": summary, "findings": findings, "tests_to_add": tests,
              "source": "rules+ai" if ai else "rules", "reviewed_at": datetime.now(timezone.utc).isoformat()}
    _, result_enc = await encrypt_or_keep(project_id, "deploy_reviews", "result", json.dumps(result))
    await conn.execute(
        """
        INSERT INTO deploy_reviews (deploy_id, project_id, status, risk, result_enc, model, input_tokens, output_tokens)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (deploy_id) DO UPDATE SET status = EXCLUDED.status, risk = EXCLUDED.risk,
          result_enc = EXCLUDED.result_enc, model = EXCLUDED.model, input_tokens = EXCLUDED.input_tokens,
          output_tokens = EXCLUDED.output_tokens, created_at = now()
        """,
        deploy_id, project_id, "done" if ai else "rules", risk, result_enc, model, tokens_in, tokens_out,
    )
    if tokens_in or tokens_out:
        from app.config import settings
        from app.metering import calculate_llm_cost_micros, record_usage

        # Tracked as our cost; billed only if the price book gives "ai_review" a price
        await record_usage(conn, project_id, "ai_review", 1, ref_type="deploy", ref_id=deploy_id,
                           our_cost_micros=calculate_llm_cost_micros(model or "", tokens_in, tokens_out,
                                                                     provider=settings.LLM_PROVIDER),
                           idempotency_key=f"review:{deploy_id}")
    return result


async def get_deploy_review(conn: asyncpg.Connection, project_id: int, deploy_id: int) -> Optional[Dict[str, Any]]:
    """The stored review of a deploy, or None."""
    row = await conn.fetchrow(
        "SELECT result_enc FROM deploy_reviews WHERE deploy_id = $1 AND project_id = $2", deploy_id, project_id
    )
    if not row:
        return None
    result = await decrypt_or_plain(project_id, "deploy_reviews", "result", row.get("result_enc"), None)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    return result if isinstance(result, dict) else None


def review_line(review: Optional[Dict[str, Any]]) -> Optional[str]:
    """One line for the verdict alert: the risk and the most serious finding."""
    if not review or review.get("risk") not in ("medium", "high") or not review.get("findings"):
        return None
    top = review["findings"][0]
    where = f"{top['file']}:{top['line']}" if top.get("line") else top["file"]
    return f"Risk review: {review['risk'].upper()}: {top['issue']} ({where})"
