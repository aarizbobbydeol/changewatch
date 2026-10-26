# Changelog

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
