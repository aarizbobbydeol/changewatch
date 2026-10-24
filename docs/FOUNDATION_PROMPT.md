# ChangeWatch: Foundation Prompt

> Paste this into a new AI session to give it accurate context on ChangeWatch. Every path, endpoint, command and number below was checked against the code in this repository. If the code changes, update this file.

---

You are working on **ChangeWatch**, an open-source, self-hosted release monitor for small Python teams. After every deploy it compares post-deploy errors and traffic with the pre-deploy baseline, gives the release a deterministic verdict (`safe`, `watch`, `rollback`, `no_data`), explains the likely cause, and alerts the team.

## 1. Non-negotiable principles

1. **Zero event loss.** Ingest goes through a reliable Redis queue: workers `BLMOVE`/`RPOPLPUSH` from `events` into a per-worker `events:processing:{worker_id}` list, and a reaper returns stuck items. Events are idempotent on `event_id`. Alerts go through a durable `outbox` table and are delivered exactly once.
2. **Tenant isolation in the database.** Postgres row-level security is `ENABLE`d and `FORCE`d. Policies check `project_id = current_setting('app.project_id')::int`. `project_transaction` / `read_project_transaction` run as the restricted role `changewatch_tenant` (migration 018, `SET LOCAL ROLE`) whenever the connected user is a superuser or has BYPASSRLS; cross-project system jobs use `get_connection()` with the owner role.
3. **Encryption.** Each project has an AES-256-GCM data key wrapped by a master key (`MASTER_KEY`, required; `docker-compose.yml` refuses to start without it). Diffs, CODEOWNERS, file contents, stack traces, extras, investigation results/steps, the project Slack webhook and channel configs are stored only in `*_enc` columns (`crypto.encrypt_or_keep` / `decrypt_or_plain`); `app.cli encrypt-existing` converts legacy plaintext. Keys rotate from the CLI with `MASTER_KEY_PREVIOUS`.
4. **Separate key roles.** The ingest key (`cw_ingest_…`) can only send events and deploys. The read key (`cw_read_…`) is the dashboard's key: it reads one project and can take team actions (acknowledge, mute, feedback, investigate) and change that project's settings and budget. Export and purge also need the admin key (`ADMIN_API_KEY`). Only SHA-256 hashes are stored. `cw_test_key_123` is a dev-only seed (`SEED_DEV_PROJECT=1`).
5. **Never write to customer repos.** `github_client.ReadOnlyGitHubClient` raises on any non-GET request. `scripts/security_check.py` uses Python `ast` to scan the codebase for mutating calls. The GitHub Action is `contents: read` and `continue-on-error: true`.
6. **Deterministic by default.** `DECISION_PROVIDER=rules` needs no hosted API. `jev` (TypeSafe Jev) and `fake` are optional. A silent source is `no_data`, never `safe`.
7. **Safe SDK.** Pure standard library. It never raises into the host app, never blocks, is fork-safe, and has a circuit breaker and a kill switch (`CHANGEWATCH_DISABLED=1`). Measured: p95 added latency is 0.002–0.006 ms (budget < 1 ms) and peak memory is 0.08 MB.
8. **The safety floor is never paused.** Usage budgets can pause AI investigations only. Verdicts and alerts always run.

## 2. Stack

- Python 3.12, FastAPI, asyncpg (no ORM, no Alembic). SQL migrations live in `server/sql/migrations/001…021` and apply automatically on API startup (or with `python -m app.cli migrate`).
- Postgres 16 (events table partitioned by time) and Redis 7 (AOF).
- Next.js 16 (React 19) dashboard, Node 24; ESLint 9 flat config (`eslint.config.mjs`).
- Docker Compose services: `postgres`, `redis`, `api`, `worker`, `detector`, `investigator`, `notifier`, `dashboard`, plus `caddy` (automatic TLS) under `--profile tls`. App services load `.env` (`env_file`), so every setting in `config.py` reaches them.

## 3. Repository layout

```text
server/app/
  main.py              FastAPI app and routes
  config.py            settings from env
  auth.py              key hashing and role checks
  crypto.py            AES-256-GCM envelope encryption
  queue.py, worker.py  reliable queue, batched multi-process worker, reaper; claims each event_id in ingest_dedupe (exactly-once counts) and scrubs events before storage (docs/DATA_PIPELINE.md)
  fingerprint.py       issue grouping
  detector.py          verdict engine (10 / 30 min checkpoints), runs every 60 s
  incident_manager.py  instant incident alerts, triggered from the worker; stores the repo-relative path (app/billing.py) when the frame matches a deploy file
  jobs.py              detector-leader jobs: calm auto-resolve + escalation (each notifies), hourly partitions (next 4 weeks) + retention (events, diffs, CODEOWNERS, files), weekly digest (DIGEST_DAY/HOUR/TIMEZONE, deduped per ISO week)
  watchdog.py          component checks (db, queue, workers, detector, notifier, outbox, disk)
  system_alerts.py     operator alerts: run the watchdog every minute (detector + notifier), one active alert per component (migration 020), "recovered" on clear, sent straight to SYSTEM_ALERT_WEBHOOK_URL; failed team alerts reported there too
  outbox.py            exactly-once alert delivery (FOR UPDATE SKIP LOCKED), routed per channel; fans out to every enabled channel
  notifier.py          sends the outbox every second (detector is the 60 s fallback)
  notifiers/           Slack, Discord, Teams, email, signed webhooks
  decisions/           rules / jev / fake providers, verifier, catalog
  investigator/        AI root cause: agent, read-only SELECT tools; worker.py consumes the `investigations` queue (reliable: processing list, 3 retries, dead letter)
  trust/               calm alerting, action links, feedback, Brier calibration, digest, secret guard
  analytics/           metrics.py (pure functions) and /v1/analytics router
  metering.py, billing.py   micro-USD usage metering and budgets
  telemetry.py         opt-in fleet telemetry (allowlist schema)
  scrub.py, pii.py     secret and PII scrubbing
  retention.py         retention cleanup and partition drops
  github_client.py     GET-only GitHub client
  cli.py               admin CLI
server/sql/            schema.sql + numbered migrations
server/tests/          server test suite
sdk/changewatch/       zero-dependency SDK + ASGIMiddleware
sdk/tests/             SDK tests
dashboard/app/         /, /incidents, /incidents/[id], /analytics, /trust, /settings, /usage, /owner, /owner/margin
  lib/session.ts       the one place for the saved read/admin key and API address (empty = same origin via next.config.js rewrites); downloadWithKey for CSV exports
  components/NavLinks  shared navigation on every page
dashboard/Dockerfile   Node 24 LTS, npm ci from package-lock.json, API_URL build arg (rewrites are fixed at build time), standalone server as non-root
action/                composite GitHub Action + standalone workflow
evals/                 12 root-cause scenarios (python -m evals.run)
scripts/               demo_app, simulate_deploy, load_test, chaos, bench, security_check
collector/             opt-in telemetry collector
```

## 4. API surface

| Auth | Endpoints |
|---|---|
| Ingest key | `POST /v1/deploys`, `POST /v1/events` |
| Read key | `GET /v1/verdict`, `/api/deploys`, `/api/issues[/{id}]`, `/api/timeline`, `/api/incidents[/{id}][/timeline]`, `POST /api/incidents/{id}/actions,feedback,investigate`, `/api/mutes`, `/api/settings`, `/v1/analytics/*`, `/v1/usage*`, `/v1/budget`, `/v1/trust/summary`, `/v1/digest/preview`; with the admin key also `/v1/export` and `DELETE /v1/project-data` |
| Other | `GET /healthz`, `GET /v1/auth/verify`, `GET /a/{token}` (signed action links), `POST /v1/integrations/slack/interactions`, `/v1/telemetry/*`, `/v1/owner/stats`, `/v1/admin/margin`, `/v1/status` |

Keys go in the `X-API-Key` header.

## 5. Verdict rules (`deploy_analysis.first_verdict` / `final_verdict` keep what each checkpoint announced) (`server/app/detector.py`, evaluated in order)

1. **`no_data`**: the source is silent, or there is no traffic or events since the deploy.
2. **`rollback`**: ≥ 20 events from new issues, **or** error rate ≥ 5× baseline with ≥ 30 errors, **or** absolute error rate ≥ 25% with ≥ 10 requests.
3. **`watch`**: ≥ 1 new issue with ≥ 5 events, **or** errors ≥ 3× baseline (minimum 10).
4. **`safe`**: none of the above.

Checkpoints run at `VERDICT_FIRST_MIN=10` and `VERDICT_FINAL_MIN=30`. The second alert is sent only if the verdict changed.

## 6. SDK usage (the real API)

```python
import changewatch
from changewatch import ASGIMiddleware

cw = changewatch.init(api_key="cw_ingest_...", endpoint="https://...", release=GIT_SHA, environment="prod", slow_ms=1500)
app.add_middleware(ASGIMiddleware, client=cw)   # ASGI apps (FastAPI/Starlette). No Flask/WSGI middleware.
changewatch.capture_exception(exc)              # manual capture
```

Install: `pip install "git+https://github.com/aarizbobbydeol/changewatch.git#subdirectory=sdk"` (not on PyPI).

## 7. Commands

```bash
.venv/bin/pytest server/tests sdk/tests            # all pass; live tests skip without Postgres/Redis
.venv/bin/python -m evals.run --provider fake      # 12/12, determinism 5/5
.venv/bin/python scripts/security_check.py         # 12/12
.venv/bin/python scripts/chaos.py                  # 0 lost events, 0 duplicate alerts
docker compose up -d                               # full stack
docker compose --profile tls up -d                 # + Caddy HTTPS (PUBLIC_HOSTNAME)
docker compose --profile demo up -d demo           # demo traffic (DEMO_INGEST_KEY, docs/DEMO.md)
python scripts/verify_pipeline.py --help           # live pipeline check (docs/DATA_PIPELINE.md)
python scripts/api_contract_check.py --help        # REST contract check of every endpoint (auth, validation, isolation)
docker compose exec api python -m app.cli create-project "name" --slack-webhook URL
docker compose exec api python -m app.cli add-channel <id> discord --config '{"webhook_url": "..."}'
```

Other CLI subcommands: `list-projects`, `generate-master-key`, `rotate-master-key`, `rotate-project-key`, `delete-project`, `price-book`, `publish`, `price-lock`, `list-channels`, `add-member`, `list-members`.

## 8. Known gaps

- Alerts do not yet include the signed one-click action links (`/a/<token>` works; nothing generates links for messages).

- Without a master key, the worker and API log a warning and store events and deploys **without** their stack trace, extra data or diff. Compose now prevents that by requiring `MASTER_KEY`, but a non-Docker setup without `MASTER_KEY` still drops them.
- The SDK ships ASGI middleware only.
- Instant alerts: measured 1.9 s from first error to delivery in Discord/webhook in a local end-to-end run; not yet benchmarked under burst load. `incidents.first_alert_ms` measures time to queue (delivery adds up to ~1 s).
- Outbox payloads (alert text: error title, file path, AI summary) and `deploy_analysis.likely_cause` are stored as plaintext.

More detail: [README](../README.md), [SELF_HOSTING](SELF_HOSTING.md), [SECURITY_MODEL](SECURITY_MODEL.md), [DATA_PIPELINE](DATA_PIPELINE.md), [VERIFICATION](VERIFICATION.md), [DECISIONS](../DECISIONS.md).
