import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import asyncpg

logger = logging.getLogger("changewatch.sources")


def is_in_idle_hours(expected_hours: Optional[Dict[str, Any]], now_dt: datetime) -> bool:
    """
    Check if the current time falls within scheduled idle hours.
    expected_hours schema:
      - weekdays: list of int [0=Monday..6=Sunday]. Default [0..4]
      - start_hour: int (e.g. 8). Active hours start.
      - end_hour: int (e.g. 20). Active hours end.
    Outside active hours is considered idle.
    """
    if not expected_hours:
        return False

    weekday = now_dt.weekday()
    weekdays = expected_hours.get("weekdays", [0, 1, 2, 3, 4])
    if weekday not in weekdays:
        return True  # Weekend / unscheduled day -> idle

    hour = now_dt.hour
    start_hour = expected_hours.get("start_hour", 8)
    end_hour = expected_hours.get("end_hour", 20)
    if hour < start_hour or hour >= end_hour:
        return True  # Night / non-working hours -> idle

    return False


async def record_source_activity(
    conn: asyncpg.Connection,
    project_id: int,
    environment: str,
    app_name: str = "default",
    is_heartbeat: bool = True,
    occurred_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Record heartbeat or event activity from a customer application source.
    Handles adaptive gap calculation and recovery from silent status.
    """
    now_dt = occurred_at or datetime.now(timezone.utc)

    # 1. Fetch current source state if exists
    row = await conn.fetchrow(
        """
        SELECT id, status, last_heartbeat_at, typical_gap_sec, silent_since
        FROM sources
        WHERE project_id = $1 AND environment = $2 AND app_name = $3
        """,
        project_id,
        environment,
        app_name,
    )

    typical_gap = 60
    recovered = False
    if row:
        typical_gap = int(row["typical_gap_sec"] or 60)
        prev_hb = row["last_heartbeat_at"]
        if is_heartbeat and prev_hb:
            gap_sec = (now_dt - prev_hb).total_seconds()
            if 1 <= gap_sec <= 3600:
                # Exponential moving average for typical gap
                typical_gap = int(0.8 * typical_gap + 0.2 * gap_sec)
                typical_gap = max(10, min(typical_gap, 3600))

        # Only traffic (heartbeats) ends a silence: an error from a background job while the web
        # app is still down would otherwise send a false "data is flowing again"
        if row["status"] == "silent" and is_heartbeat:
            recovered = True

    # 2. Upsert source
    if is_heartbeat:
        await conn.execute(
            """
            INSERT INTO sources (
                project_id, environment, app_name, status, last_heartbeat_at,
                typical_gap_sec, silent_since, updated_at
            )
            VALUES ($1, $2, $3, 'active', $4, $5, NULL, now())
            ON CONFLICT (project_id, environment, app_name) DO UPDATE SET
                status = 'active',
                last_heartbeat_at = EXCLUDED.last_heartbeat_at,
                typical_gap_sec = EXCLUDED.typical_gap_sec,
                silent_since = NULL,
                updated_at = now()
            """,
            project_id,
            environment,
            app_name,
            now_dt,
            typical_gap,
        )
    else:
        await conn.execute(
            """
            INSERT INTO sources (
                project_id, environment, app_name, status, last_event_at,
                typical_gap_sec, updated_at
            )
            VALUES ($1, $2, $3, 'active', $4, $5, now())
            ON CONFLICT (project_id, environment, app_name) DO UPDATE SET
                last_event_at = EXCLUDED.last_event_at,
                updated_at = now()
            """,
            project_id,
            environment,
            app_name,
            now_dt,
            typical_gap,
        )

    # 3. If recovered, queue recovery alert
    if recovered:
        from app.outbox import queue_notification
        title = f"Data is flowing again from {app_name} ({environment})"
        body = f"ChangeWatch has resumed receiving traffic from {app_name} in {environment}."
        await queue_notification(
            conn,
            project_id=project_id,
            dedupe_key=f"source_recovered:{project_id}:{environment}:{app_name}:{int(now_dt.timestamp() // 300)}",
            payload={
                "type": "source_recovered",
                "title": title,
                "body": body,
                "environment": environment,
                "app_name": app_name,
                "recovered_at": now_dt.isoformat(),
            },
        )
        logger.info(
            "Source recovered from silent state",
            extra={"project_id": project_id, "environment": environment, "app_name": app_name},
        )

    return {
        "status": "active",
        "recovered": recovered,
        "typical_gap_sec": typical_gap,
    }


async def check_dead_mans_switch(
    conn: asyncpg.Connection,
    silent_after_min: int = 10,
    now_dt: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Check all sources active within the last 24h. If heartbeat has not arrived
    for max(silent_after_min * 60, 3 * typical_gap_sec), mark silent and send one alert.
    """
    current_time = now_dt or datetime.now(timezone.utc)
    from app.outbox import queue_notification

    # Only inspect sources active in the last 24h with silence_alerts = true
    rows = await conn.fetch(
        """
        SELECT id, project_id, environment, app_name, status, last_heartbeat_at,
               typical_gap_sec, expected_hours, silence_alerts
        FROM sources
        WHERE updated_at >= $1::timestamptz - INTERVAL '24 hours'
          AND silence_alerts = true
          AND status != 'silent'
        """,
        current_time,
    )

    silent_sources = []
    for r in rows:
        last_hb = r["last_heartbeat_at"]
        if not last_hb:
            continue

        # Check idle hours
        exp_hours = r["expected_hours"]
        if exp_hours:
            if isinstance(exp_hours, str):
                try:
                    exp_hours = json.loads(exp_hours)
                except Exception:
                    exp_hours = None
            if is_in_idle_hours(exp_hours, current_time):
                continue  # Scheduled idle hours -> not silent

        typical_gap = int(r["typical_gap_sec"] or 60)
        threshold_sec = max(silent_after_min * 60, 3 * typical_gap)
        elapsed_sec = (current_time - last_hb).total_seconds()

        if elapsed_sec >= threshold_sec:
            silent_min = int(elapsed_sec // 60)
            proj_id = int(r["project_id"])
            env = r["environment"]
            app = r["app_name"]

            # Mark source silent
            await conn.execute(
                """
                UPDATE sources
                SET status = 'silent', silent_since = $1, updated_at = now()
                WHERE id = $2
                """,
                current_time,
                r["id"],
            )

            # Insert system alert
            await conn.execute(
                """
                INSERT INTO system_alerts (project_id, component, severity, title, detail, status, created_at)
                VALUES ($1, 'sources', 'high', $2, $3, 'active', $4)
                """,
                proj_id,
                f"No data from {app} for {silent_min} minutes",
                f"Heartbeats stopped arriving since {last_hb.isoformat()}. Threshold: {threshold_sec}s.",
                current_time,
            )

            # Dispatch notification
            dedupe_key = f"source_silent:{proj_id}:{env}:{app}:{int(last_hb.timestamp())}"
            await queue_notification(
                conn,
                project_id=proj_id,
                dedupe_key=dedupe_key,
                payload={
                    "type": "source_silent",
                    "title": f"No data from {app} for {silent_min} minutes",
                    "body": f"ChangeWatch has not received data from {app} ({env}) for {silent_min} minutes.",
                    "environment": env,
                    "app_name": app,
                    "silent_min": silent_min,
                    "severity": "high",
                    "last_seen": last_hb.isoformat(),
                },
            )

            silent_sources.append({
                "project_id": proj_id,
                "environment": env,
                "app_name": app,
                "silent_min": silent_min,
            })
            logger.warning(
                "Source marked silent by dead-man's switch",
                extra={"project_id": proj_id, "environment": env, "app_name": app, "silent_min": silent_min},
            )

    return silent_sources


async def is_source_silent(
    conn: asyncpg.Connection,
    project_id: int,
    environment: str,
) -> bool:
    """
    Check if any source for this project and environment is currently silent.
    Silence is never safe: if True, release verdict must be 'no_data'.
    """
    val = await conn.fetchval(
        """
        SELECT 1
        FROM sources
        WHERE project_id = $1 AND environment = $2 AND status = 'silent' AND silence_alerts = true
        LIMIT 1
        """,
        project_id,
        environment,
    )
    return bool(val)
