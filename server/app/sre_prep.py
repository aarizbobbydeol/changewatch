"""Deploy-time preparation and error frame analysis helpers for AI SRE.

Stage A requirements:
- parse_stack_trace(): extracts file, line, function, code from Python stack trace.
- innermost_project_frame(): finds deepest frame within the project (skips site-packages, standard library).
- frame_in_diff(): checks whether an error line falls within added ranges (+/- 3 lines tolerance).
- resolve_owners(): CODEOWNERS matching with last-match-wins semantics, fallback to top authors.
- classify_file(): determines if a path is a dependency file or a configuration file.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.codeowners import parse_codeowners


@dataclass
class StackFrame:
    file: str
    line: int
    function: str
    code: Optional[str] = None


FRAME_REGEX = re.compile(
    r'^\s*File\s+"([^"]+)",\s+line\s+(\d+),\s+in\s+([^\n]+)(?:\n\s*(.*))?',
    re.MULTILINE,
)


def parse_stack_trace(stack_trace: str) -> List[StackFrame]:
    """
    Parse a Python traceback into structured StackFrame objects.
    Preserves order from outermost to innermost frame.
    """
    if not stack_trace:
        return []

    lines = stack_trace.splitlines()
    frames: List[StackFrame] = []
    i = 0
    n = len(lines)

    frame_pattern = re.compile(r'^\s*File\s+"([^"]+)",\s+line\s+(\d+),\s+in\s+(.+)$')

    while i < n:
        line = lines[i]
        match = frame_pattern.match(line)
        if match:
            filepath = match.group(1).strip()
            lineno = int(match.group(2).strip())
            function_name = match.group(3).strip()
            i += 1
            code_line = None
            if i < n:
                next_line = lines[i]
                # In standard python tracebacks, code lines are indented and don't match frame_pattern
                if next_line.startswith((" ", "\t")) and not frame_pattern.match(next_line):
                    code_line = next_line.strip()
                    i += 1
            frames.append(
                StackFrame(
                    file=filepath,
                    line=lineno,
                    function=function_name,
                    code=code_line,
                )
            )
        else:
            i += 1

    return frames



def is_project_frame(frame: StackFrame) -> bool:
    """
    Check if a frame belongs to the application rather than standard library or 3rd-party packages.
    """
    filepath = frame.file
    if not filepath:
        return False

    # Skip internal/runtime strings
    if filepath.startswith(("<frozen", "<string>", "<stdin>", "<doctest")):
        return False

    # Skip third-party packages
    if "site-packages" in filepath or "dist-packages" in filepath or "node_modules" in filepath:
        return False

    # Skip python standard library directories
    norm = filepath.replace("\\", "/")
    if "/usr/lib/python" in norm or "/usr/local/lib/python" in norm:
        return False
    if "/System/Library/Frameworks/Python" in norm:
        return False
    if norm.startswith(("/lib/python", "/usr/include/python")):
        return False

    # Check for venv path signatures
    if "/.venv/lib/" in norm or "/venv/lib/" in norm or "/env/lib/" in norm:
        return False

    return True


def innermost_project_frame(frames: List[StackFrame]) -> Optional[StackFrame]:
    """
    Find the innermost (deepest) frame in the stack that belongs to the user's project.
    Iterates backwards from the point of exception.
    If no frames match project heuristics, returns the deepest frame available as fallback.
    """
    if not frames:
        return None

    for frame in reversed(frames):
        if is_project_frame(frame):
            return frame

    # Fallback to the innermost frame if everything was classified as external
    return frames[-1]


def frame_in_diff(frame: StackFrame, deploy_files: List[Dict[str, Any]]) -> bool:
    """
    Check if the frame's file and line number fall within any added/modified ranges
    of the release diff, with a tolerance of +/- 3 lines.
    """
    if not frame or not deploy_files:
        return False

    frame_norm = os.path.normpath(frame.file).replace("\\", "/")

    for df in deploy_files:
        df_path = df.get("path") or ""
        df_norm = os.path.normpath(df_path).replace("\\", "/")

        # Match either exact path or path suffix (e.g. /app/routes/users.py matches routes/users.py)
        if frame_norm == df_norm or frame_norm.endswith("/" + df_norm) or df_norm.endswith("/" + frame_norm):
            added_ranges = df.get("added_ranges") or []
            for r in added_ranges:
                if isinstance(r, (list, tuple)) and len(r) == 2:
                    start, end = int(r[0]), int(r[1])
                    if (start - 3) <= frame.line <= (end + 3):
                        return True

    return False


def resolve_owners(
    path: str,
    codeowners_content: Optional[str] = None,
    authors: Optional[List[str]] = None,
) -> List[str]:
    """
    Resolve owners for a given file path.
    1. Evaluates CODEOWNERS if content is provided (last matching rule wins).
    2. Falls back to file's top git authors.
    3. Returns empty list if neither matches.
    """
    if codeowners_content:
        compiled = parse_codeowners(codeowners_content)
        owners = compiled.resolve_owners(path)
        if owners:
            return owners

    if authors:
        return [a for a in authors if a]

    return []


def classify_file(path: str) -> tuple[bool, bool]:
    """
    Classify whether a file path is a dependency file or a configuration file.
    Returns (is_dependency_file, is_config_file).

    Rules:
    - Dependency files: requirements*.txt, pyproject.toml, poetry.lock, Pipfile*, uv.lock, setup.py, package.json
    - Config files: Dockerfile*, docker-compose*.yml, *.yaml, *.yml, *.toml, *.ini, *.cfg, .env.example,
      or anything under migrations/ or alembic/
    """
    basename = os.path.basename(path)
    lower_path = path.lower().replace("\\", "/")

    # Check dependency file
    is_dep = False
    dep_exact = {"pyproject.toml", "poetry.lock", "uv.lock", "setup.py", "package.json"}
    if basename in dep_exact:
        is_dep = True
    elif fnmatch.fnmatch(basename, "requirements*.txt") or fnmatch.fnmatch(basename, "Pipfile*"):
        is_dep = True

    # Check config file
    is_cfg = False
    if (
        fnmatch.fnmatch(basename, "Dockerfile*")
        or fnmatch.fnmatch(basename, "docker-compose*.yml")
        or fnmatch.fnmatch(basename, "docker-compose*.yaml")
        or basename == ".env.example"
    ):
        is_cfg = True
    elif any(basename.endswith(ext) for ext in (".yaml", ".yml", ".toml", ".ini", ".cfg")):
        is_cfg = True
    elif "migrations/" in lower_path or "alembic/" in lower_path:
        is_cfg = True

    return is_dep, is_cfg
