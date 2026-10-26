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
# A regex literal such as /[^a-z]/g (JS, Ruby): it starts where a value is expected, so it is not a division
_REGEX_LITERAL = re.compile(r"(^|[(,=:\[!&|?{};~]|\breturn)(\s*)/(?![/*])(?:\\.|\[(?:\\.|[^\]\\]){0,100}\]|[^/\\\[]){1,200}/[A-Za-z]*")
# Docstrings (Python) and /* comments */ (C-like) can span lines; a Python string can only start
# after one of these, or on a line of its own below a line ending in one (such as "def f():")
_PY_BLOCK = re.compile(r"\"\"\"|'''")
_C_BLOCK = re.compile(r"//|/\*|\*/")
_STRING_START = re.compile(r"(?:[=(,\[{:+]|\breturn|\byield)$")
# SQL in the shape of a statement, all upper or all lower case: "Could not update {name}" is English
_SQL = (r"\b(?:SELECT\b[^\"']*\bFROM|UPDATE\s+[^\s\"']+\s+SET|DELETE\s+FROM|INSERT\s+INTO"
        r"|select\b[^\"']*\bfrom|update\s+[^\s\"']+\s+set|delete\s+from|insert\s+into)\b")

# (severity, pattern on an added line with strings and comments removed, message)
_LINE_RULES: List[Tuple[str, re.Pattern, str]] = [
    ("medium", re.compile(r"[\w)\]]\s*//?\s*(?!\d)[A-Za-z_][\w.]*"), "Divides by a value that may be zero"),
    ("medium", re.compile(r"^\s*except\s*:|^\s*except\s+(?:Base)?Exception\b[^:]*:\s*pass\b"), "Swallows every exception"),
    # Not a method such as regex.exec(...) or session.exec(...)
    ("high", re.compile(r"(?<![\w.])(?:eval|exec)\s*\("), "Runs code built at runtime (eval/exec)"),
    ("high", re.compile(r"\b(?:verify\s*=\s*False|rejectUnauthorized\s*:\s*false)"), "Turns off TLS certificate checks"),
]
_RAW_RULES: List[Tuple[str, re.Pattern, str]] = [
    ("high", re.compile(r"""f["'](?=[^"']*\{)[^"']*""" + _SQL), "Builds SQL from an f-string (injection risk)"),
    ("high", re.compile(r"""["'][^"']*""" + _SQL + r"""[^"']*["']\s*\.format\("""), "Builds SQL with .format() (injection risk)"),
    # Only the labels of recognisable keys. The stored diff is scrubbed, so any value assigned to a
    # name like token or password reads [REDACTED_SECRET], and emails and IPs are redacted too:
    # those are ordinary code, not a committed secret.
    ("high", re.compile(r"\[REDACTED_(?:AWS_KEY|GITHUB_TOKEN|SECRET_KEY|PRIVATE_KEY)\]"),
     "A secret was committed (ChangeWatch removed it from its copy: rotate it)"),
    ("low", re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b"), "Leaves a TODO/FIXME in released code"),
]
# "Path(base) / name" joins paths, it does not divide
_PATH_JOIN = re.compile(r"(?i)(?:\bPath\(|\w*(?:path|dir|root|base|folder|home|url|uri)\w*\)?\s*/)")
_GUARD = re.compile(r"^\s*(?:if|elif|guard|assert)\b.*(?:\bis None\b|\bis not None\b|\bnot\s+\w|==\s*0\b|!=\s*0\b|\blen\()")
_NOT_NAMES = {"if", "elif", "guard", "assert", "not", "is", "None", "and", "or", "in", "len", "let", "return",
              "raise", "throw", "nil", "null", "undefined", "true", "false", "True", "False"}
# "except Exception:" with "pass" on the next line swallows every exception too
_EXCEPT_ALL = re.compile(r"^\s*except\s+(?:Base)?Exception\b[^:]*:\s*$")


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


def _opens_string(before: str, prev: Optional[str]) -> bool:
    """Whether a Python triple quote after `before` (on its line) starts a string. prev is the code of
    the line above, or None on the file's first line."""
    before = re.sub(r"\b[rRbBuUfF]{1,2}$", "", before).rstrip()  # f""" r""" b"""
    if before:
        return bool(_STRING_START.search(before))
    return prev is None or bool(_STRING_START.search(prev.rstrip()))


def _code_only(line: str, path: str, block: Optional[str] = None, prev: Optional[str] = "") -> Tuple[str, Optional[str]]:
    """The line without string contents, comments, regex literals and docstrings, so '/' in a URL,
    '#' text or 'and/or' in a docstring is not code; and the docstring or /* comment */ still open
    after it. A closing mark while none is open means the hunk began inside one: the text before it
    is not code either."""
    python = path.endswith(".py")
    if not python:
        line = _REGEX_LITERAL.sub(r'\1\2""', _STRING.sub('""', line))
    marks = _PY_BLOCK if python else None if path.endswith(".rb") else _C_BLOCK
    code: List[str] = []
    pos, end = 0, len(line)
    for match in marks.finditer(line) if marks else ():
        mark, before = match.group(), line[pos:match.start()]
        if block is None and mark == "//":
            end = match.start()
            break
        if block is None and (mark == "/*" or python and _opens_string(before, prev)):
            code.append(before)
            block = mark
        elif block is None or mark == ("*/" if block == "/*" else block):
            block = None
        else:
            continue
        pos = match.end()
    if block is None:
        code.append(line[pos:end])
    line = " ".join(code)
    if python:
        line = _STRING.sub('""', line)
    return (line.split("#", 1)[0] if python or path.endswith(".rb") else line), block


def _diff_lines(diff: str) -> Iterator[Tuple[str, str, Optional[int], str]]:
    """(path, '+', '-' or ' ', new-side line number (None for '-'), text) for every changed and
    context line. '+++'/'---' are file headers only before a file's first hunk: inside one, they
    are an added '++i;' or a removed '-- comment'."""
    path, new_line = "", None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split(" b/", 1)
            path, new_line = (parts[1].strip().strip('"') if len(parts) == 2 else ""), None
            continue
        match = _HUNK.match(raw)
        if match:
            new_line = int(match.group(2))
            continue
        if new_line is None:
            if raw.startswith("+++ ") and raw[4:].strip() != "/dev/null":
                path = raw[4:].strip().strip('"')
                path = path[2:] if path.startswith("b/") else path  # 'diff --git "a/x y" "b/x y"' has no ' b/'
            continue
        if raw.startswith("+"):
            yield path, "+", new_line, raw[1:]
            new_line += 1
        elif raw.startswith("-"):
            yield path, "-", None, raw[1:]
        elif not raw.startswith("\\"):
            yield path, " ", new_line, raw[1:]
            new_line += 1


def _names(guard: str) -> frozenset:
    """The names a guard checks: `if not items:` and `if len(items) == 0:` both check {items}."""
    condition = re.split(r":(?:\s|$)", _STRING.sub('""', guard), 1)[0]
    return frozenset(re.findall(r"[A-Za-z_]\w*", condition)) - _NOT_NAMES


def rules_review(diff: str, paths: List[str]) -> List[Dict[str, Any]]:
    """Findings from fixed rules, most severe first (at most 10)."""
    findings: List[Dict[str, Any]] = []
    added_by_file: Dict[str, int] = {}
    removed_guards: Dict[str, List[str]] = {}
    added_guards: Dict[str, List[str]] = {}
    # The line above on the new side (context or added): its code, its sign, and where it was
    block: Optional[str] = None
    prev: Optional[str] = None
    prev_sign, last = "", ("", 0)
    for path, sign, line_no, text in _diff_lines(diff or ""):
        is_code = path.endswith(_CODE)
        if sign == "-":
            if is_code and _GUARD.match(text):
                removed_guards.setdefault(path, []).append(text.strip())
            continue
        if (path, line_no - 1) != last:  # another file or hunk: no docstring or comment known to be open
            block, prev, prev_sign = None, (None if line_no == 1 else ""), ""
        last = (path, line_no)
        code, block = _code_only(text, path, block, prev) if is_code else (text, None)
        if (path.endswith(".py") and code.strip() in ("pass", "...") and "+" in (sign, prev_sign)
                and _EXCEPT_ALL.match(prev or "")):
            findings.append({"file": path, "line": line_no - 1, "severity": "medium", "issue": "Swallows every exception",
                             "source": "rules"})
        prev, prev_sign = code, sign
        if sign != "+":
            continue
        added_by_file[path] = added_by_file.get(path, 0) + 1
        if is_code and _GUARD.match(text):
            added_guards.setdefault(path, []).append(text.strip())
        for severity, pattern, message in _RAW_RULES:
            if pattern.search(text):
                findings.append({"file": path, "line": line_no, "severity": severity, "issue": message, "source": "rules"})
        if is_code:
            for severity, pattern, message in _LINE_RULES:
                if message.startswith("Divides") and _PATH_JOIN.search(code):
                    continue
                if pattern.search(code):
                    findings.append({"file": path, "line": line_no, "severity": severity, "issue": message, "source": "rules"})
    for path, guards in removed_guards.items():
        # Kept when an added guard starts with the same condition (tightened: `if user is None or
        # user.deleted:`) or checks the same names (rewritten: `if len(items) == 0:`)
        added = added_guards.get(path, [])
        added_names = {_names(guard) for guard in added}
        for guard in guards:
            condition = guard.rstrip(":{ ")
            tightened = any(a.startswith(condition) and not re.match(r"\w", a[len(condition):]) for a in added)
            if not tightened and _names(guard) not in added_names:
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
Report at most 8 findings, most serious first, and at most 5 tests_to_add. Keep the summary under
600 characters, each issue under 300 and each why under 500.
Output only JSON: {"risk": "low|medium|high", "summary": "...", "findings": [{"file": "...",
"line": 0, "severity": "low|medium|high", "issue": "...", "why": "..."}], "tests_to_add": ["..."]}
"""


def _within_limits(data: Any) -> Any:
    """The model's answer cut to AIReview's limits, so a long answer is kept rather than thrown away.
    An unknown severity counts as medium; an unknown risk is the highest finding's."""
    if not isinstance(data, dict):
        return data

    def items(value: Any) -> List[Any]:
        return value if isinstance(value, list) else []

    def level(value: Any) -> str:
        return str(value).lower() if str(value).lower() in _SEVERITY else "medium"

    findings = [{"file": str(f.get("file") or "")[:300], "line": int(f["line"]) if str(f.get("line")).isdecimal() else None,
                 "severity": level(f.get("severity")), "issue": str(f["issue"])[:300],
                 "why": str(f["why"])[:500] if f.get("why") else None}
                for f in items(data.get("findings")) if isinstance(f, dict) and f.get("issue")][:8]
    risk = str(data.get("risk")).lower()
    return {"risk": risk if risk in _SEVERITY else _risk(findings), "summary": str(data.get("summary") or "")[:600],
            "findings": findings, "tests_to_add": [t[:300] for t in items(data.get("tests_to_add")) if isinstance(t, str)][:5]}


async def _ai_review(conn: asyncpg.Connection, project_id: int, deploy_id: int, diff: str, message: str,
                     paths: List[str]) -> Tuple[Optional[AIReview], Optional[str], int, int]:
    """The model's review, or None (no provider, no budget, a failure or an unusable answer)."""
    from app.config import settings
    from app.llm import get_llm_client
    from app.metering import Meter, can_spend

    if (settings.LLM_PROVIDER or "").lower() == "fake" or settings.DRY_RUN_LLM:
        return None, None, 0, 0
    allowed, _ = await can_spend(conn, project_id, Meter.AI_INVESTIGATION)
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
    text = raw or ""
    start, end = text.find("{"), text.rfind("}")  # the JSON object, also inside ``` or after a preamble
    try:
        review = AIReview.model_validate(_within_limits(json.loads(text[start:end + 1])))
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
        from app.metering import Meter, calculate_llm_cost_micros, record_usage

        # Counts toward the project's AI budget like an investigation (the user's decision), once per deploy
        await record_usage(conn, project_id, Meter.AI_INVESTIGATION, 1, ref_type="deploy", ref_id=deploy_id,
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
