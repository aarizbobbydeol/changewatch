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


_UUID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_LONG_HEX_PATTERN = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_NUMBER_PATTERN = re.compile(r"\d+")


_FRAME_START = re.compile(r"^\s*(?:File\s|at\s)")


def _is_frame_line(line: str) -> bool:
    """Frames and code lines are indented in Python, JavaScript, Java and Go traces; a frame sent
    without indentation still names a file and a (normalised) line."""
    return line[:1] in (" ", "\t") or "line 0" in line or bool(_FRAME_START.match(line))


def _frame_lines(norm_trace: str) -> str:
    """The frames and code lines of a trace. The exception message and chain headers are left out:
    a message that carries an id or a value would otherwise make every event a new issue."""
    return "\n".join(line.rstrip() for line in norm_trace.splitlines() if line.strip() and _is_frame_line(line))


def normalize_message(text: str) -> str:
    """Ids, hex and numbers in a message replaced, so `order 1234` and `order 5678` group together."""
    text = _UUID_PATTERN.sub("<id>", text)
    text = _LONG_HEX_PATTERN.sub("<id>", text)
    return _NUMBER_PATTERN.sub("0", text)


def normalize_path(path: Optional[str]) -> str:
    """A request path with ids replaced by ':id' (and no query string): /users/42/orders -> /users/:id/orders."""
    if not path:
        return ""
    path = path.split("?", 1)[0]
    segments = []
    for seg in path.split("/"):
        if seg.isdigit() or _UUID_PATTERN.fullmatch(seg) or _LONG_HEX_PATTERN.fullmatch(seg):
            segments.append(":id")
        else:
            segments.append(seg)
    return "/".join(segments)


def compute_fingerprint(
    event_type: str,
    exc_type: Optional[str] = None,
    stack_trace: Optional[str] = None,
    request_path: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """
    Compute deterministic SHA1 fingerprint for issue grouping.
    - For slow_request: sha1 of (path with ids replaced + "slow")
    - For error: sha1 of (exc_type + the trace's frame lines, line numbers normalised); without
      frames, exc_type + the message with ids and numbers replaced
    """
    if event_type == "slow_request":
        base = f"{normalize_path(request_path)}:slow"
    else:
        norm_trace = normalize_stack_trace(stack_trace)
        frames = _frame_lines(norm_trace)
        if frames:
            base = f"{exc_type or 'Error'}:{frames}"
        else:
            # No frames: group by the message, without the values that change per event
            base = f"{exc_type or 'Error'}:{normalize_message(norm_trace or message or '')}"

    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def legacy_fingerprint(
    event_type: str,
    exc_type: Optional[str] = None,
    stack_trace: Optional[str] = None,
    request_path: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """The fingerprint before version 0.2 (message and raw path included). The worker uses it once
    to move an existing issue to its new fingerprint, so an upgrade does not make every known error
    look new (and the next deploy look like a regression)."""
    if event_type == "slow_request":
        base = f"{request_path or ''}:slow"
    else:
        norm_trace = normalize_stack_trace(stack_trace)
        base = f"{exc_type or 'Error'}:{norm_trace}" if norm_trace else f"{exc_type or 'Error'}:{message or ''}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()
