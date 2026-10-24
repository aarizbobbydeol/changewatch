"""Tell ChangeWatch about a deploy: commit, diff, changed files with line ranges, CODEOWNERS.

Runs inside the GitHub Action (python3 standard library only). Reads only the local git checkout;
it never writes to the repository. Settings come from environment variables set by action.yml.
Never fails the workflow: problems are printed as warnings.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_EXCLUDES = ["**/.env*", "**/*.pem", "**/*.key", "**/id_rsa*", "secrets/**"]
MAX_DIFF = 20000
MAX_FILE = 20000
MAX_FILES = 50
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def git(*args: str, max_bytes: int = 5_000_000) -> str:
    out = subprocess.run(["git", *args], capture_output=True, timeout=60)
    if out.returncode != 0:
        return ""
    return out.stdout[:max_bytes].decode("utf-8", errors="replace")


def changed_files(diff: str) -> list:
    """Paths, status and added line ranges (new-side numbers) from a unified diff."""
    files, current, added, line_no = [], None, [], None

    def finish():
        if current and current.get("path"):
            ranges = []
            for n in added:
                if ranges and n == ranges[-1][1] + 1:
                    ranges[-1][1] = n
                else:
                    ranges.append([n, n])
            current["added_ranges"] = ranges
            files.append(current)

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            finish()
            parts = line[len("diff --git "):].split(" b/", 1)
            current, added, line_no = {"path": parts[1].strip('"') if len(parts) == 2 else None, "status": "modified"}, [], None
            continue
        if current is None:
            continue
        if line_no is None:
            if line.startswith("new file mode"):
                current["status"] = "added"
            elif line.startswith("deleted file mode"):
                current["status"] = "deleted"
            elif line.startswith("rename to "):
                current["status"], current["path"] = "renamed", line[len("rename to "):].strip()
        match = _HUNK.match(line)
        if match:
            line_no = int(match.group(1))
        elif line_no is not None:
            if line.startswith("+"):
                added.append(line_no)
                line_no += 1
            elif not line.startswith(("-", "\\")):
                line_no += 1
    finish()
    return files


def main() -> int:
    url = os.environ.get("CW_URL", "").rstrip("/")
    key = os.environ.get("CW_KEY", "")
    sha = os.environ.get("CW_SHA") or git("rev-parse", "HEAD").strip()
    before = os.environ.get("CW_BEFORE", "")
    mode = (os.environ.get("CW_MODE") or "minimal").strip().lower()
    excludes = DEFAULT_EXCLUDES + [p.strip() for p in os.environ.get("CW_EXCLUDE", "").split(",") if p.strip()]
    pathspec = ["--", "."] + [f":(exclude,glob){p}" for p in excludes]

    if not url or not key:
        print("::warning::ChangeWatch: changewatch_url and changewatch_api_key are required")
        return 0
    if not before or set(before) == {"0"} or not git("cat-file", "-t", before).strip():
        before = git("rev-parse", f"{sha}^").strip()  # first push, or force push: diff against the parent

    if before:
        full_diff = git("diff", "--no-color", "--no-ext-diff", before, sha, *pathspec)
    elif git("rev-parse", "--is-shallow-repository").strip() == "true":
        # actions/checkout fetches one commit by default: its parent is missing, and "git show"
        # would list every file as new. Send the deploy without a diff and say how to fix it.
        print("::warning::ChangeWatch: shallow checkout, no diff sent. Use actions/checkout with fetch-depth: 2")
        full_diff = ""
    else:
        full_diff = git("show", "--no-color", "--format=", sha, *pathspec)  # the repository's first commit
    files = [f for f in changed_files(full_diff)][:MAX_FILES]
    author = git("log", "-1", "--pretty=%ae", sha).strip() or os.environ.get("GITHUB_ACTOR", "")
    payload = {
        "commit_sha": sha[:64],
        "author": author[:200],
        "message": git("log", "-1", "--pretty=%B", sha).strip()[:2000],
        "environment": (os.environ.get("CW_ENV") or "prod")[:50],
        "diff": full_diff[:MAX_DIFF],
        "files": [],
    }
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", os.environ.get("GITHUB_REPOSITORY", "")):
        payload["repository"] = os.environ["GITHUB_REPOSITORY"]  # lets ephemeral mode read files
    for f in files:
        item = {"path": f["path"][:1000], "status": f["status"], "added_ranges": f["added_ranges"], "authors": [author][:1]}
        if mode == "full" and f["status"] != "deleted":
            raw = subprocess.run(["git", "show", f"{sha}:{f['path']}"], capture_output=True, timeout=60).stdout
            if raw and b"\x00" not in raw[:8000]:  # text files only
                item["content"] = raw[:MAX_FILE].decode("utf-8", errors="replace")
        payload["files"].append(item)
    for candidate in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        owners = git("show", f"{sha}:{candidate}")
        if owners:
            payload["codeowners"] = owners[:20000]
            break

    req = urllib.request.Request(
        f"{url}/v1/deploys", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key, "User-Agent": "changewatch-action/0.2"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read() or b"{}")
        print(f"ChangeWatch: deploy {sha[:8]} registered (id {body.get('deploy_id')}, "
              f"{len(payload['files'])} changed files, CODEOWNERS {'sent' if 'codeowners' in payload else 'not found'})")
    except urllib.error.HTTPError as exc:
        print(f"::warning::ChangeWatch: deploy not registered (HTTP {exc.code})")
    except Exception as exc:
        print(f"::warning::ChangeWatch: deploy not registered ({type(exc).__name__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
