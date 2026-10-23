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
