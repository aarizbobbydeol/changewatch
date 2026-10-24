# Data pipeline (ETL / ELT)

ChangeWatch's data path is an ETL pipeline. Your app's errors and traffic go in at one end, and verdicts, alerts, analytics and exports come out at the other. This page shows every stage, what it guarantees, and how to check those guarantees on a running install with one command.

There is no separate pipeline system. The stages below *are* the product: the SDK, the API, the Redis queue, the worker, Postgres and the detector.

```mermaid
flowchart LR
    subgraph E["Extract"]
        SDK["Python SDK<br/><sub>errors · slow requests · heartbeats<br/>scrubbed · uuid event_id · batches of 20 / 2 s</sub>"]
        GHA["GitHub Action<br/><sub>deploy: sha, diff, files, CODEOWNERS</sub>"]
    end
    subgraph V["Validate"]
        API["API /v1/events, /v1/deploys<br/><sub>ingest key · schema caps · 600 events/min<br/>backpressure shedding · 503 + Retry-After</sub>"]
    end
    subgraph Q["Queue"]
        R[("Redis list events<br/><sub>reliable: processing list per worker,<br/>reaper, 3 retries, events:dead</sub>")]
    end
    subgraph T["Transform (worker)"]
        W["claim event_id (exactly-once)<br/>scrub secrets + PII<br/>fingerprint → issue<br/>match deploy (release, else time)<br/>storm control · encrypt"]
    end
    subgraph L["Load (Postgres, row-level security)"]
        I[("issues<br/><sub>event_count</sub>")]
        EV[("events<br/><sub>weekly partitions, encrypted</sub>")]
        RU[("event_rollups<br/><sub>per issue · deploy · minute</sub>")]
        TR[("traffic<br/><sub>requests · errors per minute</sub>")]
        D[("deploys + files")]
    end
    subgraph O["ELT outputs"]
        DET["detector: verdicts,<br/>incidents, alerts"]
        AN["/v1/analytics/*<br/>dashboard"]
        DG["weekly digest"]
        EX["/v1/export (JSON)<br/>usage CSV"]
    end
    SDK --> API
    GHA --> API
    API --> R
    API -- deploys --> D
    R --> W
    W --> I & EV & RU & TR
    I & EV & RU & TR & D --> DET & AN & DG & EX
```

## Stages

| Stage | Where | What happens |
|---|---|---|
| **Extract** | `sdk/changewatch` | Captures unhandled errors, slow requests and a heartbeat (`requests`, `errors`) every 60 s when there was traffic. Scrubs secrets and PII before sending. Every event gets a uuid `event_id`. Bounded queue (1,000, drops when full and counts the drops), batches of 20 or every 2 s, 3 s timeout. On 429/503 it honours `Retry-After` and re-queues the batch; on network errors it re-queues and backs off. |
| | `action/` (GitHub Action) / `POST /v1/deploys` | Each deploy sends sha, author, message, diff, changed files and CODEOWNERS. Deploys are written straight to Postgres (no queue) and are never dropped. |
| **Validate** | `POST /v1/events` (`server/app/main.py`) | Ingest key only. Pydantic caps every field (100 events per request, 20 KB stack trace, 10 KB `extra`). Rate limit 600 events and 60 heartbeats per key per minute (`429`). Above `QUEUE_SOFT_LIMIT` (10,000 queued) it sheds load: slow requests first, then 9 in 10 repeats of known errors. Heartbeats and the first event of a new error are never dropped. The response says how many were `accepted` and `dropped`. The batch is queued in one `MULTI/EXEC`, so it is queued whole or not at all; if Redis is down the API answers `503` with `Retry-After: 30` and the SDK keeps the events. |
| **Queue** | Redis `events` | Reliable queue: a worker moves items to its own `events:processing:<worker>` list (`BLMOVE`), removes them only after the database commit, and a reaper returns items left by a crashed worker. A batch that fails is retried per event up to 3 times, then moved to `events:dead`. |
| **Transform** | `server/app/worker.py` | Per project, in one database transaction: **claim** each `event_id` in `ingest_dedupe` and skip events already claimed; **scrub** message, stack trace, path and `extra` again (any HTTP client can send events, not only the SDK); **fingerprint** (exception type + normalized stack trace, or the message when there is no trace; the path for slow requests) and upsert the issue; **match** the deploy (by `release`, else the latest deploy before the event in that environment); **storm control** keeps at most `STORE_FULL_PER_MIN` (20) full rows per error per minute while counts stay exact; **encrypt** stack traces and `extra` (AES-256-GCM, per-project key). |
| **Load** | Postgres | `issues.event_count`, `events` (weekly partitions, encrypted columns), `event_rollups` (count per issue, deploy and minute), `traffic` (requests and errors per minute), `sources` (last seen per service). Every table has forced row-level security, and tenant work runs as a role that cannot bypass it. |
| **ELT outputs** | detector, API | The detector turns traffic and rollups into verdicts (`safe` / `watch` / `rollback` / `no_data`), incidents and alerts. `/v1/analytics/*` and the dashboard read the same tables. The weekly digest summarises them. `GET /v1/export` (admin key plus the project's read key) returns all of a project's data as JSON (decrypted); `GET /v1/usage/export.csv` returns usage. |
| **Retention** | detector, hourly | Event partitions older than `RETENTION_DAYS` (30) are dropped; code (diffs, files, CODEOWNERS) after `CODE_RETENTION_DAYS` (7); the dedupe ledger after `DEDUPE_WINDOW_HOURS` (24). |

## Guarantees

- **Exactly-once counting.** Delivery is at-least-once (the SDK and the worker both retry), so the worker deduplicates by `event_id`. The claim is in the same transaction as the counters: if the batch rolls back, the claim is released and the retry still counts. A re-sent event changes nothing: not the issue count, the rollups, the traffic or the stored rows. The window is `DEDUPE_WINDOW_HOURS` (default 24 h). Retries arrive within minutes, so this is generous.
- **No silent loss.** Events are lost only where it is visible: the SDK's own full queue (counted), backpressure shedding (returned as `dropped` and counted in `/healthz`), or `events:dead` after 3 failed attempts.
- **Counts stay exact under storms.** Storm control limits stored rows, not counts. `issues.event_count` and the sum of `event_rollups.count` always equal the number of distinct events received.
- **Nothing sensitive in plaintext.** Secrets and PII are scrubbed in the SDK and again in the worker. Stack traces and extras are stored only encrypted. Issue titles are scrubbed.
- **Tenant isolation.** Every stage is per project, and Postgres enforces it with row-level security.

## Check it on a running install

`scripts/verify_pipeline.py` sends a known batch through the real API, then checks each stage in the database:

```bash
DATABASE_URL=postgresql://changewatch:PASSWORD@localhost:5432/changewatch \
  python scripts/verify_pipeline.py --api http://localhost:8000 \
  --ingest-key cw_ingest_... --read-key cw_read_... --admin-key "$ADMIN_API_KEY"
```

It needs `asyncpg` (installed with the server requirements) and prints one line per check:

```
PASS extract: API accepted 31/31, dropped 0
PASS transform: events grouped into one issue
PASS load: issue.event_count 30 == 30 sent
PASS storm control: 20 full rows stored (cap 20/min), counts stay exact
PASS load: stack traces stored encrypted only
PASS ELT: rollups sum 30 == issue.event_count 30
PASS ELT: heartbeat added 100 requests to traffic (sent 100)
PASS exactly-once: full rows unchanged after re-send (20)
PASS exactly-once: issue.event_count unchanged after re-send (30)
PASS exactly-once: rollups unchanged after re-send (30)
PASS exactly-once: traffic unchanged after re-send (+0)
PASS export: issue event_count 30 == database 30
PASS export: 20 stored events exported with decrypted stack traces
```

It adds one test issue (`ValueError: pipeline check`) and 100 test requests to your traffic, so run it against a test project. With Docker Compose, run it from a machine that can reach Postgres, or temporarily publish the `db` port.

## Not included

There is no export to an external warehouse (BigQuery, Snowflake and similar). `GET /v1/export` and direct read access to Postgres cover that need for now.
