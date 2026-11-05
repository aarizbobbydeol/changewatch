# Changelog

## 1.0.2 (2026-11-03)

- **Django and Flask apps:** the SDK now works with Django (`changewatch.DjangoMiddleware`, WSGI or ASGI, sync and async views), Flask (`changewatch.instrument_flask(app)`) and any WSGI app (`changewatch.WSGIMiddleware`). Before, only FastAPI/Starlette apps were watched; a Django or Flask app sent nothing, so every deploy came out `no_data`. Tested against real Django 4.2 and 6.1, and Flask 2.3 and 3.1.
- **A final verdict stays final:** the detector judged every deploy of the last two hours again each minute, even after its final check, and its first rule (the app is silent right now, so `no_data`) turned a past ROLLBACK or SAFE into NO DATA as soon as the app went quiet: at night, on a quiet weekend, or after the bad release was undone. A deploy is now left alone once its final verdict is given.
- **No made-up AI explanation without an AI provider:** an install without an Anthropic key (the installer then sets `LLM_PROVIDER=fake`) showed a test answer, "Unhandled KeyError in user_service.py", as the AI explanation of every watch or rollback deploy, on the dashboard and in the verdict alert. Without a provider there is now no AI explanation there; the verdict reasons, the risk review and the rules-based investigation still say what broke and where.
- **A new deploy no longer shows "NO DATA":** until a deploy's first check (10 minutes after it) its verdict is only a running estimate, which reads `no_data` until the app's first traffic report, so a brand-new deploy looked as if the app had gone silent. The deploy APIs (`/api/deploys`, `/v1/verdict`) now say `verdict_checked`, and the dashboard shows CHECKING until the first check. A pipeline waiting for a verdict should wait for `verdict_checked`.
- **Tested as each kind of user installs it:** CI now installs the SDK on Python 3.9 to 3.14, on Linux and Windows, with the oldest and newest supported Django and Flask, and runs the GitHub Action exactly as a user's workflow does. The README has a 30-second recording of one bad deploy and screenshots from a real run.
- **The dashboard on Vercel:** the engine stays on your server, and the dashboard can also live on Vercel, passing its requests to your server over HTTPS. A Vercel build without an `https://` server address stops and says what to set. `install.sh --dashboard-url` makes alert links open it. See [docs/VERCEL.md](docs/VERCEL.md).
- **The installer on more servers:** it installs Docker from docker.com, so new Debian servers work as well as Ubuntu, on Intel/AMD and ARM. It waits while a new server finishes updating itself instead of failing. CI now installs ChangeWatch on new Ubuntu 22.04 and 24.04 servers (Intel/AMD and ARM) and adds Docker on Debian 12 and 13 on every change.

## 1.0.1 (2026-10-27)

**Setup is one command.** `scripts/install.sh` takes an empty Ubuntu server (or a machine with Docker) to a running ChangeWatch with HTTPS, fresh secrets, a first project and its keys, and optionally the demo shop. It installs Docker if missing and is safe to run again:

```bash
curl -fsSL https://raw.githubusercontent.com/aarizbobbydeol/changewatch/main/scripts/install.sh | sudo bash -s -- --domain changewatch.example.com
```

**Security:** on a server, the API (8000) and dashboard (3000) ports were published on every network interface, and Docker's published ports bypass `ufw`, so they were reachable over plain HTTP next to the HTTPS site. They now listen on `CHANGEWATCH_BIND`; the installer sets `127.0.0.1` when you give it a domain, so only HTTPS (80/443) faces the internet. **Upgrading a 1.0.0 server that has a domain:** add `CHANGEWATCH_BIND=127.0.0.1` to `.env`, then `docker compose pull && docker compose up -d`.

**Also:**
- The Python SDK is on PyPI: `pip install changewatcher` (still `import changewatch`), published from GitHub with trusted publishing.
- Security and conduct reports go through GitHub's private vulnerability reporting.
- The SDK and dashboard packages state their Apache-2.0 license, and the SDK ships the license text.

## 1.0.0 (2026-10-26)

The first release. ChangeWatch tells a team, within seconds of a deploy, whether that deploy broke something, where, and what to do about it. It runs on your own server with Docker Compose.

**Every deploy**
- Release verdicts at +10 and +30 minutes (`safe`, `watch`, `rollback`, `no_data`) from fixed, written-down rules; silence is never reported as safe. `GET /v1/verdict` lets a pipeline wait for it.
- A risk review of the diff before any error arrives: fixed rules, plus the AI when configured.
- AI spend per release, with a warning when a release doubles it.

**Every incident**
- An instant alert (Slack, Discord, Teams, email, signed webhooks) naming the file, the line, whether that line changed in this deploy, and the code owners. Chat text is made safe: no channel-wide pings or disguised links.
- An AI investigation (Claude, or local Ollama) with the change, the requests that failed, the timing, the app's log lines and past incidents; checked against its evidence.
- "Seen before" past incidents, the team's runbooks, the exact commands to undo the deploy or only the failing file, and the incident's cost.
- A full timeline and a blameless postmortem draft.
- Optional (off by default): a **draft** pull request that undoes the deploy, using the project's own GitHub token. ChangeWatch never merges anything.

**Built in**
- A Python SDK with no dependencies that never raises into or slows your app; a GitHub Action for deploys; an MCP server for AI assistants (read-only).
- Per-project keys: ingest (send), read (view and act on incidents), manage (change settings, budget, mute rules and runbooks).
- Row-level security per project, AES-256-GCM encryption of code, traces, logs, AI results and queued messages, secret and PII scrubbing, log redaction.
- Usage metering with budgets that can pause AI only, never verdicts or alerts.
- A dashboard: timeline, incidents, analytics, trust (alert accuracy), usage, settings.

**Install**: see [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md). Images: `ghcr.io/aarizbobbydeol/changewatch-server:1.0.0` and `ghcr.io/aarizbobbydeol/changewatch-dashboard:1.0.0`, used by `docker-compose.release.yml`. Every design choice and how it was verified is in [DECISIONS.md](DECISIONS.md).
