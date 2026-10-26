import hashlib
import logging
import secrets
from pathlib import Path
from typing import List
import asyncpg
from app.config import settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "sql" / "migrations"
# Several API processes start at once (uvicorn --workers N, the CLI): only one migrates at a time
MIGRATION_LOCK_ID = 7201146002


async def run_migrations(pool: asyncpg.Pool) -> List[str]:
    """
    Apply pending SQL migrations in order from server/sql/migrations/.
    Tracks applied migrations in schema_migrations table.
    """
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID)
        try:
            applied_now = await _apply_pending(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)

    if applied_now:
        # Migration 018 may have just created the restricted tenant role
        from app.db import detect_tenant_role

        await detect_tenant_role(pool)
    return applied_now


async def _apply_pending(conn: asyncpg.Connection) -> List[str]:
    applied_now: List[str] = []
    # 1. Ensure migrations tracking table exists
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # 2. Get already applied versions
    rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    applied_versions = {row["version"] for row in rows}

    # 3. Discover migration files
    if not MIGRATIONS_DIR.exists():
        logger.warning("Migrations directory %s does not exist", MIGRATIONS_DIR)
        return applied_now

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    # 4. Apply each pending migration inside its own transaction
    for file_path in migration_files:
        version = file_path.name
        if version in applied_versions:
            continue

        logger.info("Applying database migration: %s", version)
        sql_content = file_path.read_text(encoding="utf-8")

        async with conn.transaction():
            await conn.execute(sql_content)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1)",
                version,
            )

        applied_now.append(version)
        logger.info("Successfully applied database migration: %s", version)

    # 5. Backfill key hashes for any existing projects
    try:
        unhashed = await conn.fetch(
            "SELECT id, api_key FROM projects WHERE ingest_key_hash IS NULL AND api_key IS NOT NULL"
        )
        for r in unhashed:
            r_dict = dict(r) if not isinstance(r, dict) else r
            key = r_dict.get("api_key")
            if not key:
                continue
            h = hashlib.sha256(key.encode("utf-8")).hexdigest()
            prefix = key[:14] + "..."
            # Also generate a read key for legacy project
            read_key = f"cw_read_{secrets.token_urlsafe(24)}"
            read_h = hashlib.sha256(read_key.encode("utf-8")).hexdigest()
            read_p = read_key[:14] + "..."
            await conn.execute(
                """
                UPDATE projects
                SET ingest_key_hash = $1, ingest_key_prefix = $2,
                    read_key_hash = $3, read_key_prefix = $4
                WHERE id = $5
                """,
                h, prefix, read_h, read_p, r_dict["id"]
            )
    except Exception as exc:
        logger.warning("Could not backfill project key hashes: %s", exc)

    # 6. Handle optional dev project seeding (Addendum A3)
    if settings.SEED_DEV_PROJECT:
        logger.info("SEED_DEV_PROJECT is enabled: ensuring default dev project exists with hashed keys")
        dev_ingest_hash = hashlib.sha256(b"cw_test_key_123").hexdigest()
        dev_read_hash = hashlib.sha256(b"cw_read_test_123").hexdigest()
        dev_manage_hash = hashlib.sha256(b"cw_manage_test_123").hexdigest()
        await conn.execute(
            """
            INSERT INTO projects (name, api_key, ingest_key_hash, ingest_key_prefix, read_key_hash, read_key_prefix,
                                  manage_key_hash, manage_key_prefix)
            VALUES ('my-test-app', 'cw_test_key_123', $1, 'cw_test_key_12...', $2, 'cw_read_test_1...', $3, 'cw_manage_test_...')
            ON CONFLICT (ingest_key_hash) WHERE ingest_key_hash IS NOT NULL DO UPDATE SET
              read_key_hash = EXCLUDED.read_key_hash, manage_key_hash = EXCLUDED.manage_key_hash,
              manage_key_prefix = EXCLUDED.manage_key_prefix;
            """,
            dev_ingest_hash,
            dev_read_hash,
            dev_manage_hash,
        )

    return applied_now
