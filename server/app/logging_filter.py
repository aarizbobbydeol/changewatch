import logging
import re
from typing import Any

# 1. API Keys: cw_live_..., cw_test_..., cw_ingest_..., cw_read_..., cw_...
CW_KEY_PATTERN = re.compile(r"\bcw_[A-Za-z0-9_\-]{6,}\b")

# 2. Webhook URLs: Slack webhooks or generic webhook endpoints
WEBHOOK_URL_PATTERN = re.compile(
    r"https?://(?:hooks\.slack\.com/services/[^\s\"'>]+|[^\s\"'>]*webhook[^\s\"'>]*)"
)

# 3. Cloud & Provider Tokens
AWS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
GITHUB_TOKEN_PATTERN = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")
SK_TOKEN_PATTERN = re.compile(r"\bsk-[a-zA-Z0-9_\-]{10,}\b")
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.\$\/]{10,}\b")

# 4. Key-Value Secret Assignments
KV_SECRET_PATTERN = re.compile(
    r"""(?i)(?P<prefix>\b(?:api[_-]?key|secret|token|password|auth[_-]?token|access[_-]?token)\b\s*[:=]\s*["']?)(?P<val>[^\s"',;]+)(?P<suffix>["']?)"""
)

# 5. Git Diff Blocks
DIFF_PATTERN = re.compile(
    r"(?ms)(?:diff --git\s+[^\n]+|@@\s+-[0-9]+(?:,[0-9]+)?\s+\+[0-9]+(?:,[0-9]+)?\s+@@).*?(?=\n\n|\Z)"
)

# 6. Stack Traces
STACK_TRACE_PATTERN = re.compile(
    r"(?s)Traceback \(most recent call last\):.*?(?=(?:\n[A-Za-z0-9_]+Error:|\nException:|\Z))(?:\n[A-Za-z0-9_]+Error:[^\n]*|\nException:[^\n]*)?"
)
FILE_LINE_TRACE_PATTERN = re.compile(
    r"(?m)^\s*File \".+\", line \d+, in .+$"
)


def redact_sensitive_data(text: str) -> str:
    """
    Redact all sensitive data from log text:
    - API keys (cw_...)
    - Webhook URLs (hooks.slack.com, webhook URLs)
    - Tokens (sk-..., ghp_..., AKIA..., Bearer tokens, passwords, secrets)
    - Git diffs
    - Stack traces
    """
    if not text:
        return text

    # Redact API keys
    redacted = CW_KEY_PATTERN.sub("[REDACTED_KEY]", text)

    # Redact Webhook URLs
    redacted = WEBHOOK_URL_PATTERN.sub("[REDACTED_WEBHOOK_URL]", redacted)

    # Redact Cloud and AI tokens
    redacted = AWS_KEY_PATTERN.sub("[REDACTED_AWS_KEY]", redacted)
    redacted = GITHUB_TOKEN_PATTERN.sub("[REDACTED_GITHUB_TOKEN]", redacted)
    redacted = SK_TOKEN_PATTERN.sub("[REDACTED_SECRET_KEY]", redacted)
    redacted = BEARER_PATTERN.sub("Bearer [REDACTED_TOKEN]", redacted)

    # Redact key-value secret assignments
    def _redact_kv(m: re.Match) -> str:
        return f"{m.group('prefix')}[REDACTED_SECRET]{m.group('suffix')}"

    redacted = KV_SECRET_PATTERN.sub(_redact_kv, redacted)

    # Redact Git diffs
    redacted = DIFF_PATTERN.sub("[DIFF REDACTED]", redacted)

    # Redact Stack Traces
    redacted = STACK_TRACE_PATTERN.sub("[STACK TRACE REDACTED]", redacted)
    redacted = FILE_LINE_TRACE_PATTERN.sub("[STACK TRACE REDACTED]", redacted)

    return redacted


_ANY_URL_PATTERN = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)(?:[^/\s\"'>@]*@)?([^/\s\"'>]+)[^\s\"'>]*")


def redact_delivery_error(text: str) -> str:
    """Redact an error for delivery or alerting: every URL is cut to scheme://host/******.

    httpx errors quote the full target URL (for webhooks the path is the credential), and
    connection errors can quote DSNs with passwords (redis://:secret@host).
    """
    if not text:
        return text
    return _ANY_URL_PATTERN.sub(r"\1\2/******", redact_sensitive_data(text))


class LogRedactionFilter(logging.Filter):
    """
    Logging filter enforcing Security Addendum Section C3:
    'Logs must never contain keys, webhook URLs, tokens, diffs, or stack traces.
     Add a log filter that redacts them and a test.'
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. Pre-interpolate message if args exist so regexes don't break format specifiers
        try:
            if record.args:
                record.msg = record.getMessage()
                record.args = None
        except Exception:
            pass

        # 2. Redact all sensitive patterns from final message
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_data(record.msg)

        # 3. Redact stack traces from exception info
        if record.exc_info:
            record.exc_text = "[STACK TRACE REDACTED]"
            record.exc_info = None

        if hasattr(record, "exc_text") and record.exc_text:
            record.exc_text = "[STACK TRACE REDACTED]"

        return True
