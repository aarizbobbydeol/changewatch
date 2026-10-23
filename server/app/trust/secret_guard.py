"""Optional Outbound Secret Guard Filter (TD Addendum Stage 8).

Secondary defense-in-depth scrubber on outbound AI prompts and notifications.
Controlled by SECRET_GUARD_ENABLED (default False).
Never logs secret content; logs only the guard event and field name.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Tuple

from app.config import settings

logger = logging.getLogger("changewatch.trust.secret_guard")

# High-entropy credential patterns
GUARD_PATTERNS = [
    (r"(?i)(?:api_key|apikey|secret|token|password|bearer|auth|private_key)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-\.]{16,})['\"]?", "[REDACTED_SECRET]"),
    (r"ghp_[a-zA-Z0-9]{36}", "[REDACTED_GITHUB_TOKEN]"),
    (r"sk-[a-zA-Z0-9]{32,}", "[REDACTED_OPENAI_KEY]"),
    (r"xox[baprs]-[a-zA-Z0-9\-]{10,}", "[REDACTED_SLACK_TOKEN]"),
    (r"-----BEGIN (?:RSA )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA )?PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]"),
]


def is_secret_guard_enabled() -> bool:
    """Return True if secret guard feature flag is active."""
    val = os.getenv("SECRET_GUARD_ENABLED", "false").lower().strip()
    return val in ("true", "1", "yes")


def apply_secret_guard(
    text: str,
    field_name: str = "content",
) -> Tuple[str, bool, int]:
    """
    Scan outbound text for secrets. If found and guard is active:
    replaces with redaction placeholder and logs a guard audit event without the content.
    Returns (cleaned_text, was_blocked, count_redacted).
    """
    if not text:
        return text, False, 0

    if not is_secret_guard_enabled():
        return text, False, 0

    cleaned = text
    total_matches = 0

    for pattern, placeholder in GUARD_PATTERNS:
        matches = list(re.finditer(pattern, cleaned))
        if matches:
            total_matches += len(matches)
            cleaned = re.sub(pattern, placeholder, cleaned)

    if total_matches > 0:
        logger.warning(
            "SecretGuard blocked %d potential secrets in outbound field '%s'",
            total_matches,
            field_name,
        )
        return cleaned, True, total_matches

    return text, False, 0
