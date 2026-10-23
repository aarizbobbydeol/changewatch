"""ChangeWatch High-Performance Caching & Sliding-Window Structures.

Implements Performance Addendum Section 4:
1. Thread-safe LRU + TTL caches for:
   - API key to project_id (60s)
   - (project_id, environment, release) to deploy_id (invalidated on new deploy arrival)
   - (project_id, fingerprint) to issue_id
   - deploy_id to compiled CODEOWNERS rules
2. Redis Sliding-Window Counters:
   - 10-second atomic time buckets with TTL expiration in one pipeline.
   - Multi-bucket sliding summation across processes for instant alerts.
"""
from __future__ import annotations

from collections import OrderedDict
import asyncio
import threading
import time
from typing import Any, Dict, Generic, List, Optional, Tuple, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class LRUTTLCache(Generic[K, V]):
    """Thread-safe LRU cache with per-item TTL expiration."""

    def __init__(self, maxsize: int = 5000, default_ttl: float = 60.0):
        self.maxsize = maxsize
        self.default_ttl = default_ttl
        self._cache: OrderedDict[K, Tuple[V, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> Optional[V]:
        now = time.monotonic()
        with self._lock:
            if key not in self._cache:
                return None
            val, expires_at = self._cache[key]
            if now > expires_at:
                del self._cache[key]
                return None
            # Move to MRU end
            self._cache.move_to_end(key)
            return val

    def set(self, key: K, value: V, ttl: Optional[float] = None) -> None:
        duration = ttl if ttl is not None else self.default_ttl
        expires_at = time.monotonic() + duration
        with self._lock:
            if key in self._cache:
                self._cache[key] = (value, expires_at)
                self._cache.move_to_end(key)
                return

            if len(self._cache) >= self.maxsize:
                # Evict least recently used item
                self._cache.popitem(last=False)

            self._cache[key] = (value, expires_at)

    def invalidate(self, key: K) -> bool:
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    delete = invalidate

    def invalidate_project(self, project_id: int) -> int:
        """Invalidate all keys matching project_id in tuple keys."""
        removed = 0
        with self._lock:
            for k in list(self._cache.keys()):
                if isinstance(k, tuple) and len(k) > 0 and k[0] == project_id:
                    del self._cache[k]
                    removed += 1
        return removed

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


# Global in-memory LRU + TTL caches
api_key_cache = LRUTTLCache[str, Tuple[int, str]](maxsize=10000, default_ttl=60.0)
deploy_lookup_cache = LRUTTLCache[Tuple[int, str, Optional[str]], Optional[int]](maxsize=5000, default_ttl=300.0)
issue_lookup_cache = LRUTTLCache[Tuple[int, str], int](maxsize=10000, default_ttl=300.0)
codeowners_cache = LRUTTLCache[int, Any](maxsize=1000, default_ttl=3600.0)


def invalidate_deploy_cache(project_id: int) -> int:
    """Invalidate cached deploy lookups for a project upon new deploy registration."""
    return deploy_lookup_cache.invalidate_project(project_id)


# ---------------------------------------------------------------------------
# Redis Sliding-Window Counter Utilities (10-second buckets, sum last 6)
# ---------------------------------------------------------------------------


async def increment_sliding_window_counter(
    redis: Any,
    project_id: int,
    deploy_id: Optional[int],
    issue_id: int,
    count: int = 1,
    bucket_size_sec: int = 10,
    expiry_sec: int = 120,
) -> int:
    """
    Increment sliding-window counter in Redis.
    Uses 10s time buckets with automatic expiry executed in a single pipeline.
    """
    now_ts = int(time.time())
    bucket_id = now_ts // bucket_size_sec
    key = f"counter:{project_id}:{deploy_id or 0}:{issue_id}:{bucket_id}"

    pipe = redis.pipeline()
    if asyncio.iscoroutine(pipe):
        pipe = await pipe
    pipe.incrby(key, count)
    pipe.expire(key, expiry_sec)
    results = await pipe.execute()
    return int(results[0]) if results else 0


async def get_sliding_window_sum(
    redis: Any,
    project_id: int,
    deploy_id: Optional[int],
    issue_id: int,
    num_buckets: int = 6,
    bucket_size_sec: int = 10,
) -> int:
    """
    Compute sum over the last num_buckets (default 6 buckets * 10s = 60s sliding window).
    Atomic multi-key retrieval via Redis MGET.
    """
    now_ts = int(time.time())
    current_bucket = now_ts // bucket_size_sec
    keys = [
        f"counter:{project_id}:{deploy_id or 0}:{issue_id}:{current_bucket - i}"
        for i in range(num_buckets)
    ]

    values = await redis.mget(keys)
    total = 0
    for v in values:
        if v is not None:
            try:
                total += int(v)
            except (ValueError, TypeError):
                pass
    return total
