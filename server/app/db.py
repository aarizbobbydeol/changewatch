import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
import asyncpg
from app.config import settings

logger = logging.getLogger(__name__)

_db_pool: Optional[asyncpg.Pool] = None
_read_db_pool: Optional[asyncpg.Pool] = None

TENANT_ROLE = "changewatch_tenant"
_switch_to_tenant_role = False

# Connections start in system scope: system jobs (detector, outbox, retention, escalations) read
# every project, which row-level security otherwise blocks when the database user is not a
# superuser (managed Postgres). Tenant transactions turn it off; migration 022 adds the policy.
SYSTEM_SCOPE_SETTINGS = {"app.system_scope": "all"}
# Within a tenant transaction: this project only, and no system scope
TENANT_SCOPE_SQL = "SELECT set_config('app.project_id', $1, true), set_config('app.system_scope', '', true)"


async def detect_tenant_role(pool: asyncpg.Pool) -> bool:
    """Decide whether tenant transactions must run as the restricted role.

    Superusers and BYPASSRLS roles ignore row-level security, so for them tenant work runs
    as `changewatch_tenant` (created by migration 018). Called when pools start and again
    after migrations, since the role may only exist once 018 has run.
    """
    global _switch_to_tenant_role
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT (r.rolsuper OR r.rolbypassrls) AS bypasses_rls,
                       EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)
                         AND pg_has_role(current_user, $1, 'MEMBER') AS can_switch
                FROM pg_roles r WHERE r.rolname = current_user
                """,
                TENANT_ROLE,
            )
    except Exception as exc:
        logger.warning("Could not check row-level security role: %s", exc)
        return _switch_to_tenant_role
    bypasses = bool(row and row["bypasses_rls"])
    _switch_to_tenant_role = bypasses and bool(row["can_switch"])
    if bypasses and not _switch_to_tenant_role:
        logger.warning(
            "Database user bypasses row-level security and role %s is unavailable; "
            "tenant isolation relies on application filters only",
            TENANT_ROLE,
        )
    return _switch_to_tenant_role


async def init_db_pool(
    dsn: Optional[str] = None,
    min_size: int = 2,
    max_size: Optional[int] = None,
) -> asyncpg.Pool:
    """
    Initialize global asyncpg connection pools sized per process.
    Maintains separate write pool and read pool (Performance Addendum Section 5).
    The read pool enforces statement_timeout = 5000ms to prevent slow read queries from starving ingest.
    """
    global _db_pool, _read_db_pool
    database_url = dsn or settings.DATABASE_URL
    pool_max = max_size if max_size is not None else settings.DB_POOL_PER_PROCESS
    logger.info("Initializing asyncpg connection pools (min=%d, max=%d, pid=%d)", min_size, pool_max, os.getpid())

    # 1. Primary write pool
    write_kwargs = {
        "dsn": database_url,
        "min_size": min_size,
        "max_size": pool_max,
        "server_settings": dict(SYSTEM_SCOPE_SETTINGS),
    }
    if settings.DATABASE_SSL:
        write_kwargs["ssl"] = "require"
    _db_pool = await asyncpg.create_pool(**write_kwargs)

    # 2. Separate read pool with 5000ms statement_timeout
    read_kwargs = {
        "dsn": database_url,
        "min_size": min_size,
        "max_size": pool_max,
        "server_settings": {"statement_timeout": "5000ms", **SYSTEM_SCOPE_SETTINGS},
    }
    if settings.DATABASE_SSL:
        read_kwargs["ssl"] = "require"
    try:
        _read_db_pool = await asyncpg.create_pool(**read_kwargs)
    except Exception as exc:
        logger.warning("Could not initialize dedicated read pool (%s), falling back to primary pool", exc)
        _read_db_pool = None

    await detect_tenant_role(_db_pool)
    return _db_pool


async def close_db_pool() -> None:
    """Close the global asyncpg connection pools."""
    global _db_pool, _read_db_pool
    if _read_db_pool is not None:
        logger.info("Closing read asyncpg connection pool")
        await _read_db_pool.close()
        _read_db_pool = None

    if _db_pool is not None:
        logger.info("Closing primary asyncpg connection pool")
        await _db_pool.close()
        _db_pool = None


def get_db_pool() -> asyncpg.Pool:
    """Retrieve the active primary (write) asyncpg pool, raising if not initialized."""
    if _db_pool is None:
        raise RuntimeError("Database connection pool is not initialized")
    return _db_pool


def get_read_db_pool() -> asyncpg.Pool:
    """Retrieve the read asyncpg pool, falling back to primary pool if not separately configured."""
    if _read_db_pool is not None:
        return _read_db_pool
    return get_db_pool()


async def check_db_health() -> bool:
    """Check whether PostgreSQL connection is alive and healthy."""
    if _db_pool is None:
        return False
    try:
        async with _db_pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1")
            return val == 1
    except Exception as exc:
        logger.error("PostgreSQL health check failed", exc_info=exc)
        return False


@asynccontextmanager
async def get_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a raw connection from the pool for system-level operations."""
    pool = get_db_pool()
    async with pool.acquire() as conn:
        yield conn


async def _enforce_rls(conn: asyncpg.Connection) -> None:
    """Inside a project transaction, drop to the restricted tenant role when needed (see detect_tenant_role)."""
    if _switch_to_tenant_role:
        await conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}")


@asynccontextmanager
async def project_transaction(project_id: int) -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Acquire a connection from the primary write pool and execute within a transaction where
    `SET LOCAL app.project_id = ...` is enforced for Row Level Security (RLS), as a role
    that cannot bypass RLS (see _enforce_rls).
    """
    pool = get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Parameterized equivalent of SET LOCAL app.project_id = ... (and leave system scope)
            await conn.execute(TENANT_SCOPE_SQL, str(project_id))
            await _enforce_rls(conn)
            yield conn


@asynccontextmanager
async def read_project_transaction(project_id: int) -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Acquire a connection from the dedicated read pool (with statement_timeout = 5000ms)
    and execute within a transaction where `SET LOCAL app.project_id = ...` is enforced for RLS.
    """
    pool = get_read_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(TENANT_SCOPE_SQL, str(project_id))
            await _enforce_rls(conn)
            yield conn
