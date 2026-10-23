import re

# Backward-compatible individual patterns
AWS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
GITHUB_TOKEN_PATTERN = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b")
SK_TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")
KEY_VALUE_PATTERN = re.compile(
    r"""(?i)(?P<prefix>\b(?:api[_-]?key|secret|token|password|auth[_-]?token|access[_-]?token)\b\s*[:=]\s*["']?)(?P<val>[^\s"',;]+)(?P<suffix>["']?)"""
)

# Combined single-pass alternation for high-performance scrubbing
COMBINED_SECRET_PATTERN = re.compile(
    r"(?P<aws>\bAKIA[0-9A-Z]{16}\b)|"
    r"(?P<gh>\bgh[pousr]_[A-Za-z0-9_]{36,}\b)|"
    r"(?P<sk>\bsk-[A-Za-z0-9_-]{10,}\b)|"
    r"""(?P<kv>(?i:(?P<prefix>\b(?:api[_-]?key|secret|token|password|auth[_-]?token|access[_-]?token)\b\s*[:=]\s*["']?)(?P<val>[^\s"',;]+)(?P<suffix>["']?)))"""
)


def _combined_replacer(match: re.Match) -> str:
    if match.group("aws"):
        return "[REDACTED_AWS_KEY]"
    if match.group("gh"):
        return "[REDACTED_GITHUB_TOKEN]"
    if match.group("sk"):
        return "[REDACTED_SECRET_KEY]"
    prefix = match.group("prefix")
    suffix = match.group("suffix") or ""
    return f"{prefix}[REDACTED_SECRET]{suffix}"


def scrub_text(text: str) -> str:
    """
    Redact sensitive secrets from text (diffs, stack traces, payloads).
    Performance optimizations:
    - Skips strings under 8 characters immediately (shortest secret/token is >= 8 chars).
    - Single-pass alternation regex evaluation for all secret tokens.
    """
    if not text or len(text) < 8:
        return text

    # Single-pass secret redaction
    redacted = COMBINED_SECRET_PATTERN.sub(_combined_replacer, text)

    # Redact PII (emails, IPs, JWTs, card numbers)
    from app.pii import scrub_pii
    return scrub_pii(redacted)

