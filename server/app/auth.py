import hashlib
import hmac
import logging
from typing import Literal, Optional
from fastapi import Depends, Header, HTTPException, status
from app.cache import api_key_cache
from app.db import get_connection
from app.queue import get_redis

logger = logging.getLogger(__name__)

API_KEY_HEADER_NAME = "X-API-Key"
CACHE_TTL_SECONDS = 60


def hash_api_key(api_key: str) -> str:
    """Compute SHA-256 hash of API key for secure storage and constant-time lookup."""
    return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()


async def get_project_id_by_api_key(
    api_key: str,
    role: Literal["any", "ingest", "read"] = "any",
) -> int:
    """
    Resolve api_key to project_id, enforcing key role permissions (ingest vs read).
    Uses SHA-256 hashing and constant-time comparison.
    Caches successful lookups in in-memory LRU+TTL cache and Redis for 60 seconds.
    """
    cleaned_key = api_key.strip()
    key_hash = hash_api_key(cleaned_key)
    cache_key = f"apikey:{role}:{cleaned_key}" if role != "any" else f"apikey:{cleaned_key}"

    # 1. Check in-memory LRU+TTL cache
    in_memory = api_key_cache.get(cache_key)
    if in_memory is not None:
        return in_memory

    # 2. Check Redis cache
    try:
        redis = get_redis()
        cached_id = await redis.get(cache_key)
        if cached_id is not None:
            pid = int(cached_id)
            api_key_cache.set(cache_key, pid, ttl=CACHE_TTL_SECONDS)
            return pid
    except Exception as exc:
        logger.warning("Redis cache lookup failed for api_key, falling back to DB: %s", exc)
        redis = None

    # 2. Database query: lookup by hash or legacy plaintext key
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, ingest_key_hash, read_key_hash, api_key
            FROM projects
            WHERE ingest_key_hash = $1
               OR read_key_hash = $1
               OR api_key = $2
            LIMIT 1
            """,
            key_hash,
            cleaned_key,
        )

    if not row:
        logger.warning("Authentication failed: invalid API key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    row_dict = dict(row) if not isinstance(row, dict) else row
    project_id = int(row_dict["id"])
    db_ingest_hash = row_dict.get("ingest_key_hash") or ""
    db_read_hash = row_dict.get("read_key_hash") or ""
    db_api_key = row_dict.get("api_key") or ""

    has_hash_columns = bool(db_ingest_hash or db_read_hash or db_api_key)
    if not has_hash_columns:
        is_ingest = True
        is_read = True
    else:
        is_ingest = (
            (bool(db_ingest_hash) and hmac.compare_digest(db_ingest_hash, key_hash))
            or (bool(db_api_key) and hmac.compare_digest(db_api_key, cleaned_key))
        )
        is_read = (
            (bool(db_read_hash) and hmac.compare_digest(db_read_hash, key_hash))
            or (bool(db_api_key) and hmac.compare_digest(db_api_key, cleaned_key))
        )

    # 3. Role enforcement
    if role == "ingest":
        if not is_ingest:
            if is_read:
                logger.warning("Authorization failure: read key used on ingest endpoint for project %d", project_id)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Read key cannot be used for ingestion",
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )
    elif role == "read":
        if not is_read:
            if is_ingest:
                logger.warning("Authorization failure: ingest key used on read endpoint for project %d", project_id)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Ingest key cannot be used to read data",
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )
    else:  # role == "any"
        if not (is_ingest or is_read):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )

    # 4. Cache verified project_id in Redis and in-memory LRU for 60 seconds
    api_key_cache.set(cache_key, project_id, ttl=CACHE_TTL_SECONDS)
    if redis is not None:
        try:
            await redis.setex(cache_key, CACHE_TTL_SECONDS, str(project_id))
        except Exception as exc:
            logger.warning("Failed to store API key in Redis cache: %s", exc)

    return project_id


def get_api_key(
    x_api_key: Optional[str] = Header(None, alias=API_KEY_HEADER_NAME)
) -> str:
    """FastAPI dependency to extract raw X-API-Key header."""
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )
    return x_api_key.strip()


async def require_ingest_project_id(
    api_key: str = Depends(get_api_key),
) -> int:
    """FastAPI dependency requiring valid ingest key."""
    return await get_project_id_by_api_key(api_key, role="ingest")


async def require_read_project_id(
    api_key: str = Depends(get_api_key),
) -> int:
    """FastAPI dependency requiring valid read key."""
    return await get_project_id_by_api_key(api_key, role="read")


async def require_project_id(
    api_key: str = Depends(get_api_key),
) -> int:
    """FastAPI dependency allowing either valid ingest or read key."""
    return await get_project_id_by_api_key(api_key, role="any")


ADMIN_KEY_HEADER_NAME = "X-Admin-Key"


async def require_admin_key(
    x_admin_key: Optional[str] = Header(None, alias=ADMIN_KEY_HEADER_NAME),
    x_api_key: Optional[str] = Header(None, alias=API_KEY_HEADER_NAME),
) -> bool:
    """FastAPI dependency requiring valid admin key for owner/admin endpoints."""
    from app.config import settings

    provided_key = (x_admin_key or x_api_key or "").strip()
    configured_key = settings.ADMIN_API_KEY.strip()

    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin endpoints are disabled (ADMIN_API_KEY is not set)",
        )
    if not provided_key or not hmac.compare_digest(provided_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin key required",
        )
    return True
