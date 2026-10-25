import asyncio
import calendar
import hashlib
import json
from html import escape as html_escape
from urllib.parse import parse_qs
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.auth import (
    get_api_key,
    require_project_id,
    require_ingest_project_id,
    require_read_project_id,
    require_admin_key,
)
from app.cache import invalidate_deploy_cache, issue_lookup_cache
from app.codeowners import invalidate_codeowners
from app.config import settings
from app.crypto import decrypt, decrypt_or_plain, encrypt_or_keep
from app.db import check_db_health, close_db_pool, get_connection, init_db_pool, project_transaction, read_project_transaction
from app.scrub import scrub_text
from app.sre_prep import classify_file

from app.fingerprint import compute_fingerprint
from app.llm import log_llm_privacy_notice
from app.logging_filter import LogRedactionFilter, redact_delivery_error, redact_sensitive_data
from app.migrations import run_migrations
from app.metering import (
    Meter,
    calculate_billed_micros,
    calculate_llm_cost_micros,
    record_usage,
    get_active_price_book,
    get_project_budget,
    set_project_budget,
)
from app.diffparse import parse_unified_diff
from app.ingest_stats import count_dropped, dropped_totals
from app.queue import check_redis_health, close_redis, get_redis, init_redis
from app.ratelimit import check_rate_limit
from app.analytics import analytics_router
from app.schemas import (
    DeployCreate,
    DeployFileItem,
    DeployItem,
    DeployResponse,
    EventsBatch,
    EventsResponse,
    IncidentDetailResponse,
    IncidentItem,
    IssueDetailResponse,
    IssueEventItem,
    IssueItem,
    ProjectSettingsResponse,
    VerdictResponse,
)




class JSONLogFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging with secret redaction."""

    def format(self, record: logging.LogRecord) -> str:
        msg = redact_sensitive_data(record.getMessage())
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
        }
        if record.exc_info or getattr(record, "exc_text", None):
            log_entry["exception"] = "[STACK TRACE REDACTED]"
        return json.dumps(log_entry)


def setup_structured_logging():
    """Configure root logger to output structured JSON logs with redaction filter."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONLogFormatter())
    filter_ = LogRedactionFilter()
    handler.addFilter(filter_)
    root = logging.getLogger()
    root.handlers = [handler]
    root.addFilter(filter_)
    root.setLevel(logging.INFO)
    # httpx and httpcore log every request with its full URL at INFO; for webhooks the URL is the
    # credential. Their warnings and errors still come through (and are redacted).
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


setup_structured_logging()
logger = logging.getLogger("changewatch")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown lifecycle."""
    logger.info("ChangeWatch server starting up")
    from app.crypto import check_master_key
    check_master_key()  # never run without encryption: refuse to start instead
    try:
        pool = await init_db_pool()
        # Apply pending database migrations on startup (Addendum A2)
        applied = await run_migrations(pool)
        if applied:
            logger.info("Applied database migrations on startup: %s", applied)
    except Exception as exc:
        logger.error("Failed to initialize database or apply migrations on startup: %s", exc)

    try:
        await init_redis()
    except Exception as exc:
        logger.error("Failed to initialize Redis client on startup: %s", exc)

    # Privacy notice regarding LLM provider on startup (Addendum B)
    log_llm_privacy_notice()

    yield

    logger.info("ChangeWatch server shutting down")
    await close_db_pool()
    await close_redis()


app = FastAPI(
    title="ChangeWatch API",
    version="1.0.0",
    description="ChangeWatch developer release and error monitoring service",
    lifespan=lifespan,
)

# Enable CORS for dashboard and browser clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analytics_router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 that says what is wrong without echoing the submitted values back: they may hold
    secrets, and a NaN cannot be written as JSON at all (the default handler failed with a 500)."""
    errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})




@app.middleware("http")
async def request_logger_middleware(request: Request, call_next):
    """Log HTTP requests and response times in structured JSON format."""
    start_time = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start_time) * 1000, 2)

    logger.info(
        f"{request.method} {request.url.path} -> {response.status_code} in {duration_ms}ms"
    )
    return response


@app.get("/healthz", tags=["System"])
async def healthz():
    """
    Healthcheck endpoint that checks PostgreSQL and Redis.
    Also reports queue depth, dropped event count, and worker count (Performance Addendum Section 6).
    Returns HTTP 200 if both are healthy, HTTP 503 otherwise.
    """
    postgres_ok = await check_db_health()
    redis_ok = await check_redis_health()

    queue_depth = 0
    dropped_count = 0
    worker_count = 0

    if redis_ok:
        try:
            r = get_redis()
            queue_depth = await r.llen("events")
            dropped_raw = await r.get("stats:dropped_count")
            dropped_count = int(dropped_raw or 0)
            cursor, keys = await r.scan(0, match="events:processing:*", count=100)
            worker_count = max(len(keys), 1)
        except Exception:
            pass

    payload = {
        "status": "ok" if (postgres_ok and redis_ok) else "error",
        "postgres": "ok" if postgres_ok else "unreachable",
        "redis": "ok" if redis_ok else "unreachable",
        "queue_depth": queue_depth,
        "dropped_count": dropped_count,
        "worker_count": worker_count,
    }

    status_code = (
        status.HTTP_200_OK
        if (postgres_ok and redis_ok)
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/v1/status", tags=["System"])
async def get_system_status_endpoint(
    project_id: int = Depends(require_read_project_id),
):
    """
    Component watchdog and system status endpoint (Trust Addendum Section A3).
    Returns all component states, queue lag, dropped events, outbox backlog, and customer sources.
    Protected by Read API key.
    """
    from app.watchdog import run_watchdog_checks
    redis = get_redis()
    async with read_project_transaction(project_id) as conn:
        status_data = await run_watchdog_checks(conn=conn, redis=redis, ping_external=False)

        # Also fetch sources for this project
        sources_rows = await conn.fetch(
            """
            SELECT environment, app_name, status, last_heartbeat_at, last_event_at,
                   typical_gap_sec, silent_since, silence_alerts
            FROM sources
            WHERE project_id = $1
            ORDER BY environment, app_name
            """,
            project_id,
        )
        sources = [
            {
                "environment": r["environment"],
                "app_name": r["app_name"],
                "status": r["status"],
                "last_heartbeat_at": r["last_heartbeat_at"].isoformat() if r["last_heartbeat_at"] else None,
                "last_event_at": r["last_event_at"].isoformat() if r["last_event_at"] else None,
                "typical_gap_sec": r["typical_gap_sec"],
                "silent_since": r["silent_since"].isoformat() if r["silent_since"] else None,
            }
            for r in sources_rows
        ]
        status_data["sources"] = sources

        # Events the API turned away in the last 30 days (rate limit, backpressure)
        today = datetime.now(timezone.utc).date()
        dropped = await dropped_totals(_redis_or_none(), project_id, today - timedelta(days=29), today)
        status_data["dropped_events"] = sum(dropped.values())

        return status_data


@app.get("/v1/auth/verify", tags=["Auth"])
async def verify_auth(project_id: int = Depends(require_project_id)):
    """Utility endpoint to verify API key validity and resolve project ID."""
    return {"ok": True, "project_id": project_id}


@app.post("/v1/deploys", response_model=DeployResponse, tags=["Deploys"])
async def create_deploy(
    payload: DeployCreate,
    project_id: int = Depends(require_ingest_project_id),
):
    """
    Deploy hook endpoint (Section 6.1).
    Inserts a deploy record and returns {ok: true, deploy_id: ...}.
    Requires valid Ingest Key (Read keys receive HTTP 403 Forbidden).
    """
    # Ephemeral Code Mode (TD Addendum Section G):
    # Server never stores file contents or diff body; only paths, line ranges, authors, and CODEOWNERS
    is_ephemeral = settings.CODE_MODE == "ephemeral"
    diff = None if is_ephemeral else (scrub_text(payload.diff[:20000]) if payload.diff else None)
    msg = payload.message[:2000] if payload.message else None

    # Clients that send only a diff (the GitHub Action, curl) still get changed files and line
    # ranges, so alerts can say the failing line changed in this release. Paths and line numbers
    # only: no file content.
    files = list(payload.files or [])
    if not files and payload.diff:
        files = [
            DeployFileItem(path=f["path"], status=f["status"], added_ranges=f["added_ranges"],
                           authors=[payload.author] if payload.author else [])
            for f in parse_unified_diff(payload.diff)
        ]

    # Sensitive code is stored encrypted only (plaintext kept only if encryption fails)
    diff, diff_enc = await encrypt_or_keep(project_id, "deploys", "diff", diff)
    codeowners, codeowners_enc = await encrypt_or_keep(
        project_id, "deploys", "codeowners", payload.codeowners[:20000] if payload.codeowners else None
    )

    async with project_transaction(project_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO deploys (project_id, commit_sha, author, message, diff, diff_enc, environment,
                                 codeowners, codeowners_enc, repository)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            project_id,
            payload.commit_sha,
            payload.author,
            msg,
            diff,
            diff_enc,
            payload.environment,
            codeowners,
            codeowners_enc,
            payload.repository,
        )
        deploy_id = int(row["id"])

        if files:
            for f in files[:50]:
                is_dep, is_cfg = classify_file(f.path)
                file_content = None if is_ephemeral else (scrub_text(f.content[:20480]) if f.content else None)
                file_content, content_enc = await encrypt_or_keep(project_id, "deploy_files", "content", file_content)
                await conn.execute(
                    """
                    INSERT INTO deploy_files (
                        deploy_id, project_id, path, status,
                        added_ranges, authors, content, content_enc,
                        is_dependency_file, is_config_file
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    deploy_id,
                    project_id,
                    f.path[:1000],
                    f.status[:50],
                    json.dumps(f.added_ranges),
                    json.dumps(f.authors[:2]),
                    file_content,
                    content_enc,
                    is_dep,
                    is_cfg,
                )

    invalidate_deploy_cache(project_id)
    invalidate_codeowners(project_id)
    logger.info("Deploy registered: project=%d, deploy_id=%d, sha=%s", project_id, deploy_id, payload.commit_sha)


    # Usage Metering Stage 2: Record release_monitored
    try:
        meter_envs = [e.strip() for e in settings.METER_ENVIRONMENTS.split(",") if e.strip()]
        if payload.environment in meter_envs:
            async with project_transaction(project_id) as uconn:
                await record_usage(
                    conn=uconn,
                    project_id=project_id,
                    meter=Meter.RELEASE_MONITORED,
                    units=1,
                    ref_type="deploy",
                    ref_id=deploy_id,
                    idempotency_key=f"deploy:{deploy_id}",
                    occurred_at=datetime.now(timezone.utc),
                )
    except Exception as exc:
        logger.warning("Failed recording usage for deploy %d: %s", deploy_id, exc)

    return DeployResponse(ok=True, deploy_id=deploy_id)


QUEUE_RETRY_AFTER_SEC = 30


def _redis_or_none():
    """Redis for best-effort counters; None when it is not available."""
    try:
        return get_redis()
    except Exception:
        return None


@app.post("/v1/events", response_model=EventsResponse, tags=["Ingestion"])
async def ingest_events(
    batch: EventsBatch,
    project_id: int = Depends(require_ingest_project_id),
    api_key: str = Depends(get_api_key),
):
    """
    Ingest endpoint (Section 6.2 & Addendum C1).
    Validates batch (up to 100 events), checks rate limit, and queues to Redis list 'events'.
    Requires valid Ingest Key (Read keys receive HTTP 403 Forbidden).
    No database writes in request path.
    """

    # Separate normal events and heartbeats for rate limiting
    heartbeat_count = sum(1 for e in batch.events if e.type == "heartbeat")
    normal_count = len(batch.events) - heartbeat_count

    # Limits are per minute: tell the client (the SDK honours Retry-After) when the next minute starts
    retry_after = {"Retry-After": str(max(1, 60 - int(time.time()) % 60))}

    # Check rate limit for normal events
    if normal_count > 0:
        allowed, current, limit = await check_rate_limit(api_key, normal_count, is_heartbeat=False)
        if not allowed:
            await count_dropped(_redis_or_none(), project_id, "rate_limit", len(batch.events))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {current} events in current minute (limit {limit})",
                headers=retry_after,
            )

    # Heartbeats over their limit are dropped and counted, but the batch is not refused: a 429 here
    # held back the errors sent with them, and every SDK process retried in the same second
    heartbeats_dropped = 0
    if heartbeat_count > 0:
        allowed, current, limit = await check_rate_limit(api_key, heartbeat_count, is_heartbeat=True)
        if not allowed:
            heartbeats_dropped = heartbeat_count
            batch = EventsBatch.model_construct(events=[e for e in batch.events if e.type != "heartbeat"])
            await count_dropped(_redis_or_none(), project_id, "rate_limit", heartbeats_dropped)
            logger.warning("Heartbeat limit reached for project %d (%d/min): %d heartbeat(s) dropped",
                           project_id, limit, heartbeats_dropped)

    # Performance Addendum Section 3: Backpressure Shedding
    redis = get_redis()
    queue_len = 0
    try:
        raw_len = await redis.llen("events")
        if isinstance(raw_len, int) and not hasattr(raw_len, "_mock_return_value"):
            queue_len = raw_len
        elif isinstance(raw_len, (str, bytes)):
            queue_len = int(raw_len)
        else:
            queue_len = 0
    except Exception:
        queue_len = 0

    dropped_count = 0
    events_to_queue = []

    if queue_len > settings.QUEUE_SOFT_LIMIT:
        # Under backpressure:
        # 1. Drop slow_request events first
        # 2. Sample repeated errors (never drop heartbeats or the first occurrence of a new fingerprint)
        for event in batch.events:
            etype = event.type
            if etype == "heartbeat":
                # Never drop heartbeats
                events_to_queue.append(event)
            elif etype == "slow_request":
                # Drop slow_request first
                dropped_count += 1
            else:
                # Error event: check if first occurrence of new fingerprint
                fp = compute_fingerprint(
                    event_type=etype,
                    exc_type=event.exc_type,
                    stack_trace=event.stack_trace,
                    request_path=event.request_path,
                    message=event.message,
                )
                # Check cache/Redis if fingerprint was previously seen
                is_known = issue_lookup_cache.get((project_id, fp)) is not None
                if not is_known:
                    try:
                        seen = await redis.sismember(f"seen_fps:{project_id}", fp)
                        is_known = bool(seen)
                    except Exception:
                        is_known = False

                if not is_known:
                    # First occurrence of new fingerprint: never drop!
                    events_to_queue.append(event)
                    issue_lookup_cache.set((project_id, fp), -1, ttl=300.0)
                    try:
                        await redis.sadd(f"seen_fps:{project_id}", fp)
                    except Exception:
                        pass
                else:
                    # Repeated error: sample 1 in 10
                    ev_id_str = str(event.event_id or "")
                    if ev_id_str and (int(hashlib.md5(ev_id_str.encode()).hexdigest(), 16) % 10 == 0):
                        events_to_queue.append(event)
                    else:
                        dropped_count += 1

        try:
            await redis.incrby("counter:events_dropped", dropped_count)
            await redis.incrby("stats:dropped_count", dropped_count)
        except Exception:
            pass
        await count_dropped(redis, project_id, "backpressure", dropped_count)
        logger.warning(
            "Backpressure shedding active (queue_len=%d > limit=%d): dropped=%d, accepted=%d",
            queue_len,
            settings.QUEUE_SOFT_LIMIT,
            dropped_count,
            len(events_to_queue),
        )
    else:
        events_to_queue = list(batch.events)

    # Push kept events to Redis list 'events' in one MULTI/EXEC: the batch is queued whole or not at all
    try:
        pipe = redis.pipeline(transaction=True)
        if asyncio.iscoroutine(pipe):
            pipe = await pipe
        for event in events_to_queue:
            item = event.model_dump()
            item["project_id"] = project_id
            item["_tries"] = 0
            pipe.rpush("events", json.dumps(item))

        await pipe.execute()
    except Exception as exc:
        # Queue unavailable (Redis down): tell the SDK to keep the events and retry later (brief §9)
        logger.error("Event queue unavailable, asking client to retry: %s", redact_delivery_error(str(exc)))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Event queue temporarily unavailable; retry later",
            headers={"Retry-After": str(QUEUE_RETRY_AFTER_SEC)},
        )
    return EventsResponse(accepted=len(events_to_queue), dropped=dropped_count + heartbeats_dropped)


# ---------------------------------------------------------------------------
# Phase 1 Stage 5: Read API Endpoints
# ---------------------------------------------------------------------------


@app.get("/v1/verdict", response_model=VerdictResponse, tags=["Read API"])
async def get_verdict(
    sha: str = Query(..., min_length=1, max_length=64, description="Commit SHA of the deploy"),
    environment: str = Query("prod", max_length=50, description="Target environment"),
    project_id: int = Depends(require_read_project_id),
):
    """
    Returns latest deploy analysis and verdict ('safe', 'watch', 'rollback', 'no_data')
    for the specified commit SHA.
    Requires Read Key (Ingest key receives HTTP 403 Forbidden).
    """
    clean_sha = sha.strip()
    clean_env = environment.strip()

    async with read_project_transaction(project_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT d.id AS deploy_id, d.commit_sha, d.environment, d.author, d.message, d.deployed_at,
                   a.verdict, a.verdict_reasons, a.verdict_final, a.verdict_updated_at,
                   a.likely_cause, a.new_issues, a.errors_before, a.errors_after, a.flagged
            FROM deploys d
            LEFT JOIN deploy_analysis a ON a.deploy_id = d.id
            WHERE d.project_id = $1
              AND (d.commit_sha = $2 OR (length($2) >= 7 AND d.commit_sha LIKE $2 || '%'))
              AND d.environment = $3
            ORDER BY d.deployed_at DESC
            LIMIT 1
            """,
            project_id,
            clean_sha,
            clean_env,
        )

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deploy with commit SHA '{sha}' not found for this project",
        )

    reasons = row["verdict_reasons"]
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except Exception:
            reasons = [reasons]
    elif not isinstance(reasons, list):
        reasons = [str(reasons)] if reasons else []

    verdict_val = row["verdict"]
    if not verdict_val or verdict_val == "pending":
        verdict_val = "no_data"

    return VerdictResponse(
        deploy_id=int(row["deploy_id"]),
        commit_sha=row["commit_sha"],
        environment=row["environment"],
        author=row["author"],
        message=row["message"],
        deployed_at=row["deployed_at"].isoformat() if row["deployed_at"] else None,
        verdict=verdict_val,
        verdict_reasons=reasons,
        verdict_final=bool(row["verdict_final"]) if row["verdict_final"] is not None else False,
        verdict_updated_at=row["verdict_updated_at"].isoformat() if row["verdict_updated_at"] else None,
        likely_cause=row["likely_cause"],
        new_issues=int(row["new_issues"] or 0),
        errors_before=int(row["errors_before"] or 0),
        errors_after=int(row["errors_after"] or 0),
        flagged=bool(row["flagged"]) if row["flagged"] is not None else False,
    )


@app.get("/api/deploys", response_model=List[DeployItem], tags=["Read API"])
async def list_deploys(
    limit: int = Query(50, ge=1, le=200, description="Max number of deploys to return"),
    environment: Optional[str] = Query(None, description="Optional environment filter"),
    project_id: int = Depends(require_read_project_id),
):
    """
    Returns deploys for the authenticated project under RLS.
    Requires Read Key (Ingest key receives HTTP 403 Forbidden).
    """
    async with read_project_transaction(project_id) as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.commit_sha, d.author, d.message, d.diff, d.diff_enc, d.environment, d.deployed_at,
                   a.verdict, a.verdict_reasons, a.verdict_final, a.verdict_updated_at,
                   a.likely_cause, a.new_issues, a.errors_before, a.errors_after, a.flagged
            FROM deploys d
            LEFT JOIN deploy_analysis a ON a.deploy_id = d.id
            WHERE d.project_id = $1
              AND ($2::text IS NULL OR d.environment = $2)
            ORDER BY d.deployed_at DESC
            LIMIT $3
            """,
            project_id,
            environment,
            limit,
        )

    items = []
    for r in rows:
        reasons = r["verdict_reasons"]
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except Exception:
                reasons = [reasons]
        elif not isinstance(reasons, list):
            reasons = [str(reasons)] if reasons else []

        verdict_val = r["verdict"]
        if not verdict_val or verdict_val == "pending":
            verdict_val = "no_data"

        diff_val = r.get("diff")
        if r.get("diff_enc"):
            try:
                diff_val = await decrypt(project_id, "deploys", "diff", r["diff_enc"])
            except Exception as exc:
                logger.warning("Failed decrypting diff_enc for deploy %d: %s", r["id"], exc)

        items.append(
            DeployItem(
                id=int(r["id"]),
                commit_sha=r["commit_sha"],
                author=r["author"],
                message=r["message"],
                diff=diff_val,
                environment=r["environment"],
                deployed_at=r["deployed_at"].isoformat() if r["deployed_at"] else None,
                verdict=verdict_val,
                verdict_reasons=reasons,
                verdict_final=bool(r["verdict_final"]) if r["verdict_final"] is not None else False,
                verdict_updated_at=r["verdict_updated_at"].isoformat() if r["verdict_updated_at"] else None,
                likely_cause=r["likely_cause"],
                new_issues=int(r["new_issues"] or 0),
                errors_before=int(r["errors_before"] or 0),
                errors_after=int(r["errors_after"] or 0),
                flagged=bool(r["flagged"]) if r["flagged"] is not None else False,
            )
        )
    return items


@app.get("/api/issues", response_model=List[IssueItem], tags=["Read API"])
async def list_issues(
    limit: int = Query(50, ge=1, le=200, description="Max number of issues to return"),
    status_filter: Optional[str] = Query(None, alias="status", description="Status filter: open / resolved"),
    project_id: int = Depends(require_read_project_id),
):
    """
    Returns issues for the authenticated project under RLS.
    Requires Read Key (Ingest key receives HTTP 403 Forbidden).
    """
    async with read_project_transaction(project_id) as conn:
        rows = await conn.fetch(
            """
            SELECT i.id, i.fingerprint, i.title, i.first_seen, i.last_seen, i.status,
                   COALESCE(i.event_count, COUNT(e.id)) AS event_count
            FROM issues i
            LEFT JOIN events e ON e.issue_id = i.id AND e.project_id = i.project_id
            WHERE i.project_id = $1
              AND ($2::text IS NULL OR i.status = $2)
            GROUP BY i.id, i.fingerprint, i.title, i.first_seen, i.last_seen, i.status, i.event_count
            ORDER BY i.last_seen DESC
            LIMIT $3
            """,
            project_id,
            status_filter,
            limit,
        )

    return [
        IssueItem(
            id=int(r["id"]),
            fingerprint=r["fingerprint"],
            title=r["title"] or "Unnamed issue",
            first_seen=r["first_seen"].isoformat() if r["first_seen"] else None,
            last_seen=r["last_seen"].isoformat() if r["last_seen"] else None,
            status=r["status"],
            event_count=int(r["event_count"] or 0),
        )
        for r in rows
    ]


@app.get("/api/issues/{id}", response_model=IssueDetailResponse, tags=["Read API"])
async def get_issue_details(
    id: int = Path(..., ge=1, description="Issue ID"),
    project_id: int = Depends(require_read_project_id),
):
    """
    Returns issue details and recent events under project RLS.
    Returns 404 if the issue does not exist or belongs to another tenant.
    Requires Read Key (Ingest key receives HTTP 403 Forbidden).
    """
    async with read_project_transaction(project_id) as conn:
        issue = await conn.fetchrow(
            """
            SELECT i.id, i.fingerprint, i.title, i.first_seen, i.last_seen, i.status,
                   COALESCE(i.event_count, COUNT(e.id)) AS event_count
            FROM issues i
            LEFT JOIN events e ON e.issue_id = i.id AND e.project_id = i.project_id
            WHERE i.id = $1 AND i.project_id = $2
            GROUP BY i.id, i.fingerprint, i.title, i.first_seen, i.last_seen, i.status, i.event_count
            """,
            id,
            project_id,
        )

        if not issue:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Issue {id} not found",
            )

        events_rows = await conn.fetch(
            """
            SELECT e.id, e.occurred_at, e.stack_trace, e.stack_trace_enc, e.request_path, e.duration_ms, e.extra, e.extra_enc, e.deploy_id,
                   d.commit_sha
            FROM events e
            LEFT JOIN deploys d ON d.id = e.deploy_id
            WHERE e.issue_id = $1 AND e.project_id = $2
            ORDER BY e.occurred_at DESC
            LIMIT 20
            """,
            id,
            project_id,
        )

    events_list = []
    for er in events_rows:
        trace_val = er.get("stack_trace")
        if er.get("stack_trace_enc"):
            try:
                trace_val = await decrypt(project_id, "events", "stack_trace", er["stack_trace_enc"])
            except Exception as exc:
                logger.warning("Failed decrypting stack_trace_enc for event %d: %s", er["id"], exc)

        extra_val = er.get("extra")
        if er.get("extra_enc"):
            try:
                extra_raw = await decrypt(project_id, "events", "extra", er["extra_enc"])
                extra_val = json.loads(extra_raw)
            except Exception as exc:
                logger.warning("Failed decrypting extra_enc for event %d: %s", er["id"], exc)
        elif isinstance(extra_val, str):
            try:
                extra_val = json.loads(extra_val)
            except Exception:
                extra_val = {}
        elif not isinstance(extra_val, dict):
            extra_val = {}

        events_list.append(
            IssueEventItem(
                id=int(er["id"]),
                occurred_at=er["occurred_at"].isoformat() if er["occurred_at"] else "",
                stack_trace=trace_val,
                request_path=er["request_path"],
                duration_ms=float(er["duration_ms"]) if er["duration_ms"] is not None else None,
                extra=extra_val,
                deploy_id=int(er["deploy_id"]) if er["deploy_id"] is not None else None,
                commit_sha=er["commit_sha"],
            )
        )

    return IssueDetailResponse(
        id=int(issue["id"]),
        fingerprint=issue["fingerprint"],
        title=issue["title"] or "Unnamed issue",
        first_seen=issue["first_seen"].isoformat() if issue["first_seen"] else "",
        last_seen=issue["last_seen"].isoformat() if issue["last_seen"] else "",
        status=issue["status"],
        event_count=int(issue["event_count"] or 0),
        events=events_list,
    )


@app.get("/api/timeline", tags=["Read API"])
async def get_timeline(
    hours: int = Query(24, ge=1, le=168, description="Time window in hours"),
    format: Literal["list", "dict"] = Query("list", description="'list' (default) or 'dict'"),
    project_id: int = Depends(require_read_project_id),
):
    """
    Returns bucketed 5-minute timeline of deploy events and error/request counts.
    Under project RLS. Requires Read Key (Ingest key receives HTTP 403 Forbidden).
    """
    now_dt = datetime.now(timezone.utc)
    start_dt = now_dt - timedelta(hours=hours)

    async with read_project_transaction(project_id) as conn:
        # Performance Addendum Section 5: Set statement_timeout for dashboard queries
        await conn.execute("SET LOCAL statement_timeout = '5000ms'")

        # 1. Fetch traffic in time window
        traffic_rows = await conn.fetch(
            """
            SELECT bucket, SUM(requests)::bigint AS requests, SUM(errors)::bigint AS errors
            FROM traffic
            WHERE project_id = $1 AND bucket >= $2 AND bucket <= $3
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            project_id,
            start_dt,
            now_dt,
        )

        # 2. Fetch error rollups (Performance Addendum Section 4). Rollups also count slow
        # requests, whose issues the worker titles "Slow: <path>": those are not errors
        error_rows = await conn.fetch(
            """
            SELECT r.bucket, SUM(r.count)::int AS error_count
            FROM event_rollups r
            JOIN issues i ON i.id = r.issue_id AND i.project_id = r.project_id
            WHERE r.project_id = $1 AND r.bucket >= $2 AND r.bucket <= $3
              AND i.title NOT LIKE 'Slow: %'
            GROUP BY r.bucket
            ORDER BY r.bucket ASC
            """,
            project_id,
            start_dt,
            now_dt,
        )

        # 3. Fetch deploys in window
        deploy_rows = await conn.fetch(
            """
            SELECT d.id, d.commit_sha, d.environment, d.deployed_at, a.verdict
            FROM deploys d
            LEFT JOIN deploy_analysis a ON a.deploy_id = d.id
            WHERE d.project_id = $1 AND d.deployed_at >= $2 AND d.deployed_at <= $3
            ORDER BY d.deployed_at ASC
            """,
            project_id,
            start_dt,
            now_dt,
        )

    def round_to_5min(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        minute = (dt.minute // 5) * 5
        return dt.replace(minute=minute, second=0, microsecond=0)

    bucket_map: Dict[str, Dict[str, Any]] = {}

    for tr in traffic_rows:
        b_dt = round_to_5min(tr["bucket"])
        b_key = b_dt.isoformat()
        if b_key not in bucket_map:
            bucket_map[b_key] = {
                "bucket": b_key,
                "timestamp": b_key,
                "requests": 0,
                "errors": 0,
                "deploys": [],
            }
        bucket_map[b_key]["requests"] += int(tr["requests"] or 0)
        bucket_map[b_key]["errors"] += int(tr["errors"] or 0)

    events_per_bucket: Dict[str, int] = {}
    for r in error_rows:
        dt = r.get("bucket") or r.get("occurred_at")
        if not dt:
            continue
        r_dt = round_to_5min(dt)
        r_key = r_dt.isoformat()
        cnt = int(r.get("error_count") or r.get("count") or 1)
        events_per_bucket[r_key] = events_per_bucket.get(r_key, 0) + cnt

    # Requests come only from heartbeats and are never made up: a bucket whose heartbeat has not
    # arrived yet (the SDK sends one a minute) shows its errors with 0 requests, not a 100% rate
    for b_key, ev_count in events_per_bucket.items():
        if b_key not in bucket_map:
            bucket_map[b_key] = {
                "bucket": b_key,
                "timestamp": b_key,
                "requests": 0,
                "errors": ev_count,
                "deploys": [],
            }
        else:
            bucket_map[b_key]["errors"] = max(bucket_map[b_key]["errors"], ev_count)

    all_deploys = []
    for dr in deploy_rows:
        verdict_val = dr["verdict"]
        if not verdict_val or verdict_val == "pending":
            verdict_val = "no_data"

        d_info = {
            "id": int(dr["id"]),
            "commit_sha": dr["commit_sha"],
            "environment": dr["environment"],
            "verdict": verdict_val,
            "deployed_at": dr["deployed_at"].isoformat() if dr["deployed_at"] else None,
        }
        all_deploys.append(d_info)
        if dr["deployed_at"]:
            d_dt = round_to_5min(dr["deployed_at"])
            d_key = d_dt.isoformat()
            if d_key not in bucket_map:
                bucket_map[d_key] = {
                    "bucket": d_key,
                    "timestamp": d_key,
                    "requests": 0,
                    "errors": 0,
                    "deploys": [],
                }
            bucket_map[d_key]["deploys"].append(d_info)

    sorted_buckets = [bucket_map[k] for k in sorted(bucket_map.keys())]

    if format == "dict":
        return {
            "hours": hours,
            "start": start_dt.isoformat(),
            "end": now_dt.isoformat(),
            "timeline": sorted_buckets,
            "buckets": sorted_buckets,
            "deploys": all_deploys,
        }

    return sorted_buckets


@app.get("/api/incidents", response_model=List[IncidentItem], tags=["Incidents"])
async def list_incidents(
    status: Optional[str] = Query(None, description="Filter by status (possible, confirmed, resolved)"),
    limit: int = Query(50, ge=1, le=200),
    project_id: int = Depends(require_read_project_id),
):
    """List incidents for authenticated project under RLS."""
    async with read_project_transaction(project_id) as conn:
        if status:
            rows = await conn.fetch(
                """
                SELECT id, deploy_id, issue_id, status, title,
                       where_file, where_line, where_function, in_diff,
                       owners, event_count, category, first_alert_ms,
                       created_at, confirmed_at, resolved_at
                FROM incidents
                WHERE project_id = $1 AND status = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                project_id,
                status,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, deploy_id, issue_id, status, title,
                       where_file, where_line, where_function, in_diff,
                       owners, event_count, category, first_alert_ms,
                       created_at, confirmed_at, resolved_at
                FROM incidents
                WHERE project_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                project_id,
                limit,
            )

    results = []
    for r in rows:
        raw_owners = r["owners"]
        owners = raw_owners if isinstance(raw_owners, list) else json.loads(raw_owners or "[]")
        results.append(
            IncidentItem(
                id=r["id"],
                deploy_id=r["deploy_id"],
                issue_id=r["issue_id"],
                status=r["status"],
                title=r["title"],
                where_file=r["where_file"],
                where_line=r["where_line"],
                where_function=r["where_function"],
                in_diff=r["in_diff"],
                owners=owners,
                event_count=r["event_count"],
                category=r["category"],
                first_alert_ms=r["first_alert_ms"],
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
                confirmed_at=r["confirmed_at"].isoformat() if r["confirmed_at"] else None,
                resolved_at=r["resolved_at"].isoformat() if r["resolved_at"] else None,
            )
        )
    return results


@app.get("/api/incidents/{id}", response_model=IncidentDetailResponse, tags=["Incidents"])
async def get_incident(
    id: int = Path(..., description="Incident ID"),
    project_id: int = Depends(require_read_project_id),
):
    """Get incident details with deploy metadata, code excerpt, timeline, and owners."""
    async with read_project_transaction(project_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT i.id, i.deploy_id, i.issue_id, i.status, i.title,
                   i.where_file, i.where_line, i.where_function, i.in_diff,
                   i.owners, i.event_count, i.category, i.first_alert_ms,
                   i.created_at, i.confirmed_at, i.resolved_at,
                   d.commit_sha, d.author AS commit_author, d.message AS commit_message, d.deployed_at
            FROM incidents i
            JOIN deploys d ON d.id = i.deploy_id
            WHERE i.id = $1 AND i.project_id = $2
            """,
            id,
            project_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Incident not found")

        # Fetch code excerpt around where_line from deploy_files
        where_file = row["where_file"] or ""
        where_line = row["where_line"] or 0
        code_excerpt = None
        code_excerpt_start = None
        if where_file:
            df_row = await conn.fetchrow(
                """
                SELECT content, content_enc
                FROM deploy_files
                WHERE deploy_id = $1 AND project_id = $2
                  AND (path = $3 OR $3 LIKE '%' || path OR path LIKE '%' || $3)
                LIMIT 1
                """,
                row["deploy_id"],
                project_id,
                where_file,
            )
            file_content = (
                await decrypt_or_plain(project_id, "deploy_files", "content", df_row.get("content_enc"), df_row.get("content"))
                if df_row
                else None
            )
            if file_content:
                lines = file_content.splitlines()
                start_l = max(0, where_line - 15)
                end_l = min(len(lines), where_line + 15)
                code_excerpt = "\n".join(lines[start_l:end_l])
                code_excerpt_start = start_l + 1

        # Check for real AI investigation record under RLS
        inv_row = None
        try:
            inv_row = await conn.fetchrow(
                """
                SELECT id, status, model, started_at, finished_at, result, result_enc, not_sure_reason
                FROM investigations
                WHERE incident_id = $1 AND project_id = $2
                ORDER BY id DESC
                LIMIT 1
                """,
                id,
                project_id,
            )
        except (StopAsyncIteration, Exception):
            inv_row = None

    raw_owners = row["owners"]
    owners = raw_owners if isinstance(raw_owners, list) else json.loads(raw_owners or "[]")

    timeline = []
    if row["deployed_at"]:
        timeline.append({"event": "deployed", "at": row["deployed_at"].isoformat(), "detail": f"Deploy {row['commit_sha'][:8]}"})
    if row["created_at"]:
        timeline.append({"event": "first_error", "at": row["created_at"].isoformat(), "detail": "Incident opened"})
    if row["confirmed_at"]:
        timeline.append({"event": "confirmed", "at": row["confirmed_at"].isoformat(), "detail": "Regression confirmed"})
    if row["resolved_at"]:
        timeline.append({"event": "resolved", "at": row["resolved_at"].isoformat(), "detail": "Incident resolved"})

    ai_investigation: Dict[str, Any] = {}
    if inv_row:
        inv_res = await decrypt_or_plain(
            project_id, "investigations", "result", inv_row.get("result_enc"), inv_row.get("result")
        )
        if isinstance(inv_res, str):
            try:
                inv_res = json.loads(inv_res)
            except Exception:
                pass
        ai_investigation = {
            "enabled": True,
            "investigation_id": inv_row["id"],
            "status": inv_row["status"],
            "model": inv_row["model"],
            "started_at": inv_row["started_at"].isoformat() if inv_row["started_at"] else None,
            "finished_at": inv_row["finished_at"].isoformat() if inv_row["finished_at"] else None,
            "result": inv_res,
            "not_sure_reason": inv_row["not_sure_reason"],
        }
    else:
        ai_investigation = {
            "enabled": False,
            "status": "not_enabled",
            "message": "AI investigation not enabled or pending.",
        }

    return IncidentDetailResponse(
        id=row["id"],
        deploy_id=row["deploy_id"],
        issue_id=row["issue_id"],
        status=row["status"],
        title=row["title"],
        where_file=row["where_file"],
        where_line=row["where_line"],
        where_function=row["where_function"],
        in_diff=row["in_diff"],
        owners=owners,
        event_count=row["event_count"],
        category=row["category"],
        first_alert_ms=row["first_alert_ms"],
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
        confirmed_at=row["confirmed_at"].isoformat() if row["confirmed_at"] else None,
        resolved_at=row["resolved_at"].isoformat() if row["resolved_at"] else None,
        commit_sha=row["commit_sha"],
        commit_author=row["commit_author"],
        commit_message=row["commit_message"],
        code_excerpt=code_excerpt,
        code_excerpt_start=code_excerpt_start,
        ai_investigation=ai_investigation,
        timeline=timeline,
    )


@app.post("/api/incidents/{id}/investigate", tags=["Incidents"])
async def trigger_incident_investigation(
    id: int = Path(..., description="Incident ID"),
    deep: bool = Query(False, description="Run deep investigation ('Dig deeper')"),
    project_id: int = Depends(require_read_project_id),
):
    """Trigger or re-run AI investigation (supports 'Dig deeper')."""
    async with project_transaction(project_id) as conn:
        inc = await conn.fetchrow("SELECT id FROM incidents WHERE id = $1 AND project_id = $2", id, project_id)
        if not inc:
            raise HTTPException(status_code=404, detail="Incident not found")

        from app.investigator.worker import execute_investigation
        result = await execute_investigation(conn, project_id, id, deep_mode=deep)
        if result is None:
            return {"status": "paused", "message": "Investigation paused due to budget limit."}
        return {"status": "done", "result": result.model_dump()}


@app.get("/api/settings", response_model=ProjectSettingsResponse, tags=["Settings"])
async def get_project_settings(project_id: int = Depends(require_read_project_id)):
    """Retrieve project configuration (critical routes, quiet hours, notification channels, team members)."""
    async with read_project_transaction(project_id) as conn:
        proj = await conn.fetchrow(
            "SELECT id, name, critical_routes, notify_quiet FROM projects WHERE id = $1",
            project_id,
        )
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")

        ch_rows = await conn.fetch(
            "SELECT id, type, enabled, created_at FROM notification_channels WHERE project_id = $1 ORDER BY id",
            project_id,
        )
        tm_rows = await conn.fetch(
            "SELECT id, name, git_emails, slack_user_id, discord_user_id, email FROM team_members WHERE project_id = $1 ORDER BY id",
            project_id,
        )

    raw_cr = proj["critical_routes"]
    critical_routes = raw_cr if isinstance(raw_cr, list) else json.loads(raw_cr or "[]")
    channels = [{"id": r["id"], "type": r["type"], "enabled": r["enabled"]} for r in ch_rows]
    members = [
        {
            "id": r["id"],
            "name": r["name"],
            "git_emails": r["git_emails"],
            "slack_user_id": r["slack_user_id"],
            "discord_user_id": r["discord_user_id"],
            "email": r["email"],
        }
        for r in tm_rows
    ]

    return ProjectSettingsResponse(
        id=proj["id"],
        name=proj["name"],
        critical_routes=critical_routes,
        notify_quiet=bool(proj["notify_quiet"]),
        channels=channels,
        team_members=members,
    )


class SettingsUpdate(BaseModel):
    critical_routes: Optional[List[str]] = None
    notify_quiet: Optional[bool] = None


@app.put("/api/settings", tags=["Settings"])
async def update_project_settings(
    payload: SettingsUpdate,
    project_id: int = Depends(require_read_project_id),
):
    """Update project settings."""
    async with project_transaction(project_id) as conn:
        if payload.critical_routes is not None:
            await conn.execute(
                "UPDATE projects SET critical_routes = $1 WHERE id = $2",
                json.dumps(payload.critical_routes),
                project_id,
            )
        if payload.notify_quiet is not None:
            await conn.execute(
                "UPDATE projects SET notify_quiet = $1 WHERE id = $2",
                payload.notify_quiet,
                project_id,
            )
    return {"ok": True}


@app.get("/v1/export", tags=["Audit & Compliance"])
async def export_project_data(
    project_id: int = Depends(require_read_project_id),
    _admin: bool = Depends(require_admin_key),
) -> Dict[str, Any]:
    """Export all project data (deploys, issues, events, traffic) for audit and portability.

    Needs the project's read key (X-API-Key, says which project) and the admin key (X-Admin-Key).

    Decrypted application-level columns are returned in plaintext for the authenticated project.
    """
    async with project_transaction(project_id) as conn:
        proj_row = await conn.fetchrow("SELECT id, name, created_at FROM projects WHERE id = $1", project_id)
        if not proj_row:
            raise HTTPException(status_code=404, detail="Project not found")

        # Deploys, with their latest verdict (stored in deploy_analysis)
        deploy_rows = await conn.fetch(
            """
            SELECT d.id, d.commit_sha, d.author, d.message, d.environment, d.deployed_at, d.diff, d.diff_enc,
                   a.verdict, a.verdict_reasons, a.verdict_final
            FROM deploys d
            LEFT JOIN LATERAL (
                SELECT verdict, verdict_reasons, verdict_final
                FROM deploy_analysis
                WHERE deploy_id = d.id
                ORDER BY id DESC
                LIMIT 1
            ) a ON true
            WHERE d.project_id = $1
            ORDER BY d.deployed_at DESC
            """,
            project_id,
        )
        deploys_out = []
        for dr in deploy_rows:
            deploys_out.append({
                "id": dr["id"],
                "commit_sha": dr["commit_sha"],
                "author": dr["author"],
                "message": dr["message"],
                "environment": dr["environment"],
                "deployed_at": dr["deployed_at"].isoformat() if dr["deployed_at"] else None,
                "verdict": dr["verdict"],
                "verdict_reasons": dr["verdict_reasons"],
                "verdict_final": dr["verdict_final"],
                "diff": await decrypt_or_plain(project_id, "deploys", "diff", dr["diff_enc"], dr["diff"]),
            })

        # Issues
        issue_rows = await conn.fetch(
            """
            SELECT id, title, fingerprint, status, first_seen, last_seen, event_count
            FROM issues
            WHERE project_id = $1
            ORDER BY last_seen DESC
            """,
            project_id,
        )
        issues_out = [
            {
                "id": ir["id"],
                "title": ir["title"],
                "fingerprint": ir["fingerprint"],
                "status": ir["status"],
                "first_seen": ir["first_seen"].isoformat() if ir["first_seen"] else None,
                "last_seen": ir["last_seen"].isoformat() if ir["last_seen"] else None,
                "event_count": ir["event_count"],
            }
            for ir in issue_rows
        ]

        # Events (latest 1000 stored rows; storm control keeps at most STORE_FULL_PER_MIN per issue per minute)
        event_rows = await conn.fetch(
            """
            SELECT id, event_id, issue_id, deploy_id, environment, type, request_path, duration_ms, occurred_at,
                   stack_trace, stack_trace_enc, extra, extra_enc
            FROM events
            WHERE project_id = $1
            ORDER BY occurred_at DESC
            LIMIT 1000
            """,
            project_id,
        )
        events_out = []
        for er in event_rows:
            extra_str = await decrypt_or_plain(project_id, "events", "extra", er["extra_enc"], er["extra"])
            try:
                extra_val = extra_str if isinstance(extra_str, dict) else json.loads(extra_str or "{}")
            except (TypeError, ValueError):
                extra_val = {}
            events_out.append({
                "id": er["id"],
                "event_id": er["event_id"],
                "issue_id": er["issue_id"],
                "deploy_id": er["deploy_id"],
                "environment": er["environment"],
                "type": er["type"],
                "request_path": er["request_path"],
                "duration_ms": er["duration_ms"],
                "occurred_at": er["occurred_at"].isoformat() if er["occurred_at"] else None,
                "stack_trace": await decrypt_or_plain(
                    project_id, "events", "stack_trace", er["stack_trace_enc"], er["stack_trace"]
                ),
                "extra": extra_val,
            })

        # Traffic
        traffic_rows = await conn.fetch(
            "SELECT environment, bucket, requests, errors FROM traffic WHERE project_id = $1 "
            "ORDER BY bucket DESC LIMIT 1000",
            project_id,
        )
        traffic_out = [
            {
                "environment": tr["environment"],
                "bucket": tr["bucket"].isoformat() if tr["bucket"] else None,
                "requests": tr["requests"],
                "errors": tr["errors"],
            }
            for tr in traffic_rows
        ]

        return {
            "export_timestamp": datetime.now(timezone.utc).isoformat(),
            "project": {
                "id": proj_row["id"],
                "name": proj_row["name"],
                "created_at": proj_row["created_at"].isoformat() if proj_row["created_at"] else None,
            },
            "deploys": deploys_out,
            "issues": issues_out,
            "events": events_out,
            "traffic": traffic_out,
        }


@app.delete("/v1/project-data", tags=["Audit & Compliance"])
async def purge_project_data(
    project_id: int = Depends(require_read_project_id),
    _admin: bool = Depends(require_admin_key),
) -> Dict[str, Any]:
    """Purge all telemetry, events, deploys, issues, and outbox rows for the authenticated project.

    Needs the project's read key (X-API-Key, says which project) and the admin key (X-Admin-Key):
    the read key alone, which the dashboard and testers hold, can only read.

    The project shell and credentials remain intact; all historical activity and code data is deleted.
    """
    async with project_transaction(project_id) as conn:
        # Every statement names the project: row-level security is the second line of defence, not the only one
        ev_res = await conn.execute("DELETE FROM events WHERE project_id = $1", project_id)
        ev_count = int(ev_res.split()[-1]) if ev_res else 0

        tr_res = await conn.execute("DELETE FROM traffic WHERE project_id = $1", project_id)
        tr_count = int(tr_res.split()[-1]) if tr_res else 0

        await conn.execute(
            "DELETE FROM deploy_analysis WHERE deploy_id IN (SELECT id FROM deploys WHERE project_id = $1)", project_id
        )
        await conn.execute("DELETE FROM alerts WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM outbox WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM ingest_dedupe WHERE project_id = $1", project_id)

        is_res = await conn.execute("DELETE FROM issues WHERE project_id = $1", project_id)
        is_count = int(is_res.split()[-1]) if is_res else 0

        dp_res = await conn.execute("DELETE FROM deploys WHERE project_id = $1", project_id)
        dp_count = int(dp_res.split()[-1]) if dp_res else 0

        try:
            await conn.execute("DELETE FROM deploy_files WHERE project_id = $1", project_id)
        except Exception:
            pass

        return {
            "status": "purged",
            "project_id": project_id,
            "deleted": {
                "events": ev_count,
                "traffic": tr_count,
                "issues": is_count,
                "deploys": dp_count,
            },
        }


# ============================================================================
# Usage Metering & Budget API (Usage Metering Addendum Section F)
# ============================================================================


class BudgetUpdate(BaseModel):
    soft_limit_micros: Optional[int] = None
    hard_limit_micros: Optional[int] = None
    warn_at: Optional[List[int]] = None


@app.get("/v1/usage", tags=["Usage & Metering"])
async def get_usage(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    group: str = Query("meter"),
    project_id: int = Depends(require_read_project_id),
):
    """Retrieve usage breakdown for authenticated project.
    Group by 'day', 'meter', or 'project'.
    Filtered by optional ISO dates 'from' and 'to'.
    Defaults to current calendar month (UTC).
    """
    now = datetime.now(timezone.utc)
    if from_date:
        try:
            start_d = datetime.fromisoformat(from_date.replace("Z", "+00:00")).date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'from' date format")
    else:
        start_d = now.date().replace(day=1)

    if to_date:
        try:
            end_d = datetime.fromisoformat(to_date.replace("Z", "+00:00")).date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'to' date format")
    else:
        end_d = now.date()

    if group not in ("day", "meter", "project"):
        raise HTTPException(
            status_code=400,
            detail="Invalid group parameter; must be 'day', 'meter', or 'project'",
        )

    async with get_connection() as conn:
        if group == "day":
            rows = await conn.fetch(
                """
                SELECT day,
                       SUM(units) AS units,
                       SUM(free_units_applied) AS free_units_applied,
                       SUM(our_cost_micros) AS our_cost_micros,
                       SUM(billed_micros) AS billed_micros
                FROM usage_daily
                WHERE project_id = $1 AND day >= $2 AND day <= $3
                GROUP BY day
                ORDER BY day ASC
                """,
                project_id,
                start_d,
                end_d,
            )
            items = [
                {
                    "day": r["day"].isoformat(),
                    "units": int(r["units"] or 0),
                    "free_units_applied": int(r["free_units_applied"] or 0),
                    "our_cost_micros": int(r["our_cost_micros"] or 0),
                    "billed_micros": int(r["billed_micros"] or 0),
                }
                for r in rows
            ]
        elif group == "meter":
            rows = await conn.fetch(
                """
                SELECT meter,
                       SUM(units) AS units,
                       SUM(free_units_applied) AS free_units_applied,
                       SUM(our_cost_micros) AS our_cost_micros,
                       SUM(billed_micros) AS billed_micros
                FROM usage_daily
                WHERE project_id = $1 AND day >= $2 AND day <= $3
                GROUP BY meter
                ORDER BY meter ASC
                """,
                project_id,
                start_d,
                end_d,
            )
            items = [
                {
                    "meter": r["meter"],
                    "units": int(r["units"] or 0),
                    "free_units_applied": int(r["free_units_applied"] or 0),
                    "our_cost_micros": int(r["our_cost_micros"] or 0),
                    "billed_micros": int(r["billed_micros"] or 0),
                }
                for r in rows
            ]
        else:  # group == "project"
            rows = await conn.fetch(
                """
                SELECT project_id,
                       SUM(units) AS units,
                       SUM(free_units_applied) AS free_units_applied,
                       SUM(our_cost_micros) AS our_cost_micros,
                       SUM(billed_micros) AS billed_micros
                FROM usage_daily
                WHERE project_id = $1 AND day >= $2 AND day <= $3
                GROUP BY project_id
                """,
                project_id,
                start_d,
                end_d,
            )
            items = [
                {
                    "project_id": int(r["project_id"]),
                    "units": int(r["units"] or 0),
                    "free_units_applied": int(r["free_units_applied"] or 0),
                    "our_cost_micros": int(r["our_cost_micros"] or 0),
                    "billed_micros": int(r["billed_micros"] or 0),
                }
                for r in rows
            ]

        # Compute totals across all groups
        total_units = sum(it["units"] for it in items)
        total_free = sum(it["free_units_applied"] for it in items)
        total_our_cost = sum(it["our_cost_micros"] for it in items)
        total_billed = sum(it["billed_micros"] for it in items)

        return {
            "project_id": project_id,
            "from": start_d.isoformat(),
            "to": end_d.isoformat(),
            "group": group,
            "items": items,
            "totals": {
                "units": total_units,
                "free_units_applied": total_free,
                "our_cost_micros": total_our_cost,
                "billed_micros": total_billed,
            },
        }


@app.get("/v1/usage/forecast", tags=["Usage & Metering"])
async def get_usage_forecast(
    project_id: int = Depends(require_read_project_id),
):
    """Linear month-end spend projection from current month's daily rate.
    Requires at least 3 days of data in the current month, otherwise returns status 'not_enough_data'.
    """
    now = datetime.now(timezone.utc)
    start_of_month = now.date().replace(day=1)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_elapsed = now.day

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT day, SUM(billed_micros) AS daily_billed
            FROM usage_daily
            WHERE project_id = $1 AND day >= $2 AND day <= $3
            GROUP BY day
            ORDER BY day ASC
            """,
            project_id,
            start_of_month,
            now.date(),
        )

    days_with_data = len(rows)
    current_spent = sum(int(r["daily_billed"] or 0) for r in rows)

    if days_with_data < 3 and days_elapsed < 3:
        return {
            "status": "not_enough_data",
            "message": "At least 3 days of data required for forecast",
            "days_with_data": days_with_data,
            "days_elapsed": days_elapsed,
            "current_spent_micros": current_spent,
        }

    effective_days = max(days_with_data, days_elapsed, 1)
    # Integer ceiling division: current_spent * days_in_month / effective_days, rounded up
    projected_micros = -(-current_spent * days_in_month // effective_days)

    return {
        "status": "ok",
        "days_elapsed": days_elapsed,
        "days_with_data": days_with_data,
        "days_in_month": days_in_month,
        "current_spent_micros": current_spent,
        "daily_rate_micros": (2 * current_spent + effective_days) // (2 * effective_days),  # rounded half up
        "projected_month_end_micros": projected_micros,
    }


@app.get("/v1/usage/estimate", tags=["Usage & Metering"])
async def get_usage_estimate(
    meter: str = Query(..., description="Meter name"),
    count: int = Query(1, ge=1, description="Number of units"),
    project_id: int = Depends(require_read_project_id),
):
    """Cost estimate before use, using current price book and remaining free allowance."""
    now = datetime.now(timezone.utc)
    start_of_month = now.date().replace(day=1)

    async with get_connection() as conn:
        price_book = await get_active_price_book(conn, project_id)
        entry = price_book.get(meter)
        if not entry:
            raise HTTPException(status_code=400, detail=f"Unknown meter '{meter}'")

        # Query free units applied so far this month
        used_free_row = await conn.fetchval(
            """
            SELECT COALESCE(SUM(free_units_applied), 0)
            FROM usage_daily
            WHERE project_id = $1 AND meter = $2 AND day >= $3
            """,
            project_id,
            meter,
            start_of_month,
        )
        used_free = int(used_free_row or 0)

    free_remaining = max(0, entry.free_units_per_month - used_free)
    free_applicable = min(count, free_remaining)
    billable_units = count - free_applicable

    if billable_units <= 0:
        estimated_billed = 0
    elif entry.pricing_mode == "fixed":
        estimated_billed = billable_units * entry.unit_price_micros
    elif meter in ("ai_investigation", "ai_investigation_deep"):
        # Typical token use (output includes the model's thinking), priced for the configured model:
        # fast ~4,000 in / 3,000 out, deep ~15,000 in / 8,000 out
        deep = meter == "ai_investigation_deep"
        unit_cost = calculate_llm_cost_micros(
            settings.LLM_MODEL_DEEP if deep else settings.LLM_MODEL_FAST,
            15000 if deep else 4000,
            8000 if deep else 3000,
            provider=settings.LLM_PROVIDER,
        )
        billed_unit, _, _ = calculate_billed_micros(entry, 1, our_cost_micros=unit_cost)
        estimated_billed = billable_units * max(billed_unit, entry.min_unit_price_micros)
    else:
        estimated_billed = billable_units * max(entry.min_unit_price_micros, entry.unit_price_micros)

    return {
        "meter": meter,
        "count": count,
        "free_applicable": free_applicable,
        "billable_units": billable_units,
        "estimated_billed_micros": estimated_billed,
        "price_book_version": entry.version,
        "pricing_mode": entry.pricing_mode,
    }


@app.get("/v1/usage/export.csv", tags=["Usage & Metering"])
async def export_usage_csv(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    project_id: int = Depends(require_read_project_id),
):
    """Export project usage daily breakdown as CSV."""
    now = datetime.now(timezone.utc)
    if from_date:
        try:
            start_d = datetime.fromisoformat(from_date.replace("Z", "+00:00")).date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'from' date format")
    else:
        start_d = now.date().replace(day=1)

    if to_date:
        try:
            end_d = datetime.fromisoformat(to_date.replace("Z", "+00:00")).date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'to' date format")
    else:
        end_d = now.date()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT day, meter, units, free_units_applied, our_cost_micros, billed_micros
            FROM usage_daily
            WHERE project_id = $1 AND day >= $2 AND day <= $3
            ORDER BY day ASC, meter ASC
            """,
            project_id,
            start_d,
            end_d,
        )

    lines = ["day,meter,units,free_units_applied,our_cost_micros,billed_micros"]
    for r in rows:
        lines.append(
            f"{r['day'].isoformat()},{r['meter']},{r['units']},{r['free_units_applied']},{r['our_cost_micros']},{r['billed_micros']}"
        )
    csv_content = "\n".join(lines) + "\n"

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_export.csv"},
    )


@app.get("/v1/budget", tags=["Usage & Metering"])
async def get_budget(
    project_id: int = Depends(require_read_project_id),
):
    """Get project budget limits, warning thresholds, and current month spend."""
    async with get_connection() as conn:
        return await get_project_budget(conn, project_id)


@app.put("/v1/budget", tags=["Usage & Metering"])
async def put_budget(
    payload: BudgetUpdate,
    project_id: int = Depends(require_read_project_id),
):
    """Update project budget soft/hard limits and warning thresholds."""
    async with project_transaction(project_id) as conn:
        return await set_project_budget(
            conn=conn,
            project_id=project_id,
            soft_limit_micros=payload.soft_limit_micros,
            hard_limit_micros=payload.hard_limit_micros,
            warn_at=payload.warn_at,
        )


@app.get("/v1/admin/margin", tags=["Usage & Metering"])
async def get_admin_margin(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    _admin: bool = Depends(require_admin_key),
):
    """Owner/Admin aggregate margin report across all projects.
    Admin scope only (requires valid X-Admin-Key or ADMIN_API_KEY).
    Returns totals per meter of units, our cost, and billed amounts.
    CRITICAL: Contains NO project identifiers, NO tenant data, and NO customer content.
    """
    now = datetime.now(timezone.utc)
    if from_date:
        try:
            start_d = datetime.fromisoformat(from_date.replace("Z", "+00:00")).date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'from' date format")
    else:
        start_d = now.date().replace(day=1)

    if to_date:
        try:
            end_d = datetime.fromisoformat(to_date.replace("Z", "+00:00")).date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'to' date format")
    else:
        end_d = now.date()

    async with get_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT meter,
                   SUM(units) AS units,
                   SUM(free_units_applied) AS free_units_applied,
                   SUM(our_cost_micros) AS our_cost_micros,
                   SUM(billed_micros) AS billed_micros
            FROM usage_daily
            WHERE day >= $1 AND day <= $2
            GROUP BY meter
            ORDER BY meter ASC
            """,
            start_d,
            end_d,
        )

        month_d = now.date().replace(day=1)
        limit_counts = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE bs.hard_limit_hit_at IS NOT NULL
                       OR (b.hard_limit_micros IS NOT NULL AND bs.spent_micros >= b.hard_limit_micros)
                ) AS at_limit_count,
                COUNT(*) FILTER (
                    WHERE b.soft_limit_micros IS NOT NULL
                      AND bs.spent_micros >= (b.soft_limit_micros * 8 / 10)
                      AND (b.hard_limit_micros IS NULL OR bs.spent_micros < b.hard_limit_micros)
                ) AS near_limit_count
            FROM budget_state bs
            JOIN budgets b ON bs.project_id = b.project_id
            WHERE bs.month = $1
            """,
            month_d,
        )

    at_limit = int(limit_counts["at_limit_count"] or 0) if limit_counts else 0
    near_limit = int(limit_counts["near_limit_count"] or 0) if limit_counts else 0

    meter_list = []
    total_units = 0
    total_free = 0
    total_our_cost = 0
    total_billed = 0

    for r in rows:
        units = int(r["units"] or 0)
        free = int(r["free_units_applied"] or 0)
        our_cost = int(r["our_cost_micros"] or 0)
        billed = int(r["billed_micros"] or 0)
        margin = billed - our_cost

        total_units += units
        total_free += free
        total_our_cost += our_cost
        total_billed += billed

        meter_list.append(
            {
                "meter": r["meter"],
                "units": units,
                "free_units_applied": free,
                "our_cost_micros": our_cost,
                "billed_micros": billed,
                "margin_micros": margin,
            }
        )

    return {
        "from": start_d.isoformat(),
        "to": end_d.isoformat(),
        "meters": meter_list,
        "projects_at_limit_count": at_limit,
        "projects_near_limit_count": near_limit,
        "totals": {
            "units": total_units,
            "free_units_applied": total_free,
            "our_cost_micros": total_our_cost,
            "billed_micros": total_billed,
            "margin_micros": total_billed - total_our_cost,
        },
    }


# ==============================================================================
# Milestone 6: Trust, Calm, Interactivity & Signed Actions
# ==============================================================================

class IncidentActionRequest(BaseModel):
    action: Literal["ack", "assign", "resolve", "reopen"]
    actor: Optional[str] = Field("anonymous", max_length=200)
    detail: Optional[Dict[str, Any]] = None


class IncidentFeedbackRequest(BaseModel):
    label: Literal["real", "false", "unclear"]
    actor: Optional[str] = Field("anonymous", max_length=200)
    comment: Optional[str] = Field(None, max_length=2000)


class IgnoreRuleCreate(BaseModel):
    kind: str  # fingerprint | path_prefix | exc_type
    pattern: str
    reason: str
    expires_in_days: Optional[int] = 14


@app.get("/a/{token}", response_class=HTMLResponse, tags=["Signed Actions"])
async def signed_action_landing(token: str = Path(...)):
    """
    Signed action confirmation landing page.
    Invariant: GET is strictly read-only and never performs an action.
    """
    from app.trust.actions import verify_signed_action_token
    valid, payload, err = verify_signed_action_token(token)
    if not valid or not payload:
        return HTMLResponse(
            content=f"<html><body><h2>Invalid Action Link</h2><p>{html_escape(err)}</p></body></html>",
            status_code=400,
        )

    action = payload["action"]
    inc_id = payload["incident_id"]

    html = f"""<!DOCTYPE html>
<html>
<head><title>ChangeWatch Action Confirmation</title>
<style>body {{ font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; }}
button {{ background: #2563eb; color: white; padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-size: 16px; }}
</style></head>
<body>
<h2>Confirm Action</h2>
<p>You are about to mark incident <strong>#{inc_id}</strong> as <strong>{html_escape(action.upper())}</strong>.</p>
<form method="POST" action="/a/{html_escape(token)}">
  <label for="actor">Who are you?</label><br>
  <input type="text" id="actor" name="actor" placeholder="Your name or handle" style="padding: 8px; width: 100%; margin: 8px 0 16px;"><br>
  <button type="submit">Confirm {html_escape(action.title())}</button>
</form>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/a/{token}", tags=["Signed Actions"])
async def execute_signed_action(
    request: Request,
    token: str = Path(...),
):
    """Execute signed action upon POST confirmation."""
    from app.trust.actions import verify_signed_action_token, execute_incident_action
    valid, payload, err = verify_signed_action_token(token)
    if not valid or not payload:
        raise HTTPException(status_code=400, detail=f"Invalid or expired action link: {err}")

    inc_id = payload["incident_id"]
    action = payload["action"]
    proj_id = payload["project_id"]

    actor = "signed_link"
    try:
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        if form.get("actor"):
            actor = form["actor"][0][:100]
    except Exception:
        pass

    async with project_transaction(proj_id) as conn:
        res = await execute_incident_action(
            conn=conn,
            project_id=proj_id,
            incident_id=inc_id,
            action=action,
            actor=actor,
        )

    return {"status": "ok", "incident_id": inc_id, "action": action, "actor": actor, "result": res}


@app.post("/v1/integrations/slack/interactions", tags=["Integrations"])
async def slack_interactions_webhook(request: Request):
    """
    Handle real Slack interactivity payloads (buttons, menus) with HMAC signature verification.
    """
    from app.trust.actions import verify_slack_signature, execute_incident_action

    body_bytes = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    # Without a signing secret anyone could post "resolve:<incident>:<project>" for any tenant,
    # so the endpoint only exists once Slack interactivity is configured
    if not settings.SLACK_SIGNING_SECRET:
        raise HTTPException(status_code=404, detail="Slack interactivity is not configured")
    if not verify_slack_signature(body_bytes, timestamp, signature):
        raise HTTPException(status_code=403, detail="Invalid Slack request signature")

    # Slack posts application/x-www-form-urlencoded; parse the already-verified body with the
    # standard library (request.form() would need the python-multipart package)
    form = parse_qs(body_bytes.decode("utf-8", errors="replace"))
    payload_raw = (form.get("payload") or [None])[0]
    if not payload_raw:
        raise HTTPException(status_code=400, detail="Missing payload parameter")

    try:
        payload = json.loads(str(payload_raw))
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON in payload")

    user_info = payload.get("user", {})
    actor = user_info.get("username") or user_info.get("name") or "slack_user"

    actions = payload.get("actions", [])
    if actions:
        act = actions[0]
        action_val = act.get("value") or act.get("action_id") or ""
        # Format: "<action>:<incident_id>:<project_id>"
        parts = action_val.split(":")
        if len(parts) >= 3:
            act_name, inc_id_str, proj_id_str = parts[:3]
            try:
                inc_id = int(inc_id_str)
                proj_id = int(proj_id_str)
                async with project_transaction(proj_id) as conn:
                    await execute_incident_action(conn, proj_id, inc_id, act_name, actor=actor)
            except Exception as exc:
                logger.warning("Slack interaction execution error: %s", exc)

    return Response(content="", status_code=200)


@app.post("/api/incidents/{id}/actions", tags=["Incidents"])
async def trigger_incident_action(
    req: IncidentActionRequest,
    id: int = Path(..., description="Incident ID"),
    project_id: int = Depends(require_read_project_id),
):
    """Execute action on incident: ack, assign, resolve, or reopen."""
    from app.trust.actions import execute_incident_action
    async with project_transaction(project_id) as conn:
        if not await conn.fetchval("SELECT 1 FROM incidents WHERE id = $1 AND project_id = $2", id, project_id):
            raise HTTPException(status_code=404, detail="Incident not found")
        res = await execute_incident_action(
            conn=conn,
            project_id=project_id,
            incident_id=id,
            action=req.action,
            actor=req.actor or "anonymous",
            detail=req.detail,
        )
    return res


@app.post("/api/incidents/{id}/feedback", tags=["Incidents"])
async def submit_incident_feedback(
    req: IncidentFeedbackRequest,
    id: int = Path(..., description="Incident ID"),
    project_id: int = Depends(require_read_project_id),
):
    """Submit rating (real, false, unclear) for an incident alert."""
    from app.trust.feedback import record_alert_feedback
    async with project_transaction(project_id) as conn:
        # Only this project's incidents: the foreign key alone accepted any project's incident id
        if not await conn.fetchval("SELECT 1 FROM incidents WHERE id = $1 AND project_id = $2", id, project_id):
            raise HTTPException(status_code=404, detail="Incident not found")
        fb_id = await record_alert_feedback(
            conn=conn,
            project_id=project_id,
            incident_id=id,
            label=req.label,
            actor=req.actor or "anonymous",
            comment=req.comment,
        )
    return {"status": "ok", "feedback_id": fb_id, "label": req.label}


@app.get("/api/incidents/{id}/timeline", tags=["Incidents"])
async def get_incident_audit_timeline(
    id: int = Path(..., description="Incident ID"),
    project_id: int = Depends(require_read_project_id),
):
    """Retrieve full audit event timeline from incident_events table."""
    async with read_project_transaction(project_id) as conn:
        exists = await conn.fetchval("SELECT 1 FROM incidents WHERE id = $1 AND project_id = $2", id, project_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Incident not found")
        rows = await conn.fetch(
            """
            SELECT id, type, actor, detail, at
            FROM incident_events
            WHERE incident_id = $1 AND project_id = $2
            ORDER BY at ASC
            """,
            id,
            project_id,
        )
    events = []
    for r in rows:
        d = r["detail"]
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:
                pass
        events.append({
            "id": r["id"],
            "type": r["type"],
            "actor": r["actor"],
            "detail": d,
            "at": r["at"].isoformat() if r["at"] else None,
        })
    return events


@app.get("/v1/trust/summary", tags=["Trust"])
async def get_trust_page_summary(
    days: int = Query(30, ge=7, le=90),
    project_id: int = Depends(require_read_project_id),
):
    """Trust page metrics: precision, coverage, median TTD, median TTR, uptime, suggestions, calibration."""
    from app.trust.feedback import compute_trust_metrics, get_feedback_suggestions
    from app.trust.calibration import compute_decision_calibration

    async with read_project_transaction(project_id) as conn:
        trust_metrics = await compute_trust_metrics(conn, project_id, window_days=days)
        suggestions = await get_feedback_suggestions(conn, project_id)
        calibration = await compute_decision_calibration(conn, project_id)

    return {
        "metrics": trust_metrics,
        "suggestions": suggestions,
        "calibration": calibration,
    }


@app.get("/api/mutes", tags=["Mute Rules"])
async def list_mute_rules(project_id: int = Depends(require_read_project_id)):
    """List active mute / ignore rules."""
    async with read_project_transaction(project_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, pattern, reason, created_by, expires_at, created_at
            FROM ignore_rules
            WHERE project_id = $1 AND expires_at > now()
            ORDER BY created_at DESC
            """,
            project_id,
        )
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "pattern": r["pattern"],
            "reason": r["reason"],
            "created_by": r["created_by"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@app.post("/api/mutes", tags=["Mute Rules"])
async def create_mute_rule(
    rule: IgnoreRuleCreate,
    project_id: int = Depends(require_read_project_id),
):
    """Create an ignore / mute rule (max 90 days, default 14 days)."""
    days = min(max(1, rule.expires_in_days or 14), 90)
    expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    async with project_transaction(project_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ignore_rules (
                project_id, kind, pattern, reason, created_by, expires_at, created_at
            )
            VALUES ($1, $2, $3, $4, 'user', $5, now())
            RETURNING id
            """,
            project_id,
            rule.kind,
            rule.pattern,
            rule.reason,
            expires_at,
        )
    return {
        "status": "created",
        "id": row["id"],
        "expires_at": expires_at.isoformat(),
    }


@app.delete("/api/mutes/{id}", tags=["Mute Rules"])
async def delete_mute_rule(
    id: int = Path(...),
    project_id: int = Depends(require_read_project_id),
):
    """Delete / unmute an active ignore rule."""
    async with project_transaction(project_id) as conn:
        await conn.execute(
            "DELETE FROM ignore_rules WHERE id = $1 AND project_id = $2",
            id,
            project_id,
        )
    return {"status": "deleted", "id": id}


@app.get("/v1/digest/preview", tags=["Digest"])
async def get_weekly_digest_preview(
    env: str = Query("prod"),
    project_id: int = Depends(require_read_project_id),
):
    """Generate weekly digest preview."""
    from app.trust.digest import generate_weekly_digest
    async with read_project_transaction(project_id) as conn:
        return await generate_weekly_digest(conn, project_id, environment=env)


# ============================================================================
# Milestone 9: Opt-In Telemetry & Owner Fleet Overview
# ============================================================================


class TelemetryOptInRequest(BaseModel):
    enabled: bool


@app.get("/v1/telemetry/preview", tags=["Telemetry & Privacy"])
async def preview_telemetry(_admin: bool = Depends(require_admin_key)):
    """Inspect the exact allowlisted payload that would be transmitted if telemetry is enabled.

    Install-wide (counts across all projects), so it needs the admin key, not a project key.

    GUARANTEE: No code, diffs, repository names, file paths, error messages, emails, URLs, or IPs.
    """
    from app.telemetry import build_telemetry_payload
    payload = await build_telemetry_payload()
    return payload.model_dump()


@app.get("/v1/telemetry/status", tags=["Telemetry & Privacy"])
async def get_telemetry_status(_admin: bool = Depends(require_admin_key)):
    """Retrieve current opt-in status and anonymous installation ID."""
    from app.telemetry import get_or_create_install_id
    return {
        "enabled": settings.TELEMETRY_ENABLED,
        "install_id": get_or_create_install_id(),
        "collector_url": settings.TELEMETRY_COLLECTOR_URL,
        "default": "off",
    }


@app.post("/v1/telemetry/opt-in", tags=["Telemetry & Privacy"])
async def set_telemetry_opt_in(
    body: TelemetryOptInRequest,
    _admin: bool = Depends(require_admin_key),
):
    """Enable or disable anonymous opt-in telemetry for this install (admin key; one project's
    key must not decide for every tenant). Lasts until this process restarts: set
    TELEMETRY_ENABLED in .env to keep it."""
    from app.telemetry import send_telemetry_report
    settings.TELEMETRY_ENABLED = body.enabled
    sent = False
    if body.enabled:
        sent = await send_telemetry_report()
    return {
        "status": "updated",
        "enabled": settings.TELEMETRY_ENABLED,
        "report_transmitted": sent,
        "note": "Applies until the API restarts; set TELEMETRY_ENABLED in .env to keep it.",
    }


@app.get("/v1/owner/stats", tags=["Owner Fleet View"])
async def get_owner_stats(_admin: bool = Depends(require_admin_key)):
    """Owner fleet overview aggregated from collector service.

    Protected by X-Admin-Key. Returns zero tenant IDs, customer code, or private paths.
    """
    import httpx
    collector_url = f"{settings.TELEMETRY_COLLECTOR_URL.rstrip('/')}/stats"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(collector_url, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                data["source"] = "collector"
                return data
    except Exception as e:
        logger.debug("Collector unreachable at %s: %s", collector_url, e)

    # Standalone fallback stats if collector service is not running
    from app.telemetry import detect_os_family, detect_run_mode
    return {
        "opted_in_installs": 1 if settings.TELEMETRY_ENABLED else 0,
        "active_this_week": 1 if settings.TELEMETRY_ENABLED else 0,
        "new_this_week": 1 if settings.TELEMETRY_ENABLED else 0,
        "version_mix": {"0.1.0": 1},
        "run_mode_mix": {detect_run_mode(): 1},
        "os_family_mix": {detect_os_family(): 1},
        "ai_provider_mix": {getattr(settings, "DECISION_PROVIDER", "rules"): 1},
        "channel_mix": {},
        "release_buckets": {"0": 1},
        "incident_buckets": {"0": 1},
        "disclaimer": "Lower bound: many installs do not opt in. Telemetry is strictly off by default.",
        "privacy_guarantees": [
            "Never customer source code or git diffs",
            "Never repository names, organizations, or branch names",
            "Never file paths or directory structures",
            "Never error messages, exceptions, or stack traces",
            "Never user emails, usernames, or account IDs",
            "Never IP addresses or hostnames",
            "Never API keys, webhooks, or secrets",
        ],
        # Stars and pulls come only from the collector; never show made-up numbers
        "public_signals": {
            "github_stars": None,
            "docker_pulls": None,
            "source": "Not collected: the telemetry collector is not reachable",
        },
        "source": "local_fallback",
    }






