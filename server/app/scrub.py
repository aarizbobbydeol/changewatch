import re

# Backward-compatible individual patterns
AWS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
GITHUB_TOKEN_PATTERN = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b")
SK_TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")
# A secret assigned to a named key: password=..., DB_PASSWORD = "...", client_secret=...&,
# "api_key": "...", {'password': '...'}. The key may have a prefix (DB_, client_, refresh_,
# STRIPE_SECRET_) and a closing quote. A match only starts where a word starts and the prefix has
# no hyphens, so the scan stays linear on long runs like "a-a-a-...".
SECRET_KEY_WORDS = (
    r"(?:password|passwd|pwd|secret(?:[_-]?key)?|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|credentials?|auth)"
)
KV_SECRET_REGEX = (
    r"(?P<prefix>(?<![A-Za-z0-9_])[A-Za-z0-9_]*?" + SECRET_KEY_WORDS + r"(?![A-Za-z0-9_])[\"']?\s*[:=]\s*)"
    # (?!...): a value that is already redacted is left alone, so scrubbing twice changes nothing
    r"(?P<val>(?![\"']?\[REDACTED_)(?:\"[^\"\n]{0,500}\"|'[^'\n]{0,500}'|[^\s\"',;&)}\]]+))"
)
KEY_VALUE_PATTERN = re.compile("(?i)" + KV_SECRET_REGEX)

# A recognisable token keeps its specific label even when it is assigned to a key (GH_TOKEN=ghp_...)
_TOKEN_LABELS = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?:sk-|[rs]k_(?:live|test)_|xox[abprs]-)[A-Za-z0-9_-]{10,}"), "[REDACTED_SECRET_KEY]"),
)


def redact_kv_match(match: re.Match) -> str:
    """prefix plus [REDACTED_SECRET] (or the token's own label), keeping the value's quotes."""
    val = match.group("val")
    quote = val[0] if val[:1] in ("'", '"') and len(val) >= 2 and val[-1] == val[0] else ""
    inner = val[1:-1] if quote else val
    label = next((name for pattern, name in _TOKEN_LABELS if pattern.match(inner)), "[REDACTED_SECRET]")
    return f"{match.group('prefix')}{quote}{label}{quote}"


# Combined single-pass alternation for high-performance scrubbing
COMBINED_SECRET_PATTERN = re.compile(
    # A PEM private key, also inside JSON with escaped newlines, also cut off before its END line
    r"(?P<pem>-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]{0,12000}?-----END [A-Z ]*PRIVATE KEY-----|[A-Za-z0-9+/=\s\\]*))|"
    r"(?P<aws>\bAKIA[0-9A-Z]{16}\b)|"
    r"(?P<gh>\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{20,})\b)|"
    r"(?P<sk>\b(?:sk-[A-Za-z0-9_-]{10,}|[rs]k_(?:live|test)_[A-Za-z0-9]{10,}|xox[abprs]-[A-Za-z0-9-]{10,})\b)|"
    r"(?P<bearer>(?i:\bbearer\s+)[A-Za-z0-9_\-.~+/]{10,}=*)|"
    r"(?P<kv>(?i:" + KV_SECRET_REGEX + r"))"
)


def _combined_replacer(match: re.Match) -> str:
    if match.group("pem"):
        return "[REDACTED_PRIVATE_KEY]"
    if match.group("aws"):
        return "[REDACTED_AWS_KEY]"
    if match.group("gh"):
        return "[REDACTED_GITHUB_TOKEN]"
    if match.group("sk"):
        return "[REDACTED_SECRET_KEY]"
    if match.group("bearer"):
        return "Bearer [REDACTED_SECRET]"
    return redact_kv_match(match)


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

