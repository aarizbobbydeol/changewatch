"""Data retention cleanup service (Phase 1 Stage 6 & Security Stage 7)."""

import inspect
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from app.config import settings
from app.db import get_db_pool

logger = logging.getLogger(__name__)


async def drop_expired_partitions(conn: Any, cutoff: datetime) -> List[str]:
    """
    Drop PostgreSQL event partitions whose upper boundary is older than cutoff.
    Enables O(1) partition drop without creating vacuum dead tuples (Performance Addendum Section 5).
    """
    dropped: List[str] = []
    if not hasattr(conn, "fetch"):
        return dropped

    try:
        rows = await conn.fetch(
            """
            SELECT
                c.relname AS partition_name,
                pg_get_expr(c.relpartbound, c.oid) AS bound_expr
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'events' AND c.relispartition = true;
            """
        )
    except Exception as exc:
        logger.debug("Partition query skipped (unpartitioned or mocked DB): %s", exc)
        return dropped

    for row in rows:
        part_name = row.get("partition_name") if isinstance(row, dict) else row["partition_name"]
        bound_expr = (row.get("bound_expr") if isinstance(row, dict) else row["bound_expr"]) or ""

        # Never drop the default partition
        if "DEFAULT" in bound_expr.upper() or part_name == "events_default":
            continue

        # Extract upper bound timestamp from: FOR VALUES FROM ('...') TO ('<upper_ts>')
        upper_match = re.search(r"TO\s*\(\s*'([^']+)'", bound_expr)
        if upper_match:
            upper_str = upper_match.group(1)
            try:
                upper_dt = datetime.fromisoformat(upper_str.replace(" ", "T"))
                if upper_dt.tzinfo is None:
                    upper_dt = upper_dt.replace(tzinfo=timezone.utc)
                cutoff_dt = cutoff if cutoff.tzinfo is not None else cutoff.replace(tzinfo=timezone.utc)

                if upper_dt <= cutoff_dt:
                    logger.info("Dropping expired event partition %s (upper bound: %s <= cutoff: %s)", part_name, upper_dt, cutoff_dt)
                    await conn.execute(f"DROP TABLE IF EXISTS {part_name} CASCADE;")
                    dropped.append(part_name)
            except Exception as e:
                logger.warning("Error parsing or dropping partition %s (%s): %s", part_name, bound_expr, e)

    return dropped


def _parse_delete_count(status: Any) -> int:
    """Parse integer count from command status or return status if already int."""
    if status is None:
        return 0
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        parts = status.strip().split()
        if parts:
            try:
                return int(parts[-1])
            except ValueError:
                return 0
    try:
        return int(status)
    except (ValueError, TypeError):
        return 0


async def _cleanup_with_conn(
    conn: Any,
    cutoff: datetime,
    batch_size: int,
    code_cutoff: Optional[datetime] = None,
) -> Dict[str, int]:
    """Execute retention deletions using a single connection."""
    # 1. Drop expired partitions first (O(1) operation on native partitioned table)
    dropped_partitions = await drop_expired_partitions(conn, cutoff)

    total_events = 0
    batches = 0

    # 2. Batch delete expired events in a loop (fallback for rows in default partition or unpartitioned environments)
    while True:
        status = await conn.execute(
            """
            DELETE FROM events
            WHERE id IN (
                SELECT id FROM events
                WHERE occurred_at < $1
                ORDER BY id ASC
                LIMIT $2
            )
            """,
            cutoff,
            batch_size,
        )
        deleted = _parse_delete_count(status)
        if deleted == 0:
            break
        batches += 1
        total_events += deleted
        if deleted < batch_size:
            break

    # Delete expired traffic rows older than cutoff
    traffic_status = await conn.execute(
        """
        DELETE FROM traffic
        WHERE bucket < $1
        """,
        cutoff,
    )
    total_traffic = _parse_delete_count(traffic_status)

    result = {
        "events_deleted": total_events,
        "traffic_deleted": total_traffic,
        "batches": batches,
    }
    if dropped_partitions:
        result["partitions_dropped"] = len(dropped_partitions)
        result["dropped_partition_names"] = dropped_partitions

    # Clean up code content (deploy diffs, CODEOWNERS and deploy_files) older than code_cutoff if specified
    if code_cutoff is not None:
        code_cleaned = 0
        diff_status = await conn.execute(
            """
            UPDATE deploys
            SET diff = NULL, diff_enc = NULL, codeowners = NULL, codeowners_enc = NULL
            WHERE deployed_at < $1
              AND (diff IS NOT NULL OR diff_enc IS NOT NULL OR codeowners IS NOT NULL OR codeowners_enc IS NOT NULL)
            """,
            code_cutoff,
        )
        code_cleaned += _parse_delete_count(diff_status)

        try:
            files_status = await conn.execute(
                """
                DELETE FROM deploy_files
                WHERE created_at < $1
                """,
                code_cutoff,
            )
            code_cleaned += _parse_delete_count(files_status)
        except Exception:
            pass
        result["code_records_cleaned"] = code_cleaned

    logger.info(
        "Retention cleanup completed: %d events deleted in %d batches, %d traffic rows deleted (cutoff: %s)",
        total_events,
        batches,
        total_traffic,
        cutoff.isoformat(),
    )

    return result


async def cleanup_old_records(
    pool_or_conn: Optional[Any] = None,
    retention_days: Optional[int] = None,
    batch_size: int = 5000,
    code_retention_days: Optional[int] = None,
) -> Dict[str, int]:
    """Clean up expired events and traffic records (and optionally code contents).

    - retention_days: defaults to settings.RETENTION_DAYS (default 30 days).
    - code_retention_days: optional days to retain code diffs/files.
    - batch_size: number of rows per batch for event deletions (default 5000).
    - pool_or_conn: optional asyncpg Pool or Connection. If None, acquires from get_db_pool().

    Returns:
        dict: {"events_deleted": total_events, "traffic_deleted": total_traffic, "batches": num_batches, ...}
    """
    if retention_days is None:
        retention_days = settings.RETENTION_DAYS

    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    if code_retention_days is not None and code_retention_days < 0:
        raise ValueError("code_retention_days must be non-negative")
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    code_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=code_retention_days)
        if code_retention_days is not None
        else None
    )

    if pool_or_conn is None:
        pool = get_db_pool()
        async with pool.acquire() as conn:
            return await _cleanup_with_conn(conn, cutoff, batch_size, code_cutoff)

    acquire_fn = getattr(pool_or_conn, "acquire", None)
    if callable(acquire_fn):
        ctx = acquire_fn()
        if hasattr(ctx, "__aenter__") and hasattr(ctx, "__aexit__"):
            async with ctx as conn:
                return await _cleanup_with_conn(conn, cutoff, batch_size, code_cutoff)
        elif inspect.iscoroutine(ctx):
            ctx.close()

    return await _cleanup_with_conn(pool_or_conn, cutoff, batch_size, code_cutoff)
