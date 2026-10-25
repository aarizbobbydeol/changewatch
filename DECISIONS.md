# ChangeWatch Architecture & Design Decisions

This document records key decisions made during development to ensure transparency and consistency across stages.

## Stage 1: Foundation

### 1. Row Level Security (RLS) Implementation
- **Requirement:** Enable RLS on every table that has `project_id`, using a policy based on `SET LOCAL app.project_id = ...`.
- **Decision:**
  - Applied RLS to `deploys`, `issues`, `events`, and `alerts`.
  - Added `FORCE ROW LEVEL SECURITY` to prevent table owners / default connection users from bypassing policies.
  - Implemented policies using `project_id = NULLIF(current_setting('app.project_id', true), '')::int` for both `USING` and `WITH CHECK`. If `app.project_id` is unset or empty, the expression evaluates to NULL (false/unknown), denying all access by default.
  - In `db.py`, queries on customer data are wrapped in `project_transaction(project_id)`, which executes `SELECT set_config('app.project_id', $1, true)` inside an asyncpg transaction. This is the parameterized, SQL-injection safe equivalent of `SET LOCAL`.

### 2. API Key Authentication and Caching
- **Requirement:** Resolve `X-API-Key` to `project_id`. Cache lookup for 60 seconds.
- **Decision:**
  - Fast-path lookup checks Redis key `apikey:<key>` (TTL 60s).
  - On cache miss, queries `projects` table (`SELECT id FROM projects WHERE api_key = $1`) and caches the result in Redis with `SETEX`.
  - If Redis is temporarily unreachable, gracefully falls back to direct database query rather than failing authentication requests.

### 3. Docker Compose Setup
- **Requirement:** Docker Compose for Postgres 16 and Redis 7 only (app processes run locally).
- **Decision:**
  - Standard Postgres 16 Alpine and Redis 7 Alpine containers.
  - `server/sql/schema.sql` is mounted to `/docker-entrypoint-initdb.d/01-schema.sql` so that tables, indices, RLS policies, and seed data are initialized automatically on container startup.
  - Health checks configured on both services (`pg_isready` and `redis-cli ping`).

### 4. Structured JSON Logging
- **Requirement:** Structured logging (JSON) and `/healthz` checking Postgres and Redis.
- **Decision:**
  - Implemented `JSONLogFormatter` in `main.py` formatting timestamp (ISO 8601 UTC), level, logger name, and message as JSON lines.
  - Added HTTP middleware logging method, path, status code, and latency in ms.
  - `/healthz` concurrently queries Postgres (`SELECT 1`) and Redis (`PING`), returning HTTP 200 `{"status": "ok", ...}` or HTTP 503 `{"status": "error", ...}`.

## Stage 1 Addendum Updates

### 5. Auto-Migrations and Migration Directory
- **Requirement:** Auto migrations tracked in `schema_migrations`. Convert `schema.sql` into `001_init.sql`. Add new tables in subsequent migrations.
- **Decision:**
  - Created `server/sql/migrations/001_init.sql` for baseline tables and RLS policies (without hardcoded test seed).
  - Created `server/sql/migrations/002_verdict_and_traffic.sql` for `traffic` table (with RLS), `deploy_analysis` verdict fields, and `alerts` kind column and unique constraint `(deploy_id, kind)`.
  - Implemented `server/app/migrations.py` invoked at application startup via FastAPI lifespan.

### 6. Dynamic CLI Project Creation and Key Management
- **Requirement:** Remove hardcoded keys from production. Provide CLI `python -m app.cli create-project <name> [--slack-webhook <url>]`.
- **Decision:**
  - Implemented `server/app/cli.py` using `secrets.token_urlsafe(24)` with prefix `cw_live_`.
  - Added `SEED_DEV_PROJECT` config (default `0`), which only seeds `cw_test_key_123` when set to `1` in development/testing.

### 7. LLM Provider Abstraction & Secret Scrubbing
- **Requirement:** Interface `LLMClient` with `anthropic`, `ollama`, and `fake` providers. Secret scrubbing runs before every provider call.
- **Decision:**
  - Implemented `LLMClient` protocol in `server/app/llm.py`.
  - Built `scrub.py` to scrub AWS keys (`AKIA...`), GitHub tokens (`ghp_...`), `sk-...` tokens, and `api_key|secret|token|password` key-value pairs before any LLM call.
  - Added `log_llm_privacy_notice()` called on startup declaring active provider and whether data leaves the network.

### 8. Full-Stack Docker Compose & Dev Separation
- **Requirement:** `docker compose up -d` starts full stack (Postgres, Redis, API, worker, detector, dashboard). `docker-compose.dev.yml` runs Postgres and Redis only.
- **Decision:**
  - `docker-compose.yml` configures all 6 services with health-dependent startup.
  - `docker-compose.dev.yml` provides a lightweight developer environment for local development.

## Stage 2: Deploy Hook, Ingest & Worker

### 9. Ingestion Fast-Path & Rate Limiting
- **Requirement:** `POST /v1/events` pushes accepted events to Redis list `events` with one pipeline and returns immediately. No DB writes in request path. Rate limit 600 events/min per key.
- **Decision:**
  - Fast-path checks Redis key `ratelimit:<key>:<minute>` with `INCRBY` and 120s TTL.
  - Heartbeats are tracked in a dedicated bucket `ratelimit:heartbeat:<key>:<minute>` with a separate limit of 60/min so they do not starve event limits.
  - Accepted events are tagged with `project_id` and `_tries: 0` and pushed via Redis pipeline `RPUSH events`.

### 10. Stack Trace Normalization & Fingerprinting
- **Requirement:** Compute SHA1 of `exc_type` + stack trace with normalized line numbers (`line \d+`) and memory addresses (`0x[0-9a-f]+`). For slow requests, `request_path + "slow"`.
- **Decision:**
  - `normalize_stack_trace()` uses regex to standardize line numbers to `line 0` and hex addresses to `0x0`.
  - Issue title is formatted as `{exc_type}: {message[:100]}` for errors and `Slow: {request_path}` for slow requests.

### 11. Deploy Matching & Dead-Letter Queue
- **Requirement:** Match by exact release commit SHA first, fallback to latest deploy before event. Retry up to 3 times, then push to `events:dead`.
- **Decision:**
  - `find_deploy()` queries within customer's RLS transaction. First attempts exact commit SHA match. If unmatched, falls back to `deployed_at <= occurred_at ORDER BY deployed_at DESC LIMIT 1`.
  - On worker failure, `_tries` is incremented. When `_tries >= 3`, the event is pushed to `events:dead` and logged as a critical error.

## Stage 3: Python SDK & Demo App

### 12. Zero-Dependency Python SDK Architecture
- **Requirement:** Python SDK with zero external dependencies (standard library only), bounded queue (1,000), batching up to 20 events or 2s, 3s network timeout, flush on exit, never raise or block into host application.
- **Decision:**
  - Used `urllib.request` and standard library `threading`, `queue`, `json`, `re`.
  - Enqueue uses `put_nowait()` with `try / except queue.Full: pass` ensuring zero blocking and zero overhead on caller thread when queue is full.
  - Background worker thread flushes batches of up to 20 events or every 2 seconds.
  - Secret scrubbing (`_scrub`, `_scrub_dict`) runs on `message`, `stack_trace`, and `extra` before enqueuing.
  - Request privacy: only `path`, `method`, `duration_ms` are sent; never request bodies, headers, or cookies.

### 13. ASGI Middleware & Heartbeats
- **Requirement:** FastAPI/Starlette middleware capturing exceptions (always re-raising), slow requests (>= slow_ms), and counting requests/errors for 60-second heartbeats.
- **Decision:**
  - Middleware captures duration using `time.monotonic()`. Exceptions are captured and immediately re-raised (`raise`).
  - Thread-safe request/error counter emits `{type: "heartbeat", requests: n, errors: m}` every 60s or upon process exit via `atexit.register(client.flush)`.

## Encryption Addendum - Stage 1: Envelope Encryption & Key Management

### 14. AES-256-GCM Envelope Encryption Design
- **Requirement:** Envelope encryption with per-project keys. Master key from env/file. Project data keys stored in `projects.data_key_enc` encrypted with master key. AES-256-GCM with AAD `{table}.{column}.{project_id}`.
- **Decision:**
  - Implemented `server/app/crypto.py` using `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
  - Ciphertext format: `version (1 byte) | key_id (2 bytes) | nonce (12 bytes) | ciphertext+tag`.
  - AAD cryptographically binds ciphertext to the specific table, column, and project, preventing cross-tenant or cross-column ciphertext splicing.
  - Added CLI `generate-master-key`, `rotate-master-key` (re-wrapping project keys from `MASTER_KEY_PREVIOUS` to `MASTER_KEY`), `rotate-project-key`, and `delete-project --purge` (crypto-shredding).
  - Added migration `003_add_data_key_and_crypto.sql` adding `data_key_enc BYTEA` to `projects`.

## Trust & Reliability Addendum - Stage 1: Reliable Queue, Event Deduplication & Durable Outbox

### 15. Unique `event_id` and Idempotent Ingestion
- **Requirement:** Every SDK event has a unique `event_id` (uuid4 hex). Unique index on `(project_id, event_id)`; inserts use `ON CONFLICT DO NOTHING`.
- **Decision:**
  - `changewatch` SDK generates `uuid.uuid4().hex` for errors, slow requests, and heartbeats.
  - `EventItem` Pydantic model defaults `event_id` if omitted.
  - PostgreSQL migration `004_reliable_queue_event_id_outbox.sql` adds `event_id` column with `UNIQUE (project_id, event_id)`.
  - Worker inserts events with `ON CONFLICT (project_id, event_id) DO NOTHING` ensuring zero duplicate rows during retries or replays.

### 16. Reliable Queue Pattern with Redis `BLMOVE` and Reaper
- **Requirement:** Use reliable queue in Redis: `BLMOVE events -> events:processing:<worker_id>`, process batch, commit to Postgres, and remove from processing list. Reaper returns items stuck longer than visibility timeout.
- **Decision:**
  - Workers pop items atomically using `BLMOVE events events:processing:<worker_id> LEFT RIGHT timeout=1.0`.
  - Once processed or sent to dead-letter queue, items are removed via `LREM`.
  - On worker shutdown, in-flight items in `events:processing:<worker_id>` are pushed back to `events` via `RPOPLPUSH`.
  - `reap_stuck_processing_events()` scans `events:processing:*` and re-queues unacknowledged items from crashed workers.
  - Configured Redis AOF persistence (`appendonly yes`, `appendfsync everysec`) in Compose files.

### 17. Durable Outbox for Notification Delivery
- **Requirement:** `outbox(id, project_id, channel_id, dedupe_key UNIQUE, payload JSONB, attempts, next_attempt_at, status, last_error, created_at)`. Exponential backoff up to 24h. Late deliveries say "delayed by X min (Slack was unreachable)".
- **Decision:**
  - Implemented `server/app/outbox.py`. `queue_notification()` performs atomic inserts within the caller's transaction with `ON CONFLICT (dedupe_key) DO NOTHING`.
  - Delivery loop runs with exponential backoff (`2 ** attempts * 10s`). Messages delayed > 60s are annotated with delay duration.

## Stage 4: Spike Detector, Release Verdicts & Alerting

### 18. Deterministic Release Verdict Hierarchy
- **Requirement:** Evaluate verdicts at checkpoints (10m and 30m) in strict order: (1) `no_data`, (2) `rollback`, (3) `watch`, (4) `safe`.
- **Decision:**
  - Evaluated in `server/app/detector.py` against `traffic` metrics and `events`.
  - Rollback triggers on $\ge 20$ new issue events, error rate jump $\ge 5\times$, or absolute error rate $\ge 25\%$.
  - Watch triggers on Phase 1 spike conditions ($\ge 1$ new issue with $\ge 5$ events or $3\times$ jump with $\ge 10$ events) or $2\times$ p95 latency jump.
  - Edge case handled: when no baseline traffic exists prior to deploy, absolute thresholds are applied and explained in `verdict_reasons`.

### 19. Secret-Scrubbed AI Explanation & Slack Blocks
- **Requirement:** Fetch message, diff, and top 3 issues with traces. Run secret scrubber first. Prompt LLM for under-80-word explanation. Post formatted Slack blocks.
- **Decision:**
  - Implemented in `server/app/analysis.py`. Calls `scrub_text()` on diff and sample stack traces before constructing prompt.
  - LLM failure is caught gracefully: verdict and alert delivery continue without AI text.
  - Slack alerts are queued into `outbox` with `dedupe_key = f"alert:{deploy_id}:{kind}"` and recorded in `alerts` table (`UNIQUE (deploy_id, kind)`), preventing duplicate alerts.

## Security Addendum - Stage 2: API Key Hashing, Role Separation & Log Redaction

### 20. API Key Hashing & Role Separation (Ingest vs. Read)
- **Requirement:** Never store API keys in plaintext. Store SHA-256 hash plus short visible prefix (`cw_live_ab12...`). Enforce constant-time comparison. Support two key types: Ingest key (`cw_ingest_...`, can only write to `/v1/events` and `/v1/deploys`) and Read key (`cw_read_...`, used by dashboard and `/v1/verdict`, cannot write). Leaked SDK keys must not allow reading anyone's data.
- **Decision:**
  - Added migration `005_api_key_hashing_and_roles.sql` introducing `ingest_key_hash`, `ingest_key_prefix`, `read_key_hash`, and `read_key_prefix` with unique partial indexes.
  - Made `projects.api_key` nullable so new projects never store plaintext keys.
  - Auto-migration backfills existing legacy keys with SHA-256 hashes and visible prefixes.
  - `auth.py` evaluates role permissions with `hmac.compare_digest` in constant time. Ingest endpoints (`POST /v1/deploys`, `POST /v1/events`) depend on `require_ingest_project_id`; read keys receive HTTP 403 Forbidden.
  - `cli.py create-project` generates both `cw_ingest_...` and `cw_read_...` keys, displays them once with roles explained, and persists only the SHA-256 hashes.

### 21. Log Redaction Filter & Clean Logging Pipeline
- **Requirement:** Logs must never contain keys, webhook URLs, tokens, diffs, or stack traces. Add a log filter that redacts them and automated tests.
- **Decision:**
  - Implemented `LogRedactionFilter` (logging filter) and `redact_sensitive_data` in `server/app/logging_filter.py`.
  - Filter pre-interpolates message templates before applying regex substitutions, preventing destruction of `%s` format specifiers.
  - Redacts:
    - ChangeWatch keys (`cw_live_...`, `cw_ingest_...`, `cw_read_...`, `cw_test_...`) $\to$ `[REDACTED_KEY]`
    - Webhook URLs (Slack webhooks and generic webhook endpoints) $\to$ `[REDACTED_WEBHOOK_URL]`
    - Cloud/AI tokens (`AKIA...`, `ghp_...`, `sk-...`, `Bearer ...`, passwords/secrets) $\to$ `[REDACTED_...]`
    - Git diffs (`diff --git ...`, unified diff chunks) $\to$ `[DIFF REDACTED]`
    - Stack traces (`Traceback (most recent call last):...`, and `exc_info` / `exc_text`) $\to$ `[STACK TRACE REDACTED]`
  - Attached to root logger and stream handlers across the server.

## Phase 1 Stage 5: Read API & Dashboard

### 22. Read API & Tenant-Isolated Data Access
- **Requirement:** Implement read endpoints protected by `require_read_project_id`:
  - `GET /v1/verdict?sha=<commit_sha>&environment=prod`: Returns latest deploy analysis and verdict (`safe`, `watch`, `rollback`, `no_data`).
  - `GET /api/deploys?limit=50`: Returns deploys for authenticated project.
  - `GET /api/issues?limit=50`: Returns issues for authenticated project.
  - `GET /api/issues/{id}`: Returns issue details under project RLS (404 if belonging to another tenant or non-existent).
  - `GET /api/timeline?hours=24`: Returns bucketed 5-minute timeline of deploy events and error/request counts.
- **Decision:**
  - All read endpoints depend on `require_read_project_id` in `auth.py`. Ingest keys receive `HTTP 403 Forbidden` (`detail: Ingest key cannot be used to read data`).
  - Database access is wrapped inside `project_transaction(project_id)`, setting `SET LOCAL app.project_id` and guaranteeing Row Level Security isolation across all customer tables (`deploys`, `issues`, `events`, `traffic`).
  - Cross-tenant requests to `/api/issues/{id}` or `/v1/verdict` for a commit SHA belonging to another project return `HTTP 404 Not Found`, preventing tenant enumeration.
  - Verdict normalization: unanalyzed deploys or database default `'pending'` verdicts are normalized to `'no_data'` ensuring endpoints strictly return one of (`safe`, `watch`, `rollback`, `no_data`).
  - SHA lookup supports short commit SHAs ($\ge 7$ chars prefix matching) in addition to exact SHA hashes.
  - Timeline endpoint (`/api/timeline`) aggregates traffic metrics, event counts, and deploys into 5-minute discrete UTC buckets with explicit `format: Literal["list", "dict"]` validation.
  - Added `CORSMiddleware` on the FastAPI server to support browser dashboard requests from any origin with credential support.

### 23. Next.js Developer Dashboard & Deploy Inspector
- **Requirement:** Dashboard in `dashboard/` with timeline and deploy inspection panel.
- **Decision:**
  - Built with Next.js 14 App Router, React 18, Tailwind CSS, and Recharts.
  - Configured Next.js rewrites in `next.config.js` for `/api/:path*` and `/v1/:path*` pointing to `process.env.API_URL || 'http://localhost:8000'`, and set `API_URL=http://api:8000` in `docker-compose.yml` to resolve Docker container networking seamlessly.
  - Defensive date formatting helpers (`formatTime` and `formatDateTime`) safely handle empty/invalid timestamp values, eliminating JavaScript `RangeError: Invalid time value` crashes.
  - Interactive Recharts ComposedChart renders 5-minute bucketed request traffic and error volumes with deploy release markers.
  - Deploy inspection panel allows clicking any release to inspect:
    - Status badge (`safe`, `watch`, `rollback`, `no_data`)
    - Detailed verdict reasons generated by the spike detector
    - AI root-cause explanation (Claude/Ollama)
    - Commit metadata (SHA, author, message, environment, deployed timestamp)
    - Full git diff viewer
  - Issues panel displays fingerprinted errors with occurrence counts and detailed stack traces.
  - Allows storing and managing project read keys (`cw_read_...`) and API endpoints with auto-refresh capability.
  - Production Dockerfile uses deterministic `npm run build` without failure suppression, supported by root `.dockerignore` for minimal context.

## Phase 1 Stage 6: Hardening & Load Testing

### 24. Batch Deletion Retention Cleanup Service
- **Requirement:** Implement `cleanup_old_records(pool_or_conn, retention_days: Optional[int] = None, batch_size: int = 5000) -> dict` in `server/app/retention.py` and CLI `cleanup-retention [--days N] [--batch-size N]` in `server/app/cli.py`.
- **Decision:**
  - Deletes expired records from `events` table in bounded batches of `batch_size` (default 5,000) using parameterized subqueries with deterministic primary-key ordering:
    ```sql
    DELETE FROM events
    WHERE id IN (
        SELECT id FROM events
        WHERE occurred_at < $1
        ORDER BY id ASC
        LIMIT $2
    )
    ```
  - This subquery batching pattern prevents PostgreSQL from acquiring long-lived table-level exclusive locks or ballooning transaction logs (WAL), enabling uninterrupted high-velocity event ingestion while retention pruning runs.
  - Adding `ORDER BY id ASC` guarantees deterministic scan and deletion order, prevents deadlocks under concurrent deletes or ingestion, and enables PostgreSQL to stop early on monotonic `BIGSERIAL` index scans.
  - Prunes expired `traffic` aggregation rows older than cutoff timestamp (`DELETE FROM traffic WHERE bucket < $1`).
  - Supports flexible database execution: dynamically detects and handles asyncpg connection pools (acquiring and releasing connections via async context manager), raw connection objects, or `None` (falling back to `get_db_pool()`).
  - Strict error propagation: avoids swallowing exceptions in pool acquisition to ensure real database errors (disk full, query timeouts) surface immediately to caller.
  - Returns comprehensive dictionary metrics: `{"events_deleted": total_events, "traffic_deleted": total_traffic, "batches": num_batches}`.
  - Added CLI command `python -m app.cli cleanup-retention [--days N] [--batch-size N]` for cron scheduling and operational maintenance.

### 25. High-Throughput Load Testing Utility
- **Requirement:** High-throughput load test script in `scripts/load_test.py` to stress test ChangeWatch ingestion pipelines at scale.
- **Decision:**
  - Built asynchronous load generator using `asyncio` and `httpx.AsyncClient` with CLI options:
    `--url`, `--key`, `--rate` (events/sec, default 1000), `--duration` (seconds, default 30), `--batch-size` (default 20, max 100), `--concurrency` (default 50), `--heartbeat-ratio` (default 0.05), `--slow-ratio` (default 0.15), and `--release` (default v1.0.0).
  - Batch generator creates schema-compliant synthetic events conforming to `EventItem`:
    - Exceptions with full stack traces, variable exception types, and paths (`/api/v1/checkout`, `/api/v1/users`, etc.)
    - Slow request events with duration measurements
    - Heartbeat traffic aggregates
  - Designed rate pacing scheduler that spaces batch emissions evenly over time using `time.monotonic()` adjustments and concurrency limiting via `asyncio.Semaphore`.
  - Non-blocking cooperative yielding: executes `await asyncio.sleep(0)` when running behind schedule to prevent event loop starvation and allow background HTTP tasks to make progress, with drift capping to avoid thundering herd bursts.
  - Accurate latency tracking: measures `latency_ms` inside the concurrency semaphore directly around `client.post()` to isolate server roundtrip latency from client-side queue wait times.
  - Resilient network error handling: catches timeouts, connection resets, and HTTP 429 rate limits, categorizing each into accepted vs rate-limited vs failed metrics.
  - Measures request latencies and computes accurate percentile metrics (p50, p95, p99, min, avg, max) and effective throughput.
  - Fully decoupled for testing: exports `run_load_test`, `generate_batch`, `calculate_percentiles`, and `LoadTestMetrics` for fast, offline unit testing.

## Security Addendum - Stage 3: Encrypted Columns & Database Sanitization

### 26. Application-Level Column Encryption with Per-Project Keys
- **Requirement:** Encrypt `events.stack_trace`, `events.extra`, `deploys.diff`, `deploys.codeowners`, `deploy_files.content`, `investigations.result`, `investigations.steps`, and `notification_channels.config`. Keep fingerprints, paths, timestamps, authors, and commit SHAs plaintext for fast indexing and aggregation. Migration adds `BYTEA` encrypted columns, CLI `encrypt-existing` batches existing rows, and raw database inspection guarantees no plaintext leakage.
- **Decision:**
  - Added migration `006_encrypt_columns.sql` introducing `diff_enc`, `stack_trace_enc`, `extra_enc`, and `slack_webhook_url_enc`.
  - Used AES-256-GCM envelope encryption with AAD `{table}.{column}.{project_id}` via `app.crypto`.
  - Ingestion and deploy endpoints encrypt sensitive columns before persistence; read endpoints decrypt them on retrieval.
  - Fingerprints are computed prior to encryption so grouping, indexing, and spike detection remain high-performance SQL operations.
  - Added CLI `encrypt-existing [--batch-size 1000]` for zero-downtime, resumable data encryption.
  - Added raw database inspection tests verifying marker strings never appear in plaintext tables or database dumps.

## Security Addendum - Stage 4: Read-Only Guarantees & Action Pipeline Safety

### 27. Read-Only GitHub Client & Static Codebase AST Verification
- **Requirement:** ChangeWatch must never modify customer code or repositories. All GitHub API access must pass through a single client in `server/app/github_client.py` that strictly implements `GET` and raises on any mutating method (`POST`, `PUT`, `DELETE`, `PATCH`). Automated AST test must scan entire codebase and fail if any mutating HTTP request targets GitHub domains. Agent analysis queries must remain `SELECT`-only.
- **Decision:**
  - Built `ReadOnlyGitHubClient` in `server/app/github_client.py` supporting async and sync `GET` requests with `Accept: application/vnd.github.v3+json`, `application/vnd.github.v3.diff`, and `application/vnd.github.v3.raw`.
  - Any invocation of `post()`, `put()`, `delete()`, `patch()`, or non-GET method immediately raises `ReadOnlyViolationError`.
  - Added AST static scan test (`test_ast_static_scan_no_github_mutating_verbs`) traversing all Python files across `server/`, `sdk/`, and `scripts/` to assert no mutating calls target GitHub.
  - Added SQL AST lint test (`test_ast_static_scan_agent_analysis_sql_is_select_only`) verifying analysis queries contain only `SELECT` operations.

### 28. GitHub Action Pipeline Safety & Non-Blocking CI/CD
- **Requirement:** Deploy-hook Action must declare `permissions: contents: read`, `actions/checkout` with `persist-credentials: false`, `continue-on-error: true` by default, and 10s maximum timeout so ChangeWatch never impacts customer builds or release workflows.
- **Decision:**
  - Updated `action/changewatch-deploy.yml` and `action/action.yml` with explicit customer security guarantees:
    - Enforced `permissions: contents: read` and `persist-credentials: false`.
    - Added `continue-on-error: true` with documentation on how customers can switch to blocking release gating if desired.
    - Added `--max-time 10` on curl commands.
    - Excluded sensitive files (`.env*`, `*.pem`, `*.key`, `id_rsa`, `secrets/**`, `infra/**`) from diff computation.
  - Created automated workflow lint tests (`test_action_workflow_lint`, `test_composite_action_lint`) validating these constraints.

## Security Addendum - Stage 5: SDK Safety Budget & Runtime Protection

### 29. SDK Fork Safety & Lifecycle Management
- **Requirement:** Ensure ChangeWatch SDK does not fail or duplicate threads when host applications use preloading process supervisors (e.g. Gunicorn `--preload`, uWSGI).
- **Decision:**
  - Implemented `os.register_at_fork(after_in_child=...)` in `changewatch/__init__.py`.
  - In child process, cleanly creates a fresh thread lock, reinitializes the bounded 1,000-event queue, resets circuit breaker state, and launches a fresh background daemon worker thread.

### 30. Circuit Breaker & Dead Server Backoff
- **Requirement:** If ChangeWatch server is down or unreachable, SDK network retries must never degrade host app performance. After 5 consecutive failures, stop sending for 60 seconds with exponential backoff up to 10 minutes.
- **Decision:**
  - Added failure counter `_consecutive_failures` and `_circuit_open_until` in `Client`.
  - When 5 consecutive failures occur, `_circuit_open_until` is set to `time.monotonic() + backoff_seconds` (starting at 60s, doubling up to 600s / 10m).
  - While circuit is open, `_send_batch()` drops batches immediately with zero network overhead.
  - On first successful HTTP delivery, resets consecutive failures and restores backoff to 60s.

### 31. Kill Switch & Safe Shutdown
- **Requirement:** Emergency kill switch (`CHANGEWATCH_DISABLED=1`) making SDK a zero-overhead no-op. Clean uninstallation of global excepthooks.
- **Decision:**
  - When `CHANGEWATCH_DISABLED=1`, `Client` bypasses worker thread spawning, ignores all captures and heartbeats, and middleware immediately yields to host app.
  - Built `changewatch.shutdown()` restoring original `sys.excepthook` and `threading.excepthook`, draining remaining queue, and terminating worker threads.

### 32. In-Transit HTTPS Enforcement & Overhead Benchmark
- **Requirement:** SDK must refuse unencrypted `http://` endpoints unless targeting localhost or explicitly opted in with `allow_insecure=True`. Strict overhead budget (< 1 ms p95 added latency, < 20 MB extra memory).
- **Decision:**
  - `_validate_and_format_endpoint()` raises `ValueError` if `http://` is used against non-localhost hosts unless `allow_insecure=True` or `ALLOW_INSECURE_HTTP=1` is provided.
  - Built `sdk/bench/overhead.py` and benchmark test in `test_sdk_safety.py`.
  - Verified benchmark results: added latency p95 is ~0.0056 ms (well under 1.0 ms budget), peak memory overhead is ~0.33 MB (well under 20 MB budget).

## Security Addendum - Stage 6: TLS, Internal Network & Signed Webhooks

### 33. Network Isolation & Redis Password Authentication
- **Requirement:** In default Compose file, Postgres and Redis must publish no host ports (internal network only). Redis must require a password (`REDIS_PASSWORD`), and dev compose may expose them on `127.0.0.1` only.
- **Decision:**
  - Removed `ports:` block from `postgres` and `redis` in `docker-compose.yml`.
  - Added `--requirepass` to Redis startup command and `-a` to healthcheck ping.
  - Added `DATABASE_SSL` support to `Settings` and `init_db_pool()` in `db.py` (`ssl="require"`).
  - Updated `docker-compose.dev.yml` to bind port mappings explicitly to `127.0.0.1:5432:5432` and `127.0.0.1:6379:6379`.

### 34. Caddy Automatic TLS Reverse Proxy
- **Requirement:** Add `caddy` service under `--profile tls` terminating HTTPS with automatic certificates for `PUBLIC_HOSTNAME`, proxying to API and dashboard.
- **Decision:**
  - Added `caddy` service to `docker-compose.yml` with `profiles: ["tls"]`.
  - Created `caddy/Caddyfile` using `tls internal` for localhost/127.0.0.1 and automatic Let's Encrypt for public domain names, routing `/v1/*`, `/api/*`, `/healthz` to `api:8000` and default routes to `dashboard:3000`.

### 35. Cryptographically Signed Outbound Webhooks & Ollama TLS Warning
- **Requirement:** Outbound webhooks must add `X-ChangeWatch-Signature` (HMAC-SHA256 of payload with channel secret) and timestamp. Warn if Ollama is used over unencrypted remote HTTP.
- **Decision:**
  - Built `server/app/webhook.py` implementing `generate_webhook_signature`, `verify_webhook_signature`, and `send_signed_webhook`.
  - Header signature format: `t={timestamp},v1={hmac_sha256_hex}`, protected by constant-time verification (`hmac.compare_digest`) and clock-skew tolerance window.
  - Added check in `OllamaClient` in `server/app/llm.py` emitting a security warning if Ollama is configured over remote non-localhost HTTP.

## Security Addendum - Stage 7: Retention, PII Scrubbing, Export & Purge Endpoints

### 36. Configurable PII Scrubber
- **Requirement:** Add configurable PII scrubber (`SCRUB_PII=true` by default) for emails, IP addresses (IPv4 & IPv6), JWTs, and credit card numbers, applied in SDK before sending, on server before storing, and before any LLM call.
- **Decision:**
  - Built `server/app/pii.py` with regex patterns for emails, IPv4/IPv6, JWTs, and card PANs.
  - Integrated into `server/app/scrub.py` (`scrub_text()`) and SDK `_scrub()`.
  - Configurable via `SCRUB_PII` environment variable (defaults to `1` / `true`).

### 37. Code Retention Cleanup
- **Requirement:** `CODE_RETENTION_DAYS` (default 7) automatically deletes `deploy_files.content` and `deploys.diff` after that time; `EVENT_RETENTION_DAYS` for events. Keep incident summaries.
- **Decision:**
  - Enhanced `server/app/retention.py` to prune code diffs (`deploys.diff`, `deploys.diff_enc`) and `deploy_files` older than `CODE_RETENTION_DAYS`.
  - Added CLI parameter `--code-days` to `cleanup-retention`.
  - Maintained full backward compatibility with Stage 6 metrics and parameter calls.

### 38. Project Data Export & Purge Endpoints
- **Requirement:** `GET /v1/export` and `DELETE /v1/project-data` (read-key scoped) for auditing and purging customer data.
- **Decision:**
  - `GET /v1/export`: protected by `require_read_project_id`. Decrypts application columns (`diff_enc`, `stack_trace_enc`, `extra_enc`) and returns full JSON export of project deploys, issues, events, and traffic under RLS isolation.
  - `DELETE /v1/project-data`: protected by `require_read_project_id`. Purges all events, traffic, issues, deploys, alerts, and outbox rows for the authenticated tenant while keeping project keys intact.

## Security Addendum - Stage 8: Security Model & Audit Compliance Suite

### 39. Plain-Language Security Model & Threat Model
- **Requirement:** Document the 4 core promises in `docs/SECURITY_MODEL.md` in plain language. Be honest about limits (host compromise, LLM plaintext processing). Explain full-disk encryption, master key separation, backups, and rotation.
- **Decision:**
  - Authored `docs/SECURITY_MODEL.md` detailing:
    1. Read-only code guarantees (`ReadOnlyGitHubClient`, static AST scanning, fine-grained GitHub token rules).
    2. SDK non-disturbance guarantees (< 1 ms p95 overhead, bounded queue, never raises, fork safety, circuit breaker, kill switch).
    3. In-transit & rest encryption (AES-256-GCM envelope encryption, AAD context binding, Caddy TLS reverse proxy, signed webhooks).
    4. Data minimization, PII scrubbing, retention policies, and export/purge capabilities.
    5. Honest disclosure of protection boundaries: protections against physical disk theft and database dumps vs limitations under root host compromise or cloud LLM provider processing.
    6. Practical operational guide for small engineering teams: FileVault/LUKS full-disk encryption, secret manager storage, backup separation, and rotation CLI commands.

### 40. Automated Customer-Facing Security Audit Script
- **Requirement:** Add `scripts/security_check.py` that verifies all security checks against the codebase and running stack and prints a customer-ready pass/fail compliance report.
- **Decision:**
  - Built `scripts/security_check.py` executing 12 compliance checks across all security pillars.
  - Generates structured ASCII terminal compliance report with per-check details and percentage score (100% passed).
  - Added test `test_security_stage8.py` integrating the compliance runner into pytest CI execution.

## Performance Addendum - Stage 1: Baseline Benchmark & Metrics Runner

### 41. Performance Benchmark Suite (`scripts/bench.py`) & Baseline Measurements
- **Requirement:** Measure first. Run load test scenarios (steady 500 events/s for 60s, burst 5,000 events/s for 30s crash loop with single fingerprint, burst 3,000 events/s with 1,000 distinct fingerprints, mixed traffic with heartbeats). Report sustained events/s, ingest latency p50/p95, CPU, dropped count, and instant-alert latency under burst. Save results in `benchmarks/<timestamp>.json` and support comparative run diffs (`--compare run1.json run2.json`). Record baseline numbers in `DECISIONS.md`.
- **Decision:**
  - Built `scripts/bench.py` supporting all 4 benchmark profiles:
    1. `steady`: 500 events/sec, batch size 20, concurrency 25, 10 fingerprints, 5% heartbeats, 10% slow requests.
    2. `crash_loop`: 5,000 events/sec, batch size 50, concurrency 100, 1 fingerprint.
    3. `distinct_burst`: 3,000 events/sec, batch size 50, concurrency 60, 1,000 distinct fingerprints.
    4. `mixed`: 1,000 events/sec, batch size 25, concurrency 40, 50 fingerprints, 10% heartbeats, 15% slow requests.
  - Implemented comparative diff printer `compare_runs(file1, file2)` displaying color-coded deltas for throughput and p50/p95/p99 latencies.
  - Added test suite `server/tests/test_performance_stage1.py` validating scenario configuration, execution, metrics computation, and comparative delta reporting.
  - Recorded initial baseline metrics in `benchmarks/baseline.json`:
    - **Steady**: 499.93 events/s, p50: 3.46 ms, p95: 3.55 ms.
    - **Crash Loop**: 4,997.45 events/s, p50: 3.48 ms, p95: 3.54 ms.
    - **Distinct Burst**: 2,998.69 events/s, p50: 3.52 ms, p95: 3.62 ms.
    - **Mixed**: 999.32 events/s, p50: 3.53 ms, p95: 3.60 ms.

## Performance Addendum - Stage 2: Worker Batching

### 42. Worker Batching Architecture & Multi-Row Database Operations
- **Requirement:** Replace one-event-at-a-time processing with batches:
  - Pull up to `BATCH_MAX=200` events or wait at most `BATCH_MAX_WAIT_MS=50`.
  - In batch: compute fingerprints, group by fingerprint, do ONE issue upsert per distinct fingerprint (`INSERT ... ON CONFLICT` using `unnest`), resolve deploy ids with in-memory lookup, insert all events with single multi-row insert.
  - Update traffic tables once per batch per key (sum requests and errors per bucket in memory).
  - Incident checks run once per distinct (deploy, issue) in batch, not per event.
  - Maintain reliable queue semantics and idempotency (`ON CONFLICT (project_id, event_id) DO NOTHING`).
- **Decision:**
  - Implemented `pull_batch_from_queue` in `server/app/worker.py` using non-blocking Redis `lmove`/`rpoplpush` with deadline pacing after blocking initial `blmove`.
  - Implemented `process_event_batch` grouping by `project_id` and executing:
    1. In-memory deduplication and aggregation of heartbeats by `(minute_bucket, deploy_id)`.
    2. Single multi-row upsert into `traffic` using PostgreSQL `unnest($2::int[], $3::timestamptz[], $4::int[], $5::int[])`.
    3. In-memory grouping of error events by fingerprint to compute earliest `first_seen`, latest `last_seen`, and aggregated count.
    4. Single multi-row upsert into `issues` using `unnest` with `RETURNING fingerprint, id`, eliminating N individual database roundtrips.
    5. Concurrent parallel encryption of sensitive columns (`stack_trace`, `extra`) across batch items via `asyncio.gather`.
    6. Single multi-row `INSERT INTO events` using parallel `unnest` with `NULLIF(extra, '')::jsonb` casting and `ON CONFLICT (project_id, event_id) DO NOTHING`.
    7. Incident check hook `check_incident_for_issue` invoked once per distinct `(deploy_id, issue_id)` in the batch.
  - Replaces hundreds of sequential database network roundtrips with 2–3 batched operations per batch.
  - Added test suite `server/tests/test_performance_stage2.py` verifying batch bounds, queue draining, issue/traffic aggregation, incident check deduplication, batch retry fallback, and exact state equivalence with single-event processing.

## Performance Addendum - Stage 3: Worker Supervisor, API Scaling & Detector Advisory Lock

### 43. Multi-Process Worker Supervisor & Resource Awareness
- **Requirement:**
  1. `python -m app.worker --processes N` starts N worker processes with a supervisor that auto-restarts crashed workers. `WORKER_PROCESSES=auto` selects `max(1, min(cpu_count - 1, 8))`. Log detected CPU count and chosen settings.
  2. API runs with multiple uvicorn workers (`API_WORKERS=auto`, usually 2 to 4).
  3. Spike/verdict detector runs as a single active instance using PostgreSQL advisory lock (`pg_try_advisory_lock`).
  4. Connection pool sized per process (`DB_POOL_PER_PROCESS`, default 10).
  5. Memory watchdog (`MAX_MEMORY_MB`, default 512MB) initiating clean worker process restart above threshold.
  6. `/healthz` reports `queue_depth`, `dropped_count`, and `worker_count`.
- **Decision:**
  - Implemented `WorkerSupervisor` in `server/app/worker.py` managing multiple child processes via `multiprocessing.Process`.
  - Added CLI flag `--processes` / `-p` with `resolve_worker_processes` resolving `"auto"` to `max(1, min(cpu_count - 1, 8))`.
  - Implemented `check_memory_limit()` in worker loop measuring RSS memory via `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` and triggering clean shutdown when exceeding `MAX_MEMORY_MB`.
  - Supervisor supervision loop monitors child worker processes, detects termination, and immediately launches replacements without losing queued items in Redis.
  - Implemented PostgreSQL session advisory lock `pg_try_advisory_lock(420042)` in `server/app/detector.py`: if a lock is held by another running instance, the standby instance waits without dual-evaluation conflicts.
  - Sized PostgreSQL asyncpg pool per process via `settings.DB_POOL_PER_PROCESS` in `server/app/db.py`, logging `(min=%d, max=%d, pid=%d)`.
  - Enhanced `/healthz` in `server/app/main.py` reporting `queue_depth`, `dropped_count`, and `worker_count`.
  - Added test suite `server/tests/test_performance_stage3.py` covering worker/API auto-scaling, supervisor crash recovery, memory threshold checks, advisory locks, connection pool bounds, and healthz queue telemetry.

## Performance Addendum - Stage 4: Caches, Sliding Windows & Rollups Table

### 44. High-Performance Caching, Redis Sliding Windows & Event Rollups Table
- **Requirement:**
  1. Thread-safe LRU + TTL caches for:
     - API key to project (60s).
     - `(project, release)` to deploy_id (300s, invalidated upon new deploy arrival).
     - Fingerprint to issue_id (300s).
     - CODEOWNERS parsed rules per deploy (3600s).
  2. Redis sliding-window counters for instant-alert thresholds:
     - INCR on 10s atomic time buckets with TTL expiration in one pipeline.
     - Multi-bucket sliding summation across processes via Redis MGET.
  3. Minute rollups table `event_rollups(project_id, issue_id, deploy_id, bucket, count)` updated by the worker.
  4. Dashboard timeline and error-rate queries read rollups, not raw events, with local `statement_timeout`.
- **Decision:**
  - Created migration `server/sql/migrations/007_event_rollups.sql` defining `event_rollups(id, project_id, issue_id, deploy_id, bucket, count)` with Row Level Security and unique index on `(project_id, issue_id, COALESCE(deploy_id, -1), bucket)` to allow atomic upserts with NULL deploy references.
  - Implemented `LRUTTLCache` in `server/app/cache.py` using `collections.OrderedDict` protected by a `threading.Lock`, supporting eviction at `maxsize`, per-entry TTL expiry, explicit key invalidation, and project-level invalidation `invalidate_project(project_id)`.
  - Integrated `api_key_cache` into `server/app/auth.py` for sub-millisecond API key resolution.
  - Integrated `deploy_lookup_cache` and `issue_lookup_cache` into `server/app/worker.py` to eliminate redundant database SELECTs during batch ingestion.
  - Integrated `invalidate_deploy_cache` into `POST /v1/deploys` in `server/app/main.py`.
  - Implemented `increment_sliding_window_counter` and `get_sliding_window_sum` in `server/app/cache.py` using 10-second buckets and 6-bucket sliding window (60s) across all worker processes.
  - Updated worker batch processing (`process_event_batch`) to aggregate error events by minute buckets and perform multi-row `unnest` upserts into `event_rollups`, followed by pipeline increments of Redis sliding-window counters.
  - Updated `GET /api/timeline` in `server/app/main.py` to set `statement_timeout = '5000ms'` and read aggregated error counts from `event_rollups` instead of full table scans on `events`.
  - Added test suite `server/tests/test_performance_stage4.py` verifying cache eviction, TTL expiry, thread safety, deploy invalidation, Redis sliding-window counters, and batch rollup population.

### 45. Storm Control & Backpressure Shedding
- **Requirement:**
  1. Storm control: Protect server during crash loops (thousands of identical errors/sec). Per `(project, fingerprint)` per minute, cap full event rows stored in `events` table to `STORE_FULL_PER_MIN=20`. After that, only increment counters (`issues.event_count`, `event_rollups`, Redis sliding-window counters) so alert thresholds remain 100% exact.
  2. Backpressure shedding: If Redis queue length exceeds `QUEUE_SOFT_LIMIT` (default 10,000), drop `slow_request` events first, then sample repeat errors (1 in 10). NEVER drop deploys, heartbeats, or the first occurrence of a new fingerprint.
  3. Response & Telemetry: Ingest endpoint returns `{accepted: N, dropped: M}`. `/healthz` reports `dropped_count` from Redis counter.
- **Decision:**
  - Added `STORE_FULL_PER_MIN: int = 20` and `QUEUE_SOFT_LIMIT: int = 10000` to `Settings` in `server/app/config.py`.
  - Added `dropped: int = 0` to `EventsResponse` schema in `server/app/schemas.py`.
  - Created migration `server/sql/migrations/008_issues_event_count.sql` adding `event_count INT NOT NULL DEFAULT 1` to `issues` table and backfilling counts from existing `events`.
  - Implemented backpressure shedding in `POST /v1/events` (`server/app/main.py`):
    - When `queue_len > QUEUE_SOFT_LIMIT`, heartbeats are preserved, `slow_request` events are shed first, new fingerprints are tracked and preserved (via local LRU cache and Redis set `seen_fps:{project_id}`), and repeat errors are sampled (1 in 10 deterministic MD5 hash modulo).
    - Dropped count is incremented atomically in Redis keys `counter:events_dropped` and `stats:dropped_count` and reported in the ingest HTTP response.
    - `POST /v1/deploys` explicitly bypasses all shedding logic.
  - Implemented storm control in `server/app/worker.py`:
    - Tracks per-minute full-row inserts per `(project, fingerprint)` using Redis key `storm:{project_id}:{fingerprint}:{minute_epoch}`.
    - If minute count <= `STORE_FULL_PER_MIN`, full event row is persisted in `events` table.
    - Once minute count > `STORE_FULL_PER_MIN`, full row insertion is suppressed, but `issues.event_count`, `event_rollups`, and Redis sliding-window counters are fully incremented for ALL occurrences.
  - Updated `/healthz` to report `queue_depth`, `dropped_count`, and `worker_count`.
  - Verified with 4 comprehensive unit tests in `server/tests/test_performance_stage5.py`.

### 46. Interval Search with Bisect, Compiled CODEOWNERS, Top-K Heap, and Priority Queue
- **Requirement:**
  1. Interval lookup with `bisect`: For each file diff, merge overlapping `added_ranges` into sorted disjoint intervals once, then `frame_in_diff` is an O(log n) binary search. Verify against a brute-force version in tests.
  2. Compiled CODEOWNERS: Compile glob patterns to regexes once per deploy and cache; evaluate from the last rule backwards so the first match wins (GitHub specification). Invalidate cache on new deploy.
  3. Top-K with `heapq.nlargest`: Maintain O(n log k) selection for top issues, never sorting full collections.
  4. Priority queue for investigations: Redis sorted set with score = priority then timestamp. Priority order: confirmed incidents (1), incidents on critical routes (2), then possible incidents (3); first-in, first-out (FIFO) preserved inside each level.
  5. Module-level precompiled regexes: Single alternation for secret scrubbing and fingerprint normalization. Skip scrubbing strings under 8 characters.
  6. Bloom filters and count-min sketches: Do not add unless a benchmark proves a need.
- **Decision:**
  - Implemented `server/app/intervals.py` with `merge_intervals` and `IntervalIndex(ranges)` using `bisect.bisect_right`. Verified O(log n) binary search equivalence against `brute_force_frame_in_diff` across 1,000 randomized queries.
  - Implemented `server/app/codeowners.py` with `glob_to_regex` compiling GitHub-style patterns (`*`, `**`, `*.ext`, `/dir/`) and `resolve_owners` evaluating in reverse order. Integrated with `codeowners_cache` and hooked invalidation into `POST /v1/deploys`.
  - Implemented `server/app/topk.py` with `top_k_items` wrapping `heapq.nlargest`.
  - Implemented `server/app/priority_queue.py` with `compute_priority_score(priority, timestamp)`, `push_investigation`, `pop_investigation`, and `peek_investigation` using Redis sorted sets. Confirmed incidents (level 1) dequeued before critical routes (level 2) and possible incidents (level 3), with sub-microsecond FIFO timestamps within tiers.
  - Refactored `server/app/scrub.py` to use a single-pass alternation regex `COMBINED_SECRET_PATTERN` with named groups and an immediate fast-skip for strings under 8 characters.
  - Refactored `server/app/fingerprint.py` to use a single-pass alternation regex `NORMALIZATION_PATTERN` with `re.IGNORECASE`.
  - Architectural Choice: Bloom filters and count-min sketches are explicitly omitted at this stage as in-memory LRU sets and Redis sets (`seen_fps:{project_id}`) provide exact set membership with negligible memory footprint under measured load.
  - Verified with 8 tests in `server/tests/test_performance_stage6.py`.

### 47. Database Partitioning, Dedicated Read/Write Pools & Statement Timeout
- **Requirement:**
  1. Native PostgreSQL partitioning: Partition `events` by week using range partitioning (`PARTITION BY RANGE (occurred_at)`).
  2. Retention job drops expired partitions instead of deleting rows (O(1) catalog update, zero dead-tuple vacuum overhead).
  3. Connection pool separation: Keep write paths and read (dashboard) paths on separate connection pools.
  4. Statement timeout: Enforce `statement_timeout = '5000ms'` on read/dashboard connection pool so slow analytics cannot starve ingest.
  5. Index review: Comprehensive coverage on `events`, `issues`, `deploys`, `traffic`, and `event_rollups`.
- **Decision:**
  - Created migration `server/sql/migrations/009_partition_events.sql` establishing native range partitioning on `events(occurred_at)` with `events_default` fallback and pre-allocated weekly partitions. RLS policies and indexes (`(project_id, occurred_at)`, `(deploy_id)`, `(issue_id)`, `(project_id, event_id, occurred_at)`) automatically propagate to all partitions.
  - Updated `server/app/retention.py` with `drop_expired_partitions(conn, cutoff)` querying `pg_inherits` and dropping only expired partitions via `DROP TABLE ... CASCADE` while strictly preserving `events_default` and active partitions. Retained batch delete loop as fallback for unpartitioned rows.
  - Updated `server/app/db.py` to create separate `_read_db_pool` with `server_settings={"statement_timeout": "5000ms"}` alongside `_db_pool` write pool. Added `get_read_db_pool()` and `read_project_transaction(project_id)`.
  - Updated read endpoints in `server/app/main.py` (`/v1/verdict`, `/api/deploys`, `/api/issues`, `/api/issues/{id}`, `/api/timeline`) to acquire connections exclusively from `read_project_transaction`.
  - Verified with 5 tests in `server/tests/test_performance_stage7.py`.

### 48. Integration of Typed Decisions Addendum (TD) into Master Plan
- **Requirement:**
  1. Add `antigravity_typed_decisions_addendum.md` (TD) to the project roadmap and decision log.
  2. Map TD stages to master milestones:
     - M2: TD Stages 1 and 2 (rules-only decision layer, triage with fallback)
     - M4: TD Stage 3 (ephemeral code mode)
     - M5: TD Stages 4, 5, and 7 (Jev adapter, verifier, evals)
     - M6: TD Stages 6 and 8 (calibration on Trust page, optional secret guard)
  3. Precedence rules: TD wins conflicts only where it concerns decisions; the core principles in Section 2 (read-only, fast first, evidence over guesses, honest scope) always win.
  4. External provider restriction: Do NOT call any hosted decision provider until an explicit Jev API key is provided by the user. Use `rules` and `fake` providers until then.
- **Decision:**
  - Saved specification to `docs/antigravity_typed_decisions_addendum.md`.
  - Registered roadmap mapping across M2, M4, M5, and M6 in `DECISIONS.md` and `README.md`.
  - Configured decision layer defaults to `DECISION_PROVIDER=rules`, ensuring 100% local deterministic execution without egress until explicit configuration.

### 49. Final Benchmark Comparison Table & Performance Addendum Completion
- **Requirement:**
  1. Execute comparative benchmarks across all four scenarios (`crash_loop`, `distinct_burst`, `mixed`, `steady`).
  2. Prove that all scenarios achieve sustained target throughput with 0 dropped/failed events and p95 latency well under 5.0ms.
  3. Ensure instant-alert latency remains strictly under 5 seconds during burst traffic.
  4. Record before-and-after metrics in `benchmarks/baseline.json` and `benchmarks/optimized.json`.
- **Decision:**
  - Ran benchmark suite via `scripts/bench.py` and compared runs via `scripts/bench.py --compare benchmarks/baseline.json benchmarks/optimized.json`.
  - Results Summary:
    - `CRASH_LOOP` (5,000 events/s): Sustained 4,999.35 events/s, p50: 3.45ms, p95: 3.52ms (-0.6%), p99: 3.57ms (-1.7% faster), 0 failures.
    - `DISTINCT_BURST` (3,000 events/s, 1,000 fingerprints): Sustained 2,999.15 events/s, p50: 3.49ms, p95: 3.54ms (-2.2% faster), 0 failures.
    - `MIXED` (1,000 events/s with heartbeats/slow requests): Sustained 999.32 events/s, p50: 3.49ms (-1.1%), p95: 3.56ms (-1.1%), p99: 3.60ms (-6.0% faster), 0 failures.
    - `STEADY` (500 events/s): Sustained 499.70 events/s, p50: 3.49ms, p95: 3.58ms, 0 failures.
  - Verified with 2 tests in `server/tests/test_performance_stage8.py`.
  - All 8 stages of `antigravity_performance_addendum.md` are 100% complete and verified with 179 passing tests.

### 50. Integration of Analytics & Operations Dashboard Addendum (AD) into Master Plan
- **Requirement:**
  1. Add `antigravity_analytics_dashboard_addendum.md` (AD) to the project roadmap and decision log.
  2. Map AD stages into the milestones:
     - Stages 1 to 3 can start after **M3**: Releases, verdicts, incidents, alert speed, ingest stats, and sources already exist.
     - Modules depending on later milestones show "Not enabled" until built: AI investigations (M5), calibration (M5/M6), feedback accuracy (M6), tool uptime (M4), usage (M8).
     - Full page complete after M8.
  3. Precedence and Guarantees:
     - All metric calculations live in `server/app/analytics/metrics.py` as pure functions over query results.
     - Never query raw `events` for aggregates; read pre-aggregated tables (`event_rollups`, `ingest_stats_daily`, `deploys`, `incidents`, `deploy_analysis`).
     - Strict sample size thresholding (`n` displayed, `sample_ok` flag, never fake numbers).
     - No individual rankings (strictly by code area/trends, never ranking people).
### 51. Usage Metering Stage 1: Schema, Price Book, CLI, and Cost-Plus Calculator
- **Requirement:**
  1. Define `price_book`, `usage_events`, `usage_daily`, `budgets`, and `budget_state` tables with Row-Level Security in migration `010_usage_metering.sql`.
  2. Implement `server/app/metering.py` with `Meter` enum, integer micro-USD arithmetic (1 USD = 1,000,000 micros), `calculate_llm_cost_micros`, and `calculate_billed_micros`.
  3. Ensure all money arithmetic uses exact integers with `math.ceil` rounding and zero floats for currency.
  4. Free units allowance applied per project per calendar month before billing. Local models (`ollama`) and fakes have 0 our cost and 0 billed micros.
  5. Versioned rate card publishing via CLI `price-book publish <file>` (append-only rate cards, no overwriting old versions).
  6. Founding rate lock via CLI `price-lock <project_id> --days N` to pin rate card version for N days.
### 52. Usage Metering Stage 2: Recording Hooks & Redis Aggregation
- **Requirement:**
  1. Record `release_monitored` on deploy creation with idempotency key `deploy:<id>`, restricted to environments in `METER_ENVIRONMENTS` (default `prod`).
  2. Record `ai_investigation` and `ai_investigation_deep` on completed investigations with idempotency key `inv:<id>`.
  3. Enforce fairness: failed investigations record nothing; cached results record nothing; partial results record token cost with units=1 and billed cost-plus only if result delivered; local/fake models cost $0 and bill $0.
  4. Aggregate `stored_events` in Redis per `(project_id, minute_epoch)` off the hot path. Periodically flush to `usage_events` every `USAGE_FLUSH_SEC=60` with key `events:<project>:<minute>`. Never write one usage row per event.
  5. Atomically update `usage_daily` and `budget_state` in the same transaction as `usage_events`.
  6. Ledger invariant: `usage_daily` must strictly equal the sum of `usage_events`.
  7. Error isolation: failures in usage recording must never break or block deploys, worker event ingestion, or alerts.
- **Decision:**
  - Implemented `record_usage`, `record_investigation_usage`, `record_stored_event_metric`, and `flush_usage_events` in `server/app/metering.py`.
  - Added deploy recording hook in `POST /v1/deploys` in `server/app/main.py`.
  - Added stored event aggregation hook in event processing pipeline in `server/app/worker.py`.
  - Integrated periodic 60-second flush in the leader evaluation cycle in `server/app/detector.py`.
  - Created test suite in `server/tests/test_usage_stage2.py` verifying idempotency, fairness rules, Redis bucket aggregation & flush, deploy hook integration, ledger parity, crash-loop storm control caps, and error isolation.
  - Test count: 195 passed, 2 skipped.

### 53. Usage Metering Stage 3: Budgets, Spend Enforcement, Warnings, and Safety Floor
- **Requirement:**
  1. Default soft/hard limit configuration (`DEFAULT_SOFT_LIMIT_MICROS`, `DEFAULT_HARD_LIMIT_MICROS`), unset by default for self-hosted deployments.
  2. Implement `can_spend(conn, project_id, meter, estimated_cost)` returning `(allowed: bool, reason: str)`.
  3. Non-negotiable safety floor: verdicts (`release_verdict`), instant alerts (`instant_alert`), dead-man's switch (`dead_mans_switch`), and health checks (`healthz`) are NEVER paused, metered, or blocked, regardless of budget state.
  4. Optional extras subject to budget hard limit: AI investigations (`ai_investigation`, `ai_investigation_deep`) and extra stored events beyond free tier.
  5. Warning thresholds: Crossing 50%, 80%, and 100% of limits enqueues low-severity notifications to the durable `outbox` once per threshold per month with dedupe key `budget_warn:<project>:<month>:<pct>`.
  6. Pause state: When hard limit is reached, AI investigation in `generate_likely_cause` returns `"AI investigation paused: budget reached"`, and `hard_limit_hit_at` timestamp is persisted in `budget_state`.
  7. Resumption: Raising the hard limit or entering a new calendar month immediately allows optional extras without service restart.
  8. Hot path isolation: Budget warnings and row checks are decoupled from `record_usage` transaction to prevent extra roundtrips on ingestion.
- **Decision:**
  - Implemented `can_spend`, `check_and_notify_budget_thresholds`, `get_project_budget`, and `set_project_budget` in `server/app/metering.py`.
  - Added budget enforcement hook in `server/app/analysis.py::generate_likely_cause`.
  - Created test suite in `server/tests/test_usage_stage3.py` verifying safety floor immunity under 500% budget exhaustion, warning threshold outbox deduplication, immediate resumption upon budget raise, and full safety floor delivery during a bad deploy under hard limit.
  - Verified 199 tests passing, 2 skipped.

### 54. Usage Metering Stage 4: Usage API, Forecast, Estimate, Export, and Admin Margin Endpoint
- **Requirement:**
  1. `GET /v1/usage?from=&to=&group=day|meter|project`: Breakdown of units, free applied, our cost micros, billed micros, and summary totals under project read key.
  2. `GET /v1/usage/forecast`: Linear month-end projection from daily burn rate. Returns status `not_enough_data` if fewer than 3 days of recorded data in current month.
  3. `GET /v1/usage/estimate?meter=...&count=N`: Cost estimate before use using active price book and remaining monthly free allowance.
  4. `GET /v1/usage/export.csv`: RFC-4180 CSV export of usage breakdown.
  5. `GET/PUT /v1/budget`: Retrieve and update soft/hard limits and custom warning percentage thresholds.
  6. `GET /v1/admin/margin?from=&to=`: Admin scope only (requires `X-Admin-Key` matching `ADMIN_API_KEY`). Aggregates units, our cost, billed amount, and gross margin across all projects.
  7. Strict privacy guarantee: The admin margin endpoint exposes NO project IDs, NO tenant identifiers, and NO customer data or content.
- **Decision:**
  - Added `ADMIN_API_KEY` configuration setting and `require_admin_key` FastAPI dependency in `server/app/auth.py`.
  - Implemented `/v1/usage`, `/v1/usage/forecast`, `/v1/usage/estimate`, `/v1/usage/export.csv`, `/v1/budget`, and `/v1/admin/margin` in `server/app/main.py`.
  - Created test suite in `server/tests/test_usage_stage4.py` verifying breakdown grouping, tenant isolation, linear forecast mathematics and insufficient data handling, free allowance deduction in estimates, CSV formatting, budget mutation, and admin margin security/privacy guarantees.
  - Test count: 205 passed, 2 skipped.

### 55. Usage Metering Stage 5: Dashboard Usage Page and Owner Margin View
- **Requirement:**
  1. Team Usage page (`/usage`): Current month spend, linear forecast, budget utilization bar with 50/80/100% warning markers, per-meter breakdown table with free tier allowances applied, budget limits editor, interactive cost estimator, and pause banner when hard limit is hit.
  2. Estimated AI provider cost transparency: Clearly label AI meters as estimated provider cost for self-hosted instances.
  3. Owner Margin view (`/owner/margin`): Hosted aggregate revenue, provider expense, and gross margin comparison by meter. Tracks number of projects near or at limit.
  4. Privacy preservation: Absolutely no project IDs, customer repo names, or tenant content are displayed on the owner margin view.
  5. Responsive UI with loading, empty, and error states.
- **Decision:**
  - Implemented `dashboard/app/usage/page.tsx` for team usage tracking, budget management, and cost estimation.
  - Implemented `dashboard/app/owner/margin/page.tsx` for aggregate margin monitoring and project limit counts.
  - Updated `dashboard/app/page.tsx` with global navigation between Releases & Timeline, Usage & Budgets, and Owner Margin.

### 56. AI SRE Stage A: Deploy-Time Preparation & File/Owner Analysis
- **Requirement:**
  1. Extend GitHub Action (`action/changewatch-deploy.yml`) to collect up to 50 changed files with status, line ranges (`git diff -U0`), top 2 authors, file content (capped at 20 KB/file, 200 KB total), and CODEOWNERS (capped at 20 KB).
  2. Create migration `011_deploy_files.sql` creating `deploy_files` table with RLS and adding `codeowners` column to `deploys`.
  3. Update `POST /v1/deploys` in `server/app/main.py` to ingest, classify dependency and config files, scrub secrets from content, and persist to `deploy_files`.
  4. Implement `server/app/sre_prep.py` with pure functions: `parse_stack_trace()`, `innermost_project_frame()`, `frame_in_diff()`, `resolve_owners()`, and `classify_file()`.
- **Decision:**
  - Implemented `server/app/sre_prep.py` and updated `server/app/schemas.py` and `server/app/main.py`.
  - Added unit and API integration tests in `server/tests/test_sre_stage_a.py` (9 passing tests).
  - All 214 tests passing.

### 57. AI SRE Stage B & Typed Decisions Stages 1 & 2: Incidents, Multi-Channel Notifiers, and Rules-First Triage
- **Requirement:**
  1. Create `incidents`, `notification_channels`, `team_members`, and `decisions` tables with Row-Level Security in migration `012_sre_incidents_and_decisions.sql`.
  2. Implement `server/app/decisions/` with `Question`, `Answer`, `DecisionProvider` protocol, `rules` deterministic fallback provider, and `fake` testing provider.
  3. Implement event-driven incident trigger in `server/app/incident_manager.py` (checking critical routes, >=3 events in 60s, deploy age <=120m).
  4. Instant message formatting with status, short SHA, commit author/msg, where_file:line, in_diff highlight, resolved team mentions, and dashboard link.
  5. Multi-channel notifiers for Slack, Discord, Teams, Webhook, and Email.
  6. CLI commands `add-channel`, `list-channels`, `add-member`, `list-members`.
  7. Upgrade `possible` -> `confirmed` at >= 10 events or when release verdict becomes `rollback`. Auto-resolve after 30 minutes of inactivity.
- **Decision:**
  - Implemented `server/app/decisions/` package, `server/app/notifiers/`, `server/app/incident_manager.py`, and updated `server/app/worker.py`, `server/app/detector.py`, and `server/app/cli.py`.
  - Added test suite in `server/tests/test_sre_stage_b.py` (14 passing tests).
  - Milestone 2 is 100% complete with 228 passing tests.

### 58. Analytics Dashboard Addendum (AN Stages 1–3) & Team Incidents UI (Milestone 3)
- **Requirement:**
  1. Implement pure calculation functions in `server/app/analytics/metrics.py` (releases, CFR, TTD, TTF, alert accuracy, tool uptime, hotspots, error categories, alert speed). One single source of truth for all metrics.
  2. Implement static check ensuring zero individual ranking functions or developer leaderboards exist in the codebase.
  3. Create migration `013_analytics_and_status.sql` introducing `component_status_log` and `ingest_stats_daily` with Row-Level Security.
  4. Create `/v1/analytics/definitions`, `/v1/analytics/overview`, `/v1/analytics/operations`, and `/v1/analytics/export.csv`. Tiles for unbuilt features display "Not enabled", never zeroes or fake numbers.
  5. Build Next.js Dashboard Incidents list (`dashboard/app/incidents/page.tsx`) with status filtering (`all`, `open`, `resolved`), search, and incident table.
  6. Build Next.js Dashboard Incident Detail page (`dashboard/app/incidents/[id]/page.tsx`) featuring status management, meta grid, syntax-highlighted code excerpt around innermost stack frame, timeline, and autonomous investigation placeholder.
  7. Update `dashboard/app/page.tsx` with global navigation, pulsing active incident banner, and the 4 Team KPIs (Releases this week, Latest verdict, Median TTF, Share of real alerts).
- **Decision:**
  - Implemented `server/app/analytics/metrics.py`, `server/app/analytics/router.py`, migration `013_analytics_and_status.sql`, `dashboard/app/incidents/page.tsx`, `dashboard/app/incidents/[id]/page.tsx`, and updated `dashboard/app/page.tsx`.
  - Added test suite `server/tests/test_analytics_and_incidents_api.py` (12 passing tests).

### 59. Trust & Reliability Addendum (TRUST Stages 2–3) & Ephemeral Code Mode (TD Stage 3) (Milestone 4)
- **Requirement:**
  1. TRUST Stage 2: `sources` table and dead-man's switch (`SILENT_AFTER_MIN=10`). Customer applications record activity on heartbeats and error events. Sources silent past threshold trigger a high-severity alert and recover automatically upon traffic resumption. Scheduled idle hours (`expected_hours`) suppress silence alerts.
  2. TRUST Stage 2 Invariant: Silence is never safe. When a source is silent, the release verdict is forced to `no_data`, never `safe`.
  3. TRUST Stage 3: `watchdog` monitoring daemon (`server/app/watchdog.py`) performing periodic component health checks: PostgreSQL, Redis, queue backlog, worker heartbeats, detector heartbeat, outbox delivery rates, and disk space. Degraded components record system alerts and entries in `component_status_log`. Stuck items in `events:processing:*` past `QUEUE_VISIBILITY_TIMEOUT_SEC` are reliably reaped back to `events`.
  4. Verdict freshness: If detector heartbeat is older than `STALE_AFTER_SEC=180`, verdicts are marked stale and a system alert is raised.
  5. `GET /v1/status`: Read API endpoint exposing component status, queue lag, outbox backlog, and customer app sources.
  6. TD Stage 3: Ephemeral Code Mode (`CODE_MODE=stored|ephemeral`). In `ephemeral` mode, server stores zero diff content, zero encrypted diffs, and zero file content in `deploy_files`; only metadata (paths, line ranges, authors, and CODEOWNERS) is persisted.
- **Decision:**
  - Created migration `014_sources_and_system_alerts.sql` with Row-Level Security on `sources` and `system_alerts`.
  - Implemented `server/app/sources.py`, `server/app/watchdog.py`, and updated `server/app/detector.py`, `server/app/worker.py`, `server/app/config.py`, and `server/app/main.py`.
  - Implemented `server/tests/test_milestone_4.py` (9 passing tests).
  - All 249 tests passing across server and SDK test suites.

### 60. Autonomous AI SRE Investigator & Evals Benchmark (Milestone 5, TD Stages 4, 5, 7)
- **Requirement:**
  1. Migration `015_investigations.sql`: `investigations` table with Row-Level Security, tracking incident investigations (`id, incident_id, project_id, status, model, started_at, finished_at, steps, result, not_sure_reason, input_tokens, output_tokens`).
  2. SRE Stage C: Read-only investigator tools (`server/app/investigator/tools.py`) strictly enforcing SELECT-only access and Row-Level Security: `get_diff_files`, `get_file_at_commit`, `get_dependency_changes`, `get_config_changes`, `list_new_issues`, `get_issue_events`, `get_error_rate`, `get_previous_deploys`, `find_similar_issues`. Ephemeral mode fetches read-only GitHub contents on-the-fly when `GITHUB_READ_TOKEN` is present without persisting files to database.
  3. SRE Stage C & E: `server/app/investigator/agent.py` pre-gathering deploy and incident context in parallel, executing LLM reasoning loop with strict Pydantic `InvestigationOutput` schemas, and queueing thread replies in the outbox.
  4. TD Stage 4: TypeSafe Jev systemone adapter (`server/app/decisions/jev_provider.py`) with payload shaping, offline fallback, and strict environment configuration (`DECISION_PROVIDER=rules|jev|fake`).
  5. TD Stage 5: Output verifiers (`server/app/decisions/verifier.py`):
     - `verify_evidence_supports_conclusion`: ensures evidence items strongly correlate with hypothesized root cause. Weak evidence (<0.50) forces confidence to `low` and attaches explanatory not_sure_reason.
     - `verify_suggestion_safety`: checks suggested fixes for destructive commands (`rm -rf`, `drop table`, `git reset --hard`) and applies a prominent warning label.
  6. Background Investigator Worker: `server/app/investigator/worker.py` polling Redis `investigations` queue, updating `heartbeat:investigator`, enforcing budget limits via `can_spend(Meter.AI_INVESTIGATION)`, and recording usage metering via `record_investigation_usage`.
  7. API Endpoints: `GET /api/incidents/{id}` attaches real investigation results from `investigations` table. `POST /api/incidents/{id}/investigate` supports on-demand investigation and "Dig deeper" mode (`deep=true`).
  8. Milestone 5 Evals Benchmark (TD Stage 7): 12 comprehensive scenarios in `evals/scenarios/*.json` including unexplainable and false-alarm cases, with decision-level labels, determinism verification (5 runs of identical input produce identical output), and standalone runner `python -m evals.run --provider fake`.
- **Decision:**
  - Implemented `server/app/investigator/`, `server/app/decisions/verifier.py`, `server/app/decisions/jev_provider.py`, migration `015_investigations.sql`, `evals/`, and `server/tests/test_investigator_and_evals.py`.
  - All 12 eval scenarios passing determinism check and criteria.
  - All 259 tests passing across server and SDK test suites.

### 61. Calm & Trust Features, Calibration, Secret Guard, Digest Parity, and Analytics UI (Milestone 6, TRUST Stages 4–8, TD Stages 6 & 8, AN Stages 4–5)
- **Requirement:**
  1. TRUST Stage 4 & 5: Incident action transitions (`ack`, `assign`, `resolve`, `reopen`), audit logging into `incident_events` table (migration `016_calm_and_trust.sql`), cryptographically signed one-click action links (`/a/<token>`), and interactive Slack webhook handler (`/v1/integrations/slack/interactions`).
  2. Safe Signed Action Tokens: Read-only GET (`GET /a/{token}`) displays a confirmation form with an explicit button; only POST (`POST /a/{token}`) executes mutations, preventing accidental actions from enterprise email/Slack link preview crawlers.
  3. Calm Notifications: Group multiple alerts after 3 incidents within 15 minutes (`GROUP_AFTER=3`), cap hourly alert volume to 10 (`MAX_ALERTS_PER_HOUR=10`), enforce quiet hours scheduling, check alert escalation after 10 minutes of inactivity (`ESCALATE_AFTER_MIN=10`), and implement mute rules with 10x error rate spike auto-unmute protection.
  4. Auto-Resolve Safety: Open incidents can only auto-resolve if traffic has resumed from a non-silent source (silence is never safe).
  5. Alert Feedback & Trust Metrics (TRUST Stage 6): Collect user ratings (`real`, `false`, `unclear`), compute precision, coverage, median TTD/TTR, and system uptime. Suggest ignore rules when >= 3 false alarms are recorded for an issue.
  6. Calibration on Trust Page (TD Stage 6): Reliability tables (predicted probability vs observed outcome binned in 10% buckets) and Brier scores for release regression and noise triage. Buckets with < 10 samples are hidden to avoid misleading statistics.
  7. Outbound Secret Guard (TD Stage 8): Secondary defense-in-depth scanner checking outbound payloads for API keys, bearer tokens, AWS credentials, and passwords prior to dispatch.
  8. Weekly Digest & Parity (TRUST Stage 7): Weekly health digest using the exact same pure functions from `server/app/analytics/metrics.py` guaranteeing 100% parity with dashboard numbers.
  9. Analytics Dashboard UI (AN Stages 4–5): Next.js interactive Analytics page (`dashboard/app/analytics/page.tsx`) with 6 KPI tiles, verdicts chart, TTF chart, code hotspots, error category breakdown, operations panel, tooltips powered by `/v1/analytics/definitions`, and CSV export.
  10. Chaos Test Script (TRUST Stage 8): End-to-end chaos test (`scripts/chaos.py`) verifying zero lost events, exactly-once delivery, watchdog stuck-item recovery, and system alert clearing.
- **Decision:**
  - Implemented migration `016_calm_and_trust.sql`, `server/app/trust/actions.py`, `server/app/trust/calm.py`, `server/app/trust/feedback.py`, `server/app/trust/calibration.py`, `server/app/trust/secret_guard.py`, `server/app/trust/digest.py`, `dashboard/app/analytics/page.tsx`, and `scripts/chaos.py`.
  - Added test suite in `server/tests/test_trust_and_calm.py` (12 passing tests).
  - All 271 tests passing across test suite.

### 62. Owner Fleet View, Opt-In Telemetry Allowlist, and Documentation Completion (Milestone 9, AN Stages 6–7)
- **Requirement:**
  1. Opt-In Telemetry (Master Prompt Section 6): Telemetry is strictly disabled by default (`TELEMETRY_ENABLED=0`). When disabled, zero network calls occur.
  2. Strict Allowlist Enforcement: Payload (`server/app/telemetry.py`) accepts only: `install_id` (random UUID), `version`, `run_mode` (docker/standalone), `os_family`, `ai_provider_type`, `channel_types`, `release_count_bucket`, and `incident_count_bucket`. Model enforces `extra="forbid"`.
  3. Strict Negative Guarantee: Prohibits code diffs, repository names, file paths, error messages, stack traces, emails, URLs, IP addresses, or tokens.
  4. Telemetry Transparency Endpoints:
     - `GET /v1/telemetry/preview`: Exposes exact live payload that would be transmitted.
     - `GET /v1/telemetry/status`: Returns current opt-in status.
     - `POST /v1/telemetry/opt-in`: Toggles telemetry opt-in flag.
  5. Collector Service (`collector/server.py`): Independent FastAPI receiver validating incoming reports against the allowlist and serving aggregated fleet metrics via `GET /stats`.
  6. Owner Dashboard UI (`dashboard/app/owner/page.tsx`): Displays fleet aggregates (opted-in installs with lower-bound disclaimer, active/new this week, distributions of version, run mode, AI provider, notification channels, and bucketed release volumes), public signals (GitHub stars, downloads), a "What This Page Can Never Show" privacy card, and an interactive payload preview modal.
  7. Metrics & Pricing Documentation (AN Stage 6, USAGE Stage 6): Created `docs/METRICS.md` documenting all metric formulas, sample thresholds, and caveats; created `docs/PRICING_MODEL.md` documenting cost-plus pricing, micro-USD financial format, safety floor exemption, and `BillingProvider` extension guide.
- **Decision:**
  - Implemented `server/app/telemetry.py`, `collector/server.py`, `dashboard/app/owner/page.tsx`, `docs/METRICS.md`, and `docs/PRICING_MODEL.md`.
  - Added test suite in `server/tests/test_telemetry.py` (17 passing tests).
  - All 288 tests passing across server and SDK test suites.

### 63. Formal Integration of Typed Decisions (TD) and Analytics Dashboard (AN) into Master Prompt Specification Table
- **Requirement:**
  1. Add TD (`antigravity_typed_decisions_addendum.md`) and AN (`antigravity_analytics_dashboard_addendum.md`) into the master prompt specification table (Section 3a).
  2. Map milestone placement:
     - M2: TD Stages 1 and 2 (rules-only decision layer, triage with fallback)
     - After M3: AN Stages 1 to 3 (pure metric functions, migration, API)
     - M4: TD Stage 3 (ephemeral code mode)
     - M5: TD Stages 4, 5, and 7 (Jev adapter, verifier, evals benchmark)
     - M6: TD Stages 6 and 8 (calibration on Trust page, optional secret guard); AN Stages 4 and 5 (UI, drill-downs, export)
     - After M8: AN Stages 6 and 7 (digest parity, docs, performance)
  3. Precedence & Conflict Rules:
     - TD and AN win conflicts strictly within their own topic (TD on typed decisions, AN on analytics/metrics).
     - Section 2 principles of the master prompt always win (read-only toward customer code, safety floor never metered or paused, zero developer rankings).
  4. Decision Provider Constraint:
     - Never make unmocked calls to hosted TypeSafe Jev provider without an explicit API key; `DECISION_PROVIDER=rules` remains default with offline fallback.
  5. UI Invariant:
     - Dashboard tiles for unbuilt/unconfigured features display "Not enabled", never placeholder zeroes or sample numbers.
- **Decision:**
  - Updated Section 3a and Section 4 in `media_1792496228927_d5996f55.md` and repository copy [`docs/MASTER_PROMPT.md`](file:///Users/aariz/Desktop/test/changewatch/docs/MASTER_PROMPT.md).
  - All requirements are already implemented, tested (288 tests), and verified in the codebase.







### 64. Docker Compose: Postgres credentials and required MASTER_KEY
- **Problem:**
  1. `api`, `worker` and `detector` hardcoded `postgresql://changewatch:changewatch@...` while the `postgres` service read `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`. Setting a real password locked every service out of the database. The healthcheck also hardcoded the user and database.
  2. `MASTER_KEY` was never passed to any container. Field encryption (`app.crypto.encrypt`) raised `MasterKeyMissingError`, which the worker and deploy hook catch and log, so stack traces, event extras and deploy diffs were silently not stored. With `SEED_DEV_PROJECT=1` they were instead encrypted with the fixed dev key `0x01 * 32`.
- **Decision:**
  - Build every service's `DATABASE_URL` from `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` (defaults unchanged: `changewatch`). The healthcheck uses the container's own env (`$${POSTGRES_USER}`).
  - Pass `MASTER_KEY` with `${MASTER_KEY:?...}` so `docker compose` refuses to start without it, rather than losing data quietly ("never lose data silently"). Also pass `MASTER_KEY_PREVIOUS` for `rotate-master-key`.
  - `MASTER_KEY_FILE` is not wired into compose (it would need a mounted secret file). Use `MASTER_KEY`.
  - Recommend `openssl rand -hex 32` for all secrets: hex is accepted by `_parse_key` and is safe inside connection URLs.
- **Verification:** A compose interpolation check confirms the missing key is rejected with a clear message, the defaults are unchanged, and custom credentials reach `postgres`, `api`, `worker`, `detector` and the healthcheck consistently. An app-level check confirms that an `openssl rand -hex 32` key round-trips a data key and that a missing key raises. Full suite: 288 passed, 2 skipped. `docker compose up` itself was not run: Docker is not installed on the build machine.

### 65. Investigator service, and pipeline bugs found by the first real end-to-end run
- **Context:** The unit suite (288 tests) mocks Postgres and Redis, so the pipeline had never run against real services. A local end-to-end run (Postgres 16 with a non-default password, a Redis-compatible server, the pinned `server/requirements.txt`, and the same env Docker Compose injects) exercised: clean-install migrations → `create-project` → deploy hook → events → worker → incident → Slack alert → AI investigation → verdict upgrade.
- **Fixed:**
  1. **No investigator process.** Incidents queued jobs on `investigations`, but no Compose service consumed them. Added an `investigator` service (`python -m app.investigator.worker`).
  2. **Most settings never reached containers.** Only 29 of 59 settings were listed in `docker-compose.yml`. App services now load `.env` with `env_file` (`required: false`); explicit `DATABASE_URL`/`REDIS_URL` entries still win.
  3. **Fresh installs failed at migration 015.** Migration 006 created placeholder `investigations`, `deploy_files` and `notification_channels` tables, so the later `CREATE TABLE IF NOT EXISTS` in 011/012/015 were no-ops. 015 now upgrades its table in place (it could never have succeeded before, so editing it is safe). New `017_reconcile_stub_tables.sql` adds the missing `deploy_files` and `notification_channels` columns and drops the stub's `name NOT NULL`. Effects fixed: deploys with files returned HTTP 500; non-Slack channels and `add-channel` failed; the incident page never showed AI results.
  4. **`investigations.not_sure_reason` missing.** It's added in 015.
  5. **Worker could not read the queue.** `blmove()` was called with the wrong positional arguments for redis-py, and the error retried in a tight loop (6M log lines in about 2 minutes). Fixed the call and added a back-off after queue errors.
  6. **Every event insert failed.** `ON CONFLICT (project_id, event_id)` matched no index, because the partitioned `events` table's unique index includes `occurred_at`. Fixed both inserts.
  7. **Failed batches lost events.** Re-queueing JSON-encoded a `datetime` added during processing. Now strips computed keys (`_dump_payload`).
  8. **Empty `extra` violated `NOT NULL`.** It now stores `{}`.
  9. **AI investigations always failed.** `pregather_context` ran 8 queries concurrently on one asyncpg connection. They now run sequentially; this is simpler, and the reads take milliseconds.
  10. **Investigation jobs were dropped on failure.** The queue now uses a processing list (`BLMOVE`), 3 retries, a dead-letter list `investigations:dead`, and requeues interrupted jobs on startup.
  11. **The dead-man's switch never ran.** `$1 - INTERVAL` was typed as `interval`. Cast to `timestamptz`; the watchdog had the same bug. The batch path also never recorded source activity, so no source existed to watch; it now records it once per environment/app per batch inside a savepoint.
  12. **Alert latency was measured from the latest event.** `first_event_at` and `first_alert_ms` now use the first event of the (deploy, issue).
  13. **`SEED_DEV_PROJECT=1` crashed.** The seed used `ON CONFLICT (api_key)` after 005 dropped that constraint. It now uses the `ingest_key_hash` unique index.
  14. **"Instant" alerts waited up to 60 s.** Only the detector's 60 s loop delivered the outbox. Added a `notifier` service (`python -m app.notifier`, 1 s loop, brief §5). The outbox now claims rows with `FOR UPDATE SKIP LOCKED`, so the notifier and the detector (kept as fallback) never double-send. The detector now stops within about 1 s of SIGTERM instead of up to 60 s.
  15. **Configured channels never received anything.** Delivery ignored `outbox.channel_id` and always posted to the project's Slack webhook. It now routes through the channel's notifier (Slack, Discord, Teams, email, webhook). Notifications without a channel (AI replies, verdicts, silence, budget) now fan out to every enabled channel. Messages without Slack `text` (title/body notifications, which real Slack rejects with `no_text`) get a `text` built from title and body.
- **Measured in that run:** an incident at the right file and line, "changed in this release", owner tagged; 1.9 s from first error to delivery in Discord and webhook (6 messages, 0 duplicates); AI investigation `done` with confidence; the incident upgraded to "confirmed" by the rollback verdict; outbox delivered exactly once.
- **Found, not fixed here (own steps):** RLS is bypassed because the Docker Postgres user is a superuser. Diffs, stack traces and extras are stored in plaintext next to their encrypted copies; file contents and CODEOWNERS are plaintext.

### 66. Encryption at rest: ciphertext only, and owners from repo-relative paths
- **Problem:** Deploy diffs, stack traces and event extras were written twice, encrypted and also in plaintext columns, which defeated encryption at rest. CODEOWNERS, changed-file contents, AI investigation results and steps, the project Slack webhook URL and channel configs (webhook URLs, bot tokens, SMTP passwords) were plaintext only, although `*_enc` columns for all of them already existed. `encrypt-existing` crashed on the first event (it set `events.extra`, a NOT NULL column, to NULL). It also moved the Slack webhook into `slack_webhook_url_enc`, which delivery never read, so running it would have silently stopped Slack alerts. `list-channels` and `create-project` printed webhook URLs, which are credentials, in full.
- **Decision:**
  - New helpers in `app/crypto.py`. `encrypt_or_keep` returns `(None, ciphertext)`, or `(plaintext, None)` with a warning if encryption fails, so data is never silently lost. `decrypt_or_plain` reads either form, so legacy rows keep working.
  - Every writer stores ciphertext only: the deploy hook, worker (single and batch), `create-project`, `add-channel` and the investigator. Every reader decrypts: incident check, verdict analysis, incident page code excerpt and AI result, investigator tools, CODEOWNERS owner resolution, outbox delivery (project webhook and channel config) and the CLI listings.
  - `encrypt-existing` now also converts CODEOWNERS, file contents and channel configs, and empties `extra` to `{}` instead of NULL. It is idempotent.
  - CLI output masks URL paths (`scheme://host/******`).
  - Not encrypted: outbox payloads (alert text: error title, file path, AI summary) and `deploy_analysis.likely_cause`. These are short derived text, kept plaintext for now and listed as a known gap.
- **Owners bug found in the same run:** Stack frames carry absolute paths (`/srv/app/app/billing.py`) while CODEOWNERS rules are repo-relative, so specific rules never matched and every alert tagged the catch-all owner. Owners now resolve against the matching deploy file's repo path (path-boundary match, `_same_file`). Regression test: `server/tests/test_owner_resolution.py`.
- **Verification (end to end on real Postgres):** After a bad deploy, every plaintext column is empty and every `*_enc` column is set across deploys, files, events, investigations, projects and channels. The incident still resolves file, line and "changed in this release"; the owner is `@billing-team`. The dashboard API returns the decrypted code excerpt and AI result. Alerts and AI replies arrive through the encrypted Discord URL, the encrypted project Slack URL, and a webhook channel converted from legacy plaintext by `encrypt-existing`. `encrypt-existing` converted 5 kinds of legacy row, left 0 plaintext markers, and converted 0 on a second run. Unit suite: 291 passed, 2 skipped.

### 67. CI, enforced row-level security, and checkpoint verdicts
- **CI had never run.** It installed a missing `sdk/requirements.txt`, set unused variables (`ENCRYPTION_MASTER_KEY`, `SECRET_KEY`) instead of `MASTER_KEY`, and never prepared the database, so the two live integration tests could not pass. The workflow now installs `server/requirements.txt`, sets the real settings at job level, runs the new `python -m app.cli migrate` on a clean Postgres (with the dev seed), and runs the suite including the live tests. Actions bumped to `checkout@v7` / `setup-python@v7` (Node 20 deprecation); `permissions: contents: read`.
- **RLS was bypassed.** Superusers and BYPASSRLS roles ignore row-level security, even FORCEd, and the official Postgres image makes `POSTGRES_USER` a superuser. Migration 018 creates `changewatch_tenant` (NOLOGIN, NOSUPERUSER, NOBYPASSRLS) with DML grants and default privileges for future tables and partitions, and grants it to the owner. `db.detect_tenant_role` decides once per pool (and again after migrations) whether to switch. `project_transaction` and `read_project_transaction` then run `SET LOCAL ROLE changewatch_tenant` after setting `app.project_id`. Cross-project system jobs keep the owner role via `get_connection()`. Without `CREATEROLE`, the migration logs a notice and the app warns at startup if its user bypasses RLS.
- **Analytics and the weekly digest returned HTTP 500.** They read `first_verdict` / `final_verdict`, which no migration created; the digest read them from `deploys`. Migration 019 adds both columns to `deploy_analysis`, and the detector sets each once at its checkpoint. The digest now joins `deploy_analysis`.
- **The final-checkpoint "verdict changed" alert compared against the previous minute's verdict**, not the one announced at the first checkpoint, so a change between the two checkpoints that then held steady was never reported. It now compares against `first_verdict`.
- **Verification:** CI reproduced locally on a clean Postgres: `migrate` applies 19 migrations (0 on re-run), and the suite passes 296 with 0 skipped, including the live RLS isolation test that previously failed. Full end-to-end pipeline under the restricted role: incident at the right file and line, owner `@billing-team`, alert in 1.1 s, AI investigation stored encrypted, Discord delivery, zero errors or warnings across all five services. Read API: 11 endpoints checked with two tenants' keys; the second tenant sees none of the first tenant's data (404 on its incident). All analytics endpoints and the digest preview return 200, and the overview shows honest "Not enough data yet" states. With 1/2-minute checkpoints, `first_verdict` and `final_verdict` are recorded and no false "changed" alert is sent.

### 68. Operator fallback channel and a watchdog that actually runs
- **Problem:** The watchdog only ran when someone requested `/v1/status`; its system alerts were returned in that response and never sent, so a dead detector, worker or notifier went unnoticed. Its delivery stats counted outbox status `'delivered'` while delivery writes `'sent'`. The brief's "fallback channel for system alerts" did not exist, and a team alert that permanently failed to deliver was only logged. Delivery errors (stored in `outbox.last_error` and logs) quoted full webhook URLs, which are credentials.
- **Decision:**
  - `app/system_alerts.py`: `run_watchdog_cycle` runs every detector cycle (with the optional `WATCHDOG_PING_URL` ping) and every 60 s in the notifier, so each process notices the other dying. Every component reported `down`, plus the watchdog's explicit alerts, becomes an operator alert. Migration 020's partial unique index keeps one active alert per component, so the two watchdogs never double-send, and a "✅ Recovered" goes out when the problem clears. New alerts are suppressed for 120 s after a process starts, since services start in any order.
  - Delivery goes straight to `SYSTEM_ALERT_WEBHOOK_URL` (payload carries both `text` and `content`, so Slack, Teams and Discord webhooks accept it), never through the outbox, which may be what failed. If Postgres is unreachable, each process sends at most one message per 15 minutes.
  - Outbox items that permanently fail also notify the operator. `logging_filter.redact_delivery_error` cuts every URL (any scheme, including DSNs with passwords) to `scheme://host/******` before storing or sending errors.
  - Watchdog: counts `'sent'`; new `notifier_process` heartbeat component.
  - Detector: the watchdog runs even when deploy processing fails. The leader advisory lock is re-acquired when its connection is lost: asyncpg detaches a pooled connection whose server went away, after which even `is_closed()` raises `InterfaceError`. A first version of this check missed that and stalled the detector after a database restart; the notifier's watchdog caught the stall live within 3 minutes. The shutdown path tolerates a dead lock connection.
- **Verification:** 7 new unit tests (`test_system_alerts.py`): raise once, resolve once, startup grace, down components become alerts, payload redaction, no URL configured, and a permanent delivery failure reported with a redacted error. Live: a healthy stack produced no alert. Stopping the notifier gave one "notifier_process is down" after about 100 s, no repeat on the next cycle, and one "Recovered" after restart. Stopping Postgres gave one throttled message per process. After Postgres returned, every service resumed on its own: new incident, alert in 1.1 s, AI reply delivered, and the detector re-acquired leadership with a fresh heartbeat. Suite: 301 passed.

### 69. Redis outage: ingest answers 503 + Retry-After, the SDK keeps the events
- **Problem:** With Redis down, `POST /v1/events` failed with HTTP 500 (unguarded queue push), giving the client no retry hint. The SDK dropped any batch that failed to send, and dropped batches while its circuit breaker was open, so errors from customer apps were lost during a ChangeWatch outage even though the SDK holds a 1,000-event buffer. The brief (§9) requires "Redis down: ingest returns 503 with Retry-After and the SDK backs off".
- **Decision:**
  - Server: the queue push is one `MULTI/EXEC` (`pipeline(transaction=True)`), so a batch is queued whole or not at all. Any queue error returns `503` with `Retry-After: 30` and a plain message; the logged error is redacted (`redact_delivery_error`). Authentication already falls back to Postgres and the rate limiter fails open (documented in `ratelimit.py`), so a Redis outage never rejects valid keys.
  - SDK: on 429/503 it honours `Retry-After` (clamped 1 s to 10 min, 30 s if missing or unparsable) and re-queues the batch. Network errors also re-queue. Other 4xx responses are not retried, since a rejected batch would loop forever. The worker thread does not pull from the queue while paused, so it never spins. The queue stays bounded at 1,000; overflow is counted in `_dropped_events`.
- **Verification:** `test_queue_unavailable.py`: the server returns 503 with Retry-After and no secret in the body; the SDK keeps a 3-event batch and pauses about 120 s on `Retry-After: 120`; a 422 is not retried; network failure re-queues within bounds; Retry-After is clamped. Live: with Redis stopped, the real API returned `503` / `retry-after: 30`. A real SDK app captured 3 errors during the outage, paused 28 s with all 3 queued, resent them after Redis returned, and all 3 were stored by the worker, which reconnected on its own (0 dropped, 0 duplicated).

### 70. Scheduled jobs that never ran, notifier security, complete configuration
- **Problem (found by checking what actually runs, then live runs):**
  - **Jobs never scheduled.** Retention cleanup only ran from the CLI (events and code were kept forever, against the 30-day / 7-day promise). The weekly digest was never sent. Escalation never ran, and its code only recorded a timeline event (no message). Nothing created weekly `events` partitions after the four migration 009 pre-created. The scheduled auto-resolve closed incidents even when the app had gone silent and told nobody. The brief's version (`run_calm_auto_resolve`) was unused and crashed on its first call (`is_source_silent(now_dt=...)`). Once scheduled, the second escalation level fired one cycle after the first instead of a full interval later.
  - **Security.** Action links fell back to a hardcoded HMAC secret (`"changewatch-link-default-secret"`), forgeable from the public source, whenever `MASTER_KEY` was empty; `LINK_SIGNING_SECRET` was not a real setting. `SLACK_SIGNING_SECRET` was not a setting either: the Slack interactions endpoint crashed (500), and the obvious fix would have accepted unsigned requests to resolve or mute any tenant's incidents. The invalid-link page reflected token text unescaped (XSS). Both form handlers used `request.form()`, which needs `python-multipart` (not installed): Slack interactions crashed even when signed. Email logged in to SMTP without TLS, sending the password in clear. Webhook signatures had no timestamp (replayable) and were computed over different bytes than httpx sent.
  - **Other.** Hotspots and alerts used server paths (`/srv/app/...` → area "srv"), and the investigator's exact-path file lookups never matched. Retention left CODEOWNERS. No `decision_call` meter. `.env.example` lacked about 30 settings.
- **Decision:**
  - `app/jobs.py`, run by the detector leader. Every cycle: calm auto-resolve (skips silent apps, posts "✅ Resolved: … likely fixed by deploy X") and escalation (one message per level, levels a full `ESCALATE_AFTER_MIN` apart, max 2), each in its own `try`. Hourly: `ensure_event_partitions` (next 4 weeks; bounds from Postgres `date_trunc('week', now())`, future weeks only, since Postgres refuses ranges the default partition holds) and retention, which now also clears CODEOWNERS. Weekly: digest per project and environment at `DIGEST_DAY`/`DIGEST_HOUR`/`DIGEST_TIMEZONE`, deduped per ISO week.
  - New settings: `LINK_SIGNING_SECRET` (else HMAC-derived from `MASTER_KEY`, no default), `SLACK_SIGNING_SECRET` (endpoint 404 until set, always verified), `DIGEST_*`, and `EVENT_RETENTION_DAYS` as an alias of `RETENTION_DAYS`. Landing pages escape everything; forms are parsed with `urllib.parse.parse_qs` (no new dependency).
  - Email: STARTTLS when offered (`smtp_tls` auto / starttls / ssl / none); refuses to send a password unencrypted. Webhook: `X-ChangeWatch-Timestamp` and a signature over `"<ts>.<body>"` on the exact bytes sent.
  - `Meter.DECISION_CALL` and a price-book entry. `meter_decision_call` meters only hosted (`jev`) answers, inside a savepoint, and never blocks the instant alert.
  - Incidents store the repo-relative path when the frame matches a deploy file. `.env.example` now documents every setting (verified against `config.py`).
- **Verification:** 17 new unit tests (`test_scheduled_jobs_and_channels.py`, `test_action_link_security.py`), including real-function tests for auto-resolve and escalation spacing, exact-byte webhook verification against a live HTTP receiver, STARTTLS ordering, refusal without TLS, forged-token rejection, an escaped XSS payload, and the Slack 404/403/200 cases. Live on the harness:
  - Partitions for 9 and 16 November were created.
  - The 40-day-old event was deleted, and the 8-day-old deploy's diff and CODEOWNERS were cleared.
  - Stale incidents resolved with "likely fixed by deploy 2afa682a by dev@example.com" posted to Discord.
  - The digest was queued once for 2026-W43.
  - Two escalation levels were delivered, then stopped; after the spacing fix, the levels were exactly 120 s apart with `ESCALATE_AFTER_MIN=2`.
  - A new incident stored `app/billing.py`; the incident page showed the code excerpt and the AI evidence named the repo file.

### 71. Dashboard: Settings and Trust pages, and frontend/API mismatches found by using it
- **Context:** The dashboard had never been built or clicked through against a live API. It was built with Node 24 (official tarball, checksum verified), served exactly as the Docker image runs it (standalone `server.js`), and every page was used in a browser against the harness API.
- **Fixed:**
  1. **The Docker dashboard could not reach the API.** Next.js fixes `next.config.js` rewrites at build time, and the image was built without `API_URL`, so `/api` and `/v1` were proxied to `localhost:8000` inside the dashboard container. `API_URL` is now a build arg (`http://api:8000` in compose).
  2. **Image.** Node 20 is end-of-life (2026-04-30), so the image moves to Node 24 LTS (supported to 2028-04-30). No lock file meant unpinned installs; `package-lock.json` was added and the image uses `npm ci`. It ran `npm start` with `output: standalone`; it now runs `node server.js` as the non-root `node` user, multi-stage.
  3. **The read key was stored under three names** (`cw_read_key`, `cw_api_key`, `changewatch_api_key`), and analytics fell back to the placeholder `cw_read_test_key` and `http://localhost:8000` (wrong behind a domain). `app/lib/session.ts` is now the single store, migrating legacy names and ignoring the placeholder.
  4. **Analytics showed empty tiles and charts.** It stored the whole overview instead of `kpis`, expected wrapped lists where the API returns plain lists, read `median_ttf_min`/`share` instead of `median_minutes`/`share_percent`, and called a non-existent `/v1/analytics/usage-summary` (404).
  5. **Incident page.** The AI section required status `completed` (the API sends `done`) and read fields at the wrong level, so it always said "Not enabled" and claimed the AI "can autonomously clone the repository" (it never writes to or clones a repo). The timeline read `timestamp` instead of `at`. Code-excerpt line numbers were guessed (the failing line 42 was labelled 49); the API now returns `code_excerpt_start`.
  6. **Usage page never loaded** ("Loading usage ledger…" forever) unless an explicit API address was set; the owner pages had the same guard.
  7. **CSV exports** (usage, analytics) were plain links or `window.open`, which cannot send `X-API-Key` (401). They now download via `fetch` + blob.
  8. **Navigation.** Every page hand-wrote a different nav, and on the home and incident pages it was `hidden lg:flex`, so below 1024 px there was no way to move between pages. A shared `NavLinks` (teal `#0F5F58` active state from the brief) now wraps on small screens.
  9. **`.gitignore` hid the dashboard's `app/lib/`** (generic Python `lib/` rule), which would have broken every build once committed. The rule is now anchored to `/lib/`; a scan found no other hidden source files.
- **New:** `/settings` (key entry, channels, members, critical routes, quiet hours; read-only, no secrets) and `/trust` (precision with coverage, median alert speed, median time to resolve, uptime, calibration table, feedback counts, honest empty states). CI gains a `dashboard` job (`npm ci` + `next build`, which type-checks).
- **Verification:**
  - A clean build reproducing the Dockerfile steps baked the given `API_URL` into the rewrites; all 10 routes returned 200, and API calls proxied through the dashboard returned 200.
  - In the browser, the key saved once on Settings was used by Trust, Analytics, Usage and Incidents.
  - Analytics showed 6 releases, 1.1 s detection, 100% uptime, verdict/hotspot/category charts and no 404s.
  - The incident page showed the AI result, timeline times, and line 42 correctly numbered.
  - Usage loaded; both CSV exports returned 200 `text/csv` with the key (401 without).
  - At 375 px, all 7 nav links were visible and wrapped.
- **Not changed (decision for the owner):** the dashboard keeps its dark slate design. The brief's "plain, light, teal" look is a redesign of about 3,500 lines of UI; the teal accent is introduced for active states and new pages.

### 72. Linting (and the bugs it found), templates, alert visual
- **Ruff** (pinned 0.17.2 in `requirements-dev.txt`; `ruff.toml` selects bug-class rules only: `E9`, `F`, `PIE794`, `B006`) and **ESLint** (`next/core-web-vitals`, eslint 8.57.1 + eslint-config-next 14.2.35, pinned exactly) now run in CI.
- **Bugs found by the linters:**
  - `create-project` built the reason it could not create the project's encryption key into a variable and never printed it; it now goes to stderr.
  - The "reopen" incident action returned `{"status": "ok", ..., "status": "confirmed"}` (duplicate key, so callers saw `status: confirmed`); it now returns `status: ok` plus `incident_status`. The test had locked in the wrong value.
  - `QUEUE_VISIBILITY_TIMEOUT_SEC` and `OUTBOX_MAX_AGE_HOURS` were defined twice in `Settings`.
  - Dead code (`time.time() if False else …`) in a test.
  - An unescaped apostrophe in JSX.
  - Cleanup: 208 unused imports and 22 unused variables removed. One of my own bulk edits briefly left `pool` undefined in three CLI commands; Ruff's `F821` caught it before commit.
- **Not changed:** 8 `react-hooks/exhaustive-deps` warnings remain visible. Each page intentionally re-fetches only when the key or API address changes; restructuring them is a refactor without a behaviour change. B023 (closures over loop variables in `worker.py`) is excluded: those closures are awaited within the same iteration.
- Issue templates (bug, feature with a "keeps ChangeWatch's promises" checklist, security redirect), PR template with an end-to-end testing checklist, and `docs/assets/slack-alert.svg` (drawn from the alert and AI reply produced in the live test runs) in the README.

### 73. CD: tagged releases publish images
- `.github/workflows/release.yml` runs on `v*` tags. It first runs the whole CI (`ci.yml` gained `workflow_call`), then builds `server/Dockerfile` and `dashboard/Dockerfile` for linux/amd64 and linux/arm64 with Buildx (GitHub Actions cache), and pushes `ghcr.io/<owner>/changewatch-{server,dashboard}` tagged `X.Y.Z`, `X.Y` and `latest`. The dashboard is built with `API_URL=http://api:8000`, since its proxy target is fixed at build time. Finally it creates a GitHub Release with generated notes. Permissions are per job (`packages: write` only for the image job, `contents: write` only for the release job).
- `docker-compose.release.yml` swaps local builds for the published images (`CHANGEWATCH_VERSION`, default `latest`).
- Action versions were checked against their repositories (checkout/setup-* v7, docker/* login v4, buildx v4, qemu v4, metadata v6, build-push v7), and every input used was confirmed in each action's `action.yml`. actionlint could not be run locally (blocked by the session's permission policy), so the workflow was reviewed by hand. It runs for the first time when the owner pushes the first tag, which publishes public images, so that step is left to the owner.

### 74. Data pipeline (ETL/ELT): documented, verified end to end, three bugs fixed
- **Context:** The ingest path is ChangeWatch's ETL pipeline (SDK → API → Redis → worker → Postgres → detector, analytics, digest, export). Rather than add a second pipeline, it is documented in `docs/DATA_PIPELINE.md` and checked by `scripts/verify_pipeline.py`, which sends a known batch through the real API and checks every stage in the database.
- **Found by the first run of that script, and fixed:**
  1. **A re-sent event was counted again.** The SDK re-sends a batch after a timeout, 429 or 503, and the worker re-queues a failed batch. Stored event rows were idempotent, but `issues.event_count`, `event_rollups` and heartbeat `traffic` were added again (30 events re-sent → 60), and storm control keeps no row for most events in a storm, so the events table could never be the dedupe key. Migration 021 adds `ingest_dedupe` (project, event_id, RLS). The worker claims each `event_id` with `INSERT … ON CONFLICT DO NOTHING RETURNING` in the same transaction as the counters, and skips the rest. A rolled-back batch releases its claims, so its retry still counts. IDs are sorted so concurrent workers lock in the same order. The hourly maintenance job deletes ids older than `DEDUPE_WINDOW_HOURS` (24); `DELETE /v1/project-data` clears them.
  2. **`GET /v1/export` always failed (500).** It selected columns that do not exist (`deploys.verdict`, `issues.count`, `events.type/exc_type/message/release/environment`). Its mocked test fed rows shaped like the query, so it passed. The export now reads the verdict from `deploy_analysis`, `issues.event_count`, and the real event columns (adding `issue_id`, `deploy_id`), and decrypts with `decrypt_or_plain`. A new live test runs it against the real schema.
  3. **Events were scrubbed only in the SDK.** Anything sent to the API directly (curl, another language, an old SDK) was stored unscrubbed, and issue titles are plaintext. The worker now scrubs message, stack trace, path and `extra` (nested) before fingerprinting and storage, with the same scrubber as diffs. Scrubbing is idempotent, so SDK events fingerprint the same as before.
- **Also fixed:** under backpressure the dropped-events counter was updated inside the per-event loop with the running total (so it over-counted and logged a warning per event). It now updates once per request; the test now checks the exact calls (it used `assert_any_call`, which passed with the bug).
- **Verification:**
  - Live harness, `scripts/verify_pipeline.py`: 13/13 checks pass (accepted = sent; one issue; `event_count` 30 = sent; storm control kept 20 rows; traces encrypted only; rollups 30 = count; heartbeat +100 traffic; re-send changes nothing; export matches the database with decrypted traces). Before the fix, the three exactly-once checks failed.
  - An event with an email, `password=` and a GitHub token sent with curl was stored with a redacted title and a redacted (encrypted) stack trace.
  - Ledger expiry deleted exactly the 5 rows older than 24 h.
  - Tests: 331 pass against a freshly migrated Postgres (21 migrations) and Redis, 327 + 4 skipped without them. New: claim filtering and in-batch duplicates, a live re-send and rollback test, a live export test, server scrubbing tests. The new live tests fail on the old code.
- **Noted, not changed:** `traffic` keeps one row per project per minute, and the minute a deploy starts in keeps the previous deploy's id. The detector selects traffic by time window, so verdicts are unaffected. No warehouse export target was added.


### 75. Demo traffic for a hosted demo, and a verdict window fix it exposed
- **Demo:** `server/app/demo.py` (`python -m app.demo`, or the `demo` Compose profile) is a pretend shop that uses only the public API, like a real app with the SDK and the GitHub Action. It sends 30 minutes of past traffic, a healthy deploy, a bad deploy after 5 minutes, the fix 10 minutes later, then a deploy every 30 minutes (healthy, healthy, bad, fix). Each bad deploy sends a real one-line diff, the file content and CODEOWNERS; its errors carry a stack trace through the changed line. The bug rotates between three files. Heartbeats report each minute's requests and errors. `docs/DEMO.md` covers a 5-minute local try and a 24/7 demo for testers (share only the demo project's read key; use a server with no real data).
- **Bug found by the first demo run:** a deploy's analysis window (30 minutes after it) did not stop at the next deploy, and the new-issues query had no end time at all. A healthy deploy followed 1 minute later by a bad one was given `watch` with "3 new error issues"; in a live test, the earlier deploy got `rollback` for 25 errors that only the later deploy caused. Small teams often deploy several times in 30 minutes, so their earlier deploys were blamed for later ones. The window now ends at the next deploy to the same environment (`next_deployed_at`, computed in the detector's deploy query), and new issues must first appear inside the window. Events explicitly tagged with a deploy's release still count for it after the next deploy (a rolling deploy).
- **Verification:**
  - Live test `test_verdict_window.py`: deploy B 5 minutes after A introduces a new error with 25 events. B is `rollback`, A is not and does not mention new errors; the same data without the cap reproduces the old `rollback` for A. The test fails on the old detector.
  - Demo on the live harness, fresh project, short timings (bad deploy after 2 min, fix 3 min later): the healthy deploy was `safe` (first and final verdict), the bad deploy `rollback`, the fix `safe`; the incident resolved by itself. Slack received "Possible regression … app/billing.py:42 (this line was changed in this release) … Owners: @demo-shop/payments" about 1 s after the first errors, the AI reply in the thread, "Confirmed regression", then the verdicts. Before the window fix (and without history), the same healthy deploy got `watch`. The demo kept running through an API restart (one failed batch, logged).
  - `test_demo.py` (9 tests): schedule, errors only from the live bug and pointing at the changed line, heartbeat errors equal the errors sent, unique event ids, ≤100 events per request, history baseline, `--once` stops, no key → exit 2.
  - Compose renders the `demo` service with and without `DEMO_INGEST_KEY`; without it the demo exits with a message instead of failing `docker compose config` for everyone.

### 76. REST API contract check, and what it found
- `scripts/api_contract_check.py` reads the running API's `/openapi.json` and calls every operation (47) with no key, an unknown key, the wrong key type, a valid key, out-of-range or invalid parameters, malformed JSON bodies, and project A's key on project B's incident and issue ids. It checks status codes, that no call returns 5xx, and that every error is JSON with `detail`. Endpoints that change data only get the auth and malformed-body checks, so it is safe against real projects.
- **Found and fixed:**
  1. **`GET /v1/analytics/export.csv?metric=overview` returned 500.** The export calls the other route functions directly, so their unpassed parameters were FastAPI `Query(...)` objects, not defaults (`env=Query("prod")` reached Postgres). Its mocked test passed because mocks ignore arguments. Every parameter is now passed explicitly; an AST scan found no other direct calls to route functions.
  2. **The dashboard's two CSV buttons downloaded a file saying "Unknown metric"** (HTTP 200): they request `verdicts` and `ttf`, which the export did not support. Both are now exported, and `metric` is a fixed choice, so an unknown metric is a 422 instead of a 200 with an error row.
  3. **`GET /api/incidents/{id}/timeline` returned 200 `[]`** for an incident that does not exist or belongs to another project (row-level security hid the data, but the answer was wrong). It is now 404, like the other incident endpoints.
  4. **Admin endpoints answered 403 for a missing or wrong admin key.** They now answer 401 for that, and 403 only when `ADMIN_API_KEY` is not set (admin endpoints disabled).
- **Verification:** 279 checks, 0 failures against the live harness API with an admin key configured (before the fixes: 9 failures, of which 2 were wrong sample values in the script). New live tests for all five CSV metrics, the unknown-metric 422 and the timeline 404 fail on the old code. The dashboard's CSV downloads, fetched through its proxy, now return real rows.

### 77. Tools and versions audit: upgrades, and the bugs they exposed
- **How it was checked:** every pinned Python package against PyPI and the OSV vulnerability database (all 43 installed packages, not just the direct ones), the dashboard lock file with `npm audit`, and runtimes against endoflife.date. Upgrades were built in a separate environment and verified there with the full live run (pytest with Postgres and Redis, the 13-check pipeline script, the 279-check REST contract script, a demo story, security, evals, chaos) before the pins changed.
- **Python (server):** fastapi 0.115.0 → 0.162.0 (Starlette 0.38.6 → 1.x: 3 high and 4 moderate advisories), uvicorn 0.30 → 0.54, pydantic 2.8 → 2.14, asyncpg 0.29 → 0.32, redis 5.0 → 8.1, anthropic 0.34.2 → 2.3.0, httpx 0.27 → 0.28, python-dotenv 1.0.1 → 1.2.4 (advisory). The old Anthropic SDK pulled in `tokenizers` and `huggingface_hub`, which are now gone from the image. pytest and pytest-asyncio moved to `requirements-dev.txt` (the image no longer ships them; CI installs the dev file).
- **PyYAML was undeclared.** `metering.py` imports it, but it was installed only as a dependency of `huggingface_hub`; upgrading the Anthropic SDK would have made the API crash on start. It is now pinned.
- **AI investigations could not work with `LLM_PROVIDER=anthropic`.** The default model `claude-3-5-sonnet-20241022` was retired on 2025-10-28 (and `claude-3-haiku-20240307`, in the price table, after it). The brief's `LLM_MODEL_FAST` / `LLM_MODEL_DEEP` now exist: `claude-sonnet-5-5` for automatic investigations and deploy summaries, `claude-opus-5-5` for "Dig deeper" (which previously only raised max_tokens). `LLM_MODEL` is still read as the fast model, and a retired model is reported at startup. Effort is configurable (`LLM_EFFORT_FAST` medium, `LLM_EFFORT_DEEP` high).
- **The client was written for older models.** It read `response.content[0].text`, but current models always think, so the first block can be a thinking block; it now joins the text blocks. `max_tokens` (1000 / 1500 / 300) left no room for thinking; it is now 8000 / 16000 / 4000. A refusal returns no text (logged with its category), and requests opt into the server-side refusal fallback (`fallbacks="default"`). With SDK 2.x the key is passed only when set (a client holds one credential) and is sent as a bearer token on API version 2026-10-21; a mock-transport test checks the exact request.
- **AI cost metering was invented.** Every investigation was stored and billed as 500 input / 250 output tokens. The real usage from the response is now stored and metered. Prices are the current ones (USD per million tokens: Sonnet 5.5 2/10, Opus 5.5 4/20, Haiku 5.5 0.10/0.50); an unknown model is priced at the highest configured rate, with a warning.
- **Floats in money math** (against the project rule): LLM cost multiplied token counts by float prices, and the cost-plus markup by a float margin. With a 1.1 margin, about 1 in 18 charges was one micro-dollar too high (100 × 1.1 → 111). Both now use exact decimals; the usage forecast uses integer division.
- **Dashboard: Next.js 14 was end of life (2025-10-26)** with 27 advisories, 3 of them critical remote code execution in the image optimizer. Now Next.js 16.4.3, React 19.3, ESLint 9 with the flat config (`next lint` no longer exists), TypeScript 5.9, `@types/node` 24, recharts 2.15.4 (supports React 19), Tailwind 3.4.19. `npm audit`: 8 findings (1 critical, 4 high) → 3 moderate, all in Tailwind 3's build-time CSS parser, which only reads our own stylesheet. React's new hook rules flag the pages' load-saved-key-after-mount pattern (localStorage does not exist during server rendering); that rule is a warning, not an error.
- **Runtimes:** Postgres 16 (supported to 2028-11), Redis 7.4 (security fixes to 2029), Python 3.12 (to 2028-10), Node 24 LTS (to 2028-04), Caddy 2: all current.
- **Bugs found while checking the upgraded stack end to end:**
  1. **Time to first alert was overwritten.** Each alert for an incident recomputed `first_alert_ms` and `alerted_at`, so the "confirmed" alert ~35 s later replaced the real first-alert time: the incident page, Trust page ("Alert speed 38.8 s"), analytics and digest showed ~38 s where Slack had the alert in ~4 s. Only the first alert sets them now (a live test fails on the old code: 37,002 ms vs 2,000).
  2. **Alert links pointed at `http://localhost:3000`** on every install. Links now use `DASHBOARD_URL`, or `https://PUBLIC_HOSTNAME`, or localhost for a local install. Caddy now also routes `/a/*` (signed action links) to the API.
  3. **The operations panel showed placeholders as facts.** It said the AI investigator and calibration were "Not enabled… arrive in M5/M6" (both exist), counted outbox rows with status `delivered` (the outbox writes `sent`) so delivery always showed "100%", read ingest counts from a table nothing writes, and the dashboard filled missing values with "Data Flowing", "1 active source" and "< 5.0s". It now reports real received/stored counts, deliveries, sources (a stopped demo app showed as "1 silent"), investigations and calibration. Events the API turns away (rate limit, backpressure) are counted per project and UTC day in Redis and shown there and in `/v1/status`. A 429 now carries `Retry-After`.
  4. **The owner page showed made-up "public signals"** (142 GitHub stars, 4,200 Docker pulls) for a brand-new repository; it now says "Not available" unless a collector provides real numbers.
  5. **Owner pages never loaded on a normal install:** they required an explicit API address (the default is the dashboard's own), sent the admin key to project endpoints that need the read key (telemetry status, opt-in, preview), and the margin page refetched on every keystroke.
  6. The demo's code excerpts now show realistic code around the changed line.
- **Verification:** 358 tests pass against live Postgres and Redis on the upgraded stack (security 12/12, evals 12/12, chaos 0 lost); `verify_pipeline.py` 13/13; `api_contract_check.py` 279/279; a demo story gave safe → rollback → safe with the alert ~1 s after the errors arrived; the Next 16 dashboard was built exactly as the Dockerfile does (clean `npm ci`, standalone server), all 9 routes returned 200, and every page was used in a browser with no console errors. New tests fail on the old code where they guard a bug.


### 78. Managed Postgres: system jobs saw no data; export and purge need the admin key
- **Found by testing a database owned by a user that is not a superuser** (the normal case on managed Postgres such as RDS, Cloud SQL, Supabase or Neon; the Docker default connects as a superuser, which hid this):
  1. **Fresh installs could not migrate** when the `changewatch_tenant` role already existed in the cluster (roles are cluster-wide, so a second ChangeWatch database is enough): migration 018 tried to `GRANT` it and failed. The grant is now skipped with a notice when not allowed; FORCE row-level security still applies to a non-superuser owner.
  2. **Every cross-project job saw zero rows.** Row-level security is FORCEd, so it applies to the table owner, and its policy matches nothing when no project is set. The detector's deploy scan, outbox delivery, retention, escalations, auto-resolve and the watchdog all read across projects, so on managed Postgres there would be no verdicts and no alerts, silently. Measured: a tenant transaction saw its deploy, the system view saw 0 deploys and 0 outbox rows.
- **Fix:** migration 022 gives every row-level-security table a second permissive policy that matches when `app.system_scope = 'all'`. Pool connections start in that scope (a startup setting, so it costs no query and survives the pool's reset), and `project_transaction` / `read_project_transaction` clear it while setting the project. Tenant work still sees only its project; system jobs see all, as they already did on the Docker default. A live test checks every RLS table has the policy, so a table added later without it fails CI.
- **Verified end to end** on a database owned by a non-superuser, non-BYPASSRLS user: migrations apply; each tenant sees only its own deploy while system jobs see both; with API, worker, detector and notifier running against it, a demo story produced the SAFE verdict, the instant alert 6 s after the bad deploy, the confirmed alert and the ROLLBACK verdict.
- **Export and purge:** `DELETE /v1/project-data` (wipe a project) and `GET /v1/export` accepted the dashboard read key alone, which `docs/DEMO.md` tells owners to share with testers. Both now need the admin key as well (the read key still says which project), as the brief specifies. The read key's real scope is now described correctly in the Settings page, DEMO.md, SECURITY_MODEL and the foundation doc: it also allows team actions and this project's settings and budget (left as is: a product decision for the owner).
- **Cleanup:** `sources.is_source_silent` no longer imports `unittest.mock` to spot test doubles; the tests patch it explicitly.

### 79. A retired model in an existing .env is replaced, not left failing
- **Found by running the investigator with `LLM_PROVIDER=anthropic` against a local stand-in for the Messages API** (`ANTHROPIC_BASE_URL`, no real calls): the request still named `claude-3-5-sonnet-20241022`. Every install made from the old `.env.example` has `LLM_MODEL=claude-3-5-sonnet-20241022`, and the new defaults never applied. The retired-model warning was also logged only by the API process, not by the investigator or detector that make the calls.
- **Fix:** a configured model from a retired family (Claude 3.x and earlier) is replaced by the current default (`claude-sonnet-5-5` fast, `claude-opus-5-5` deep), and the API, detector and investigator each log which variable named it and what is used instead.
- **Verified live:** with the old `.env`, the investigator logged the replacement, sent `claude-sonnet-5-5` with `max_tokens` 8000, effort `medium`, `fallbacks: "default"`, API version 2026-10-21 and a bearer key, parsed the thinking + text reply, stored the real 4,321 / 876 tokens and metered 17,402 micro-USD (4,321 × 2 + 876 × 10). Before the fix the same run sent the retired model (priced at the highest configured rate, 34,804).

### 80. Ingest robustness: one bad event, an outage or a second environment no longer costs other events
- **Found by an independent review and reproduced live** (three cases, each batched with another tenant's valid event): a request path with a NUL byte (Postgres text cannot store it), a NaN `occurred_at` (accepted by the API, crashes the worker), and prod plus staging heartbeats in the same minute (the traffic upsert failed with "cannot affect row a second time"). In all three, the whole batch was retried three times and moved to `events:dead`, including the other tenant's good event, which was never stored. A short Postgres outage did the same to every event received meanwhile: connection errors counted toward the three tries, with no backoff.
- **Fixes:**
  - A failed batch is now processed one event at a time, so only the event that fails is retried and dead-lettered.
  - Database or network errors (connection lost, restart, too many connections, deadlock) are not counted: the batch goes back unchanged and the worker backs off from 1 s to 30 s.
  - `python -m app.cli requeue-dead` replays dead-lettered events; nothing could before.
  - The worker strips NUL characters before storage. The API rejects NaN or infinite timestamps and durations, and counts above the integer column.
  - The 422 response no longer echoes submitted values: it failed with a 500 on NaN, and could reflect secrets.
  - Database errors inside the batch transaction are no longer swallowed. The incident check and decision persistence run in savepoints; previously a swallowed error aborted the transaction, COMMIT silently rolled back, and the batch was still acknowledged.
  - Event rollup errors now fail the batch instead of being ignored.
- **Environments kept apart (migration 023):** events get `environment` and `type`; traffic is keyed by (project, environment, minute). The detector counts only the deploy's own environment, so a staging deploy's errors or heartbeats no longer change a prod verdict. Errors are told from slow requests by `type`: the old test (`stack_trace IS NOT NULL`) dropped every HTTP error once stack traces were stored only encrypted. The timeline sums environments; export includes them.
- **Also fixed:**
  - Incidents counted batches, not events (1,000 errors in 5 batches showed as about 24); they now add the batch's event count.
  - Slow-request issues no longer open incidents (they only affect the verdict, as the single-event path already did).
  - A redeployed SHA (a rollback to it) is matched to its latest deploy at or before the event, not an arbitrary one. The cache is checked against the event time.
  - Mute rules now apply: the incident is recorded but no alert or AI investigation is sent. `is_issue_muted` had no callers.
  - Spike counters and stored-event metering are updated only after the batch commits; the stored-events meter was never recorded by the batch path at all.
  - Dead-letter logs carry ids only, not the payload.
  - The single-event path now uses the batch code, removing ~200 duplicated lines that had already diverged.
- **Verification:**
  - Live tests: a bad event next to a good one; NUL storage; prod and staging heartbeats in one minute; redeployed SHA; a prod verdict that ignores staging errors while the staging deploy gets `rollback`, with encrypted HTTP errors counted; NaN gets 422 without echo; a transient error is not counted. 369 tests pass against Postgres 16.
  - On the live harness, the three reproductions now store every valid event and dead-letter nothing new. Replaying the 7 events dead-lettered earlier recovered 6 (all of the other tenant's); the NaN event queued before the API fix failed alone.
  - `verify_pipeline.py` 13/13; `api_contract_check.py` 281/281.

### 81. SDK, security and end-to-end fixes from the review (and what testing the real customer path found)
- **SDK (runs inside customers' apps):**
  - One `extra` value that JSON cannot encode (a UUID, a datetime) made every later send fail. The batch was never cleared, so it grew forever and nothing was sent again; reproduced live, the next error never arrived. The batch is now always cleared, and `extra` is made JSON-safe (unknown types become strings) and capped at the server's 10 KB.
  - Events older than the server's 24 h limit are left out and counted, so they no longer make the server reject a whole batch.
  - Exception text is cut to size before scrubbing.
  - The email pattern scanned long runs like `a-a-a-...` from every position. That is quadratic: 0.7 s per 40 KB, blocking the customer's app and, in the server copy, the worker. A lookbehind makes it linear (0.35 ms, same matches on normal emails).
  - Queue-full drops are counted.
  - The three new SDK tests fail on the old code (the old suite took 3 minutes because of the regex).
- **Security:**
  - The auth cache and rate limiter used raw API keys in Redis key names, readable via SCAN/MONITOR and saved to the append-only file. They now use the key's SHA-256.
  - Telemetry opt-in and preview (install-wide, with cross-tenant counts) accepted any project's read key; they now need the admin key. The response says the switch lasts until restart (`TELEMETRY_ENABLED` in .env keeps it).
  - A process that started before migration 018 created the restricted role never used it until restarted. It now checks again every 30 s while the role is missing.
  - Export and purge name the project in every statement instead of relying on row-level security alone.
  - Concurrent first encryptions for a project could create different data keys and save only one, leaving some data undecryptable (reproduced live: 8 concurrent calls, 2 keys). The key is now saved only if none exists, and everyone adopts the saved one.
  - Every service refuses to start without a valid master key (there was no check, despite the error text). `encrypt_or_keep` raises instead of storing plaintext; the worker's retries and `requeue-dead` keep the data.
  - The investigator now uses the same redacting JSON logs as the other services.
- **Verdicts and the detector:**
  - One failing deploy no longer stops the rest of the cycle (auto-resolve, escalation, outbox).
  - The AI explanation is requested once per deploy, not every cycle while the provider fails.
  - "AI investigation paused: budget reached" is no longer stored as the deploy's likely cause.
  - Auto-resolve no longer stalls once 50 incidents with a silent app pile up: they are filtered in the query, oldest first.
- **The GitHub Action sent only the diff.** The server stored changed files only when a client sent them, and the Action never sent CODEOWNERS. So for real users (not the demo), alerts could never say "this line was changed in this release" or name an owner.
  - The server now derives changed files and exact added line ranges from any diff (`app/diffparse.py`), and scrubs the stored diff.
  - The Action is now a small standard-library Python script (`action/notify.py`). It honours `exclude` and `mode` (both were ignored), adds default exclusions (`.env`, keys, `secrets/**`), and sends changed files with line ranges, CODEOWNERS and the commit author's email; `full` mode adds file contents. Inputs reach it through the environment instead of being pasted into shell code. A shallow checkout gets a warning to use `fetch-depth: 2` instead of a whole-repository diff.
- **A resolved error that comes back was never alerted** (issues first seen before a deploy were skipped). The brief's "regression flag when a resolved issue returns" now works: if the issue's last incident was resolved before the deploy and the error stayed quiet until then, its return opens an incident, and the alert says the error is back.
- **Also:** validation errors (422) no longer echo submitted values.
- **Verification (live, real customer path):**
  - A throwaway git repo (CODEOWNERS, a change to `app/billing.py` line 42, a change under `secrets/`) was run through `action/notify.py` against the harness API. It registered one changed file with range [[42, 42]] (`secrets/` excluded), the author's email, encrypted content and CODEOWNERS.
  - Errors from that line with the commit SHA produced: "Possible regression … at app/billing.py:42 (this line was changed in this release) after deploy fc578519 by dev@acme.example. This error was resolved before and is back. Owners: @acme/payments", then the AI reply in the thread through the real Anthropic SDK against a local stand-in API.
  - 380 tests pass against Postgres 16, plus security 12/12, evals 12/12 and chaos. `verify_pipeline.py` 13/13; `api_contract_check.py` 280/280. Dashboard lint has 0 errors and the build passes.

### 82. Ephemeral mode can read files from GitHub (the fallback never worked)
- With `CODE_MODE=ephemeral` the server stores no file contents, and the investigator was meant to read a changed file from GitHub with `GITHUB_READ_TOKEN`. It imported a function that does not exist (the ImportError was swallowed), and deploys never recorded their repository anyway.
- Deploys now accept `repository` ("owner/name", validated; migration 024), and the Action sends `GITHUB_REPOSITORY`. The investigator reads the file at the deploy's commit through the existing read-only client (GET only) and scrubs it before use. Tests check the request (owner, repo, path, ref = commit) and that a password in the file is scrubbed.

### 83. An end-to-end test of the real Compose stack in CI; migrations take turns
- **New CI job `e2e`** builds and starts the Docker Compose stack exactly as users run it, plus a webhook receiver standing in for Slack (`docker-compose.e2e.yml`, `scripts/e2e_receiver.py`). It uses fresh random secrets and short verdict timings, then runs:
  - `scripts/e2e_check.py`, the customer journey: two projects created with the CLI, baseline traffic, a deploy of a real git change through the GitHub Action script, errors from the changed line. It expects the instant alert (file:line, "changed in this release", CODEOWNERS owner, commit author), the AI investigation reply and a rollback/watch verdict at the receiver. Then it checks the read and verdict APIs, tenant isolation, that the ingest key cannot read, every dashboard page and its API proxy, the export (admin key), and that re-sent events are not counted twice.
  - `verify_pipeline.py` inside the API container (it needs the database) and `api_contract_check.py` against the same stack.
  - Service logs are printed only when a step fails; the stack is removed afterwards.
- **Fresh installs: the two API processes migrated at the same moment.** Compose starts uvicorn with two workers, and each applied the migrations on startup; the slower one failed with "duplicate key … pg_type_typname_nsp_index" and skipped the rest of its startup work. Migrations now hold a Postgres advisory lock, so processes take turns and the second finds nothing to do. The new live test starts three runners on an empty database; it fails on the old code and passes now.
- **Verification:** `e2e_check.py` passed 29/29 twice against the local harness (once with the AI investigator on the real Anthropic SDK against a local stand-in API, once in the built-in `fake` mode CI uses). Docker is not installed on this Mac, so the Compose job itself runs for the first time on GitHub.

### 84. AI investigations are queued after the incident is saved; an end-to-end walk-through
- **The AI reply could be lost.** The worker queued the investigation job in Redis inside its database transaction. The investigator picks jobs up within milliseconds, so it could look for an incident that was not committed yet, fail its three quick retries and drop the job. On a busy worker (large batches, slow commits) the team would get the alert but never the AI reply. A rolled-back batch also left a job pointing at an incident that never existed. Jobs are now collected during the batch and queued after the commit (and only for incident checks whose savepoint succeeded). A new live test checks, from a separate connection at the moment the job is queued, that the incident is visible; it failed on the old code.
- **`docs/ARCHITECTURE.md`** follows one bad deploy from push to rollback verdict: each process, what it stores, the verdict rules with their defaults, what happens when each part fails, and which test covers which step. Linked from the README.

### 85. Bug hunt, part 1: ChangeWatch watching itself, delivery, incident lifecycle
An independent bug hunt (10 reviewers, then a critic, then three checks per finding) read `main`. About 20 of its 70 findings were already fixed on this branch (#80 to #82). These were not; each fix has a test that fails on the old code.
- **A dead detector looked "recovered".** Its heartbeat expires after 5 minutes, and a missing heartbeat counted as "degraded", which cleared the operator alert with a false "Recovered" while the detector was still dead (and a detector that never started was never reported). A missing heartbeat is now "down".
- **"Notification deliveries are failing" could never fire.** It counted rows marked `failed` among those created in the last 15 minutes, but a row is marked failed only after about 2.5 hours of retries. It now counts messages of the last hour that failed 3 or more times.
- **An operator alert that failed to send was lost** (the row was recorded, so it was never sent again). An unsent alert or recovery is now retried on the next cycle.
- **Tool uptime was always 100%**: the watchdog added a row every minute and never set `ended_at`. Rows are now stretches of time (opened when a component stops being ok, closed when it recovers); migration 025 closes the old rows as one-minute observations. Uptime counts ongoing outages and counts overlapping ones once.
- **A crash mid-batch re-sent every alert of the batch** (up to 49 duplicates): 50 rows were sent inside one transaction and committed at the end. Each message is now committed right after its send.
- **One unreachable channel delayed every project's alerts** (30 rows × 10 s timeouts in the same locked batch). Up to 4 messages go out at once, at most one per channel, so a slow channel holds up only its own messages.
- **Discord messages over 2,000 characters were rejected** and retried until they failed (long AI replies). Content and embed fields are clipped to Discord's limits.
- **Auto-resolve closed incidents whose errors were still firing.** It looked only at `incidents.last_event_at`, which stops moving when the deploy is 2 hours old or a newer deploy takes the events. It now also requires the error itself to be quiet in that environment.
- **A resolved incident that fired again stayed resolved, silently.** It is now reopened as confirmed and the team is alerted ("resolved before and is back"); the alert and the next "Resolved" message get their own dedupe keys so the outbox does not drop them as repeats.
- **Release-tagged events had no end time in the verdict.** Events linked to a deploy by `release` were counted whenever they happened, even after the next deploy; every "after" query now ends at the window's end.
- **A slow AI provider held up verdict alerts.** The optional "likely cause" call could take over 2 minutes per flagged deploy, in the loop that sends every project's verdicts. It now gives up after 20 seconds and the verdict goes out without it.
- **An error from a background job ended a silence** with a false "Data is flowing again" while the web app sent no traffic. Only heartbeats end a silence now.
- Also: a CLI test only passed when an earlier test had left the database pool open; it now stubs encryption itself.

### 86. Bug hunt, part 2: secrets in logs and text, key rotation
- **Credential URLs were logged in full.** httpx logs every request with its URL at INFO, and the log filter only masked Slack URLs and URLs containing "webhook". Teams workflow URLs (`…&sig=…`), Zapier hooks and the operator webhook appeared in plain text on every delivery. httpx request logging is off, and every URL in a log line is now cut to `scheme://host/******`. Email addresses are masked in logs and delivery errors (SMTP errors quote the refused recipients, which were stored in `outbox.last_error` and sent to the operator).
- **The secret scrubber missed common forms**: prefixed keys (`DB_PASSWORD`, `client_secret`, `refresh_token`, `STRIPE_SECRET_KEY`, `AWS_SECRET_ACCESS_KEY`), quoted keys (`"password": "…"`, `{'password': '…'}`), query strings (`client_secret=…&refresh_token=…`), Bearer tokens, `github_pat_…`, Stripe and Slack tokens. These reached issue titles, stored traces and the AI provider. One pattern now covers them in the server scrubber and the log filter, with the same copy in the SDK. It stays linear on long inputs, keeps a quoted value's quotes, keeps specific labels (`GH_TOKEN=ghp_…` still says GitHub token), and leaves its own output alone when run again.
- **`rotate-project-key` destroyed data.** It replaced the project's key without re-encrypting anything, while the running services kept using the old key from memory. Every encrypted record became unreadable, including the alert webhook, so alerts were then marked delivered without being sent. Safe rotation needs versioned project keys, so for now the command refuses, explains why and points to master-key rotation. SECURITY_MODEL.md says so.
- **`rotate-master-key` could not be resumed**: a project already re-wrapped by a run that stopped part-way made the next run crash. Such keys are now skipped (and a key that neither master key opens still stops the run).

### 87. Bug hunt, part 3: the SDK and its packaging
- **The documented `pip install` of the SDK failed**: setuptools found two top-level packages (`changewatch` and `bench`) and refused to build. `pyproject.toml` now lists the package, the SDK has its own short README, and CI installs the SDK the way users do.
- **Events were lost at exit.** Only the queue was flushed: the batch the background thread already held (a crash in the first 2 seconds, the last errors before SIGTERM) died with the thread, and a pending Retry-After pause dropped everything. At exit the worker now hands its batch back and everything is sent once, giving up at the first failure so a dead server cannot hold up shutdown.
- **Redirects were followed with the API key.** urllib re-sends headers (including `X-API-Key`) to the redirect target, even over plain HTTP, and turns the POST into a GET without the events. The SDK no longer follows redirects and logs a warning to fix the endpoint.
- **Long environment names broke whole batches.** Events allowed 20 characters, deploys 50, and heartbeats were never cut, so an environment like `production-eu-central-1` (23) made every batch with a heartbeat fail with 422, taking the errors in it along. Events now allow 50 like deploys, and the SDK cuts release and environment once at start.
- **Request paths and nested `extra` values were not scrubbed** (emails or tokens in `/users/<email>/reset`, dicts inside lists). They are now, and a path is cut again after scrubbing because markers can be longer than what they replace.
- **A heartbeat that did not fit in a full queue lost its counts**; they are now carried into the next heartbeat.

### 88. Bug hunt, part 4: billing, ratings, dashboard, operations
- **Billing:**
  - A failed or unusable AI answer (outage, timeout, refusal, cut-off JSON) was billed like a real one. It is now stored as `partial` (the rules-based answer is still shown, and a later run may still get the AI) and records no usage.
  - Two investigations of one incident at the same moment (the queued job and an API or "Dig deeper" call) were both billed. They now take turns per incident, and the second reuses the first result.
  - Two usage records at the same moment could both take the last free unit. Free-unit accounting now holds a lock per project, meter and month.
  - Stored-event usage that arrived after its minute was flushed was silently ignored (same idempotency key), and a database error after the Redis delete lost the count. Each flush is now its own record, written before the count is taken off Redis.
  - A price book published with a future `effective_from` applied at once (also to new price locks). It now applies from its date.
  - Budget warnings (50/80/100%) were never sent: the function existed but nothing called it. They are now sent when spend crosses a threshold, as a readable message to the project's channels (they used to have no text the chat formatter could show), with amounts formatted without floats.
  - The dashboard read budget fields the API does not return, so the "AI Investigations Paused" banner never appeared; saving limits reset custom warning levels; dollar input used floating point. All three are fixed (the dollar conversion is exact).
- **Ratings and actions:**
  - The incident page's Confirm/Resolve/Dismiss buttons only changed the page, so nothing was saved. They are now "I'm on it", Resolve and Reopen through `POST /api/incidents/{id}/actions`, plus Real / False alarm / Unclear ratings through `/feedback`, and the page reloads what the server has.
  - The feedback endpoint accepted another project's incident id (and told you whether it existed). Both endpoints now answer 404 outside the caller's project, and 422 instead of 500 for an unknown action or rating. The feedback joins also check the project.
  - "Alerts that were real" was always computed from an empty list, and the dashboard read the wrong key for it.
- **Dashboard:**
  - The timeline counted slow requests as errors and made up request counts (a newest bucket at about a 100% error rate); requests now come only from heartbeats.
  - The incident and analytics pages now say a read key is needed, or that it was rejected, instead of loading forever or showing an empty project.
  - Auto-refresh no longer resets the selected deploy every 15 seconds; trust suggestions show their text; evidence shows its kind; the metrics link goes to the document on GitHub.
- **Operations:**
  - The Caddyfile used `tls internal` inside `handle`, which Caddy rejects, so the documented HTTPS profile could not start. Caddy already uses its internal CA for localhost, so the block is gone, and CI validates the Caddyfile with the official image.
  - `API_WORKERS` is now used (`python -m app.serve`; compose had a fixed `--workers 2`).
  - `cleanup-retention` also cleans code by default after `CODE_RETENTION_DAYS`, as its help said.
  - The default price book is in the server image, so the documented publish command works.
  - The DEMO.md delete command and the README quick-start key are corrected.
- **Verification:** 445 tests pass against Postgres 16 (63 new in parts 1 to 4, each new test failing on the old code), security 12/12, evals 12/12, chaos. Dashboard lint has 0 errors and the build passes.
