import logging
from typing import Optional
import redis.asyncio as aioredis
from app.config import settings

logger = logging.getLogger(__name__)

_redis_client: Optional[aioredis.Redis] = None


async def init_redis(url: Optional[str] = None) -> aioredis.Redis:
    """Initialize the global Redis connection."""
    global _redis_client
    redis_url = url or settings.REDIS_URL
    logger.info("Initializing Redis connection")
    kwargs = {"decode_responses": True}
    if settings.REDIS_PASSWORD and "@" not in redis_url:
        kwargs["password"] = settings.REDIS_PASSWORD
    _redis_client = aioredis.from_url(redis_url, **kwargs)
    return _redis_client


async def close_redis() -> None:
    """Close the global Redis connection."""
    global _redis_client
    if _redis_client is not None:
        logger.info("Closing Redis connection")
        await _redis_client.aclose()
        _redis_client = None


def get_redis() -> aioredis.Redis:
    """Retrieve active Redis client, raising if not initialized."""
    if _redis_client is None:
        raise RuntimeError("Redis connection is not initialized")
    return _redis_client


async def check_redis_health() -> bool:
    """Check whether Redis is alive and responding to ping."""
    if _redis_client is None:
        return False
    try:
        res = await _redis_client.ping()
        return bool(res)
    except Exception as exc:
        logger.error("Redis health check failed", exc_info=exc)
        return False
