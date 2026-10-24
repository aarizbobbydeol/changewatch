"""Per-project counts of events the API turned away, so the dashboard can show them.

Rate-limit rejections (429) and backpressure shedding happen in the API before anything reaches
Postgres, so they are counted in Redis: one counter per project, day and reason.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)

DROP_REASONS = ("rate_limit", "backpressure")
KEEP_DAYS = 400  # longer than the longest analytics period (365 days)


def _key(project_id: int, day: date, reason: str) -> str:
    return f"ingest:dropped:{project_id}:{day.isoformat()}:{reason}"


async def count_dropped(redis: Any, project_id: int, reason: str, n: int) -> None:
    """Add `n` dropped events for today. Never raises: counting must not break ingest."""
    if n <= 0 or redis is None:
        return
    key = _key(project_id, datetime.now(timezone.utc).date(), reason)  # UTC, like the readers
    try:
        await redis.incrby(key, n)
        await redis.expire(key, KEEP_DAYS * 86400)
    except Exception as exc:
        logger.warning("Could not count dropped events: %s", exc)


async def dropped_totals(redis: Any, project_id: int, start: date, end: date) -> Dict[str, int]:
    """Dropped events per reason between `start` and `end` (inclusive)."""
    totals = {reason: 0 for reason in DROP_REASONS}
    if redis is None or end < start:
        return totals
    days = [start + timedelta(days=i) for i in range(min((end - start).days, KEEP_DAYS) + 1)]
    keys = [_key(project_id, d, reason) for reason in DROP_REASONS for d in days]
    try:
        values = await redis.mget(keys)
    except Exception as exc:
        logger.warning("Could not read dropped-event counts: %s", exc)
        return totals
    for i, value in enumerate(values or []):
        totals[DROP_REASONS[i // len(days)]] += int(value or 0)
    return totals
