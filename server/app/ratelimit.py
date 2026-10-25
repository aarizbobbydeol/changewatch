import hashlib
import logging
import time
from typing import Tuple
from app.config import settings
from app.queue import get_redis

logger = logging.getLogger(__name__)


async def check_rate_limit(
    api_key: str,
    event_count: int,
    is_heartbeat: bool = False,
) -> Tuple[bool, int, int]:
    """
    Check rate limit for an API key in 1-minute buckets in Redis.
    Normal events limit: settings.RATE_LIMIT_PER_MIN (default 600).
    Heartbeat events limit: settings.HEARTBEAT_LIMIT_PER_MIN (default 1200, one per app process).

    Returns:
        (allowed: bool, current_usage: int, limit: int)
    """
    if event_count <= 0:
        return True, 0, settings.RATE_LIMIT_PER_MIN

    limit = settings.HEARTBEAT_LIMIT_PER_MIN if is_heartbeat else settings.RATE_LIMIT_PER_MIN
    prefix = "ratelimit:heartbeat" if is_heartbeat else "ratelimit"
    minute_bucket = int(time.time() // 60)
    # Hash, not the key itself (Redis keys are visible to SCAN and saved to disk)
    key = f"{prefix}:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:32]}:{minute_bucket}"

    try:
        redis = get_redis()
        pipe = redis.pipeline()
        pipe.incrby(key, event_count)
        pipe.expire(key, 120)  # Retain for 2 minutes
        results = await pipe.execute()
        current_count = int(results[0])

        if current_count > limit:
            logger.warning(
                "Rate limit exceeded for key %s: %d > %d",
                api_key[:10] + "...",
                current_count,
                limit,
            )
            return False, current_count, limit

        return True, current_count, limit

    except Exception as exc:
        logger.error("Redis rate limit check failed: %s. Failing open.", exc)
        return True, 0, limit
