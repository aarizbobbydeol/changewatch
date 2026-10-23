"""High-performance compiled CODEOWNERS parser and evaluator.

Features:
- Compiles git glob patterns to regexes once per deploy.
- Evaluates from last rule backwards so the last-defined match wins (GitHub specification).
- Thread-safe caching in codeowners_cache with deploy-level and project-level invalidation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from app.cache import codeowners_cache

logger = logging.getLogger("changewatch.codeowners")


@dataclass(frozen=True)
class CodeownerRule:
    pattern_str: str
    regex: re.Pattern
    owners: List[str]


def glob_to_regex(pattern: str) -> re.Pattern:
    """
    Compile a GitHub CODEOWNERS glob pattern into a compiled regular expression.
    Rules:
    - If pattern starts with '/', it is anchored to the repository root.
    - If pattern ends with '/', it matches anything inside that directory.
    - If pattern has no slash, it matches in any directory (e.g. '*.js' matches 'app/main.js').
    - '*' matches anything except a slash.
    - '**' matches anything including slashes.
    """
    orig_pattern = pattern
    anchored_root = pattern.startswith("/")
    if anchored_root:
        pattern = pattern[1:]

    is_dir_only = pattern.endswith("/")
    if is_dir_only:
        pattern = pattern[:-1]

    # Escape special regex characters except * and ?
    tokens = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # '**'
                tokens.append(".*")
                i += 2
                continue
            else:
                tokens.append("[^/]*")
                i += 1
                continue
        elif c == "?":
            tokens.append("[^/]")
            i += 1
            continue
        elif c in r"\.+^$()[]{}|":
            tokens.append(f"\\{c}")
            i += 1
        else:
            tokens.append(c)
            i += 1

    regex_body = "".join(tokens)

    if is_dir_only:
        regex_body = f"{regex_body}/.*"

    if anchored_root:
        full_regex = f"^{regex_body}$"
    elif "/" in orig_pattern.rstrip("/"):
        full_regex = f"^{regex_body}$"
    else:
        full_regex = f"(?:^|/){regex_body}$"

    return re.compile(full_regex)


class CompiledCodeowners:
    """Precompiled rules evaluated with last-match-wins semantics."""

    def __init__(self, rules: List[CodeownerRule]) -> None:
        self.rules = rules

    def resolve_owners(self, filepath: str) -> List[str]:
        """
        Evaluate filepath against compiled rules.
        Iterates in reverse so that the last matching rule wins immediately.
        """
        # Normalize filepath (strip leading slashes)
        normalized = filepath.lstrip("/")

        for rule in reversed(self.rules):
            if rule.regex.search(normalized):
                return list(rule.owners)

        return []

    def __len__(self) -> int:
        return len(self.rules)


def parse_codeowners(content: str) -> CompiledCodeowners:
    """Parse raw CODEOWNERS file content into a CompiledCodeowners instance."""
    rules: List[CodeownerRule] = []

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        pattern = parts[0]
        owners = parts[1:]

        try:
            regex = glob_to_regex(pattern)
            rules.append(CodeownerRule(pattern_str=pattern, regex=regex, owners=owners))
        except Exception as exc:
            logger.warning("Failed to compile CODEOWNERS pattern '%s': %s", pattern, exc)

    return CompiledCodeowners(rules)


def get_or_compile_codeowners(
    project_id: int,
    deploy_id: int,
    content: Optional[str] = None,
) -> Optional[CompiledCodeowners]:
    """Retrieve compiled CODEOWNERS from cache or compile and cache it."""
    cache_key = (project_id, deploy_id)
    cached = codeowners_cache.get(cache_key)
    if cached is not None:
        return cached

    if content is None:
        return None

    compiled = parse_codeowners(content)
    codeowners_cache.set(cache_key, compiled, ttl=3600.0)
    return compiled


def invalidate_codeowners(project_id: int, deploy_id: Optional[int] = None) -> None:
    """Invalidate CODEOWNERS cache for a deploy or entire project."""
    if deploy_id is not None:
        codeowners_cache.delete((project_id, deploy_id))
    else:
        codeowners_cache.invalidate_project(project_id)
