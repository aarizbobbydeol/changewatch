"""Tests for Performance Addendum Stage 6:
1. Interval search with bisect: O(log n) binary search compared against brute force oracle.
2. Compiled CODEOWNERS: Last-match-wins semantics and cache invalidation on deploy arrival.
3. Top-K issue selection with heapq.nlargest: O(n log k) efficiency without sorting full lists.
4. Investigation Priority Queue: Redis sorted set with level separation and intra-level FIFO.
5. Precompiled single-pass regexes for secret scrubbing and trace normalization.
"""
import random
from unittest.mock import AsyncMock
import pytest

from app.cache import codeowners_cache
from app.codeowners import (
    get_or_compile_codeowners,
    invalidate_codeowners,
    parse_codeowners,
)
from app.fingerprint import normalize_stack_trace
from app.intervals import (
    IntervalIndex,
    brute_force_frame_in_diff,
    frame_in_diff,
    merge_intervals,
)
from app.priority_queue import (
    InvestigationPriority,
    get_queue_depth,
    pop_investigation,
    push_investigation,
)
from app.scrub import scrub_text
from app.topk import top_k_items


# -----------------------------------------------------------------------------
# 1. Interval Search with bisect vs Brute Force Oracle
# -----------------------------------------------------------------------------

def test_interval_merging_logic():
    """Verify overlapping, adjacent, and separate intervals merge correctly."""
    # Overlapping intervals
    ranges = [(10, 20), (15, 25), (30, 40)]
    merged = merge_intervals(ranges)
    assert merged == [(10, 25), (30, 40)]

    # Adjacent intervals (20 and 21) merge into (10, 30)
    ranges2 = [(10, 20), (21, 30)]
    assert merge_intervals(ranges2) == [(10, 30)]

    # Unsorted input with subsets
    ranges3 = [(50, 60), (10, 30), (15, 20), (5, 8)]
    assert merge_intervals(ranges3) == [(5, 8), (10, 30), (50, 60)]

    # Empty list
    assert merge_intervals([]) == []


def test_bisect_interval_search_vs_brute_force_randomized():
    """
    Generate 1,000 randomized queries across various file ranges and verify that
    IntervalIndex (bisect O(log n)) matches the brute force scan 100% of the time.
    """
    random.seed(42)
    files = ["app/main.py", "app/worker.py", "app/auth.py", "scripts/test.py"]

    raw_diff: dict[str, list[tuple[int, int]]] = {}
    indexed_diff: dict[str, IntervalIndex] = {}

    for fname in files:
        num_ranges = random.randint(1, 20)
        ranges = []
        for _ in range(num_ranges):
            start = random.randint(1, 500)
            end = start + random.randint(0, 50)
            ranges.append((start, end))

        raw_diff[fname] = ranges
        indexed_diff[fname] = IntervalIndex(ranges)

    # Test exact boundary points, points inside, points outside, and negative numbers
    for _ in range(1000):
        fname = random.choice(files + ["nonexistent/file.py"])
        line = random.randint(-5, 600)

        expected = brute_force_frame_in_diff(raw_diff, fname, line)
        actual = frame_in_diff(indexed_diff, fname, line)

        assert actual == expected, (
            f"Mismatch for {fname} line {line}: expected {expected}, got {actual}"
        )


# -----------------------------------------------------------------------------
# 2. Compiled CODEOWNERS: Last-Match-Wins & Cache Invalidation
# -----------------------------------------------------------------------------

SAMPLE_CODEOWNERS = """
# Global fallback
* @core-team @security-lead

# Specific path overrides
/app/auth/ @security-team
/app/worker.py @data-team
*.sql @db-admins

# Last rule wins! This overrides *.sql for migrations
/sql/migrations/ @migration-lead
"""


def test_codeowners_last_match_wins():
    """Verify that GitHub CODEOWNERS last-match-wins rule evaluation works accurately."""
    compiled = parse_codeowners(SAMPLE_CODEOWNERS)

    # 1. Fallback rule matches generic file
    assert compiled.resolve_owners("app/main.py") == ["@core-team", "@security-lead"]

    # 2. Path override matches /app/auth/
    assert compiled.resolve_owners("app/auth/login.py") == ["@security-team"]

    # 3. Exact file match /app/worker.py
    assert compiled.resolve_owners("app/worker.py") == ["@data-team"]

    # 4. Extension match *.sql
    assert compiled.resolve_owners("server/schema.sql") == ["@db-admins"]

    # 5. Last-match-wins: /sql/migrations/001.sql matches both *.sql and /sql/migrations/
    # The latter appears later in the file, so it MUST win!
    assert compiled.resolve_owners("sql/migrations/001_init.sql") == ["@migration-lead"]

    # 6. Leading slashes stripped properly
    assert compiled.resolve_owners("/sql/migrations/002_test.sql") == ["@migration-lead"]


def test_codeowners_cache_and_invalidation():
    """Verify CODEOWNERS caching per (project, deploy) and invalidation."""
    codeowners_cache.clear()

    project_id = 99
    deploy_id = 101

    # Initially not cached
    assert get_or_compile_codeowners(project_id, deploy_id, content=None) is None

    # Parse and cache
    compiled = get_or_compile_codeowners(project_id, deploy_id, content=SAMPLE_CODEOWNERS)
    assert compiled is not None
    assert len(compiled) == 5

    # Subsequent retrieval hits cache without needing content
    cached = get_or_compile_codeowners(project_id, deploy_id, content=None)
    assert cached is compiled

    # Invalidate specific deploy
    invalidate_codeowners(project_id, deploy_id=deploy_id)
    assert get_or_compile_codeowners(project_id, deploy_id, content=None) is None

    # Project-level invalidation
    get_or_compile_codeowners(project_id, 202, content=SAMPLE_CODEOWNERS)
    invalidate_codeowners(project_id)
    assert get_or_compile_codeowners(project_id, 202, content=None) is None


# -----------------------------------------------------------------------------
# 3. Top-K Issue Selection with heapq.nlargest
# -----------------------------------------------------------------------------

def test_top_k_selection():
    """Verify top_k_items correctly selects top K items without full sort."""
    issues = [
        {"id": 1, "title": "Crash A", "count": 15},
        {"id": 2, "title": "Crash B", "count": 250},
        {"id": 3, "title": "Crash C", "count": 3},
        {"id": 4, "title": "Crash D", "count": 89},
        {"id": 5, "title": "Crash E", "count": 42},
    ]

    top3 = top_k_items(issues, k=3)
    assert len(top3) == 3
    assert [i["count"] for i in top3] == [250, 89, 42]

    # Boundary conditions
    assert top_k_items(issues, k=0) == []
    assert len(top_k_items(issues, k=10)) == 5


# -----------------------------------------------------------------------------
# 4. Investigation Priority Queue with Redis Sorted Sets
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_investigation_priority_queue_ordering():
    """
    Verify investigation priority queue ordering:
    1. Confirmed Incidents (priority 1) popped before Critical Routes (priority 2),
       which are popped before Possible Incidents (priority 3).
    2. Strict FIFO order is preserved within each priority level.
    """
    # Mock Redis sorted set in-memory
    zset_store: dict[str, float] = {}

    mock_redis = AsyncMock()

    async def _mock_zadd(key, mapping):
        for member, score in mapping.items():
            zset_store[member] = score
        return len(mapping)

    async def _mock_zpopmin(key, count=1):
        if not zset_store:
            return []
        # Sort by score ascending
        sorted_items = sorted(zset_store.items(), key=lambda x: x[1])
        popped = sorted_items[:count]
        for m, _ in popped:
            del zset_store[m]
        return [(m.encode("utf-8"), s) for m, s in popped]

    async def _mock_zrange(key, start, stop, withscores=False):
        if not zset_store:
            return []
        sorted_items = sorted(zset_store.items(), key=lambda x: x[1])
        slice_items = sorted_items[start : stop + 1 if stop >= 0 else None]
        return [(m.encode("utf-8"), s) for m, s in slice_items]

    async def _mock_zcard(key):
        return len(zset_store)

    mock_redis.zadd.side_effect = _mock_zadd
    mock_redis.zpopmin.side_effect = _mock_zpopmin
    mock_redis.zrange.side_effect = _mock_zrange
    mock_redis.zcard.side_effect = _mock_zcard

    # Push items in interleaved order
    t0 = 1000.0
    # Level 3: Possible incidents
    await push_investigation(mock_redis, {"id": "possible_1"}, InvestigationPriority.POSSIBLE_INCIDENT, timestamp=t0 + 1)
    await push_investigation(mock_redis, {"id": "possible_2"}, InvestigationPriority.POSSIBLE_INCIDENT, timestamp=t0 + 2)

    # Level 1: Confirmed incidents
    await push_investigation(mock_redis, {"id": "confirmed_1"}, InvestigationPriority.CONFIRMED_INCIDENT, timestamp=t0 + 10)
    await push_investigation(mock_redis, {"id": "confirmed_2"}, InvestigationPriority.CONFIRMED_INCIDENT, timestamp=t0 + 20)

    # Level 2: Critical route incidents
    await push_investigation(mock_redis, {"id": "critical_1"}, InvestigationPriority.CRITICAL_ROUTE_INCIDENT, timestamp=t0 + 5)
    await push_investigation(mock_redis, {"id": "critical_2"}, InvestigationPriority.CRITICAL_ROUTE_INCIDENT, timestamp=t0 + 15)

    assert await get_queue_depth(mock_redis) == 6

    # Pop all 6 and verify expected dequeue sequence:
    # 1. confirmed_1 (priority 1, ts 10)
    # 2. confirmed_2 (priority 1, ts 20)
    # 3. critical_1  (priority 2, ts 5)
    # 4. critical_2  (priority 2, ts 15)
    # 5. possible_1  (priority 3, ts 1)
    # 6. possible_2  (priority 3, ts 2)
    popped_ids = []
    for _ in range(6):
        item = await pop_investigation(mock_redis)
        assert item is not None
        popped_ids.append(item["id"])

    assert popped_ids == [
        "confirmed_1",
        "confirmed_2",
        "critical_1",
        "critical_2",
        "possible_1",
        "possible_2",
    ]

    # Queue is now empty
    assert await pop_investigation(mock_redis) is None
    assert await get_queue_depth(mock_redis) == 0


# -----------------------------------------------------------------------------
# 5. Precompiled Regexes & Short String Optimization
# -----------------------------------------------------------------------------

def test_single_pass_scrub_and_short_string_skip():
    """Verify single-pass alternation regex redacts all secrets and skips strings < 8 chars."""
    # Strings under 8 characters must be skipped immediately without regex execution
    assert scrub_text("") == ""
    assert scrub_text("abc") == "abc"
    assert scrub_text("short") == "short"

    # AWS Key
    aws_text = "Deploy error with key AKIAIOSFODNN7EXAMPLE in config"
    assert "[REDACTED_AWS_KEY]" in scrub_text(aws_text)

    # GitHub Token
    gh_text = "git token ghp_1234567890abcdef1234567890abcdef1234 in git url"
    assert "[REDACTED_GITHUB_TOKEN]" in scrub_text(gh_text)

    # Secret Key
    sk_text = "Anthropic api call sk-ant-api03-abcdefghijklmn failed"
    assert "[REDACTED_SECRET_KEY]" in scrub_text(sk_text)

    # Key-value assignment
    kv_text = 'Database connection failed: api_key="super_secret_val_123"'
    assert 'api_key="[REDACTED_SECRET]"' in scrub_text(kv_text)

    # PII Scrubbing integrated
    pii_text = "Customer email test.user@example.com reported error with IP 192.168.1.10"
    scrubbed = scrub_text(pii_text)
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_IP]" in scrubbed


def test_single_pass_stack_trace_normalization():
    """Verify stack trace normalization replaces line numbers and hex addresses."""
    trace = """
    File "/app/worker.py", line 42, in process_event
        handler = 0x7ffee4b1
    File "/app/handler.py", line 105, in run
        raise KeyError(0x102938470)
    """
    normalized = normalize_stack_trace(trace)
    assert "line 0" in normalized
    assert "line 42" not in normalized
    assert "line 105" not in normalized
    assert "0x0" in normalized
    assert "0x7ffee4b1" not in normalized
    assert "0x102938470" not in normalized

    # Short string check
    assert normalize_stack_trace("err") == "err"
