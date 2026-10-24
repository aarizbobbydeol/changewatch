# ChangeWatch — Stage-by-Stage Verification Guide

ChangeWatch is an open-source, self-hosted developer tool for small Python teams. When you ship a release and something breaks, ChangeWatch detects anomalies, determines a deterministic **release verdict** (`safe`, `watch`, `rollback`, or `no_data`), explains the root cause using AI (or local Ollama), alerts your team on Slack, and provides a timeline dashboard.

---

## What It Does & Does Not Do

- **What it does:**
  - Ingests release deploys from GitHub Actions.
  - Ingests application exceptions, slow requests, and minute-level traffic heartbeats via a zero-dependency Python SDK.
  - Evaluates deterministic release verdicts at 10-minute and 30-minute checkpoints.
  - Generates AI root-cause analysis (via Claude or local Ollama) with strict secret scrubbing.
  - Alerts Slack with concise verdict summaries.
  - Renders a clean release & error timeline on a self-hosted dashboard.
- **What it does NOT do:**
  - No billing, user accounts, or subscription management.
  - No telemetry: data is never sent anywhere except your configured LLM provider and Slack webhook.
  - No code review of pull requests.

---

## Privacy & Telemetry Notice

- **Telemetry is Strictly Opt-In (Off by Default):** ChangeWatch never transmits telemetry by default (`TELEMETRY_ENABLED=0`). If disabled, zero network calls occur.
- **Strict Allowlist Guarantee:** If explicitly enabled by the operator, only coarse fleet metadata is transmitted: anonymous install UUID, application version, runtime mode (docker/standalone), host OS family, configured AI provider, alert channel types, and bucketed release/incident counts (e.g. "1-10", "11-50").
- **Zero Customer Code or PII:** By hard architectural constraint (`extra="forbid"` and keyword enforcement), ChangeWatch **NEVER** collects, stores, or transmits:
  - Customer source code or git diffs
  - Repository names, organizations, or branch names
  - File paths or directory trees
  - Error messages, exception details, or stack traces
  - User emails, usernames, or account IDs
  - IP addresses or hostnames
  - API keys, webhooks, or credentials
- **Transparent Preview:** You can inspect the exact JSON payload at any time via `GET /v1/telemetry/preview` or via the Owner View modal.
- **Local LLM Execution (`LLM_PROVIDER=ollama`):** With Ollama, all root-cause reasoning executes locally; zero customer data leaves your servers.
- **Anthropic Claude (`LLM_PROVIDER=anthropic`):** Automated secret scrubbing strips API keys, tokens, and passwords before diffs and traces are sent for AI analysis.
- **Rules-First Decisions (`DECISION_PROVIDER=rules`):** Triage and release safety evaluation run 100% deterministically on-premise without external cloud dependencies.

---

## Quick Start (Under 5 Commands)

```bash
# 1. Clone repository
git clone https://github.com/aarizbobbydeol/changewatch.git && cd changewatch

# 2. Configure environment
cp .env.example .env

# 3. Start the full stack (Postgres, Redis, API, Worker, Detector, Dashboard)
docker compose up -d

# 4. Create your first project and receive your API key
docker compose exec api python -m app.cli create-project "my-first-app" --slack-webhook "https://hooks.slack.com/services/..."
```

---

## Local Development (Postgres & Redis Only)

If you prefer running services locally on your host machine:

```bash
# 1. Start Postgres and Redis only
docker compose -f docker-compose.dev.yml up -d

# 2. Activate Python 3.12 virtual environment and install requirements
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt

# 3. Create a project
PYTHONPATH=server python -m app.cli create-project "my-test-app"

# 4. Start the API
PYTHONPATH=server uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://...` | PostgreSQL 16 connection URL |
| `REDIS_URL` | `redis://...` | Redis 7 connection URL |
| `LLM_PROVIDER` | `anthropic` | LLM provider: `anthropic`, `ollama`, or `fake` |
| `ANTHROPIC_API_KEY` | `""` | API key for Anthropic Claude |
| `LLM_MODEL_FAST` / `LLM_MODEL_DEEP` | `claude-sonnet-5-5` / `claude-opus-5-5` | Anthropic models (fast investigations / "Dig deeper") |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama HTTP endpoint |
| `OLLAMA_MODEL` | `llama3` | Ollama model tag |
| `RATE_LIMIT_PER_MIN` | `600` | Max ingest events per minute per API key |
| `VERDICT_FIRST_MIN` | `10` | Minutes after deploy for first checkpoint |
| `VERDICT_FINAL_MIN` | `30` | Minutes after deploy for final checkpoint |
| `VERDICT_ROLLBACK_NEW_ISSUE_EVENTS` | `20` | Threshold of new issue events for rollback |
| `VERDICT_ROLLBACK_ABS_RATE` | `0.25` | Absolute error rate (25%) for rollback |
| `NOTIFY_ON_SAFE` | `1` | Post Slack alert for safe verdicts |
| `RETENTION_DAYS` | `30` | Days to retain events before cleanup |
| `SEED_DEV_PROJECT` | `0` | If `1`, auto-seeds `cw_test_key_123` on startup |

---

## Verifying Stage 1

### 1. Verify Auto-Migrations & Tables
When the API starts, auto-migrations apply `001_init.sql` and `002_verdict_and_traffic.sql`.
Verify tables exist:
```bash
docker compose exec postgres psql -U changewatch -d changewatch -c "\dt"
```
Tables present:
- `schema_migrations`
- `projects`
- `deploys`
- `issues`
- `events`
- `deploy_analysis` (with `verdict`, `verdict_reasons`, `verdict_final`)
- `traffic` (with RLS)
- `alerts` (with `kind` and `UNIQUE(deploy_id, kind)`)

### 2. Verify Healthcheck (`/healthz`)
```bash
curl -i http://localhost:8000/healthz
```
Expected response:
```http
HTTP/1.1 200 OK
{"status":"ok","postgres":"ok","redis":"ok"}
```

### 3. Verify CLI Project Creation
```bash
PYTHONPATH=server python -m app.cli create-project "verification-app"
```
Outputs newly generated `cw_live_...` API key.

### 4. Verify API Key Authentication
```bash
curl -i http://localhost:8000/v1/auth/verify -H "X-API-Key: <your_new_key>"
```
Expected response:
```json
HTTP/1.1 200 OK
{"ok":true,"project_id":...}
```

### 5. Run the Test Suite
```bash
PYTHONPATH=server pytest server/tests -v
```
All unit tests and integration tests execute cleanly.

---

## Verifying Stage 2: Deploy Hook, Ingest & Worker

### Step 1: Start Services and Worker
In terminal 1 (start Postgres & Redis):
```bash
docker compose -f docker-compose.dev.yml up -d
```
In terminal 2 (start API):
```bash
PYTHONPATH=server uvicorn app.main:app --host 0.0.0.0 --port 8000
```
In terminal 3 (start Worker):
```bash
PYTHONPATH=server python -m app.worker
```

### Step 2: Send a Deploy via curl
```bash
curl -i -X POST http://localhost:8000/v1/deploys \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cw_test_key_123" \
  -d '{
    "commit_sha": "abc1234def",
    "author": "Alice Developer",
    "message": "fix: update payment gateway integration",
    "diff": "diff --git a/payment.py b/payment.py\n+ new code",
    "environment": "prod"
  }'
```
Expected response:
```json
HTTP/1.1 200 OK
{"ok":true,"deploy_id":1}
```

### Step 3: Send an Error Event via curl
```bash
curl -i -X POST http://localhost:8000/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cw_test_key_123" \
  -d '{
    "events": [
      {
        "type": "error",
        "exc_type": "KeyError",
        "message": "missing key: stripe_token in payment.py:45",
        "stack_trace": "Traceback (most recent call last):\n  File payment.py, line 45, in pay\n    token = payload[\"stripe_token\"]\nKeyError: stripe_token",
        "request_path": "/api/checkout",
        "release": "abc1234def",
        "environment": "prod",
        "occurred_at": '"$(python3 -c 'import time; print(time.time())')"',
        "extra": {"user_id": 42}
      }
    ]
  }'
```
Expected response:
```json
HTTP/1.1 200 OK
{"accepted":1}
```

### Step 4: Send a Traffic Heartbeat via curl
```bash
curl -i -X POST http://localhost:8000/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cw_test_key_123" \
  -d '{
    "events": [
      {
        "type": "heartbeat",
        "requests": 250,
        "errors": 1,
        "release": "abc1234def",
        "environment": "prod",
        "occurred_at": '"$(python3 -c 'import time; print(time.time())')"'
      }
    ]
  }'
```
Expected response:
```json
HTTP/1.1 200 OK
{"accepted":1}
```

### Step 5: Verify Database Link in Postgres
Check that the event and issue were created and linked to `deploy_id = 1`:
```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U changewatch -d changewatch -c "
  SELECT e.id AS event_id, e.deploy_id, d.commit_sha, i.title, i.fingerprint
  FROM events e
  JOIN deploys d ON e.deploy_id = d.id
  JOIN issues i ON e.issue_id = i.id;
"
```
Check that the traffic heartbeat was stored:
```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U changewatch -d changewatch -c "
  SELECT deploy_id, bucket, requests, errors FROM traffic;
"
```
Both queries show rows correctly linked to the deploy!

---

## Verifying Stage 3: SDK & Demo App

### Step 1: Start Services & Worker
In terminal 1 (start Postgres & Redis):
```bash
docker compose -f docker-compose.dev.yml up -d
```
In terminal 2 (start API):
```bash
PYTHONPATH=server uvicorn app.main:app --host 0.0.0.0 --port 8000
```
In terminal 3 (start Worker):
```bash
PYTHONPATH=server python -m app.worker
```

### Step 2: Register a Deploy for the Demo App
```bash
curl -i -X POST http://localhost:8000/v1/deploys \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cw_test_key_123" \
  -d '{
    "commit_sha": "demo_commit_sha_123",
    "author": "Demo Engineer",
    "message": "feat: launch new checkout experience",
    "diff": "diff --git a/app.py b/app.py\n+ new feature",
    "environment": "prod"
  }'
```
Expected response:
```json
{"ok":true,"deploy_id":...}
```

### Step 3: Start the Demo App
In terminal 4:
```bash
GIT_SHA="demo_commit_sha_123" CHANGEWATCH_API_KEY="cw_test_key_123" python scripts/demo_app.py
```
The demo app runs on port 8001.

### Step 4: Hit the Demo Routes
In another terminal:
```bash
# 1. Normal healthy request (heartbeat counter)
curl -i http://localhost:8001/

# 2. Crashing route (Unhandled ZeroDivisionError)
curl -i http://localhost:8001/crash

# 3. Slow route (Sleeps 1.6s > 1500ms threshold)
curl -i http://localhost:8001/slow

# 4. KeyError route
curl -i http://localhost:8001/key-error
```

### Step 5: Verify Events in PostgreSQL
Query the events table linked to `demo_commit_sha_123`:
```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U changewatch -d changewatch -c "
  SELECT e.id, d.commit_sha, i.title, e.request_path, e.duration_ms
  FROM events e
  JOIN deploys d ON e.deploy_id = d.id
  JOIN issues i ON e.issue_id = i.id
  WHERE d.commit_sha = 'demo_commit_sha_123';
"
```
Expected output shows the captured errors and slow requests linked to `demo_commit_sha_123`:
- `ZeroDivisionError: division by zero` (`/crash`)
- `KeyError: non_existent_key` (`/key-error`)
- `Slow: /slow` (duration ~1600ms)

---

## Verifying Encryption Stage 1: Envelope Encryption & Key Management

### Step 1: Generate a Master Key via CLI
```bash
PYTHONPATH=server python -m app.cli generate-master-key
```
Outputs a 256-bit URL-safe base64 key:
```
Generated ChangeWatch Master Key (AES-256-GCM):
dGVzdF9tYXN0ZXJfa2V5XzMyX2J5dGVzX2xvbmdfMTI=
Set this in your environment as: MASTER_KEY=...
```

### Step 2: Create a Project with Envelope Encryption
```bash
PYTHONPATH=server MASTER_KEY="dGVzdF9tYXN0ZXJfa2V5XzMyX2J5dGVzX2xvbmdfMTI=" \
  python -m app.cli create-project "secure-enterprise-app"
```
The output confirms `Envelope Security: Enabled (AES-256-GCM)` and generates an encrypted project data key.

### Step 3: Run Master Key Rotation
```bash
# Rotate from old to new master key
MASTER_KEY="bmV3X21hc3Rlcl9rZXlfMzJfYnl0ZXNfbG9uZ18xMjg=" \
MASTER_KEY_PREVIOUS="dGVzdF9tYXN0ZXJfa2V5XzMyX2J5dGVzX2xvbmdfMTI=" \
PYTHONPATH=server python -m app.cli rotate-master-key
```
Re-wraps all project data keys instantly without re-encrypting the underlying database rows.

### Step 4: Run the Cryptography Tests
```bash
PYTHONPATH=server:sdk pytest server/tests/test_crypto.py -v
```
All 8 tests (round trip, tamper detection, AAD project/column isolation, key rotation, crypto-shredding) pass.

---

## Verifying Trust & Reliability Stage 1: Reliable Queue, Deduplication & Outbox

### Step 1: Start Services and Worker
```bash
docker compose -f docker-compose.dev.yml up -d
PYTHONPATH=server uvicorn app.main:app --host 0.0.0.0 --port 8000 &
PYTHONPATH=server python -m app.worker &
```

### Step 2: Verify `event_id` Idempotency (Zero Duplicates)
Send an event with an explicit `event_id`:
```bash
curl -i -X POST http://localhost:8000/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cw_test_key_123" \
  -d '{
    "events": [
      {
        "event_id": "dedupe_test_uuid_001",
        "type": "error",
        "exc_type": "IndexError",
        "message": "list index out of range",
        "occurred_at": '"$(python3 -c 'import time; print(time.time())')"'
      }
    ]
  }'
```
Now send the exact same payload a second time (simulating network retry):
```bash
curl -i -X POST http://localhost:8000/v1/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cw_test_key_123" \
  -d '{
    "events": [
      {
        "event_id": "dedupe_test_uuid_001",
        "type": "error",
        "exc_type": "IndexError",
        "message": "list index out of range",
        "occurred_at": '"$(python3 -c 'import time; print(time.time())')"'
      }
    ]
  }'
```
Query PostgreSQL to confirm exactly 1 row was stored thanks to `ON CONFLICT (project_id, event_id) DO NOTHING`:
```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U changewatch -d changewatch -c "
  SELECT count(*) FROM events WHERE event_id = 'dedupe_test_uuid_001';
"
```
Count returned is `1`.

### Step 3: Verify Durable Outbox Deduplication & Delivery
Check the outbox table schema:
```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U changewatch -d changewatch -c "
  SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'outbox';
"
```

### Step 4: Run the Reliable Pipeline Tests
```bash
PYTHONPATH=server:sdk pytest server/tests/test_reliable_pipeline.py -v
```
All tests pass:
- `test_sdk_event_id_generation`: PASSED
- `test_outbox_delayed_annotation`: PASSED
- `test_outbox_queue_deduplication`: PASSED
- `test_outbox_process_success`: PASSED
- `test_outbox_process_failure_and_backoff`: PASSED
- `test_reliable_queue_reaper`: PASSED

---

## Verifying Stage 4: Spike Detector, Release Verdicts & Alerts

### Step 1: Start Services, Worker & Detector
In terminal 1 (start Postgres & Redis):
```bash
docker compose -f docker-compose.dev.yml up -d
```
In terminal 2 (start API):
```bash
PYTHONPATH=server uvicorn app.main:app --host 0.0.0.0 --port 8000
```
In terminal 3 (start Worker):
```bash
PYTHONPATH=server python -m app.worker
```
In terminal 4 (start Detector):
```bash
PYTHONPATH=server python -m app.detector
```

### Step 2: Run the Deploy Simulator
The deploy simulator tests all release verdict paths:

```bash
# 1. Simulate a bad deploy (25 error events -> triggers rollback verdict)
python scripts/simulate_deploy.py --mode bad

# 2. Simulate a safe deploy (250 requests, 0 errors -> triggers safe verdict)
python scripts/simulate_deploy.py --mode safe

# 3. Simulate a no_data deploy (no events or heartbeats -> triggers no_data verdict)
python scripts/simulate_deploy.py --mode no_data
```

### Step 3: Verify Release Verdicts in PostgreSQL
Query the `deploy_analysis` table:
```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U changewatch -d changewatch -c "
  SELECT d.commit_sha, da.verdict, da.verdict_reasons, da.likely_cause
  FROM deploy_analysis da
  JOIN deploys d ON da.deploy_id = d.id
  ORDER BY da.id DESC LIMIT 3;
"
```
You will see:
- Bad deploy evaluated to `verdict: rollback` with AI root cause explanation.
- Safe deploy evaluated to `verdict: safe`.
- No-data deploy evaluated to `verdict: no_data`.

### Step 4: Verify Idempotent Alerting in Outbox
Check the `outbox` and `alerts` tables:
```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U changewatch -d changewatch -c "
  SELECT deploy_id, kind, channel FROM alerts;
"
```
Each deploy has a single unique alert record per kind (`UNIQUE (deploy_id, kind)`), preventing duplicate alerts.

### Step 5: Run Stage 4 Tests
```bash
PYTHONPATH=server:sdk pytest server/tests/test_stage4.py -v
```
All release verdict, scrubbing, and alerting tests pass.

---

## Verifying Security Addendum Stage 2 (Key Hashing, Roles & Log Redaction)

### Step 1: Create a Project and Verify Ingest vs. Read Keys
Run the CLI project creation command:
```bash
PYTHONPATH=server python -m app.cli create-project "my-secure-app"
```
Notice the output:
- Generates an **Ingest Key** (`cw_ingest_...`) and a **Read Key** (`cw_read_...`).
- Keys are hashed with SHA-256 before storing; plaintext keys are NEVER saved in Postgres.

### Step 2: Verify Key Role Enforcement
1. **Ingest Key:**
   - Allowed on `POST /v1/deploys` and `POST /v1/events`.
   - Forbidden (HTTP 403) on read endpoints like `/v1/verdict` or read APIs.
2. **Read Key:**
   - Allowed on read endpoints.
   - Forbidden (HTTP 403 `Read key cannot be used for ingestion`) on `POST /v1/deploys` and `POST /v1/events`.

### Step 3: Verify Log Redaction
ChangeWatch logs never leak sensitive information:
- API keys (`cw_...`) are redacted to `[REDACTED_KEY]`.
- Webhook URLs (`hooks.slack.com`) are redacted to `[REDACTED_WEBHOOK_URL]`.
- Provider tokens (`AKIA...`, `ghp_...`, `sk-...`, `Bearer ...`) are redacted.
- Diffs are redacted to `[DIFF REDACTED]`.
- Stack traces are redacted to `[STACK TRACE REDACTED]`.

### Step 4: Run Security Stage 2 Automated Tests
```bash
PYTHONPATH=server:sdk pytest server/tests/test_security_stage2.py -v
```
All 8 role enforcement, hashing, and log redaction tests pass.

---

## Verifying Phase 1 Stage 5 (Read API & Dashboard)

### 1. Read API Overview
All read endpoints are protected by `require_read_project_id`. They require an `X-API-Key` header with a valid Read Key (`cw_read_...`). Ingest keys receive `HTTP 403 Forbidden`.

| Endpoint | Method | Query Parameters | Description |
|---|---|---|---|
| `/v1/verdict` | GET | `sha=<commit_sha>`, `environment=prod` | Returns latest deploy analysis and verdict (`safe`, `watch`, `rollback`, `no_data`) |
| `/api/deploys` | GET | `limit=50`, `environment=prod` | Returns deploys list for authenticated project under RLS |
| `/api/issues` | GET | `limit=50`, `status=open` | Returns fingerprinted issues and event counts |
| `/api/issues/{id}` | GET | None | Returns issue details and recent event stack traces (404 if not found or other tenant) |
| `/api/timeline` | GET | `hours=24`, `format=list` | Returns 5-minute bucketed timeline of traffic, errors, and release deploys |

### 2. Manual Verification with cURL

#### Query Verdict for a Release Commit
```bash
curl -s -H "X-API-Key: cw_read_your_key_here" \
  "http://localhost:8000/v1/verdict?sha=abc1234&environment=prod"
```
Response:
```json
{
  "deploy_id": 1,
  "commit_sha": "abc1234",
  "environment": "prod",
  "author": "Alice",
  "message": "feat: checkout flow",
  "deployed_at": "2026-10-19T10:00:00+00:00",
  "verdict": "safe",
  "verdict_reasons": ["traffic observed, error rates within normal thresholds"],
  "verdict_final": true,
  "verdict_updated_at": "2026-10-19T10:30:00+00:00",
  "likely_cause": null,
  "new_issues": 0,
  "errors_before": 0,
  "errors_after": 0,
  "flagged": false
}
```

#### Query 5-Minute Timeline
```bash
curl -s -H "X-API-Key: cw_read_your_key_here" \
  "http://localhost:8000/api/timeline?hours=24"
```
Response:
```json
[
  {
    "bucket": "2026-10-19T10:00:00+00:00",
    "timestamp": "2026-10-19T10:00:00+00:00",
    "requests": 250,
    "errors": 2,
    "deploys": [
      {
        "id": 1,
        "commit_sha": "abc1234",
        "environment": "prod",
        "verdict": "safe"
      }
    ]
  }
]
```

### 3. Tenant Isolation & Role Enforcement Verification
1. **Ingest Key Blocked:** Attempting to query `/api/deploys` with an Ingest Key returns `HTTP 403 Forbidden`:
   ```bash
   curl -s -H "X-API-Key: cw_ingest_your_write_key" "http://localhost:8000/api/deploys"
   # Output: {"detail": "Ingest key cannot be used to read data"}
   ```
2. **Tenant Isolation:** Querying `/api/issues/{id}` or `/v1/verdict?sha={sha}` where the resource belongs to another project returns `HTTP 404 Not Found` due to Row Level Security (`project_transaction`).

### 4. Running the Dashboard
The Next.js dashboard runs at port 3000:
```bash
# Start full stack including dashboard
docker compose up -d

# Open http://localhost:3000 in your browser
# Enter your project's Read Key (cw_read_...) into the top bar to inspect:
# - Visual 5-minute requests & errors timeline
# - Deploy inspection panel with AI root cause and git diff
# - Error issues with stack traces
```

### 5. Run Automated Tests
```bash
.venv/bin/pytest server/tests/test_read_api.py -v
```
All 13 Read API, role permission, tenant isolation, CORS, and parameter validation tests pass.

---

## Verifying Stage 6: Hardening & Load Testing

### 1. Manual Retention Cleanup Verification
Run the retention cleanup utility manually or set it as a cron task:
```bash
# In Docker:
docker compose exec api python -m app.cli cleanup-retention --days 30 --batch-size 5000

# Local Development:
PYTHONPATH=server python -m app.cli cleanup-retention --days 30 --batch-size 5000
```
Expected output:
```text
==========================================
 ChangeWatch Retention Cleanup Completed  
==========================================
Retention Window:  30 days
Batch Size:        5000
Events Deleted:    0
Traffic Deleted:   0
Batches Executed:  0
==========================================
```

### 2. High-Throughput Load Testing
Run the asynchronous load testing utility against a running ChangeWatch instance:
```bash
python scripts/load_test.py \
  --url http://localhost:8000 \
  --key cw_test_key_123 \
  --rate 1000 \
  --duration 10 \
  --batch-size 20
```
Expected output:
```text
============================================================
               ChangeWatch Load Test Report
============================================================
Target URL:             http://localhost:8000
Target Rate:            1000 events/sec
Batch Size:             20 events/request
Planned Duration:       10.00 s
Actual Duration:        10.05 s
------------------------------------------------------------
Total Events Sent:      10,000
  - Accepted (200):     9,500 (95.0%)
  - Rate-Limited (429): 500 (5.0%)
  - Failed (5xx/Err):   0 (0.0%)
------------------------------------------------------------
Total HTTP Requests:    500
  - 200 OK:             475
  - 429 Too Many Req:   25
  - 5xx / Network Err:  0
------------------------------------------------------------
Latency Percentiles (ms):
  - p50:                14.20 ms
  - p95:                42.10 ms
  - p99:                88.50 ms
  - min / avg / max:    5.10 / 18.30 / 120.40 ms
------------------------------------------------------------
Effective Throughput:   945.27 events/sec (accepted)
                        995.02 events/sec (sent)
============================================================
```

### 3. Run Automated Unit & Load Tests
Execute the Stage 6 test suite:
```bash
.venv/bin/pytest server/tests/test_stage6_retention.py -v
```
All 25 unit tests covering batch deletions, active record preservation, pool error propagation, CLI commands, synthetic generators, percentile calculations, and async load runners pass.

Run the entire full regression test suite across the project:
```bash
.venv/bin/pytest server/tests sdk/tests -v
```
All unit tests pass (2 live database integration tests are skipped when offline).

---

## Verifying Security Addendum Stage 3: Column Encryption & Database Inspection

### 1. Column Encryption & Envelope Encryption
ChangeWatch protects sensitive customer data (`events.stack_trace`, `events.extra`, `deploys.diff`, `deploys.slack_webhook_url`) using AES-256-GCM envelope encryption with per-project data keys and Associated Authenticated Data (AAD):
```bash
# Encrypt existing plaintext rows in batches
PYTHONPATH=server python -m app.cli encrypt-existing --batch-size 1000
```

### 2. Database Inspection Verification
Run the database inspection test to guarantee that no plaintext diffs, stack traces, or webhook secrets exist in database rows or database dumps:
```bash
.venv/bin/pytest server/tests/test_security_stage3.py -v
```

---

## Verifying Security Addendum Stage 4: Read-Only Guarantees & Action Pipeline Safety

### 1. Zero-Write Guarantee & Read-Only GitHub Client
ChangeWatch enforces provable read-only access to customer code and repositories:
- All GitHub interactions are mediated exclusively through `server/app/github_client.py` via `ReadOnlyGitHubClient`.
- Any call to `post()`, `put()`, `delete()`, or `patch()` raises `ReadOnlyViolationError`.
- No feature can create commits, branches, pull requests, or comments on customer repositories.
- Analysis SQL queries are strictly `SELECT`-only.

### 2. GitHub Token Minimum Permissions
If using `GITHUB_READ_TOKEN` or a personal access token for private repositories, configure the exact minimum permissions:
- Token Type: **GitHub Fine-Grained Personal Access Token**
- Repository Permissions: **Contents: Read-only**
- All other permissions: **No access** (None)

### 3. GitHub Action Non-Blocking Pipeline Safety
The deploy-hook workflow (`action/changewatch-deploy.yml`) and composite action (`action/action.yml`) enforce:
- `permissions: contents: read`
- `persist-credentials: false` on repository checkout
- `continue-on-error: true` (ChangeWatch outages or slow networks will never fail customer CI/CD pipelines)
- Strict 10s request timeout (`--max-time 10`)
- Secret exclusions (`*.env*`, `*.pem`, `*.key`, `id_rsa`, `secrets/**`, `infra/**`)

### 4. Run Automated Stage 4 Tests
```bash
.venv/bin/pytest server/tests/test_security_stage4.py -v
```
All 8 tests pass, confirming GET-only enforcement, static AST scans, and Action workflow linting.

---

## Verifying Security Addendum Stage 5: SDK Safety Budget & Runtime Protection

### 1. Run the SDK Overhead & Memory Benchmark
ChangeWatch guarantees that observing requests in production adds < 1.0 ms at p95 and < 20 MB of extra memory:
```bash
.venv/bin/python sdk/bench/overhead.py
```
Expected output:
```text
============================================================
      ChangeWatch SDK Overhead & Memory Budget Benchmark      
============================================================
Iterations:             10000
Added Latency p50:      0.0036 ms
Added Latency p95:      0.0056 ms  (Budget: < 1.0 ms)
Added Latency p99:      0.0445 ms
Peak Added Memory:      0.33 MB  (Budget: < 20 MB)
------------------------------------------------------------
PASSED: All safety and performance budgets satisfied.
============================================================
```

### 2. Manual Verification of Kill Switch & Circuit Breaker
1. **Kill Switch:**
   Start an app with `CHANGEWATCH_DISABLED=1`. The SDK initializes as a zero-overhead no-op without creating worker threads or queue buffers.
2. **Circuit Breaker:**
   If the ChangeWatch backend is down or unreachable, 5 consecutive failed sends trip the circuit breaker. The SDK immediately enters backoff (60s up to 10m), saving CPU cycles and network sockets.
3. **Fork Safety:**
   Under preloaded environments (`gunicorn --preload`), `os.register_at_fork` cleanly reinitializes the bounded queue and restarts daemon worker threads in child worker processes.

### 3. Run Automated Stage 5 Tests
```bash
.venv/bin/pytest sdk/tests/test_sdk_safety.py -v
```
All 8 tests pass, confirming fork safety, circuit breaker tripping, kill switch behavior, HTTPS enforcement, and overhead budgets.

---

## Verifying Security Addendum Stage 6: TLS, Internal Network & Signed Webhooks

### 1. Launching with Automatic TLS (Caddy Profile)
To run ChangeWatch with automatic HTTPS termination:
```bash
# Set public hostname (or leave empty for localhost / self-signed TLS)
PUBLIC_HOSTNAME=changewatch.mycompany.internal docker compose --profile tls up -d
```
Caddy will automatically provision Let's Encrypt certificates for public FQDNs, or issue internal CA certificates for `localhost`, `127.0.0.1`, and private internal hostnames.

### 2. Internal Network Verification
Verify that PostgreSQL and Redis publish no ports on host interfaces:
```bash
docker compose ps
```
Both `changewatch-postgres` and `changewatch-redis` report only internal port exposures (no `0.0.0.0:5432` or `0.0.0.0:6379`).

### 3. Outbound Signed Webhooks Verification
Outbound webhooks sent to custom HTTP channels include `X-ChangeWatch-Signature` (`t={timestamp},v1={hmac_sha256}`) and `X-ChangeWatch-Timestamp`.
Receivers can verify the authenticity using `app.webhook.verify_webhook_signature(payload_bytes, secret, signature_header)`.

### 4. Run Automated Stage 6 Tests
```bash
.venv/bin/pytest server/tests/test_security_stage6.py -v
```
All 8 tests pass, verifying signed webhooks, tamper detection, clock skew tolerance, Ollama TLS warnings, and Compose security rules.

---

## Verifying Security Addendum Stage 7: Retention, PII Scrubbing, Export & Purge Endpoints

### 1. PII Scrubbing
Sensitive customer identifiers (emails, IPv4/IPv6, JWT tokens, credit card numbers) are automatically redacted in the SDK, in the worker before storage, and before any LLM analysis:
```bash
# Toggle via environment variable (default: 1 / true)
SCRUB_PII=1
```

### 2. Code Retention Cleanup
Prune deploy diffs older than `CODE_RETENTION_DAYS` (default 7 days) and events older than `RETENTION_DAYS` (default 30 days):
```bash
PYTHONPATH=server python -m app.cli cleanup-retention --days 30 --code-days 7
```

### 3. Data Portability & Audit: Export & Purge Endpoints
1. **Export Project Data:**
   ```bash
   curl -s -H "X-API-Key: cw_read_your_key" -H "X-Admin-Key: $ADMIN_API_KEY" "http://localhost:8000/v1/export" | jq .
   ```
   Returns full project metadata, deploys, issues, events, and traffic. Sensitive columns (`diff`, `stack_trace`, `extra`) are decrypted in-memory for the authenticated tenant.
2. **Purge Project Data:**
   ```bash
   curl -s -X DELETE -H "X-API-Key: cw_read_your_key" -H "X-Admin-Key: $ADMIN_API_KEY" "http://localhost:8000/v1/project-data" | jq .
   ```
   Purges all project events, traffic, issues, and deploys while leaving tenant credentials intact.

### 4. Run Automated Stage 7 Tests
```bash
.venv/bin/pytest server/tests/test_security_stage7.py -v
```
All 6 tests pass, validating PII scrubbing, code retention cleanup, and audit/purge endpoints.

---

## Verifying Security Addendum Stage 8: Security Model & Audit Script

### 1. Review Security Model Documentation
The complete threat model, architecture promises, limits, envelope encryption design, and production security guide is documented in [`docs/SECURITY_MODEL.md`](file:///Users/aariz/Desktop/test/changewatch/docs/SECURITY_MODEL.md).

### 2. Run the Customer-Facing Security Audit Script
Run the automated security check suite to generate a compliance report across all 8 security pillars:
```bash
.venv/bin/python scripts/security_check.py
```
Expected output:
```text
================================================================================
          ChangeWatch Security & Safety Compliance Report          
================================================================================

[1. Read-Only Code Guarantee]
  [PASS] GitHub Client Mutating Verbs Prohibited
         POST/PUT/DELETE/PATCH raise ReadOnlyViolationError
  [PASS] GitHub Action Read-Only & Non-Blocking Lint
         contents: read, persist-credentials: false, continue-on-error: true, timeout <= 10s
  [PASS] Static AST Scan: Zero Mutating GitHub Calls
         0 violations across repo

[2. SDK Safety & Overhead Budget]
  [PASS] SDK Emergency Kill Switch (CHANGEWATCH_DISABLED=1)
         SDK becomes zero-overhead no-op
  [PASS] SDK Strict HTTPS Enforcement
         Rejects unencrypted remote HTTP endpoints
  [PASS] SDK Latency Budget (< 1.0 ms p95)
         Measured p95 added latency: 0.0017 ms
  [PASS] SDK Memory Budget (< 20 MB peak)
         Measured peak memory overhead: 0.08 MB

[3. Envelope Encryption at Rest]
  [PASS] AES-256-GCM Envelope Encryption & AAD Binding
         AES-256-GCM envelope encryption verified with AAD context binding

[4. Key Role Separation & Storage]
  [PASS] SHA-256 API Key Hashing
         Plaintext keys never stored in database

[5. Log & Data Redaction]
  [PASS] Log Redaction Filter
         API keys, AWS tokens, and Slack webhooks redacted
  [PASS] Automated PII Scrubber (SCRUB_PII=1)
         Emails, IPs, JWTs, and credit card PANs scrubbed

[6. In-Transit Security & Webhooks]
  [PASS] Cryptographically Signed Webhooks (HMAC-SHA256)
         X-ChangeWatch-Signature timestamped & tamper-resistant

--------------------------------------------------------------------------------
Compliance Summary: 12/12 Checks Passed (100.0%)
================================================================================
```

### 3. Run Automated Stage 8 Tests
```bash
.venv/bin/pytest server/tests/test_security_stage8.py -v
```
All tests pass, verifying both `docs/SECURITY_MODEL.md` promises and `scripts/security_check.py` execution.

---

## Verifying Performance Addendum - Stage 1: Baseline Benchmark & Metrics Runner

### 1. Run Automated Stage 1 Tests
Execute the Stage 1 test suite verifying scenario definitions, throughput/latency percentile calculation, report persistence, and side-by-side run comparisons:
```bash
.venv/bin/pytest server/tests/test_performance_stage1.py -v
```

### 2. Run the Benchmark Suite & Save Baseline
Run all 4 standardized benchmark scenarios (`steady`, `crash_loop`, `distinct_burst`, `mixed`) with the benchmark runner:
```bash
.venv/bin/python scripts/bench.py --duration 2 --mock --output benchmarks/baseline.json
```
Output displays sustained throughput and p50/p95 latency percentiles for each scenario:
```text
======================================================================
         ChangeWatch Performance Benchmark Suite Runner         
======================================================================

Running Scenario: STEADY (Steady traffic: 500 events/s with balanced distribution)...
  -> Throughput:   499.93 events/sec
  -> Latency p50:  3.46 ms
  -> Latency p95:  3.55 ms
  -> Accepted:     1000/1000

Running Scenario: CRASH_LOOP (Crash loop burst: 5,000 events/s with single identical fingerprint)...
  -> Throughput:   4997.45 events/sec
  -> Latency p50:  3.48 ms
  -> Latency p95:  3.54 ms
  -> Accepted:     10000/10000

Running Scenario: DISTINCT_BURST (High entropy burst: 3,000 events/s across 1,000 distinct fingerprints)...
  -> Throughput:   2998.69 events/sec
  -> Latency p50:  3.52 ms
  -> Latency p95:  3.62 ms
  -> Accepted:     6000/6000

Running Scenario: MIXED (Mixed realistic workload with heartbeats and slow requests)...
  -> Throughput:   999.32 events/sec
  -> Latency p50:  3.53 ms
  -> Latency p95:  3.60 ms
  -> Accepted:     2000/2000
```

### 3. Compare Two Benchmark Runs
Compare any two benchmark output files side-by-side:
```bash
.venv/bin/python scripts/bench.py --compare benchmarks/baseline.json benchmarks/baseline.json
```
This generates a formatted table detailing throughput and latency deltas with speedup verdicts.

---

## Verifying Performance Addendum - Stage 2: Worker Batching

### 1. Run Automated Stage 2 Tests
Execute the Stage 2 test suite verifying batch bounds, multi-row database operations, and state equivalence:
```bash
.venv/bin/pytest server/tests/test_performance_stage2.py -v
```
All 7 tests verify:
- `test_pull_batch_from_queue_respects_batch_max_and_timeout`: PASSED
- `test_pull_batch_from_queue_empty`: PASSED
- `test_process_event_batch_groups_issues_and_events`: PASSED
- `test_process_event_batch_heartbeat_aggregation`: PASSED
- `test_incident_check_called_once_per_distinct_deploy_issue`: PASSED
- `test_batch_processing_vs_single_equivalence`: PASSED
- `test_handle_event_batch_with_retry_failure_fallback`: PASSED

### 2. Verify Batch Processing with Configurable Batch Bounds
Run the worker with custom batch configuration parameters:
```bash
BATCH_MAX=100 BATCH_MAX_WAIT_MS=25 .venv/bin/python -m app.worker
```
The worker pulls up to 100 events or waits at most 25 ms, grouping fingerprints and heartbeats in memory and committing batched multi-row transactions with RLS enforcement.

---

## Verifying Performance Addendum - Stage 3: Worker Supervisor & Concurrency Scaling

### 1. Run Automated Stage 3 Tests
Execute the Stage 3 test suite verifying worker process resolution, supervisor crash recovery, memory threshold enforcement, detector advisory locking, connection pool bounds, and healthz metrics:
```bash
.venv/bin/pytest server/tests/test_performance_stage3.py -v
```
All 8 tests pass:
- `test_resolve_worker_processes`: PASSED
- `test_resolve_api_workers`: PASSED
- `test_worker_supervisor_process_management_and_restart`: PASSED
- `test_worker_supervisor_stop_all`: PASSED
- `test_check_memory_limit`: PASSED
- `test_detector_advisory_lock`: PASSED
- `test_db_pool_per_process_sizing`: PASSED
- `test_healthz_reports_queue_metrics`: PASSED

### 2. Verify Multi-Process Worker Supervisor
Launch the multi-process supervisor with explicit or automatic process counts:
```bash
.venv/bin/python -m app.worker --processes 2
```
Output displays:
```text
Worker startup: detected_cpus=8, chosen_processes=2 (spec=2)
Starting WorkerSupervisor with 2 processes
Worker supervisor initializing 2 worker processes
Worker supervisor started child worker 0 (PID: ...)
Worker supervisor started child worker 1 (PID: ...)
```
If a worker process is sent a kill signal, the supervisor automatically restarts a replacement process without losing queue items.

### 3. Verify Healthcheck Telemetry
Inspect `/healthz` to confirm queue depth, dropped event count, and active worker count:
```bash
curl -s http://localhost:8000/healthz | jq
```
Output includes:
```json
{
  "status": "ok",
  "postgres": "ok",
  "redis": "ok",
  "queue_depth": 0,
  "dropped_count": 0,
  "worker_count": 2
}
```

---

## Verifying Performance Addendum - Stage 4: Caches, Sliding Windows & Rollups Table

### 1. Run Automated Stage 4 Tests
Execute the Stage 4 test suite verifying thread-safe LRU+TTL caches, deploy invalidation, Redis sliding-window atomic counters, and batch rollup population:
```bash
.venv/bin/pytest server/tests/test_performance_stage4.py -v
```
All 8 tests pass:
- `test_lru_ttl_cache_basic_and_ttl`: PASSED
- `test_lru_eviction`: PASSED
- `test_cache_invalidation`: PASSED
- `test_project_invalidation`: PASSED
- `test_deploy_lookup_cache_invalidation_helper`: PASSED
- `test_cache_concurrent_thread_safety`: PASSED
- `test_redis_sliding_window_counter`: PASSED
- `test_worker_batch_populates_event_rollups_and_sliding_window`: PASSED

### 2. Verify In-Memory Cache Invalidation
When a new deploy arrives, cached deploy lookups for that project are immediately cleared:
```python
from app.cache import deploy_lookup_cache, invalidate_deploy_cache

# Set a cached deploy
deploy_lookup_cache.set((1, "prod", "commit_a"), 42)
assert deploy_lookup_cache.get((1, "prod", "commit_a")) == 42

# Invalidate project deploys
invalidate_deploy_cache(1)
assert deploy_lookup_cache.get((1, "prod", "commit_a")) is None
```

### 3. Verify Timeline Rollup Queries
Verify that `/api/timeline` reads aggregated error counts from `event_rollups` with statement timeout:
```bash
curl -s -H "X-API-Key: <READ_KEY>" "http://localhost:8000/api/timeline?hours=24" | jq
```
The query executes against `event_rollups` bucketed into 5-minute intervals without scanning millions of raw rows in `events`.

---

## Verifying Performance Addendum - Stage 5: Storm Control & Backpressure Shedding

### 1. Run Automated Stage 5 Tests
Execute the Stage 5 test suite verifying storm control row caps, counter preservation, backpressure shedding order, and healthcheck dropped reporting:
```bash
.venv/bin/pytest server/tests/test_performance_stage5.py -v
```
All 4 tests pass:
- `test_storm_control_caps_full_event_rows_and_keeps_counters_exact`: PASSED
- `test_backpressure_shedding_order`: PASSED
- `test_deploys_never_dropped_under_backpressure`: PASSED
- `test_healthz_reports_dropped_count`: PASSED

### 2. Manual Verification of Storm Control
During a crash loop burst (e.g. 100 identical errors in one minute):
1. Send 100 error events with the same fingerprint.
2. Observe that only `STORE_FULL_PER_MIN` (default 20) rows are inserted into the `events` table.
3. Observe that `issues.event_count` is incremented by 100 and `event_rollups` contains the full 100 count.
4. Alerts trigger accurately because threshold evaluation operates on counters, not row counts.

### 3. Manual Verification of Backpressure Shedding
When Redis queue depth exceeds `QUEUE_SOFT_LIMIT` (default 10,000):
1. Send a mixed batch containing a deploy, heartbeats, a new error, repeated errors, and `slow_request` events.
2. Verify HTTP 200 response: `{"accepted": 3, "dropped": 5}`.
3. Verify `slow_request` events were dropped first.
4. Verify heartbeats and the first occurrence of the new error were accepted.
5. Query `/healthz` and verify `dropped_count` reflects dropped items:
```bash
curl -s http://localhost:8000/healthz | jq
```
```json
{
  "status": "ok",
  "postgres": "ok",
  "redis": "ok",
  "queue_depth": 10500,
  "dropped_count": 5,
  "worker_count": 2
}
```

---

## Verifying Performance Addendum - Stage 6: Interval Search, Compiled CODEOWNERS & Priority Queue

### 1. Run Automated Stage 6 Tests
Execute the Stage 6 test suite verifying interval search binary searches, CODEOWNERS compilation, top-k heaps, priority queues, and single-pass regexes:
```bash
.venv/bin/pytest server/tests/test_performance_stage6.py -v
```
All 8 tests pass:
- `test_interval_merging_logic`: PASSED
- `test_bisect_interval_search_vs_brute_force_randomized`: PASSED (1,000 randomized queries matched)
- `test_codeowners_last_match_wins`: PASSED
- `test_codeowners_cache_and_invalidation`: PASSED
- `test_top_k_selection`: PASSED
- `test_investigation_priority_queue_ordering`: PASSED (FIFO within priority tiers)
- `test_single_pass_scrub_and_short_string_skip`: PASSED
- `test_single_pass_stack_trace_normalization`: PASSED

### 2. Manual Verification of Interval Search with Bisect
In a Python shell, verify that overlapping git diff added ranges are merged and queried in O(log n) time:
```python
from app.intervals import IntervalIndex, frame_in_diff

raw_ranges = {"app/main.py": IntervalIndex([(10, 20), (15, 25), (40, 50)])}
assert frame_in_diff(raw_ranges, "app/main.py", 22) is True   # Inside merged range (10, 25)
assert frame_in_diff(raw_ranges, "app/main.py", 30) is False  # In gap
assert frame_in_diff(raw_ranges, "app/main.py", 45) is True   # Inside range (40, 50)
```

### 3. Manual Verification of Compiled CODEOWNERS
Verify that rules evaluate backwards with last-match-wins:
```python
from app.codeowners import parse_codeowners

codeowners_text = """
* @global-owner
/src/ @frontend-team
/src/critical.py @security-lead
"""
compiled = parse_codeowners(codeowners_text)
assert compiled.resolve_owners("src/critical.py") == ["@security-lead"]
assert compiled.resolve_owners("src/utils.py") == ["@frontend-team"]
assert compiled.resolve_owners("backend/main.py") == ["@global-owner"]
```

### 4. Manual Verification of Investigation Priority Ordering
Verify that confirmed incidents are processed before critical route incidents and possible incidents:
```python
from app.priority_queue import compute_priority_score, InvestigationPriority

s1 = compute_priority_score(InvestigationPriority.CONFIRMED_INCIDENT, timestamp=100.0)
s2 = compute_priority_score(InvestigationPriority.CRITICAL_ROUTE_INCIDENT, timestamp=10.0)
s3 = compute_priority_score(InvestigationPriority.POSSIBLE_INCIDENT, timestamp=5.0)

# Lower score dequeues first:
assert s1 < s2 < s3
```

---

## Verifying Performance Addendum - Stage 7: Partitioning & Separate Connection Pools

### 1. Run Automated Stage 7 Tests
Execute the Stage 7 test suite verifying weekly range partition retention drops, dedicated read/write pools, and statement timeout:
```bash
.venv/bin/pytest server/tests/test_performance_stage7.py -v
```
All 5 tests pass:
- `test_drop_expired_partitions_drops_only_old`: PASSED (only expired weekly tables dropped)
- `test_cleanup_with_conn_integrates_partition_dropping`: PASSED
- `test_read_and_write_pool_separation`: PASSED (read pool configured with statement_timeout=5000ms)
- `test_read_project_transaction_enforces_rls_on_read_pool`: PASSED
- `test_migration_009_partition_events_sql_exists`: PASSED

### 2. Manual Verification of Partition Drop Retention
In a Python / asyncpg session against Postgres:
```python
from datetime import datetime, timedelta, timezone
from app.retention import drop_expired_partitions

# Partitions whose upper boundary is before cutoff are dropped in O(1) catalog operations:
# e.g. events_2026_09_28 dropped, events_default and active partitions preserved.
```

---

## Verifying Performance Addendum - Stage 8: Final Benchmark Comparison Table

### 1. Run Automated Stage 8 Tests
Execute the Stage 8 test suite verifying benchmark artifact existence, delta calculations, and sub-5ms p95 latencies:
```bash
.venv/bin/pytest server/tests/test_performance_stage8.py -v
```
All 2 tests pass:
- `test_baseline_and_optimized_benchmark_files_exist`: PASSED (all scenarios 0 failures, p95 < 5.0ms)
- `test_compare_runs_output`: PASSED (accurate comparative delta table printed)

### 2. Run Comparative Benchmark CLI
Execute `scripts/bench.py` to compare baseline and optimized performance runs:
```bash
.venv/bin/python scripts/bench.py --compare benchmarks/baseline.json benchmarks/optimized.json
```
Report highlights:
- **Crash loop (5,000 events/s):** 4,999.35 events/s sustained, p95 3.52ms, p99 3.57ms (-1.7% faster), 0 failures.
- **Distinct burst (3,000 events/s, 1,000 fingerprints):** 2,999.15 events/s sustained, p95 3.54ms (-2.2% faster), 0 failures.
- **Mixed realistic traffic (1,000 events/s):** 999.32 events/s sustained, p99 3.60ms (-6.0% faster), 0 failures.
- **Steady traffic (500 events/s):** 499.70 events/s sustained, p95 3.58ms, 0 failures.

---

## Master Roadmap: Typed Decisions (TD) & Analytics Dashboard (AD) Integration

The execution plan incorporates `antigravity_typed_decisions_addendum.md` (TD) and `antigravity_analytics_dashboard_addendum.md` (AD) into the milestones:

| Milestone | Scope & Components | TD Stages | AD Stages |
|---|---|---|---|
| **M1** | Core Ingest, Deploy Hook, Redis Pipeline, Worker Fingerprinting, Verdicts, SDK | Foundation | — |
| **M2** | Incident Detection, Sub-5s Instant Alert, Multi-Channel Notifications | **TD Stage 1 & 2** (rules decision layer, triage with fallback) | — |
| **M3** | Autonomous Read-Only Investigator Agent (SELECT-only tools, root-cause analysis) | Phase 2 Stage C | **AD Stage 1–3** (metrics pure functions, migration, API) |
| **M4** | Hardening, Concurrency Supervisor, Storm Control & Native Partitioning | **TD Stage 3** (ephemeral code mode) | Watchdog status log |
| **M5** | AI SRE Evals & Verification | **TD Stage 4, 5, 7** (Jev adapter, verifier, evals) | AI investigation tiles |
| **M6** | Calibration, Trust Page & Guardrails | **TD Stage 6 & 8** (calibration on Trust page, optional secret guard) | Calibration & feedback tiles |
| **M8** | Usage Metering & Operations Completion | Final decision models | **AD Stage 4–7** (Full Next.js analytics UI, drill-downs, parity tests) |

*Precedence Rules:*
- TD wins conflicts only where it concerns decisions; the core principles in Section 2 (read-only, fast first, evidence over guesses, honest scope) always win.
- No hosted decision provider will be called until an explicit Jev API key is provided (`DECISION_PROVIDER=rules` by default).
- AD metric calculations live exclusively in `server/app/analytics/metrics.py` as pure functions over pre-aggregated tables (never raw `events`).
- Individual rankings are strictly forbidden; metrics aggregate by code area and release trends only.

---

## Verifying Usage Metering Addendum - Stage 1: Schema, Price Book, CLI, and Cost-Plus Calculator

### 1. Run Automated Stage 1 Tests
Execute the Stage 1 test suite covering price book YAML parsing, integer micro-USD token cost arithmetic, cost-plus calculation, rate card publishing, founding rate locks, and CLI subcommands:
```bash
.venv/bin/pytest server/tests/test_usage_stage1.py -v
```
All 9 tests pass:
- `test_price_book_yaml_loading`: PASSED (validates Version 1 rate card)
- `test_price_book_yaml_validation_errors`: PASSED (rejects invalid versions, pricing modes, negative prices)
- `test_calculate_llm_cost_micros`: PASSED (local/fake models cost 0, Anthropic token prices calculate exact integer micros)
- `test_calculate_billed_micros_fixed_pricing`: PASSED (releases and stored events apply monthly free allowances first)
- `test_calculate_billed_micros_cost_plus`: PASSED (applies margin multiplier, ceil rounding, and floor)
- `test_publish_price_book_versioning`: PASSED (append-only rate cards, rejects re-publishing existing versions)
- `test_price_lock_pinning_and_expiry`: PASSED (pins version for N days, reverts to latest on expiry)
- `test_meter_enum_and_types`: PASSED (all active and reserved meters defined)
- `test_cli_price_commands`: PASSED (`price-book publish` and `price-lock` subcommands)

### 2. Manual Verification of Price Book Publishing via CLI
In a clean environment with the database running, publish a new rate card version:
```bash
docker compose exec api python -m app.cli price-book publish config/price_book.default.yaml
```
Output:
```
==========================================
 ChangeWatch Price Book Published         
==========================================
Version:        1
Source File:    config/price_book.default.yaml
Effective From: 2026-01-01T00:00:00Z
Meters Defined: ['release_monitored', 'ai_investigation', 'ai_investigation_deep', 'stored_events']
==========================================
```

### 3. Manual Verification of Founding Rate Lock via CLI
Pin rate card version 1 for a project for 365 days:
```bash
docker compose exec api python -m app.cli price-lock 1 --days 365
```
Output:
```
==========================================
 ChangeWatch Price Lock Activated         
==========================================
Project ID:      1
Pinned Version:  1
Locked Until:    2027-10-20T...
Duration (days): 365
==========================================
```

### 4. Manual Verification of Cost-Plus Math
In a Python shell, verify that money is strictly integer micro-USD with ceiling rounding:
```python
from app.metering import PriceBookEntry, calculate_billed_micros

entry = PriceBookEntry(
    version=1,
    meter="ai_investigation",
    pricing_mode="cost_plus",
    unit_price_micros=0,
    margin_multiplier=1.30,
    min_unit_price_micros=1000,
    free_units_per_month=20,
)

# 1. Within free allowance: 1 unit, 20 free -> $0
billed, free_applied, billable = calculate_billed_micros(entry, units=1, our_cost_micros=10000, free_units_available=20)
assert billed == 0 and free_applied == 1

# 2. Billable: our cost $0.01 (10,000 micros) * 1.30 -> 13,000 micros ($0.013)
billed, free_applied, billable = calculate_billed_micros(entry, units=1, our_cost_micros=10000, free_units_available=0)
assert billed == 13000 and isinstance(billed, int)
```

---

## Verifying Usage Metering Addendum - Stage 2: Recording Hooks & Redis Aggregation

### 1. Run Automated Stage 2 Tests
Execute the Stage 2 test suite verifying deploy hooks, AI investigation fairness, stored event aggregation, ledger parity, crash loop caps, and error isolation:
```bash
.venv/bin/pytest server/tests/test_usage_stage2.py -v
```
All 7 tests pass:
- `test_record_usage_idempotency_and_ledger`: PASSED (replaying keys never double-counts)
- `test_record_investigation_fairness_rules`: PASSED (failed/cached record nothing; partial undelivered bills $0)
- `test_stored_events_redis_aggregation_and_flush`: PASSED (counts aggregated per minute in Redis and flushed in batches)
- `test_deploy_hook_usage_recording_integration`: PASSED (POST /v1/deploys automatically triggers `release_monitored`)
- `test_usage_daily_equals_sum_of_usage_events`: PASSED (`usage_daily` strictly equals sum of `usage_events`)
- `test_crash_loop_storm_control_caps_stored_events`: PASSED (100k crash loop errors cap stored units at `STORE_FULL_PER_MIN=20`)
- `test_usage_recording_failure_does_not_break_deploy_or_ingest`: PASSED (error isolation ensures deploys succeed even if usage tables fail)

### 2. Manual Verification of Deploy Hook Recording
Create a deploy and inspect the usage ledger in Postgres:
```bash
# 1. Post a deploy
curl -X POST http://localhost:8000/v1/deploys \
  -H "Authorization: Bearer <INGEST_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"commit_sha":"c0ffee123456","author":"dev@example.com","message":"test release","environment":"prod"}'

# 2. Query usage_events table (via psql or docker compose exec db)
SELECT id, meter, units, billed_micros, free_units_applied, idempotency_key
FROM usage_events
WHERE meter = 'release_monitored'
ORDER BY id DESC LIMIT 1;
```
Expected output:
- `meter`: `release_monitored`
- `units`: `1`
- `free_units_applied`: `1` (if within 200 free units/month)
- `billed_micros`: `0`
- `idempotency_key`: `deploy:<id>`

### 3. Manual Verification of Stored Events Aggregation & Flush
1. Send error events to `POST /v1/events`.
2. Inspect Redis key `usage:events:<project_id>:<minute_epoch>`:
   ```bash
   docker compose exec redis redis-cli SMEMBERS "usage:pending_event_buckets"
   ```
3. Allow the 60-second flush to run (or trigger `flush_usage_events` via detector cycle).
4. Verify `usage_events` has a batch entry with `meter = 'stored_events'` and `ref_type = 'batch'`.



