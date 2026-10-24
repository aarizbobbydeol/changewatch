"""AI Output Verifiers (TD Addendum Stage 5).

Provides deterministic and typed verification of AI investigator output:
1. verify_evidence_supports_conclusion: ensures evidence actually supports the cause statement.
2. verify_suggestion_safety: screens suggested fix text for hazardous or destructive commands.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

VERIFY_MIN = 0.50
UNSAFE_MAX = 0.50

# Unsafe command patterns in suggested fixes
UNSAFE_PATTERNS = [
    r"rm\s+-rf",
    r"drop\s+database",
    r"drop\s+table",
    r"truncate\s+table",
    r"delete\s+from\s+[a-z0-9_]+\s*;?$",  # DELETE without WHERE clause
    r"git\s+push\s+.*--force",
    r"git\s+reset\s+--hard",
    r"chmod\s+-R\s+777",
    r"kill\s+-9\s+-1",
]


def verify_evidence_supports_conclusion(
    cause: str,
    evidence: List[Dict[str, Any]],
    where_file: Optional[str] = None,
) -> Tuple[bool, float, str]:
    """
    Verify whether the retrieved evidence supports the stated root-cause hypothesis.
    Returns (is_supported, score, note).
    """
    if not evidence:
        return False, 0.0, "evidence is empty"

    cause_lower = cause.lower()
    matched_refs = 0

    for ev in evidence:
        detail = str(ev.get("detail", "")).lower()
        # Check if detail keywords match cause keywords
        common_words = set(detail.split()) & set(cause_lower.split())
        # Filter stopwords
        common_words = {w for w in common_words if len(w) > 3}
        if len(common_words) >= 2 or (where_file and where_file.lower() in detail):
            matched_refs += 1

    score = min(1.0, matched_refs / max(1, len(evidence)))
    if matched_refs == 0:
        score = 0.2  # Weak correlation

    is_supported = score >= VERIFY_MIN
    note = "evidence is valid" if is_supported else "evidence is weak"
    return is_supported, score, note


def verify_suggestion_safety(suggested_fix: str) -> Tuple[bool, float, Optional[str]]:
    """
    Verify whether a suggested fix text contains dangerous or destructive operations.
    Returns (is_safe, unsafe_probability, warning_label).
    """
    if not suggested_fix:
        return True, 0.0, None

    for pattern in UNSAFE_PATTERNS:
        if re.search(pattern, suggested_fix, re.IGNORECASE):
            return False, 0.95, "Warning: Suggested fix contains potentially destructive operations. Review carefully before executing."

    return True, 0.0, None
