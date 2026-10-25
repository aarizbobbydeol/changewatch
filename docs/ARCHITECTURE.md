# How ChangeWatch works, end to end

This page follows one bad deploy from the moment it is pushed to the moment the team gets a
rollback verdict. It names every process, what each one stores, what happens when something
breaks, and how each step is tested. Exact settings live in the [README](../README.md#️-configuration);
the data path is covered stage by stage in [DATA_PIPELINE.md](DATA_PIPELINE.md); security
is in [SECURITY_MODEL.md](SECURITY_MODEL.md).

## The short version

ChangeWatch answers one question after every deploy: **did this release break something, and
should we roll it back?**

1. The **GitHub Action** tells ChangeWatch what changed: the commit, the diff, which lines were
   added, and who owns each file (CODEOWNERS).
2. The **SDK** in your app reports errors, slow requests, and a count of requests every minute.
3. When the same new error shows up a few times right after a deploy, the **worker** opens an
   incident and an alert goes out within seconds. The alert names the file and line, says
   whether that line changed in this release, and tags the owner.
4. The **investigator** asks an AI model (or the built-in rules) for the likely cause and posts it
   as a reply in the same thread.
5. The **detector** compares error rates before and after the deploy and gives a verdict
   (`safe`, `watch`, `rollback` or `no_data`) at 10 minutes and again at 30.
6. The **notifier** delivers every message from a durable outbox, so an alert survives a crash or
   a Slack outage.

## The pieces

ChangeWatch runs as eight containers (`docker-compose.yml`), plus optional Caddy for HTTPS and a demo app. Each one is a small program with one job.

| Process | What it does | Code |
|---|---|---|
| **api** | Accepts deploys and events, serves the read API used by the dashboard and the CLI. Applies database migrations on startup (one process at a time). | `server/app/main.py` |
| **worker** | Takes events off the Redis queue and stores them: removes secrets, groups errors into issues, links each event to its deploy, encrypts stack traces, counts traffic. Opens incidents for new errors right after a deploy. | `server/app/worker.py`, `incident_manager.py` |
| **detector** | Every 60 seconds: verdicts for recent deploys, escalation and auto-resolve of incidents, the dead-man's switch for silent apps, retention, the weekly digest, the watchdog. | `server/app/detector.py`, `trust/` |
| **investigator** | Takes investigation jobs off a Redis queue, gathers the diff, stack trace and error rates with read-only tools, asks the model, checks the answer, and stores it. | `server/app/investigator/` |
| **notifier** | Every second: sends what is waiting in the outbox to Slack, Discord, Teams, email or a signed webhook, with retries. Watches the other processes. | `server/app/notifier.py`, `outbox.py`, `notifiers/` |
| **dashboard** | The web UI (Next.js). It talks to the API with the project's read key. | `dashboard/` |
| **postgres**, **redis** | Postgres 16 stores everything (with row-level security per project). Redis holds the queues, caches and rate limits. | `server/sql/migrations/` |

Outside the server there are two small clients that run on your side:

| Client | What it does | Code |
|---|---|---|
| **SDK** | `changewatch.init(...)` in a Python app. Captures errors, slow requests and a request count per minute, removes secrets and emails, sends in the background. Never blocks or crashes your app. No dependencies. | `sdk/changewatch/` |
| **GitHub Action** | Runs after your deploy step. Reads the local git checkout (never writes to it), sends the commit, diff, changed lines and CODEOWNERS. Never fails your workflow. | `action/notify.py`, `action/action.yml` |

## One bad deploy, step by step

This is the exact scenario the end-to-end test runs (`scripts/e2e_check.py`). A team ships a
change to `app/billing.py` line 42 that divides by a discount that can be zero.

### 1. The deploy is registered (second 0)

The deploy job runs the ChangeWatch Action. It diffs the new commit against the previous one,
works out that lines 42 to 42 of `app/billing.py` were added, finds `.github/CODEOWNERS`, and
sends all of it to `POST /v1/deploys` with the project's **ingest key**. Files matching `.env*`,
`*.pem`, `*.key` and `secrets/**` are left out before anything is sent.

The API scrubs the diff, encrypts the code and CODEOWNERS with the project's own key, and writes
the deploy straight to Postgres (tables `deploys`, `deploy_files`). Deploys never go through the
queue, so they are never dropped.

It then asks the investigator service for a **risk review** of the diff, before any error
arrives. Fixed rules come first (no AI, no cost): a division by a value that may be zero, a
swallowed exception, `eval`, SQL built from strings, TLS checks turned off, a committed secret, a
removed `if` check, migrations, dependency files, very large releases. With an AI provider and
budget, the model adds its findings and the tests worth writing. The result is stored encrypted
(`deploy_reviews`), shown on the deploy panel and at `GET /api/deploys/{id}/review`, given to the
investigator if errors follow, and added as one line to the verdict alert when the risk is
medium or high. Nothing is written to the repository or to GitHub.

### 2. Errors arrive (seconds 1 to 5)

Customers start hitting the bug. The SDK catches each `ZeroDivisionError`, removes secrets
and emails from the message and stack trace, gives the event a unique `event_id`, and sends
events in batches of up to 20 every 2 seconds to `POST /v1/events`. With `capture_logs=True`,
each error also carries the app's last 50 log lines (scrubbed the same way); they are stored
encrypted and shown under "Logs before the error".

The API checks the key and the shape of every field, applies the rate limit, and pushes the
batch onto the Redis list `events` in one step. It answers immediately with how many events it
accepted. The API itself never touches the events table, which is why it stays fast under load.

### 3. The worker stores the errors (within a second)

A worker moves a batch from `events` to its own processing list, so a crash cannot lose it.
For each project, in one database transaction, it:

- **claims each `event_id`**, so an event the SDK sent twice is counted once;
- **scrubs** secrets and emails again (any HTTP client can call the API, not only the SDK);
- **fingerprints** the error (exception type plus the normalized stack trace) and adds it to
  its issue, here `ZeroDivisionError: division by zero`;
- **links it to the deploy**: by the `release` the SDK sent (the commit SHA), otherwise the
  latest deploy before the event in that environment;
- keeps full copies of at most 20 events per error per minute, while the counts stay exact;
- **encrypts** the stack trace and stores the event, a per-minute rollup and the traffic count.

Only after the transaction commits does it remove the batch from its processing list. If
Postgres is briefly down, the batch goes back on the queue unchanged and the worker waits
(1 second, then up to 30). If one event is malformed, only that event is retried and, after 3
tries, moved to `events:dead`; the rest of the batch is stored.
`python -m app.cli requeue-dead` puts dead events back once the cause is fixed.

### 4. The instant alert (seconds 2 to 5)

Still inside that transaction, the worker checks each new error of the batch against its
deploy. An incident opens when the error:

- was first seen after this deploy (or was resolved before it and is now back), and
- happened at least 3 times in the last 60 seconds, or on a route the team marked critical,
- and no mute rule covers it.

The incident starts as **possible** and becomes **confirmed** at 10 events. The worker matches
the stack frames against the deploy's changed lines. `app/billing.py:42` was added in this
release, so the alert says so, and CODEOWNERS gives `@acme/payments` as the owner. The alert
goes into the **outbox** table in the same transaction as the incident, so it can never be
lost or sent for an incident that was rolled back.

Once the transaction has committed, the worker queues an AI investigation job in Redis
(`investigations`). Queuing it any earlier would let the investigator look for an incident it
cannot see yet.

The notifier picks the alert up within a second:

> ⚠️ *Possible regression*: ZeroDivisionError: division by zero (15x) at app/billing.py:42
> *(this line was changed in this release)* after deploy 68595deb by dev@acme.example.
> Owners: @acme/payments.

### 5. The AI explanation (seconds 5 to 30)

The investigator takes the job, checks the project's AI budget (this is the only thing a budget
can pause), and gathers the evidence first: the stack trace; what the deploy changed in the
failing file (the diff hunks near the failing line) and the code around it; the routes, methods
and `extra` fields of the requests that failed; this error per minute around the deploy; other new
errors from the same deploy; dependency and configuration changes; the error rate before and after;
the previous deploys; and similar past incidents with how they were explained and resolved. Each part has a size limit, so one huge diff cannot crowd out the rest. The model may then call read-only tools: database
`SELECT`s, and reading a file from GitHub when code is not stored (a code check in CI makes sure
nothing can write to your repository). The answer is checked against the evidence (a fix that
cites nothing, or a dangerous one, is flagged), stored encrypted, and posted as a reply in the
alert's thread.

If there is no model (`LLM_PROVIDER=fake`), the call fails, or the answer is not valid, the
investigator posts a plain answer built from the same evidence instead, for example "Line 42 in
app/billing.py was modified in deploy 68595deb". The alert never waits for the AI.

### 6. The verdict (minutes 10 and 30)

Every 60 seconds the detector looks at the deploys of the last 2 hours. At the first checkpoint
(10 minutes) it compares the time since the deploy with the 30 minutes before it, in the same
environment, using the request counts from the heartbeats. The rules are fixed and written down
(`server/app/detector.py`), with these defaults:

| Verdict | When |
|---|---|
| 🔴 **rollback** | New error types with 20 or more events between them; or the error rate is 5 times what it was, with at least 30 errors; or more than 25% of requests fail. |
| 🟡 **watch** | A new error type with 5 or more events; or 3 times as many errors as before (at least 10); or the slowest 5% of requests got twice as slow. |
| 🟢 **safe** | Traffic was seen and error rates are within normal limits. |
| ❓ **no_data** | No traffic since the deploy, or the app went silent. ChangeWatch never calls a release safe without evidence. |

The verdict is sent to the channel and is available at `GET /v1/verdict?sha=…`, so a deploy
pipeline can wait for it and roll back automatically. At 30 minutes it is checked again; the
team hears about it only if it changed.

These rules are the **safety floor**: they need no AI and no network, and budgets never pause
or meter them.

### 7. Afterwards

- **Escalation:** a confirmed incident nobody acknowledged gets a reminder after 10 minutes.
- **Auto-resolve:** an incident with no new errors for 30 minutes, while traffic kept flowing, is
  resolved, and the thread gets a "✅ Resolved" message. If the error comes back after a later
  deploy, the new alert says "This error was resolved before and is back."
- **Silent apps:** if an app stops sending heartbeats for 10 minutes, the deploy gets `no_data`
  rather than a false `safe`, and the team is told once that data stopped (and again when it
  is flowing again).
- **Feedback:** the alert's buttons and the dashboard let the team acknowledge, resolve, reopen
  and rate an alert (real, false alarm, unclear). Ratings feed the accuracy numbers on the Trust page.
- **Memory of past incidents:** a new incident is compared with the last 180 days of this project:
  the same error coming back, or an error in the same function or file. The closest strong match
  adds one line to the alert ("Seen before: incident #12 on 2026-09-30 (the same function),
  resolved automatically after 25 min"), the incident page lists them under "Seen before", and the
  investigator is told how they were explained and how they ended.
- **Runbooks:** the team writes steps for a kind of error once (Settings → Runbooks), matched by
  error type, file and route. The best match adds a line to the alert ("Runbook: Billing totals
  fail (https://wiki…)"), its steps are on the incident page and in the AI investigation, and the
  postmortem draft names it, or suggests writing one when none matched.
- **Cost:** if the app reports its own AI calls (`record_ai_usage`), the deploy page shows AI
  spend per 1,000 requests before and after the deploy, and one message goes out when a release
  at least doubles it. Each incident shows its failed requests, how long it lasted, what its AI
  investigation cost and, with a value per failed request set in Settings, an estimated impact.
- **Timeline and postmortem:** the incident page shows everything in time order: deploy, first
  error, each alert delivered, the AI answer, verdicts, who acknowledged or rated it, how it was
  resolved. "Write draft" turns that into a blameless postmortem in Markdown (summary, impact,
  detection, timeline, cause, resolution, action items), written from these records without an
  AI call (`GET /api/incidents/{id}/postmortem`).
- **Retention:** event partitions older than 30 days are dropped, code after 7 days.
- **Weekly digest:** a summary of deploys, verdicts and incidents.

## Where the data lives

| Table | What it holds | Encrypted |
|---|---|---|
| `projects` | Name, hashed keys (the raw keys are shown once), the wrapped data key, settings. | data key wrapped by `MASTER_KEY` |
| `deploys`, `deploy_files` | Commit, author, message, environment, diff, changed lines, CODEOWNERS. | diff, file contents, CODEOWNERS |
| `issues` | One row per distinct error, with the exact count. | |
| `events` | The stored copies of events (weekly partitions). | stack trace, `extra` |
| `event_rollups`, `traffic` | Counts per issue per minute; requests and errors per minute. | |
| `incidents`, `investigations` | What was alerted, its status, the AI result. | AI result and steps |
| `outbox` | Every message to send, with its delivery status. | |
| `usage_events`, `usage_daily`, `budgets`, `price_book` | Metering in integer micro-dollars (no floating point). | |

Every project table has row-level security: work done for a project runs as a database role that can
only see that project's rows, even if a query forgets its `WHERE project_id`.

## When something breaks

| What fails | What happens |
|---|---|
| ChangeWatch is down | The Action prints a warning and your deploy continues. The SDK keeps up to 1,000 events and retries. |
| Redis is down | The API answers `503` with `Retry-After: 30`; the SDK waits and resends. Deploys are still accepted. |
| Postgres is down | Queued events wait in Redis; the worker backs off and retries. Nothing is dead-lettered for an outage. The detector raises an operator alert. |
| A worker crashes mid-batch | Its processing list is returned to the queue; the `event_id` ledger stops double counting. |
| Slack is down | The outbox retries with backoff, then marks the message failed and sends an operator alert to `SYSTEM_ALERT_WEBHOOK_URL`. |
| The AI provider fails or the budget runs out | No AI reply (or a "paused" note). Alerts and verdicts are unaffected. |
| A process stops | The watchdog sees its heartbeat go stale and sends an operator alert, then "recovered" when it is back. |

## How we know it works

| Layer | What it checks | Where |
|---|---|---|
| Unit and live tests | Over 380 tests. Most run against a real Postgres 16 and Redis: tenant isolation, encryption, exactly-once counting, regressions found in audits. | `server/tests`, `sdk/tests` |
| Evals | 12 recorded incidents: the verdict engine and the AI must give the right call. | `evals/` |
| Security check | The read-only rule (no writes to customer repositories), encryption at rest, ingest and read keys kept apart, log redaction, scrubbing, the SDK's overhead budget. | `scripts/security_check.py` |
| Chaos | Worker and Redis disruptions: no lost events, each alert delivered once, operator alerts raised and cleared, a fallback when the AI or Slack fail. | `scripts/chaos.py` |
| Data pipeline | Sends a known batch and checks every stage in the database, plus re-sends. | `scripts/verify_pipeline.py` |
| REST API contract | Every endpoint with no key, the wrong key, another tenant's key and bad input: no 500s, no data from other tenants. | `scripts/api_contract_check.py` |
| **End to end** | The whole story above, on the real Docker Compose stack: deploy through the Action, errors, alert content, AI reply, verdict, read API, dashboard pages, export, re-sends. | `scripts/e2e_check.py`, CI job `e2e` |

CI (`.github/workflows/ci.yml`) runs all of these on every push and pull request: the test
job, Python and dashboard lint, the dashboard build, and the end-to-end job. A version tag
(`v*`) runs CI again and then publishes the server and dashboard images
(`.github/workflows/release.yml`).

To run the end-to-end check yourself:

```bash
docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --build
```

```bash
python scripts/e2e_check.py --admin-key "$ADMIN_API_KEY"
```

Set `VERDICT_FIRST_MIN=1` and `VERDICT_FINAL_MIN=2` in `.env` first, or the verdict step waits the
normal 10 minutes.
