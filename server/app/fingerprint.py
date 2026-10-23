import hashlib
import re
from typing import Optional

# Regex for line numbers in Python / general traces: e.g. "line 42"
LINE_NUMBER_PATTERN = re.compile(r"(?i)\bline\s+\d+\b")

# Regex for memory addresses: e.g. 0x7ffee4b1, 0x102938470
HEX_ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]+")

# Combined single-pass alternation for trace normalization
NORMALIZATION_PATTERN = re.compile(
    r"(?P<line>\bline\s+\d+\b)|(?P<addr>0x[0-9a-fA-F]+)",
    flags=re.IGNORECASE,
)


def _norm_replacer(match: re.Match) -> str:
    if match.group("line"):
        return "line 0"
    return "0x0"


def normalize_stack_trace(stack_trace: Optional[str]) -> str:
    """Normalize line numbers and memory addresses in stack traces using single-pass regex."""
    if not stack_trace:
        return ""
    if len(stack_trace) < 8:
        return stack_trace.strip()
    return NORMALIZATION_PATTERN.sub(_norm_replacer, stack_trace).strip()


def compute_fingerprint(
    event_type: str,
    exc_type: Optional[str] = None,
    stack_trace: Optional[str] = None,
    request_path: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """
    Compute deterministic SHA1 fingerprint for issue grouping.
    - For slow_request: sha1 of (request_path + "slow")
    - For error: sha1 of (exc_type + normalized stack trace)
    """
    if event_type == "slow_request":
        base = f"{request_path or ''}:slow"
    else:
        norm_trace = normalize_stack_trace(stack_trace)
        if norm_trace:
            base = f"{exc_type or 'Error'}:{norm_trace}"
        else:
            # Fallback if no stack trace is available
            base = f"{exc_type or 'Error'}:{message or ''}"

    return hashlib.sha1(base.encode("utf-8")).hexdigest()
