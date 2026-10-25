import asyncio
import json
import logging
import math
import os
import signal
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
import asyncpg
from app.analysis import dispatch_verdict_alert, generate_likely_cause
from app.config import settings
from app.db import close_db_pool, get_connection, get_db_pool, init_db_pool, project_transaction
from app.metering import flush_usage_events
from app.outbox import deliver_outbox_batch
from app.queue import close_redis, get_redis, init_redis

logger = logging.getLogger("changewatch.detector")

# The AI "likely cause" on a watch/rollback verdict is optional: give up on it after this long
LIKELY_CAUSE_TIMEOUT_SEC = 20.0


def calculate_p95(samples: List[float]) -> float:
    """Calculate 95th percentile from a list of numerical samples."""
    if not samples:
        return 0.0
    sorted_s = sorted(samples)
    idx = math.ceil(0.95 * len(sorted_s)) - 1
    return float(sorted_s[max(0, min(idx, len(sorted_s) - 1))])


async def evaluate_deploy(
    conn: asyncpg.Connection,
    project_id: int,
    deploy: asyncpg.Record,
    now_dt: datetime,
) -> Tuple[str, List[str], bool, bool]:
    """
    Evaluate deterministic release verdict rules for a deploy (Addendum Section C3).
    Returns (verdict, reasons, is_final, is_flagged).
    """
    deploy_id = deploy["id"]
    deployed_at = deploy["deployed_at"]

    age_min = (now_dt - deployed_at).total_seconds() / 60.0
    is_final = age_min >= settings.VERDICT_FINAL_MIN

    # RULE 0: Silence is never "safe" (TRUST Addendum Section A2)
    # The release verdict is no_data when a source is silent.
    from app.sources import is_source_silent
    source_env = deploy["environment"] if "environment" in deploy else "prod"
    if await is_source_silent(conn, project_id, source_env):
        return "no_data", ["Source is silent (dead-man's switch triggered)"], is_final, False

    # 30-minute analysis window after deploy, capped at now and at the next deploy to the same
    # environment: errors after that belong to the next deploy, not this one
    window_after_end = min(now_dt, deployed_at + timedelta(minutes=30))
    next_deployed_at = deploy["next_deployed_at"] if "next_deployed_at" in deploy else None
    if next_deployed_at:
        window_after_end = min(window_after_end, next_deployed_at)
    window_before_start = deployed_at - timedelta(minutes=30)

    # 1. Traffic metrics from traffic table
    traffic_after_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(requests), 0) AS reqs, COALESCE(SUM(errors), 0) AS errs
        FROM traffic
        WHERE project_id = $1 AND environment = $5 AND bucket <= $4 AND (deploy_id = $2 OR bucket >= $3)
        """,
        project_id,
        deploy_id,
        deployed_at,
        window_after_end,
        source_env,
    )
    reqs_after = int(traffic_after_row["reqs"])
    errs_after = int(traffic_after_row["errs"])

    traffic_before_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(requests), 0) AS reqs, COALESCE(SUM(errors), 0) AS errs
        FROM traffic
        WHERE project_id = $1 AND environment = $4 AND bucket >= $2 AND bucket < $3
        """,
        project_id,
        window_before_start,
        deployed_at,
        source_env,
    )
    reqs_before = int(traffic_before_row["reqs"])
    errs_before = int(traffic_before_row["errs"])

    # 2. Event metrics
    events_after_count = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM events
        WHERE project_id = $1 AND environment = $5 AND occurred_at <= $4 AND (deploy_id = $2 OR occurred_at >= $3)
        """,
        project_id,
        deploy_id,
        deployed_at,
        window_after_end,
        source_env,
    )
    events_after_count = int(events_after_count or 0)

    errors_after_count = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM events
        WHERE project_id = $1 AND environment = $5 AND type = 'error'
          AND occurred_at <= $4 AND (deploy_id = $2 OR occurred_at >= $3)
        """,
        project_id,
        deploy_id,
        deployed_at,
        window_after_end,
        source_env,
    )
    errors_after_count = int(errors_after_count or 0)

    errors_before_count = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM events
        WHERE project_id = $1 AND environment = $4 AND type = 'error'
          AND occurred_at >= $2 AND occurred_at < $3
        """,
        project_id,
        window_before_start,
        deployed_at,
        source_env,
    )
    errors_before_count = int(errors_before_count or 0)

    # 3. New issues introduced since deploy
    new_issues = await conn.fetch(
        """
        SELECT i.id, COUNT(e.id) AS ev_count
        FROM issues i
        JOIN events e ON e.issue_id = i.id
        WHERE i.project_id = $1 AND i.first_seen >= $2 AND i.first_seen <= $4
          AND e.environment = $5 AND e.type = 'error'
          AND e.occurred_at <= $4 AND (e.deploy_id = $3 OR e.occurred_at >= $2)
        GROUP BY i.id
        """,
        project_id,
        deployed_at,
        deploy_id,
        window_after_end,
        source_env,
    )
    new_issues_count = len(new_issues)
    new_issue_events = sum(int(r["ev_count"]) for r in new_issues)

    # 4. Slow request durations
    dur_after_rows = await conn.fetch(
        """
        SELECT duration_ms FROM events
        WHERE project_id = $1 AND environment = $5 AND type = 'slow_request'
          AND occurred_at <= $4 AND (deploy_id = $2 OR occurred_at >= $3)
          AND duration_ms IS NOT NULL
        """,
        project_id,
        deploy_id,
        deployed_at,
        window_after_end,
        source_env,
    )
    samples_after = [float(r["duration_ms"]) for r in dur_after_rows]

    dur_before_rows = await conn.fetch(
        """
        SELECT duration_ms FROM events
        WHERE project_id = $1 AND environment = $4 AND type = 'slow_request'
          AND occurred_at >= $2 AND occurred_at < $3
          AND duration_ms IS NOT NULL
        """,
        project_id,
        window_before_start,
        deployed_at,
        source_env,
    )
    samples_before = [float(r["duration_ms"]) for r in dur_before_rows]

    reasons: List[str] = []
    has_traffic = (reqs_after > 0) or (events_after_count > 0)

    # Base error rates
    rate_after = (errs_after / reqs_after) if reqs_after > 0 else 0.0
    rate_before = (errs_before / reqs_before) if reqs_before > 0 else 0.0

    has_baseline = (reqs_before > 0 or errors_before_count > 0)
    if not has_baseline and has_traffic:
        reasons.append("no baseline traffic before deploy; using absolute thresholds")

    # RULE 1: no_data (Zero heartbeats or events linked to deploy)
    if not has_traffic:
        return "no_data", ["no traffic seen from the app since this deploy"], is_final, False

    # RULE 2: rollback
    # (a) New issues with >= VERDICT_ROLLBACK_NEW_ISSUE_EVENTS (default 20)
    if new_issue_events >= settings.VERDICT_ROLLBACK_NEW_ISSUE_EVENTS:
        reasons.append(
            f"{new_issues_count} new error types appeared, {new_issue_events} events combined"
        )
        return "rollback", reasons, is_final, True

    # (b) Error rate after >= 5x before, with >= 30 error events
    if has_baseline and rate_before > 0:
        if rate_after >= 5.0 * rate_before and (errors_after_count >= 30 or errs_after >= 30):
            mult = rate_after / rate_before
            reasons.append(
                f"error rate {rate_after*100:.1f}% vs {rate_before*100:.1f}% before ({mult:.1f}x)"
            )
            return "rollback", reasons, is_final, True

    # (c) Error rate after >= VERDICT_ROLLBACK_ABS_RATE (default 0.25)
    if rate_after >= settings.VERDICT_ROLLBACK_ABS_RATE and reqs_after >= 10:
        reasons.append(
            f"error rate {rate_after*100:.1f}% exceeds absolute threshold ({settings.VERDICT_ROLLBACK_ABS_RATE*100:.0f}%)"
        )
        return "rollback", reasons, is_final, True

    # RULE 3: watch
    # (a) Phase 1 spike conditions:
    #     (i) >= 1 new issue with >= 5 events total
    if new_issues_count >= 1 and new_issue_events >= 5:
        reasons.append(
            f"{new_issues_count} new error issues appeared ({new_issue_events} events)"
        )
        return "watch", reasons, is_final, True

    #     (ii) Total error events after >= 3x before (min 10 events after)
    min_spike_events = settings.SPIKE_MIN_EVENTS
    spike_ratio = settings.SPIKE_RATIO
    if errors_after_count >= min_spike_events:
        if errors_before_count == 0 or (errors_after_count >= spike_ratio * errors_before_count):
            reasons.append(
                f"error count jumped to {errors_after_count} vs {errors_before_count} before"
            )
            return "watch", reasons, is_final, True

    # (b) p95 duration after is at least 2x before with at least 20 samples
    if len(samples_after) >= 20 and len(samples_before) >= 10:
        p95_after = calculate_p95(samples_after)
        p95_before = calculate_p95(samples_before)
        if p95_before > 0 and p95_after >= 2.0 * p95_before:
            reasons.append(
                f"p95 latency doubled: {p95_after:.0f}ms vs {p95_before:.0f}ms before"
            )
            return "watch", reasons, is_final, True

    # RULE 4: safe
    reasons.append("traffic observed, error rates within normal thresholds")
    return "safe", reasons, is_final, False


async def process_active_deploys_once() -> int:
    """
    Examine deploys from the last 2 hours, evaluate verdicts, update deploy_analysis,
    and dispatch alerts when checkpoints are reached.
    """
    now_dt = datetime.now(timezone.utc)
    two_hours_ago = now_dt - timedelta(hours=2)
    processed_count = 0

    async with get_connection() as conn:
        deploys = await conn.fetch(
            """
            SELECT d.id, d.project_id, d.commit_sha, d.author, d.message, d.environment, d.deployed_at,
                   (SELECT min(n.deployed_at) FROM deploys n
                    WHERE n.project_id = d.project_id AND n.environment = d.environment
                      AND n.deployed_at > d.deployed_at) AS next_deployed_at
            FROM deploys d
            WHERE d.deployed_at >= $1
            ORDER BY d.deployed_at DESC
            """,
            two_hours_ago,
        )

    for dep in deploys:
        deploy_id = dep["id"]
        project_id = dep["project_id"]

        # One deploy failing (bad data, a lock timeout) must not stop the others, nor the
        # auto-resolve, escalation and outbox steps after this loop
        try:
            async with project_transaction(project_id) as conn:
                verdict, reasons, is_final, is_flagged = await evaluate_deploy(
                    conn, project_id, dep, now_dt
                )

                # Check existing analysis record
                analysis_row = await conn.fetchrow(
                    "SELECT verdict, likely_cause, verdict_final FROM deploy_analysis WHERE deploy_id = $1",
                    deploy_id,
                )
                prev_verdict = analysis_row["verdict"] if analysis_row else None
                existing_cause = analysis_row["likely_cause"] if analysis_row else None

                # Generate AI explanation for watch / rollback if not already present
                likely_cause = existing_cause
                if verdict in ("watch", "rollback") and not likely_cause and await _first_ai_attempt(deploy_id):
                    # Optional AI text: it must never hold up this verdict's alert or the other
                    # projects' deploys (the provider's own timeout and retries reach 2+ minutes)
                    try:
                        likely_cause = await asyncio.wait_for(
                            generate_likely_cause(conn, project_id, deploy_id), timeout=LIKELY_CAUSE_TIMEOUT_SEC
                        )
                    except asyncio.TimeoutError:
                        logger.warning("AI explanation for deploy %d timed out; sending the verdict without it", deploy_id)

                # Upsert into deploy_analysis
                reasons_json = json.dumps(reasons)
                await conn.execute(
                    """
                    INSERT INTO deploy_analysis (
                      deploy_id, new_issues, errors_before, errors_after,
                      flagged, likely_cause, verdict, verdict_reasons,
                      verdict_updated_at, verdict_final
                    )
                    VALUES ($1, 0, 0, 0, $2, $3, $4, $5::jsonb, now(), $6)
                    ON CONFLICT (deploy_id) DO UPDATE SET
                      flagged = EXCLUDED.flagged,
                      likely_cause = COALESCE(deploy_analysis.likely_cause, EXCLUDED.likely_cause),
                      verdict = EXCLUDED.verdict,
                      verdict_reasons = EXCLUDED.verdict_reasons,
                      verdict_updated_at = now(),
                      verdict_final = EXCLUDED.verdict_final
                    """,
                    deploy_id,
                    is_flagged,
                    likely_cause,
                    verdict,
                    reasons_json,
                    is_final,
                )

                # Alert Checkpoints
                age_min = (now_dt - dep["deployed_at"]).total_seconds() / 60.0

                # Remember the verdict announced at each checkpoint (set once, never overwritten)
                first_verdict = None
                if age_min >= settings.VERDICT_FIRST_MIN:
                    first_verdict = await conn.fetchval(
                        """
                        UPDATE deploy_analysis SET first_verdict = COALESCE(first_verdict, $2)
                        WHERE deploy_id = $1 RETURNING first_verdict
                        """,
                        deploy_id,
                        verdict,
                    )
                if is_final:
                    await conn.execute(
                        "UPDATE deploy_analysis SET final_verdict = COALESCE(final_verdict, $2) WHERE deploy_id = $1",
                        deploy_id,
                        verdict,
                    )

                # Checkpoint 1: First check
                first_alert_exists = await conn.fetchval(
                    "SELECT 1 FROM alerts WHERE deploy_id = $1 AND kind = 'verdict_first'",
                    deploy_id,
                )

                if not first_alert_exists and age_min >= settings.VERDICT_FIRST_MIN:
                    # Send first check notification
                    should_notify = True
                    if verdict == "safe" and not settings.NOTIFY_ON_SAFE:
                        should_notify = False

                    if should_notify:
                        await dispatch_verdict_alert(
                            conn=conn,
                            project_id=project_id,
                            deploy_id=deploy_id,
                            verdict=verdict,
                            reasons=reasons,
                            kind="verdict_first",
                            likely_cause=likely_cause,
                        )

                # Checkpoint 2: Final check (verdict_change)
                final_alert_exists = await conn.fetchval(
                    "SELECT 1 FROM alerts WHERE deploy_id = $1 AND kind IN ('verdict_final', 'verdict_change')",
                    deploy_id,
                )
                if is_final and not final_alert_exists:
                    # Addendum C4: post only if verdict changed from the one announced at the first check
                    baseline_verdict = first_verdict or prev_verdict
                    if baseline_verdict and baseline_verdict != verdict:
                        await dispatch_verdict_alert(
                            conn=conn,
                            project_id=project_id,
                            deploy_id=deploy_id,
                            verdict=verdict,
                            reasons=reasons,
                            kind="verdict_change",
                            likely_cause=likely_cause,
                        )
                    else:
                        # Record verdict_final to prevent future checks
                        await conn.execute(
                            """
                            INSERT INTO alerts (project_id, deploy_id, channel, kind)
                            VALUES ($1, $2, 'slack', 'verdict_final')
                            ON CONFLICT (deploy_id, kind) DO NOTHING
                            """,
                            project_id,
                            deploy_id,
                        )

                if verdict == "rollback":
                    # Upgrade incidents from possible to confirmed when verdict is rollback (Stage B2)
                    possible_incidents = await conn.fetch(
                        "SELECT id, title, where_file, where_line, where_function, in_diff, owners, event_count, first_event_at FROM incidents WHERE deploy_id = $1 AND status = 'possible'",
                        deploy_id,
                    )
                    for inc in possible_incidents:
                        inc_id = inc["id"]
                        await conn.execute(
                            "UPDATE incidents SET status = 'confirmed', confirmed_at = now() WHERE id = $1",
                            inc_id,
                        )
                        owners_list = json.loads(inc["owners"]) if inc["owners"] else []
                        first_event_at_ts = inc["first_event_at"].timestamp() if inc["first_event_at"] else None
                        from app.notifiers.incident_alert import dispatch_incident_alert
                        await dispatch_incident_alert(
                            conn=conn,
                            project_id=project_id,
                            incident_id=inc_id,
                            status="confirmed",
                            commit_sha=dep["commit_sha"],
                            author=dep["author"] or "Unknown",
                            message=dep["message"] or "",
                            error_title=inc["title"] or "Error",
                            event_count=inc["event_count"],
                            where_file=inc["where_file"] or "unknown",
                            where_line=inc["where_line"] or 0,
                            where_function=inc["where_function"] or "unknown",
                            in_diff=bool(inc["in_diff"]),
                            raw_owners=owners_list,
                            first_event_at=first_event_at_ts,
                        )

        except Exception as exc:
            logger.warning("Verdict for deploy %s (project %s) failed this cycle: %s", deploy_id, project_id, exc)
            continue

        processed_count += 1

    # Calm auto-resolve (only while traffic flows) and escalation, each telling the team.
    # Separate try blocks: one failing must not silently disable the other.
    from app.jobs import run_auto_resolve, run_escalations

    for job_name, job in (("Auto-resolve", run_auto_resolve), ("Escalation", run_escalations)):
        try:
            async with get_connection() as jconn:
                await job(jconn)
        except Exception as exc:
            logger.warning("%s check failed: %s", job_name, exc)

    # Deliver pending outbox notifications
    await deliver_outbox_batch()
    return processed_count



async def _first_ai_attempt(deploy_id: int) -> bool:
    """True once per deploy: the AI explanation is asked for once, not again every cycle while
    the provider is failing (each attempt can take the full LLM timeout)."""
    try:
        redis = get_redis()
        return bool(await redis.set(f"ai_cause_attempted:{deploy_id}", "1", nx=True, ex=3 * 3600))
    except Exception:
        return True


DETECTOR_ADVISORY_LOCK_ID = 420042


async def try_acquire_detector_lock(conn: asyncpg.Connection) -> bool:
    """Attempt to acquire session-level advisory lock for detector leader election."""
    row = await conn.fetchrow("SELECT pg_try_advisory_lock($1) AS acquired", DETECTOR_ADVISORY_LOCK_ID)
    return bool(row and row["acquired"])


async def release_detector_lock(conn: asyncpg.Connection) -> None:
    """Release session-level advisory lock for detector."""
    try:
        await conn.execute("SELECT pg_advisory_unlock($1)", DETECTOR_ADVISORY_LOCK_ID)
    except Exception as exc:
        logger.warning("Error releasing detector advisory lock: %s", exc)


async def run_detector():
    """
    Background spike & verdict detector loop running every 60 seconds.
    Uses PostgreSQL session advisory lock (pg_try_advisory_lock) for single-instance leader election.
    """
    logger.info("ChangeWatch spike & verdict detector started (PID: %d)", os.getpid())
    from app.crypto import check_master_key
    from app.llm import log_llm_privacy_notice
    check_master_key()  # refuse to start without encryption
    log_llm_privacy_notice()  # the detector writes AI deploy summaries too
    await init_db_pool()
    await init_redis()

    is_running = True

    def _shutdown():
        nonlocal is_running
        logger.info("Shutdown signal received for detector")
        is_running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    pool = get_db_pool()
    lock_conn: Optional[asyncpg.Connection] = None
    has_lock = False

    try:
        while is_running:
            try:
                # A session advisory lock dies with its connection (e.g. Postgres restarted).
                # asyncpg detaches a pooled connection whose server went away, and then even
                # is_closed() raises InterfaceError, so treat that as lost too.
                lock_lost = False
                if lock_conn is not None:
                    try:
                        lock_lost = lock_conn.is_closed()
                    except asyncpg.InterfaceError:
                        lock_lost = True
                if lock_lost:
                    logger.warning("Detector lock connection was lost; re-acquiring leadership")
                    has_lock = False
                    try:
                        await pool.release(lock_conn)
                    except Exception:
                        pass
                    lock_conn = None

                # Attempt to acquire advisory lock if not currently held
                if not has_lock:
                    if lock_conn is None:
                        lock_conn = await pool.acquire()
                    has_lock = await try_acquire_detector_lock(lock_conn)
                    if not has_lock:
                        logger.info(
                            "Detector standby: advisory lock %d held by another active instance. Standing by (10s)...",
                            DETECTOR_ADVISORY_LOCK_ID,
                        )
                        await asyncio.sleep(10)
                        continue
                    else:
                        logger.info(
                            "Acquired detector leader lock (%d). Running active detector cycle.",
                            DETECTOR_ADVISORY_LOCK_ID,
                        )

                processed = await process_active_deploys_once()
                logger.debug("Detector cycle completed: processed %d deploys", processed)

                # Record detector heartbeat for watchdog (Trust Addendum Section A3)
                try:
                    redis_conn = get_redis()
                    if redis_conn:
                        await redis_conn.set("heartbeat:detector", str(datetime.now(timezone.utc).timestamp()), ex=300)
                except Exception as exc:
                    logger.debug("Failed updating detector heartbeat: %s", exc)

                # Dead-Man's Switch check for silent sources (Trust Addendum Section A2)
                try:
                    async with pool.acquire() as dm_conn:
                        from app.sources import check_dead_mans_switch
                        await check_dead_mans_switch(dm_conn, silent_after_min=settings.SILENT_AFTER_MIN)
                except Exception as exc:
                    logger.warning("Failed running dead-man's switch check: %s", exc)

                # Usage Metering: Periodic usage flush of stored_events buckets from Redis
                try:
                    redis_conn = get_redis()
                    if redis_conn:
                        async with pool.acquire() as flush_conn:
                            flushed = await flush_usage_events(conn=flush_conn, redis=redis_conn)
                            if flushed > 0:
                                logger.info("Flushed %d stored_events usage units from Redis", flushed)
                except Exception as exc:
                    logger.warning("Failed flushing usage events in detector cycle: %s", exc)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error during detector evaluation cycle: %s", exc, exc_info=True)

            # Hourly maintenance (partitions, retention) and the weekly digest
            if has_lock:
                try:
                    from app.jobs import run_maintenance, send_weekly_digests
                    await run_maintenance(pool)
                    await send_weekly_digests(pool)
                except Exception as exc:
                    logger.warning("Scheduled jobs failed: %s", exc)

            # Watchdog runs every cycle, even when deploy processing failed (e.g. database down)
            try:
                from app.system_alerts import run_watchdog_cycle
                await run_watchdog_cycle(pool, get_redis(), ping_external=True)
            except Exception as exc:
                logger.warning("Watchdog cycle failed: %s", exc)

            # Sleep in short steps so a shutdown signal is honoured within a second
            for _ in range(60):
                if not is_running:
                    break
                await asyncio.sleep(1)
    finally:
        logger.info("Detector cleaning up resources")
        if lock_conn is not None:
            try:
                if has_lock:
                    await release_detector_lock(lock_conn)
                await pool.release(lock_conn)
            except Exception as exc:
                # A lost connection already dropped the lock; still close the pools below
                logger.debug("Could not release detector lock connection: %s", exc)
        await close_db_pool()
        await close_redis()


if __name__ == "__main__":
    from app.main import setup_structured_logging
    setup_structured_logging()
    asyncio.run(run_detector())
