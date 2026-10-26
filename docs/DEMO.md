# Demo: show ChangeWatch working without a real app

The demo is a pretend online shop. It sends deploys and traffic to ChangeWatch through the public API, exactly as a real app with the SDK and the GitHub Action would. Every few deploys it ships a one-line bug, so people see the whole loop: deploy, errors from the changed line, an instant alert naming the line and its owner, an incident, a verdict, then the fix.

Use it to try ChangeWatch locally, or to run a hosted demo for friends and testers.

## What people will see

| When | What the demo does | What ChangeWatch shows |
|---|---|---|
| Start | 30 minutes of past traffic (the API accepts events up to 24 hours old), so the first deploy has a baseline and the background error is already known | Traffic and one known issue |
| Minute 0 | Healthy deploy, normal traffic (about 450 requests a minute, one known background timeout a minute) | Deploy on the dashboard; verdict `safe` after `VERDICT_FIRST_MIN` (10) |
| Minute 5 | Bad deploy: `app/billing.py` line 42 starts dividing by `order.discount`; about 8% of checkouts fail | Instant alert within seconds: the error, `app/billing.py:42` "changed in this release", owner `@demo-shop/payments`; an incident; the AI investigation in the thread (if an AI provider is set) |
| Minute 15 | Fix deploy (reverts the line); errors stop | Verdict for the bad deploy; the incident calms and resolves after `RESOLVE_AFTER_MIN` |
| Every 30 min after | Healthy, healthy, bad, fix, … The bug rotates between `app/billing.py:42` (ZeroDivisionError), `app/orders.py:88` (KeyError) and `app/search.py:57` (AttributeError) | Analytics, trust metrics and the weekly digest fill up over the following hours and days |

Authors are fake (`alex@demo-shop.example`, …). The demo never touches a repository.

## Try it locally (about 5 minutes)

With ChangeWatch running (`docker compose up -d`, see the README):

1. Create a demo project. Add a Slack or Discord webhook to see the alert there, or leave it out and watch the dashboard:

   ```bash
   docker compose exec api python -m app.cli create-project demo-shop --slack-webhook https://hooks.slack.com/services/...
   ```

   It prints an **ingest key** (for the demo) and a **read key** (for the dashboard).

2. Run one short story: a healthy deploy, a bad deploy after 1 minute, the fix 3 minutes later. It exits by itself.

   ```bash
   docker compose run --rm demo python -m app.demo --once --first-bad-after-min 1 --fix-after-min 3 --ingest-key cw_ingest_...
   ```

3. Open the dashboard at http://localhost:3000, paste the read key in Settings, and look at Incidents and Deploys.

## Run it 24/7 for friends

Set up a server with HTTPS first: [SELF_HOSTING.md](SELF_HOSTING.md) covers a small VPS, a domain and the Caddy `tls` profile. Then:

1. Create the demo project as above and put its ingest key in `.env`:

   ```bash
   DEMO_INGEST_KEY=cw_ingest_...
   ```

2. Start the demo next to the other services. It restarts with them:

   ```bash
   docker compose --profile tls --profile demo up -d
   ```

3. Share the dashboard address and the demo project's **read key**. It sees only the demo project and cannot send events, export or delete data. It can still acknowledge and resolve incidents and start AI investigations (changing settings, the budget, mute rules or runbooks needs the manage key, which you keep), so share it with people you trust, and for a public demo use `LLM_PROVIDER=fake` (or `ollama`) so investigations cost nothing. Never share the ingest key, the admin key or `MASTER_KEY`.

Use a server that holds only demo data. Testers who connect their own app should get their own project (`create-project`), which row-level security keeps separate.

Stop the demo with `docker compose --profile demo stop demo`. Its data ages out with normal retention (30 days), or delete the project and crypto-shred its data with `docker compose exec api python -m app.cli delete-project --project-id <id> --purge`.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_INGEST_KEY` | (required) | Ingest key of the demo project |
| `DEMO_DEPLOY_EVERY_MIN` | `30` | Minutes between deploys after the first story |
| `DEMO_REQUESTS_PER_MIN` | `450` | Simulated traffic |
| `DEMO_FIRST_BAD_AFTER_MIN` | `5` | When the first bad deploy ships |
| `DEMO_FIX_AFTER_MIN` | `10` | How long each bug stays live |
| `DEMO_HISTORY_MIN` | `30` | Minutes of past traffic sent at start (`0` to skip). Each restart sends it again, which adds to the traffic of minutes the demo was already running |

The demo stays well under the ingest rate limit (600 events a minute): about 40 events a minute while a bug is live, a few otherwise. It needs about 0.25 CPU and 128 MB.

## How it works

`server/app/demo.py` is a small state machine (`DemoShop.tick`) driven by the clock. Each bad deploy sends a real diff, the changed file's content and CODEOWNERS; its errors carry a stack trace through the changed line and `release` set to the deploy's sha. Heartbeats report the requests and errors of each minute. Every event has a unique `event_id`. `server/tests/test_demo.py` checks the schedule, that errors only come from the live bug and point at the changed line, and that heartbeats match the errors sent.
