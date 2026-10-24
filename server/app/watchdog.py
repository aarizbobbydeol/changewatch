import logging
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import asyncpg
import httpx

from app.config import settings

logger = logging.getLogger("changewatch.watchdog")


async def reap_stuck_queue_items(redis) -> int:
    """
    Reap items stuck in processing queues longer than QUEUE_VISIBILITY_TIMEOUT_SEC (60s).
    Reliable queue pattern: moves stuck items back to the main 'events' queue.
    """
    if not redis:
        return 0

    reaped = 0
    try:
        keys = await redis.keys("events:processing:*")
        now_ts = datetime.now(timezone.utc).timestamp()
        timeout = settings.QUEUE_VISIBILITY_TIMEOUT_SEC

        for key in keys:
            key_str = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            if not key_str.startswith("events:processing:"):
                continue
            # Check length of processing list
            raw_len = await redis.llen(key_str)
            try:
                length = int(raw_len)
            except (ValueError, TypeError):
                length = 0

            if length > 0:
                # Check timestamp key if set or requeue items
                time_key = f"time:{key_str}"
                started_str = await redis.get(time_key)
                if started_str:
                    try:
                        started_ts = float(started_str)
                        if now_ts - started_ts >= timeout:
                            # Stuck! Requeue items to events bounded by length
                            items_to_reap = min(length, 500)
                            for _ in range(items_to_reap):
                                item = await redis.rpoplpush(key_str, "events")
                                if not item:
                                    break
                                reaped += 1
                            await redis.delete(time_key)
                    except (ValueError, TypeError):
                        pass
                else:
                    # No timestamp, set it now
                    await redis.set(time_key, str(now_ts), ex=300)
    except Exception as exc:
        logger.warning("Error reaping stuck queue items: %s", exc)

    return reaped


async def run_watchdog_checks(
    conn: Optional[asyncpg.Connection],
    redis: Any,
    ping_external: bool = False,
    now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Run full component watchdog checks:
    - Postgres reachability & disk space
    - Redis reachability, queue depth, queue lag
    - Worker heartbeats & detector heartbeat
    - Outbox backlog & notification delivery rate (last 15m)
    - Verdict freshness (STALE_AFTER_SEC)
    - Reaping stuck items
    - Optional external ping
    """
    current_time = now_dt or datetime.now(timezone.utc)
    components: Dict[str, Dict[str, Any]] = {}
    system_alerts: List[Dict[str, Any]] = []
    overall_status = "ok"
    verdicts_fresh = True

    # 1. Database Check
    db_status = "ok"
    db_detail = "PostgreSQL responsive"
    if conn:
        try:
            val = await conn.fetchval("SELECT 1")
            if val != 1:
                db_status = "down"
                db_detail = "Unexpected response from database"
        except Exception as exc:
            db_status = "down"
            db_detail = f"Database query failed: {exc}"
    else:
        db_status = "down"
        db_detail = "No database connection available"

    components["database"] = {"status": db_status, "detail": db_detail}
    if db_status != "ok":
        overall_status = "down"

    # 2. Redis & Queue Check
    redis_status = "ok"
    queue_depth = 0
    if redis:
        try:
            await redis.ping()
            raw_len = await redis.llen("events")
            queue_depth = int(raw_len) if raw_len is not None else 0
            if queue_depth > 5000:
                redis_status = "down"
                redis_detail = f"Queue severely backed up ({queue_depth} items)"
            elif queue_depth > 1000:
                redis_status = "degraded"
                redis_detail = f"Queue backlog elevated ({queue_depth} items)"
            else:
                redis_detail = f"Queue normal ({queue_depth} items)"
        except Exception as exc:
            redis_status = "down"
            redis_detail = f"Redis ping failed: {exc}"
    else:
        redis_status = "down"
        redis_detail = "Redis connection not available"

    components["queue"] = {"status": redis_status, "detail": redis_detail, "depth": queue_depth}
    if redis_status == "down":
        overall_status = "down"
    elif redis_status == "degraded" and overall_status == "ok":
        overall_status = "degraded"

    # 3. Worker Heartbeat Check
    worker_status = "ok"
    worker_detail = "Worker processes active"
    if redis:
        try:
            worker_keys = await redis.keys("heartbeat:worker:*")
            active_workers = len(worker_keys)
            if active_workers == 0 and queue_depth > 0:
                worker_status = "down"
                worker_detail = "No active workers detected while queue has items"
            elif active_workers == 0:
                worker_status = "degraded"
                worker_detail = "No active worker heartbeats"
            else:
                worker_detail = f"{active_workers} worker processes active"
        except Exception as exc:
            worker_status = "degraded"
            worker_detail = f"Could not verify workers: {exc}"
    components["worker"] = {"status": worker_status, "detail": worker_detail}

    # 4. Detector Heartbeat & Verdict Freshness
    detector_status = "ok"
    detector_detail = "Detector loop active"
    if redis:
        try:
            det_hb = await redis.get("heartbeat:detector")
            if det_hb:
                last_det_ts = float(det_hb)
                lag_sec = current_time.timestamp() - last_det_ts
                if lag_sec > settings.STALE_AFTER_SEC:
                    detector_status = "down"
                    detector_detail = f"Detector has not run for {int(lag_sec // 60)} minutes"
                    verdicts_fresh = False
                elif lag_sec > 60:
                    detector_status = "degraded"
                    detector_detail = f"Detector delayed by {int(lag_sec)} seconds"
            else:
                # No detector heartbeat key found
                detector_status = "degraded"
                detector_detail = "Detector heartbeat key not found"
                verdicts_fresh = False
        except Exception as exc:
            detector_status = "degraded"
            detector_detail = f"Could not check detector heartbeat: {exc}"
    components["detector"] = {
        "status": detector_status,
        "detail": detector_detail,
        "verdicts_fresh": verdicts_fresh,
    }
    if detector_status == "down":
        system_alerts.append({
            "component": "detector",
            "severity": "high",
            "title": "Detector has not run for several minutes",
            "detail": detector_detail,
        })
        if overall_status == "ok":
            overall_status = "degraded"

    # 5. Outbox Backlog & Notifications Check
    outbox_status = "ok"
    outbox_backlog = 0
    if conn and db_status == "ok":
        try:
            backlog_count = await conn.fetchval(
                "SELECT COUNT(*) FROM outbox WHERE status = 'pending'"
            )
            outbox_backlog = int(backlog_count or 0)

            # Failure rate in last 15 minutes
            stats_15m = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'sent') AS delivered,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed
                FROM outbox
                WHERE created_at >= $1::timestamptz - INTERVAL '15 minutes'
                """,
                current_time,
            )
            delivered = int(stats_15m["delivered"] or 0)
            failed = int(stats_15m["failed"] or 0)

            if failed > 5 and failed > delivered:
                outbox_status = "degraded"
                outbox_detail = f"Outbox notification deliveries failing ({failed} failed, {delivered} delivered in last 15m)"
                system_alerts.append({
                    "component": "notifier",
                    "severity": "high",
                    "title": "Notification deliveries are failing",
                    "detail": outbox_detail,
                })
            elif outbox_backlog > 100:
                outbox_status = "degraded"
                outbox_detail = f"Outbox backlog high ({outbox_backlog} pending)"
            else:
                outbox_detail = f"Outbox healthy ({outbox_backlog} pending, {delivered} delivered recently)"
        except Exception as exc:
            outbox_status = "degraded"
            outbox_detail = f"Could not query outbox stats: {exc}"
    else:
        outbox_status = "degraded"
        outbox_detail = "Database unavailable for outbox check"

    components["notifier"] = {
        "status": outbox_status,
        "detail": outbox_detail,
        "backlog": outbox_backlog,
    }

    # 5b. Investigator Heartbeat Check
    investigator_status = "ok"
    investigator_detail = "Investigator ready"
    if redis:
        try:
            inv_hb = await redis.get("heartbeat:investigator")
            if inv_hb:
                inv_ts = float(inv_hb)
                inv_lag = current_time.timestamp() - inv_ts
                if inv_lag > 180:
                    investigator_status = "degraded"
                    investigator_detail = f"Investigator delayed by {int(inv_lag)} seconds"
            else:
                investigator_detail = "Investigator heartbeat not registered (on-demand)"
        except Exception as exc:
            investigator_status = "degraded"
            investigator_detail = f"Could not verify investigator: {exc}"
    components["investigator"] = {"status": investigator_status, "detail": investigator_detail}

    # 5c. Notifier Heartbeat Check (instant alerts wait for the detector's 60 s fallback without it)
    notifier_status = "ok"
    notifier_detail = "Notifier delivering alerts"
    if redis:
        try:
            nt_hb = await redis.get("heartbeat:notifier")
            if not nt_hb:
                notifier_status = "down"
                notifier_detail = "Notifier is not running: alerts are only sent by the detector once a minute"
            elif current_time.timestamp() - float(nt_hb) > 60:
                notifier_status = "degraded"
                notifier_detail = f"Notifier delayed by {int(current_time.timestamp() - float(nt_hb))} seconds"
        except Exception as exc:
            notifier_status = "degraded"
            notifier_detail = f"Could not verify notifier: {exc}"
    components["notifier_process"] = {"status": notifier_status, "detail": notifier_detail}
    if notifier_status == "down" and overall_status == "ok":
        overall_status = "degraded"

    # 6. Disk Space Check
    try:
        disk_usage = shutil.disk_usage("/")
        free_gb = disk_usage.free / (1024 ** 3)
        if free_gb < 0.25:
            disk_status = "down"
            disk_detail = f"Critical disk space: {free_gb:.2f} GB free"
            overall_status = "down"
        elif free_gb < 1.0:
            disk_status = "degraded"
            disk_detail = f"Low disk space: {free_gb:.2f} GB free"
            if overall_status == "ok":
                overall_status = "degraded"
        else:
            disk_status = "ok"
            disk_detail = f"Disk space healthy ({free_gb:.1f} GB free)"
    except Exception as exc:
        disk_status = "ok"
        disk_detail = f"Disk check bypassed: {exc}"

    components["storage"] = {"status": disk_status, "detail": disk_detail}

    # 7. Reap stuck items in queue
    reaped_count = await reap_stuck_queue_items(redis)
    if reaped_count > 0:
        logger.info("Watchdog reaped %d stuck items back to events queue", reaped_count)

    # 8. Record component status logs in DB for any degraded/down component
    if conn and db_status == "ok":
        for comp_name, comp_info in components.items():
            if comp_info["status"] != "ok":
                try:
                    await conn.execute(
                        """
                        INSERT INTO component_status_log (component, status, started_at, detail)
                        VALUES ($1, $2, $3, $4)
                        """,
                        comp_name,
                        comp_info["status"],
                        current_time,
                        comp_info["detail"],
                    )
                except Exception as exc:
                    logger.debug("Failed logging component status: %s", exc)

    # 9. External Ping URL (healthchecks.io style)
    if ping_external and settings.WATCHDOG_PING_URL:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                ping_url = settings.WATCHDOG_PING_URL
                if overall_status == "down":
                    ping_url = f"{ping_url.rstrip('/')}/fail"
                await client.get(ping_url)
                logger.debug("Sent external watchdog ping to %s", ping_url)
        except Exception as exc:
            logger.warning("Failed sending external watchdog ping: %s", exc)

    return {
        "overall_status": overall_status,
        "verdicts_fresh": verdicts_fresh,
        "components": components,
        "queue_depth": queue_depth,
        "outbox_backlog": outbox_backlog,
        "system_alerts": system_alerts,
        "checked_at": current_time.isoformat(),
    }
