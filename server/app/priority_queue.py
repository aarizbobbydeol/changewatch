"""Investigation Priority Queue using Redis Sorted Sets.

Priority Ordering:
1. Confirmed Incidents (highest priority)
2. Incidents on Critical Routes
3. Possible Incidents (lowest priority)

First-In, First-Out (FIFO) is strictly preserved within each priority level.
"""
from __future__ import annotations

import json
import logging
import time
from enum import IntEnum
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("changewatch.priority_queue")

REDIS_INVESTIGATION_KEY = "investigations:queue"


class InvestigationPriority(IntEnum):
    CONFIRMED_INCIDENT = 1
    CRITICAL_ROUTE_INCIDENT = 2
    POSSIBLE_INCIDENT = 3


def compute_priority_score(priority: InvestigationPriority | int, timestamp: Optional[float] = None) -> float:
    """
    Compute monotonic floating score where lower score = higher dequeue priority.
    - Level multiplier: 10,000,000,000.0 ensures levels are completely non-overlapping.
    - Timestamp addition ensures FIFO ordering within each priority tier.
    """
    ts = timestamp if timestamp is not None else time.time()
    level = int(priority)
    return (level * 10_000_000_000.0) + ts


async def push_investigation(
    redis: Any,
    item: Dict[str, Any],
    priority: InvestigationPriority | int = InvestigationPriority.POSSIBLE_INCIDENT,
    timestamp: Optional[float] = None,
    queue_key: str = REDIS_INVESTIGATION_KEY,
) -> float:
    """Push an investigation task onto the priority queue."""
    ts = timestamp if timestamp is not None else time.time()
    score = compute_priority_score(priority, ts)

    payload = dict(item)
    payload["_priority"] = int(priority)
    payload["_queued_at"] = ts

    serialized = json.dumps(payload, sort_keys=True)
    await redis.zadd(queue_key, {serialized: score})
    return score


async def pop_investigation(
    redis: Any,
    queue_key: str = REDIS_INVESTIGATION_KEY,
) -> Optional[Dict[str, Any]]:
    """Pop the highest priority (lowest score) investigation item."""
    res = await redis.zpopmin(queue_key, count=1)
    if not res:
        return None

    # res is list of (member, score) or (member, score) tuples
    member, score = res[0]
    if isinstance(member, bytes):
        member = member.decode("utf-8")

    try:
        return json.loads(member)
    except Exception as exc:
        logger.error("Failed to deserialize popped investigation item: %s", exc)
        return None


async def peek_investigation(
    redis: Any,
    queue_key: str = REDIS_INVESTIGATION_KEY,
) -> Optional[Tuple[Dict[str, Any], float]]:
    """Peek at the highest priority item without removing it."""
    res = await redis.zrange(queue_key, 0, 0, withscores=True)
    if not res:
        return None

    member, score = res[0]
    if isinstance(member, bytes):
        member = member.decode("utf-8")

    try:
        return json.loads(member), float(score)
    except Exception as exc:
        logger.error("Failed to deserialize peeked investigation item: %s", exc)
        return None


async def get_queue_depth(
    redis: Any,
    queue_key: str = REDIS_INVESTIGATION_KEY,
) -> int:
    """Return total number of items waiting in the priority queue."""
    return await redis.zcard(queue_key)
