import argparse
import asyncio
import json
import logging
import multiprocessing
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
import asyncpg
from app.cache import (
    deploy_lookup_cache,
    increment_sliding_window_counter,
    issue_lookup_cache,
)
from app.config import resolve_worker_processes, settings
from app.crypto import decrypt_or_plain, encrypt_or_keep
from app.db import close_db_pool, init_db_pool, project_transaction
from app.fingerprint import compute_fingerprint
from app.metering import record_stored_event_metric
from app.queue import close_redis, get_redis, init_redis

logger = logging.getLogger("changewatch.worker")

EVENTS_QUEUE = "events"
DEAD_LETTER_QUEUE = "events:dead"
MAX_RETRIES = 3
BATCH_MAX = int(os.environ.get("BATCH_MAX", "200"))
BATCH_MAX_WAIT_MS = float(os.environ.get("BATCH_MAX_WAIT_MS", "50"))
MAX_MEMORY_MB = int(os.environ.get("MAX_MEMORY_MB", "512"))


async def find_deploy(
    conn: asyncpg.Connection,
    project_id: int,
    environment: str,
    occurred_at_dt: datetime,
    release: Optional[str] = None,
) -> Optional[int]:
    """
    Find matching deploy:
    1. First by exact release == commit_sha (checked in LRU+TTL deploy_lookup_cache)
    2. Fallback: latest deploy for that project & environment with deployed_at <= occurred_at
    """
    # 1. Exact match on commit SHA with LRU+TTL cache
    if release:
        cache_key = (project_id, environment, release)
        cached = deploy_lookup_cache.get(cache_key)
        if cached is not None:
            return cached

        row = await conn.fetchrow(
            """
            SELECT id FROM deploys
            WHERE project_id = $1 AND commit_sha = $2 AND environment = $3
            LIMIT 1
            """,
            project_id,
            release,
            environment,
        )
        if row:
            dep_id = int(row["id"])
            deploy_lookup_cache.set(cache_key, dep_id, ttl=300.0)
            return dep_id

    # 2. Latest deploy before or at occurred_at
    row = await conn.fetchrow(
        """
        SELECT id FROM deploys
        WHERE project_id = $1 AND environment = $2 AND deployed_at <= $3
        ORDER BY deployed_at DESC
        LIMIT 1
        """,
        project_id,
        environment,
        occurred_at_dt,
    )
    if row:
        return int(row["id"])

    return None


async def process_single_event(payload: Dict[str, Any]) -> None:
    """Process an event from the Redis queue with RLS enforcement and idempotency."""
    project_id = int(payload["project_id"])
    event_id = payload.get("event_id")
    event_type = payload.get("type", "error")
    occurred_at = float(payload["occurred_at"])
    occurred_at_dt = datetime.fromtimestamp(occurred_at, tz=timezone.utc)
    environment = payload.get("environment", "prod")
    release = payload.get("release")

    async with project_transaction(project_id) as conn:
        deploy_id = await find_deploy(
            conn=conn,
            project_id=project_id,
            environment=environment,
            occurred_at_dt=occurred_at_dt,
            release=release,
        )

        if event_type == "heartbeat":
            # Addendum C1: Upsert into traffic table
            requests_count = int(payload.get("requests") or 0)
            errors_count = int(payload.get("errors") or 0)
            minute_bucket = occurred_at_dt.replace(second=0, microsecond=0)

            await conn.execute(
                """
                INSERT INTO traffic (project_id, deploy_id, bucket, requests, errors)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (project_id, bucket)
                DO UPDATE SET
                  requests = traffic.requests + EXCLUDED.requests,
                  errors = traffic.errors + EXCLUDED.errors,
                  deploy_id = COALESCE(traffic.deploy_id, EXCLUDED.deploy_id)
                """,
                project_id,
                deploy_id,
                minute_bucket,
                requests_count,
                errors_count,
            )
            from app.sources import record_source_activity
            try:
                await record_source_activity(
                    conn=conn,
                    project_id=project_id,
                    environment=payload.get("environment") or "prod",
                    app_name=payload.get("app_name") or "default",
                    is_heartbeat=True,
                    occurred_at=occurred_at_dt,
                )
            except Exception as sexc:
                logger.debug("Failed recording source activity for heartbeat: %s", sexc)

            return

        # For error and slow_request events
        fingerprint = compute_fingerprint(
            event_type=event_type,
            exc_type=payload.get("exc_type"),
            stack_trace=payload.get("stack_trace"),
            request_path=payload.get("request_path"),
            message=payload.get("message"),
        )

        if event_type == "slow_request":
            title = f"Slow: {payload.get('request_path') or 'Unknown'}"
        else:
            exc = payload.get("exc_type") or "Error"
            msg = (payload.get("message") or "")[:100]
            title = f"{exc}: {msg}".strip(": ")

        # Upsert issue with atomic event_count tracking (Storm Control Section 3)
        issue_row = await conn.fetchrow(
            """
            INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count)
            VALUES ($1, $2, $3, $4, $4, 'open', 1)
            ON CONFLICT (project_id, fingerprint)
            DO UPDATE SET
              last_seen = GREATEST(issues.last_seen, EXCLUDED.last_seen),
              event_count = issues.event_count + 1
            RETURNING id
            """,
            project_id,
            fingerprint,
            title,
            occurred_at_dt,
        )
        issue_id = int(issue_row["id"])
        issue_lookup_cache.set((project_id, fingerprint), issue_id, ttl=300.0)

        # Storm control: cap full event rows to STORE_FULL_PER_MIN per (project, fingerprint) per minute
        m_epoch = int(occurred_at_dt.replace(second=0, microsecond=0).timestamp())
        storm_key = f"storm:{project_id}:{fingerprint}:{m_epoch}"
        store_full = True
        try:
            redis = get_redis()
            if redis:
                val = await redis.incr(storm_key)
                await redis.expire(storm_key, 120)
                if val > settings.STORE_FULL_PER_MIN:
                    store_full = False
        except Exception:
            store_full = True

        if store_full:
            # Store stack_trace and extra encrypted only (plaintext kept only if encryption fails)
            raw_extra = payload.get("extra") or {}
            raw_trace, stack_trace_enc = await encrypt_or_keep(
                project_id, "events", "stack_trace", payload.get("stack_trace")
            )
            extra_plain, extra_enc = await encrypt_or_keep(
                project_id, "events", "extra", json.dumps(raw_extra) if raw_extra else None
            )
            extra_json = extra_plain or "{}"

            # Insert event linked to issue and deploy (idempotent via event_id unique constraint)
            await conn.execute(
                """
                INSERT INTO events (
                  event_id, issue_id, project_id, deploy_id, occurred_at,
                  stack_trace, stack_trace_enc, request_path, duration_ms, extra, extra_enc
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
                ON CONFLICT (project_id, event_id, occurred_at) WHERE event_id IS NOT NULL DO NOTHING
                """,
                event_id,
                issue_id,
                project_id,
                deploy_id,
                occurred_at_dt,
                raw_trace,
                stack_trace_enc,
                payload.get("request_path"),
                payload.get("duration_ms"),
                extra_json,
                extra_enc,
            )

        # Update event_rollups table (Performance Addendum Section 4)
        minute_bucket = occurred_at_dt.replace(second=0, microsecond=0)
        try:
            await conn.execute(
                """
                INSERT INTO event_rollups (project_id, issue_id, deploy_id, bucket, count)
                VALUES ($1, $2, $3, $4, 1)
                ON CONFLICT (project_id, issue_id, COALESCE(deploy_id, -1), bucket)
                DO UPDATE SET count = event_rollups.count + EXCLUDED.count
                """,
                project_id,
                issue_id,
                deploy_id,
                minute_bucket,
            )
        except Exception as e:
            logger.warning("Failed updating event_rollups: %s", e)

        # Record customer app source activity (Trust Addendum Section A2)
        try:
            from app.sources import record_source_activity
            await record_source_activity(
                conn=conn,
                project_id=project_id,
                environment=payload.get("environment") or "prod",
                app_name=payload.get("app_name") or "default",
                is_heartbeat=False,
                occurred_at=occurred_at_dt,
            )
        except Exception as sexc:
            logger.debug("Failed recording source activity for event: %s", sexc)

        # Update Redis sliding-window counter (Performance Addendum Section 4)
        try:
            redis = get_redis()
        except Exception:
            redis = None

        if redis:
            try:
                await increment_sliding_window_counter(
                    redis, project_id=project_id, deploy_id=deploy_id, issue_id=issue_id, count=1
                )
            except Exception as e:
                logger.warning("Failed incrementing sliding window counter: %s", e)

            if store_full:
                try:
                    await record_stored_event_metric(redis, project_id=project_id, occurred_at=occurred_at_dt, count=1)
                except Exception as e:
                    logger.warning("Failed recording stored event usage metric: %s", e)

        logger.info(
            "Event processed: project=%d, event_id=%s, type=%s, issue=%d, deploy=%s",
            project_id,
            str(event_id),
            event_type,
            issue_id,
            str(deploy_id),
        )

        if deploy_id is not None and event_type == "error":
            await check_incident_for_issue(conn, project_id, deploy_id, issue_id)


async def check_incident_for_issue(
    conn: asyncpg.Connection,
    project_id: int,
    deploy_id: Optional[int],
    issue_id: int,
) -> None:
    """
    Incident check hook (Performance Addendum Stage 2 & AI SRE Phase 2).
    Executed once per distinct (deploy, issue) in the batch, rather than once per event.
    """
    if deploy_id is None:
        return

    try:
        ev_row = await conn.fetchrow(
            """
            SELECT occurred_at, request_path, stack_trace, stack_trace_enc
            FROM events
            WHERE project_id = $1 AND deploy_id = $2 AND issue_id = $3
            ORDER BY occurred_at DESC
            LIMIT 1
            """,
            project_id,
            deploy_id,
            issue_id,
        )
        if not ev_row:
            return

        occurred_at_dt = ev_row["occurred_at"]
        # Alert latency and time-to-detect are measured from the first event, not the latest
        first_event_dt = await conn.fetchval(
            "SELECT min(occurred_at) FROM events WHERE project_id = $1 AND deploy_id = $2 AND issue_id = $3",
            project_id,
            deploy_id,
            issue_id,
        )
        payload = {
            "request_path": ev_row["request_path"],
            "stack_trace": await decrypt_or_plain(
                project_id, "events", "stack_trace", ev_row.get("stack_trace_enc"), ev_row.get("stack_trace")
            ),
        }
        iss_row = await conn.fetchrow(
            "SELECT title FROM issues WHERE id = $1 AND project_id = $2",
            issue_id,
            project_id,
        )
        if iss_row and iss_row["title"]:
            title_parts = iss_row["title"].split(":", 1)
            payload["exc_type"] = title_parts[0].strip()

        from app.incident_manager import check_and_trigger_incident

        await check_and_trigger_incident(
            conn=conn,
            project_id=project_id,
            deploy_id=deploy_id,
            issue_id=issue_id,
            payload=payload,
            occurred_at_dt=occurred_at_dt,
            first_event_dt=first_event_dt,
        )
    except Exception as exc:
        logger.warning(
            "check_incident_for_issue failed for project=%d, deploy=%s, issue=%d: %s",
            project_id,
            deploy_id,
            issue_id,
            exc,
        )



async def _record_batch_sources(
    conn: asyncpg.Connection,
    project_id: int,
    events: List[Dict[str, Any]],
    is_heartbeat: bool,
) -> None:
    """Record source activity once per (environment, app) for the dead-man's switch (Trust Addendum A2)."""
    from app.sources import record_source_activity

    latest: Dict[Tuple[str, str], datetime] = {}
    for ev in events:
        key = (ev.get("environment") or "prod", ev.get("app_name") or "default")
        if key not in latest or ev["occurred_at_dt"] > latest[key]:
            latest[key] = ev["occurred_at_dt"]
    for (environment, app_name), occurred_at_dt in latest.items():
        try:
            # Savepoint: a failure here must not abort the batch transaction
            async with conn.transaction():
                await record_source_activity(
                    conn=conn,
                    project_id=project_id,
                    environment=environment,
                    app_name=app_name,
                    is_heartbeat=is_heartbeat,
                    occurred_at=occurred_at_dt,
                )
        except Exception as exc:
            logger.debug("Failed recording source activity for batch: %s", exc)


async def process_event_batch(batch: List[Dict[str, Any]]) -> None:
    """
    Process a batch of events with RLS enforcement, in-memory grouping,
    and batched multi-row database operations (Performance Addendum Stage 2).
    """
    if not batch:
        return

    # Group events by project_id for tenant RLS transactions
    by_project: Dict[int, List[Dict[str, Any]]] = {}
    for item in batch:
        pid = int(item["project_id"])
        by_project.setdefault(pid, []).append(item)

    for project_id, events in by_project.items():
        async with project_transaction(project_id) as conn:
            heartbeats: List[Dict[str, Any]] = []
            error_events: List[Dict[str, Any]] = []

            for ev in events:
                etype = ev.get("type", "error")
                occurred_at = float(ev["occurred_at"])
                ev["occurred_at_dt"] = datetime.fromtimestamp(occurred_at, tz=timezone.utc)
                if etype == "heartbeat":
                    heartbeats.append(ev)
                else:
                    error_events.append(ev)

            # Local batch cache: (environment, release) -> deploy_id
            deploy_cache: Dict[Tuple[str, Optional[str]], Optional[int]] = {}

            async def _resolve_deploy(ev: Dict[str, Any]) -> Optional[int]:
                env = ev.get("environment", "prod")
                rel = ev.get("release")
                key = (env, rel)
                if rel and key in deploy_cache:
                    return deploy_cache[key]
                dep_id = await find_deploy(
                    conn=conn,
                    project_id=project_id,
                    environment=env,
                    occurred_at_dt=ev["occurred_at_dt"],
                    release=rel,
                )
                if rel:
                    deploy_cache[key] = dep_id
                return dep_id

            # 1. Process Heartbeats
            if heartbeats:
                hb_aggregates: Dict[Tuple[datetime, Optional[int]], Dict[str, int]] = {}
                for hb in heartbeats:
                    dep_id = await _resolve_deploy(hb)
                    m_bucket = hb["occurred_at_dt"].replace(second=0, microsecond=0)
                    k = (m_bucket, dep_id)
                    if k not in hb_aggregates:
                        hb_aggregates[k] = {"requests": 0, "errors": 0}
                    hb_aggregates[k]["requests"] += int(hb.get("requests") or 0)
                    hb_aggregates[k]["errors"] += int(hb.get("errors") or 0)

                dep_ids = [k[1] for k in hb_aggregates]
                buckets = [k[0] for k in hb_aggregates]
                reqs = [v["requests"] for v in hb_aggregates.values()]
                errs = [v["errors"] for v in hb_aggregates.values()]

                await conn.execute(
                    """
                    INSERT INTO traffic (project_id, deploy_id, bucket, requests, errors)
                    SELECT $1, u.deploy_id, u.bucket, u.requests, u.errors
                    FROM unnest($2::int[], $3::timestamptz[], $4::int[], $5::int[])
                      AS u(deploy_id, bucket, requests, errors)
                    ON CONFLICT (project_id, bucket)
                    DO UPDATE SET
                      requests = traffic.requests + EXCLUDED.requests,
                      errors = traffic.errors + EXCLUDED.errors,
                      deploy_id = COALESCE(traffic.deploy_id, EXCLUDED.deploy_id)
                    """,
                    project_id,
                    dep_ids,
                    buckets,
                    reqs,
                    errs,
                )
                logger.debug(
                    "Processed batch heartbeats: project=%d, distinct_buckets=%d, total_heartbeats=%d",
                    project_id,
                    len(hb_aggregates),
                    len(heartbeats),
                )
                await _record_batch_sources(conn, project_id, heartbeats, is_heartbeat=True)

            # 2. Process Error & Slow Request Events
            if error_events:
                await _record_batch_sources(conn, project_id, error_events, is_heartbeat=False)
                fp_groups: Dict[str, Dict[str, Any]] = {}
                for ev in error_events:
                    ev_type = ev.get("type", "error")
                    fp = compute_fingerprint(
                        event_type=ev_type,
                        exc_type=ev.get("exc_type"),
                        stack_trace=ev.get("stack_trace"),
                        request_path=ev.get("request_path"),
                        message=ev.get("message"),
                    )
                    ev["fingerprint"] = fp

                    if ev_type == "slow_request":
                        title = f"Slow: {ev.get('request_path') or 'Unknown'}"
                    else:
                        exc = ev.get("exc_type") or "Error"
                        msg = (ev.get("message") or "")[:100]
                        title = f"{exc}: {msg}".strip(": ")
                    ev["title"] = title

                    if fp not in fp_groups:
                        fp_groups[fp] = {
                            "title": title,
                            "first_seen": ev["occurred_at_dt"],
                            "last_seen": ev["occurred_at_dt"],
                            "count": 1,
                        }
                    else:
                        fp_groups[fp]["first_seen"] = min(fp_groups[fp]["first_seen"], ev["occurred_at_dt"])
                        fp_groups[fp]["last_seen"] = max(fp_groups[fp]["last_seen"], ev["occurred_at_dt"])
                        fp_groups[fp]["count"] += 1

                # Upsert all distinct issues in ONE query with atomic event_count tracking (Storm Control Section 3)
                fps = list(fp_groups.keys())
                titles = [fp_groups[f]["title"] for f in fps]
                first_seens = [fp_groups[f]["first_seen"] for f in fps]
                last_seens = [fp_groups[f]["last_seen"] for f in fps]
                counts = [fp_groups[f]["count"] for f in fps]

                rows = await conn.fetch(
                    """
                    INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count)
                    SELECT $1, u.fp, u.title, u.first_seen, u.last_seen, 'open', u.cnt
                    FROM unnest($2::text[], $3::text[], $4::timestamptz[], $5::timestamptz[], $6::int[])
                      AS u(fp, title, first_seen, last_seen, cnt)
                    ON CONFLICT (project_id, fingerprint)
                    DO UPDATE SET
                      last_seen = GREATEST(issues.last_seen, EXCLUDED.last_seen),
                      event_count = issues.event_count + EXCLUDED.event_count
                    RETURNING fingerprint, id
                    """,
                    project_id,
                    fps,
                    titles,
                    first_seens,
                    last_seens,
                    counts,
                )

                fp_to_issue_id: Dict[str, int] = {}
                if rows:
                    for r in rows:
                        iss_id = int(r["id"])
                        fp = r["fingerprint"]
                        fp_to_issue_id[fp] = iss_id
                        issue_lookup_cache.set((project_id, fp), iss_id, ttl=300.0)
                else:
                    for i, f in enumerate(fps, start=1):
                        fp_to_issue_id[f] = i
                        issue_lookup_cache.set((project_id, f), i, ttl=300.0)

                # Pre-resolve deploy and issue IDs for all error events
                for ev in error_events:
                    dep_id = await _resolve_deploy(ev)
                    iss_id = fp_to_issue_id.get(ev["fingerprint"], 1)
                    ev["deploy_id"] = dep_id
                    ev["issue_id"] = iss_id

                # Storm control: cap full event rows to STORE_FULL_PER_MIN per (project, fingerprint) per minute
                full_events_to_store: List[Dict[str, Any]] = []
                redis = None
                try:
                    redis = get_redis()
                except Exception:
                    pass

                for fp, group_info in fp_groups.items():
                    fp_events = [e for e in error_events if e.get("fingerprint") == fp]
                    if not fp_events:
                        continue

                    m_epoch = int(group_info["first_seen"].replace(second=0, microsecond=0).timestamp())
                    storm_key = f"storm:{project_id}:{fp}:{m_epoch}"

                    quota_remaining = settings.STORE_FULL_PER_MIN
                    if redis:
                        try:
                            new_val = await redis.incrby(storm_key, len(fp_events))
                            await redis.expire(storm_key, 120)
                            prev_val = new_val - len(fp_events)
                            quota_remaining = max(0, settings.STORE_FULL_PER_MIN - prev_val)
                        except Exception as e:
                            logger.warning("Redis storm control check failed: %s", e)
                            quota_remaining = settings.STORE_FULL_PER_MIN

                    full_events_to_store.extend(fp_events[:quota_remaining])

                # Parallel encryption and insert of full event rows only up to quota
                async def _enc_ev(ev: Dict[str, Any]):
                    # Encrypted only; plaintext kept only if encryption fails
                    raw_extra = ev.get("extra")
                    raw_trace, trace_enc = await encrypt_or_keep(project_id, "events", "stack_trace", ev.get("stack_trace"))
                    extra_plain, extra_enc = await encrypt_or_keep(
                        project_id, "events", "extra", json.dumps(raw_extra) if raw_extra else None
                    )
                    return raw_trace, trace_enc, extra_plain or "", extra_enc

                if full_events_to_store:
                    enc_results = await asyncio.gather(*[_enc_ev(ev) for ev in full_events_to_store])

                    event_ids = []
                    issue_ids = []
                    deploy_ids = []
                    occurred_ats = []
                    raw_traces = []
                    trace_encs = []
                    req_paths = []
                    durations = []
                    extra_jsons = []
                    extra_encs = []

                    for ev, (raw_trace, trace_enc, extra_json, extra_enc) in zip(full_events_to_store, enc_results):
                        event_ids.append(str(ev.get("event_id") or uuid.uuid4().hex))
                        issue_ids.append(ev["issue_id"])
                        deploy_ids.append(ev["deploy_id"])
                        occurred_ats.append(ev["occurred_at_dt"])
                        raw_traces.append(raw_trace)
                        trace_encs.append(trace_enc)
                        req_paths.append(ev.get("request_path"))
                        durations.append(float(ev["duration_ms"]) if ev.get("duration_ms") is not None else None)
                        extra_jsons.append(extra_json)
                        extra_encs.append(extra_enc)

                    # Single multi-row insert for stored error events in batch
                    await conn.execute(
                        """
                        INSERT INTO events (
                          event_id, issue_id, project_id, deploy_id, occurred_at,
                          stack_trace, stack_trace_enc, request_path, duration_ms, extra, extra_enc
                        )
                        SELECT
                          u.event_id,
                          u.issue_id,
                          $1,
                          u.deploy_id,
                          u.occurred_at,
                          u.stack_trace,
                          u.stack_trace_enc,
                          u.request_path,
                          u.duration_ms,
                          COALESCE(NULLIF(u.extra, '')::jsonb, '{}'::jsonb),
                          u.extra_enc
                        FROM unnest(
                          $2::text[],
                          $3::int[],
                          $4::int[],
                          $5::timestamptz[],
                          $6::text[],
                          $7::bytea[],
                          $8::text[],
                          $9::double precision[],
                          $10::text[],
                          $11::bytea[]
                        ) AS u(
                          event_id, issue_id, deploy_id, occurred_at,
                          stack_trace, stack_trace_enc, request_path, duration_ms, extra, extra_enc
                        )
                        ON CONFLICT (project_id, event_id, occurred_at) WHERE event_id IS NOT NULL DO NOTHING
                        """,
                        project_id,
                        event_ids,
                        issue_ids,
                        deploy_ids,
                        occurred_ats,
                        raw_traces,
                        trace_encs,
                        req_paths,
                        durations,
                        extra_jsons,
                        extra_encs,
                    )

                # 3. Update event_rollups table for the batch (Performance Addendum Section 4)
                rollup_aggregates: Dict[Tuple[int, Optional[int], datetime], int] = {}
                for ev in error_events:
                    iss_id = ev.get("issue_id")
                    if iss_id is None:
                        continue
                    dep_id = ev.get("deploy_id")
                    m_bucket = ev["occurred_at_dt"].replace(second=0, microsecond=0)
                    k = (iss_id, dep_id, m_bucket)
                    rollup_aggregates[k] = rollup_aggregates.get(k, 0) + 1

                if rollup_aggregates:
                    r_issue_ids = [k[0] for k in rollup_aggregates]
                    r_deploy_ids = [k[1] for k in rollup_aggregates]
                    r_buckets = [k[2] for k in rollup_aggregates]
                    r_counts = list(rollup_aggregates.values())

                    try:
                        await conn.execute(
                            """
                            INSERT INTO event_rollups (project_id, issue_id, deploy_id, bucket, count)
                            SELECT $1, u.issue_id, u.deploy_id, u.bucket, u.count
                            FROM unnest($2::int[], $3::int[], $4::timestamptz[], $5::int[])
                              AS u(issue_id, deploy_id, bucket, count)
                            ON CONFLICT (project_id, issue_id, COALESCE(deploy_id, -1), bucket)
                            DO UPDATE SET count = event_rollups.count + EXCLUDED.count
                            """,
                            project_id,
                            r_issue_ids,
                            r_deploy_ids,
                            r_buckets,
                            r_counts,
                        )
                    except Exception as e:
                        logger.warning("Failed updating event_rollups in batch: %s", e)

                    # Update Redis sliding-window counters across the batch in one pipeline
                    try:
                        redis = get_redis()
                    except Exception:
                        redis = None

                    if redis:
                        try:
                            pipe = redis.pipeline()
                            if asyncio.iscoroutine(pipe):
                                pipe = await pipe
                            now_ts = int(time.time())
                            bucket_id = now_ts // 10
                            for (iss_id, dep_id, _), count in rollup_aggregates.items():
                                key = f"counter:{project_id}:{dep_id or 0}:{iss_id}:{bucket_id}"
                                pipe.incrby(key, count)
                                pipe.expire(key, 120)
                            await pipe.execute()
                        except Exception as e:
                            logger.warning("Failed updating Redis sliding-window counters in batch: %s", e)

                # Incident check hook: runs once per distinct (deploy, issue) in batch
                distinct_deploy_issues = {
                    (ev["deploy_id"], ev["issue_id"])
                    for ev in error_events
                    if ev.get("deploy_id") and ev.get("issue_id")
                }
                for dep_id, iss_id in distinct_deploy_issues:
                    await check_incident_for_issue(conn, project_id, dep_id, iss_id)

                logger.info(
                    "Processed batch events: project=%d, total_events=%d, distinct_issues=%d",
                    project_id,
                    len(error_events),
                    len(fp_groups),
                )


def _dump_payload(payload: Dict[str, Any]) -> str:
    """Serialize an event for re-queueing, dropping values computed during processing."""
    return json.dumps({k: v for k, v in payload.items() if k != "occurred_at_dt"})


async def handle_event_with_retry(payload: Dict[str, Any]) -> None:
    """Handle event with exponential retry and dead-letter queue dispatch."""
    redis = get_redis()
    tries = int(payload.get("_tries", 0))

    try:
        await process_single_event(payload)
    except Exception as exc:
        tries += 1
        payload["_tries"] = tries
        logger.error(
            "Failed processing event (attempt %d/%d): %s",
            tries,
            MAX_RETRIES,
            exc,
            exc_info=True,
        )

        if tries < MAX_RETRIES:
            # Re-queue for retry
            await redis.rpush(EVENTS_QUEUE, _dump_payload(payload))
        else:
            # Dispatch to dead-letter queue
            logger.critical(
                "Event exceeded max retries (%d). Pushing to %s: %s",
                MAX_RETRIES,
                DEAD_LETTER_QUEUE,
                payload,
            )
            await redis.rpush(DEAD_LETTER_QUEUE, _dump_payload(payload))


async def reap_stuck_processing_events(timeout_sec: Optional[int] = None) -> int:
    """
    Reaper: returns items stuck in events:processing:* lists back to the main queue.
    Prevents event loss if a worker crashes or is killed mid-batch.
    """
    redis = get_redis()
    reaped_count = 0
    try:
        # Scan for all worker processing queues
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match="events:processing:*", count=100)
            for proc_key in keys:
                # Atomically move items from processing queue back to main events queue
                while True:
                    item = await redis.rpoplpush(proc_key, EVENTS_QUEUE)
                    if not item:
                        break
                    reaped_count += 1
            if cursor == 0:
                break
    except Exception as exc:
        logger.error("Error during stuck events reaping: %s", exc)

    if reaped_count > 0:
        logger.warning("Reaper returned %d stuck items from crashed worker queues to %s", reaped_count, EVENTS_QUEUE)
    return reaped_count


async def pull_batch_from_queue(
    redis: Any,
    worker_id: str,
    batch_max: int = BATCH_MAX,
    max_wait_ms: float = BATCH_MAX_WAIT_MS,
    timeout_sec: float = 1.0,
) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Pull up to batch_max events from Redis using reliable queue pattern.
    Blocks for the first event up to timeout_sec, then continues collecting
    events non-blockingly until batch_max is reached or max_wait_ms has elapsed.
    """
    processing_queue = f"events:processing:{worker_id}"
    batch: List[Tuple[str, Dict[str, Any]]] = []

    try:
        # First item with blocking move
        raw_first = None
        if hasattr(redis, "blmove"):
            # redis-py signature: blmove(first_list, second_list, timeout, src, dest)
            raw_first = await redis.blmove(EVENTS_QUEUE, processing_queue, timeout_sec, "LEFT", "RIGHT")
        elif hasattr(redis, "brpoplpush"):
            raw_first = await redis.brpoplpush(EVENTS_QUEUE, processing_queue, timeout=int(timeout_sec))

        if not raw_first:
            return []

        try:
            payload_first = json.loads(raw_first)
            batch.append((raw_first, payload_first))
        except Exception:
            batch.append((raw_first, {}))

        # Subsequent items non-blocking up to batch_max or deadline
        deadline = time.monotonic() + (max_wait_ms / 1000.0)
        while len(batch) < batch_max and time.monotonic() < deadline:
            raw_next = None
            if hasattr(redis, "lmove"):
                raw_next = await redis.lmove(EVENTS_QUEUE, processing_queue, "LEFT", "RIGHT")
            elif hasattr(redis, "rpoplpush"):
                raw_next = await redis.rpoplpush(EVENTS_QUEUE, processing_queue)

            if not raw_next:
                remaining = deadline - time.monotonic()
                if remaining > 0.005:
                    await asyncio.sleep(min(0.005, remaining))
                    continue
                else:
                    break

            try:
                payload_next = json.loads(raw_next)
                batch.append((raw_next, payload_next))
            except Exception:
                batch.append((raw_next, {}))

    except Exception as exc:
        logger.error("Error pulling batch from queue: %s", exc)
        # Back off so a persistent queue error cannot spin the CPU and flood the logs.
        if not batch:
            await asyncio.sleep(min(timeout_sec, 1.0))

    return batch


async def handle_event_batch_with_retry(
    batch: List[Tuple[str, Dict[str, Any]]],
    redis: Optional[Any] = None,
) -> None:
    """Process a batch of events with fallback to individual retries on failure."""
    if not batch:
        return

    r = redis or get_redis()
    payloads = [item[1] for item in batch]
    try:
        await process_event_batch(payloads)
    except Exception as exc:
        logger.error(
            "Batch processing of %d events failed: %s; falling back to individual retry",
            len(batch),
            exc,
            exc_info=True,
        )
        for _, payload in batch:
            tries = int(payload.get("_tries", 0)) + 1
            payload["_tries"] = tries
            if tries < MAX_RETRIES:
                await r.rpush(EVENTS_QUEUE, _dump_payload(payload))
            else:
                logger.critical(
                    "Event exceeded max retries (%d). Pushing to %s: %s",
                    MAX_RETRIES,
                    DEAD_LETTER_QUEUE,
                    payload,
                )
                await r.rpush(DEAD_LETTER_QUEUE, _dump_payload(payload))


def check_memory_limit() -> bool:
    """Check if worker process RSS exceeds MAX_MEMORY_MB."""
    try:
        import resource
        rusage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            mb = rusage / (1024 * 1024)
        else:
            mb = rusage / 1024
        if mb > MAX_MEMORY_MB:
            logger.warning(
                "Worker memory threshold exceeded: %.1f MB > %d MB limit. Clean shutdown requested.",
                mb,
                MAX_MEMORY_MB,
            )
            return True
    except Exception:
        pass
    return False


async def run_worker(worker_id: Optional[str] = None):
    """
    Main worker loop consuming from Redis queue using reliable queue pattern (BLMOVE).
    Atomically moves events from 'events' to 'events:processing:<worker_id>'.
    """
    worker_uuid = worker_id or f"{os.getpid()}_{uuid.uuid4().hex[:6]}"
    processing_queue = f"events:processing:{worker_uuid}"

    logger.info("ChangeWatch reliable queue worker started (ID: %s)", worker_uuid)
    await init_db_pool()
    await init_redis()

    redis = get_redis()
    is_running = True

    def _shutdown():
        nonlocal is_running
        logger.info("Shutdown signal received for worker %s", worker_uuid)
        is_running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    last_hb_time = 0.0
    try:
        while is_running:
            if check_memory_limit():
                logger.info("Worker %s terminating cleanly due to memory limit", worker_uuid)
                break

            # Heartbeat for Watchdog (Trust Addendum Section A3)
            now_ts = time.time()
            if now_ts - last_hb_time > 15.0:
                try:
                    await redis.set(f"heartbeat:worker:{worker_uuid}", str(datetime.now(timezone.utc).timestamp()), ex=120)
                    last_hb_time = now_ts
                except Exception:
                    pass

            try:
                batch = await pull_batch_from_queue(
                    redis=redis,
                    worker_id=worker_uuid,
                    batch_max=BATCH_MAX,
                    max_wait_ms=BATCH_MAX_WAIT_MS,
                    timeout_sec=1.0,
                )
                if not batch:
                    continue

                await handle_event_batch_with_retry(batch, redis=redis)

                # Atomically remove successfully processed items from processing queue
                pipe = redis.pipeline()
                for raw_data, _ in batch:
                    pipe.lrem(processing_queue, 1, raw_data)
                await pipe.execute()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in worker execution: %s", exc, exc_info=True)
                await asyncio.sleep(0.5)

    finally:
        logger.info("Worker %s reclaiming any unprocessed items in %s", worker_uuid, processing_queue)
        try:
            # Reclaim any uncommitted items back to the main queue
            while True:
                item = await redis.rpoplpush(processing_queue, EVENTS_QUEUE)
                if not item:
                    break
        except Exception as exc:
            logger.error("Failed reclaiming processing queue: %s", exc)

        await close_db_pool()
        await close_redis()


def _worker_process_target(worker_id: str):
    """Target function executed inside child worker process."""
    from app.main import setup_structured_logging
    setup_structured_logging()
    asyncio.run(run_worker(worker_id=worker_id))


class WorkerSupervisor:
    """
    Process supervisor for multi-process worker scaling.
    Monitors child worker processes, auto-restarts crashed workers,
    and cleanly propagates shutdown signals (Performance Addendum Section 1).
    """
    def __init__(self, num_processes: int):
        self.num_processes = max(1, num_processes)
        self.is_running = True
        self.workers: Dict[int, multiprocessing.Process] = {}

    def _start_worker(self, index: int) -> multiprocessing.Process:
        worker_id = f"proc_{index}_{uuid.uuid4().hex[:6]}"
        proc = multiprocessing.Process(
            target=_worker_process_target,
            args=(worker_id,),
            name=f"ChangeWatchWorker-{index}",
        )
        proc.start()
        self.workers[index] = proc
        logger.info("Worker supervisor started child worker %d (PID: %d, ID: %s)", index, proc.pid, worker_id)
        return proc

    def start_all(self):
        logger.info("Worker supervisor initializing %d worker processes", self.num_processes)
        for i in range(self.num_processes):
            self._start_worker(i)

    def check_and_restart(self) -> int:
        """Check all child processes and restart any that terminated unexpectedly."""
        restarted = 0
        for i, proc in list(self.workers.items()):
            if not proc.is_alive():
                exit_code = proc.exitcode
                logger.warning(
                    "Worker process %d (PID: %s) terminated with exit code %s. Restarting replacement worker...",
                    i,
                    proc.pid,
                    exit_code,
                )
                new_proc = self._start_worker(i)
                self.workers[i] = new_proc
                restarted += 1
        return restarted

    def stop_all(self, timeout: float = 3.0):
        """Cleanly terminate all managed child worker processes."""
        self.is_running = False
        logger.info("Stopping all %d worker processes...", len(self.workers))
        for i, proc in self.workers.items():
            if proc.is_alive():
                proc.terminate()
        for i, proc in self.workers.items():
            proc.join(timeout=timeout)
            if proc.is_alive():
                proc.kill()
        logger.info("All worker processes stopped.")

    def run_forever(self):
        """Supervision loop maintaining active child workers."""
        self.start_all()

        def _sig_handler(sig, frame):
            logger.info("Supervisor caught signal %d; initiating graceful shutdown", sig)
            self.stop_all()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, _sig_handler)
            except (ValueError, AttributeError):
                pass

        try:
            while self.is_running:
                time.sleep(1.0)
                if not self.is_running:
                    break
                self.check_and_restart()
        finally:
            self.stop_all()


def main():
    from app.main import setup_structured_logging
    setup_structured_logging()

    parser = argparse.ArgumentParser(description="ChangeWatch Queue Worker")
    parser.add_argument(
        "--processes", "-p",
        type=str,
        default=os.environ.get("WORKER_PROCESSES", "auto"),
        help="Number of worker processes (integer or 'auto')",
    )
    args, _ = parser.parse_known_args()

    num_processes = resolve_worker_processes(args.processes)
    cpu_count = os.cpu_count() or 1
    logger.info(
        "Worker startup: detected_cpus=%d, chosen_processes=%d (spec=%s)",
        cpu_count,
        num_processes,
        args.processes,
    )

    if num_processes == 1:
        asyncio.run(run_worker())
    else:
        supervisor = WorkerSupervisor(num_processes=num_processes)
        supervisor.run_forever()


if __name__ == "__main__":
    main()
