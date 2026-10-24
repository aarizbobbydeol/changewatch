"""Changed files and added line ranges from a unified diff (git diff / git show output).

Deploys sent with only a diff (the GitHub Action, curl) still get per-file line ranges, so an
alert can say that the failing line was changed in this release.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _strip_prefix(path: str, prefix: str) -> str:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path[len(prefix):] if path.startswith(prefix) else path


def _ranges(lines: List[int]) -> List[List[int]]:
    out: List[List[int]] = []
    for n in lines:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def parse_unified_diff(diff: str, max_files: int = 50) -> List[Dict[str, object]]:
    """[{"path", "status", "added_ranges"}] for each file in the diff (new-side line numbers)."""
    files: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    added: List[int] = []
    new_line: Optional[int] = None  # None outside a hunk: header lines are only read there

    def finish() -> None:
        if current is not None and current.get("path"):
            current["added_ranges"] = _ranges(added)
            files.append(current)

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            finish()
            current, added, new_line = {"path": None, "status": "modified"}, [], None
            parts = line[len("diff --git "):].split(" b/", 1)
            if len(parts) == 2:
                current["path"] = parts[1].strip().strip('"')
            continue
        if current is None:
            continue
        if new_line is None:
            if line.startswith("new file mode"):
                current["status"] = "added"
            elif line.startswith("deleted file mode"):
                current["status"] = "deleted"
            elif line.startswith("rename to "):
                current["status"] = "renamed"
                current["path"] = line[len("rename to "):].strip()
            elif line.startswith("+++ ") and line[4:].strip() != "/dev/null":
                current["path"] = _strip_prefix(line[4:], "b/")
            elif line.startswith("--- ") and not current.get("path") and line[4:].strip() != "/dev/null":
                current["path"] = _strip_prefix(line[4:], "a/")
        match = _HUNK.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None:
            continue
        if line.startswith("+"):
            added.append(new_line)
            new_line += 1
        elif line.startswith("-") or line.startswith("\\"):
            pass  # removed line, or "\ No newline at end of file"
        else:
            new_line += 1  # context line
    finish()
    return files[:max_files]
