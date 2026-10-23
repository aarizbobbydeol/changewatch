# MASTER PROMPT: build ChangeWatch (working name) in gated milestones

Read this whole prompt first. It explains what we are building, the decisions already made, how the spec files fit together, and the order to build in. It does not replace the spec files; it tells you which parts to build, when, and when to stop.

## 1. What we are building

An open-source, self-hosted tool for small Python teams that answers one question: **"Did my last release break something, and if so what, where, who changed it, when, and why?"**

How it works:
1. A GitHub Action logs every release (commit, author, diff, changed files, owners) to our server. It only reads.
2. A small pure-Python SDK in the customer's app sends errors, slow requests, and a heartbeat with request counts. It never blocks, raises, or slows the app.
3. The server links every event to the release that was live, groups errors into issues, and opens an incident when a new problem appears after a release.
4. Within seconds, with no AI, the team gets one message: what broke, the exact file and line, whether that line changed in this release, who changed it, who owns it, and a dashboard link.
5. Ten and thirty minutes after each deploy, a rule-based **release verdict** (safe, watch, rollback, no_data) is posted. The AI only explains; it never decides the verdict.
6. Later, a **read-only AI investigator** adds the likely cause, evidence, a suggested fix, and a confidence level, as a reply in the same thread.
7. The tool must never fail silently: if the customer's app stops reporting, or the tool itself degrades, the team is told.

The goal for developers is to be **mind free**: they stop watching dashboards because the tool reliably speaks up at the right time, and only then.

## 2. Principles (non-negotiable; every decision must respect them)

1. **Trust and reliability beat cleverness.** A boring correct alert beats a smart uncertain one. Say "not sure" when the data does not explain a failure.
2. **Read-only toward customer code.** Never modify a customer repository, open pull requests, deploy, or roll back. Only the tool's own database is ever written.
3. **The safety floor is never gated, paused, metered, or delayed:** instant alerts, verdicts, the dead-man's switch, the watchdog and system alerts, notifications.
4. **Never lose data silently:** reliable queue, idempotent writes, durable outbox, honest "dropped events" counters.
5. **Encrypted and minimal:** hashed API keys, per-project encryption of sensitive fields, secrets scrubbed before storage and before any AI call, short retention for code content.
6. **Honest privacy:** state clearly where data goes. With a local model nothing leaves the network; with the Anthropic API, scrubbed diffs and traces are sent to it. No telemetry by default.
7. **Calm:** few false alarms, one incident equals one thread, mutes expire, feedback is collected, thresholds are never changed silently.
8. **No developer rankings.** Show areas of code and trends, never people ranked.
9. **Cheap to run.** Small footprint, batching, storm control so a crash loop cannot inflate cost.
10. **Simplicity.** Build only what the current milestone needs.

## 3. Decisions already made (do not reopen without asking me)

- Backend in **Python 3.12**: FastAPI, asyncpg (plain SQL, no ORM), pydantic v2, PostgreSQL 16, Redis 7, pytest. LLM access only through a provider interface (`anthropic`, `ollama`, `fake`). Dashboard in Next.js, TypeScript, Tailwind. Docker Compose for the full self-hosted stack.
- **Self-hosted, open source, Apache-2.0.** Customer SDK is pure Python, standard library only.
- **Not now:** Rust or Go rewrites, the serverless cloud mode, billing and payments, user accounts, cost tracking, AI code review, app log ingestion, postmortem drafts, runbooks, past-incident memory, revert or fix pull requests, other language SDKs. If any of these spec files are present in the repo (`antigravity_hybrid_cloud_addendum.md`, `antigravity_python_rust_hybrid_addendum.md`), **ignore them for now**. Do not build or stub them, but avoid choices that would block them later (for example, keep queue, scheduler, counters, and storage behind small interfaces).
- Pricing is pay-as-you-go with minimal margin and budget caps, but only usage metering is built (no payments).
- I work on **macOS with no Homebrew**. Use `python3`, a virtual environment, Docker Desktop, and official installers. Never suggest Homebrew.

## 3a. Spec files and how they fit

Each file contains detailed requirements and stages. Follow them, in the order of the milestones below.

| Short name | File | Role |
|---|---|---|
| P1 | Phase 1 prompt | Core pipeline: schema, deploy hook, ingest, worker, SDK, detector, LLM and Slack, read API, dashboard, hardening, load test |
| ADD | `antigravity_addendum.md` | Open source packaging, LLM provider switch, heartbeats, release verdict |
| TRUST | `antigravity_trust_reliability_addendum.md` | Reliable queue, dead-man's switch, watchdog, calm notifications, feedback, ownership, health digest, chaos tests |
| SEC | `antigravity_security_addendum.md` | Encryption, hashed and scoped keys, read-only guarantees, SDK safety, retention |
| PERF | `antigravity_performance_addendum.md` | Batching, worker processes, storm control, data structures, benchmarks |
| USAGE | `antigravity_usage_metering_addendum.md` | Usage metering, price book, budgets (no payments) |
| SRE | `antigravity_ai_sre_prompt.md` | Deploy-time prep, incidents, instant alert, notifier, read-only investigator, evals |
| TD | `antigravity_typed_decisions_addendum.md` | Decision layer: rules-only triage, ephemeral code mode, Jev adapter, verifiers, evals, calibration, secret guard |
| AN | `antigravity_analytics_dashboard_addendum.md` | Analytics dashboard: pure metric calculations, definitions API, team UI, operations panel, export, digest parity |

**Conflict and Precedence Rules:**
- TD and AN win conflicts only within their own topic (TD governs decisions; AN governs analytics and metrics).
- If two other files disagree, the one listed later in this table wins.
- The **Principles in section 2 always win** over all spec files without exception (safety floor never metered/paused, customer code strictly read-only, zero developer rankings).
- **Decision Provider Constraint:** Do not call any hosted decision provider until given a Jev API key (`DECISION_PROVIDER=rules` by default with offline fallback).
- **Analytics Constraint:** Tiles for features not built yet must show "Not enabled", never zeros or sample numbers.

**TD & AN Milestone Placement:**
- **M2:** TD Stages 1 and 2 (rules-only decision layer, triage with fallback)
- **M3 (after M3):** AN Stages 1 to 3 (metric functions, migration, API)
- **M4:** TD Stage 3 (ephemeral code mode)
- **M5:** TD Stages 4, 5 and 7 (Jev adapter, verifier, evals)
- **M6:** TD Stages 6 and 8 (calibration on the Trust page, optional secret guard); AN Stages 4 and 5 (UI, drill-downs, export)
- **M8 (after M8):** AN Stages 6 and 7 (digest parity, docs, performance)

Write every conflict and your decision in `DECISIONS.md`.

## 4. The build plan: milestones

Each milestone lists what to build and a "done when" check. **Stop at the end of every stage and at every milestone gate** (see section 5).

### M0. Foundation
Build: P1 Stage 1; ADD sections A1 to A3 (full-stack Compose skeleton, migration runner, project-creation CLI with no hardcoded keys); `.gitignore`, `.env.example`, `DECISIONS.md`.
Done when: `docker compose up -d` starts the stack, migrations apply, a project can be created with the CLI, `/healthz` is OK.

### M1. Core pipeline (reliable from the start)
Build: P1 Stages 2 and 3; ADD C1 (heartbeats and the `traffic` table); TRUST Stage 1 (`event_id`, reliable queue pattern, durable outbox). Do the reliable queue now because retrofitting it later is costly.
Done when: a curl-sent deploy and error appear linked in the database; the demo app with the SDK produces events with the right `deploy_id`; killing a worker mid-batch loses no events and creates no duplicates.

### M2. Detection, instant alert, verdict (no AI required)
Build: ADD section B (LLM provider interface; `fake` and `ollama` and `anthropic`); ADD section C (release verdict, alerts uniqueness); SRE Stage A (deploy-time preparation: files, owners, CODEOWNERS) and SRE Stage B (incidents, instant alert, notifier channels, team members). The AI investigator itself is NOT in this milestone.
Done when: a simulated bad deploy produces, within seconds, one Slack message with what, where, who, when, and owner tagged; a verdict at 10 minutes and a final check at 30; a healthy deploy gives `safe`; a stopped app gives `no_data`, never `safe`; the false-alarm scenario sends nothing.

### M3. A usable dashboard
Build: P1 Stage 5; ADD C5 (verdict badges, `/v1/verdict`); SRE Stage D without the AI parts (incident page with code excerpt, timeline, owners, "AI investigation not enabled" state). Also build the **Team dashboard** described in section 6.
Done when: the dashboard shows the overview, the timeline with release markers, verdicts, the open incident card, and system status, with honest loading, empty, and error states.

### M4. Release-ready hardening (this is the v0.1 gate)
Build: TRUST Stages 2 and 3 (dead-man's switch, watchdog and system alerts, status panel); SEC Stages 1 to 5 (crypto, hashed and scoped keys, column encryption with the encrypt-existing command, read-only guarantees, SDK safety budget); P1 Stage 6 (hardening, load test baseline); ADD sections A4 to A8 (license, README, CONTRIBUTING, SECURITY, CI, no telemetry, pinned dependencies) and ADD Stage 6 clean-clone test.
Done when: all hardening items pass; a clean clone reaches a first alert in under 30 minutes by following the README; the security checks pass; the baseline benchmark is recorded.
**GATE: stop here. I will test with real teams before anything below starts.**

### M5. AI investigator (after real feedback)
Build: SRE Stages C and E (pre-gathered context, read-only tools, agent loop with limits, validated output, thread reply) and the AI parts of Stage D; evals with at least 12 scenarios including the unexplainable and false-alarm cases.
Done when: the investigator posts a cause, evidence, fix or rollback suggestion, and confidence within about 30 to 45 seconds; it says "not sure" on the unexplainable scenario; `python -m evals.run --provider fake` passes.

### M6. Calm and trust features
Build: TRUST Stages 4 to 8 (ack and assign, signed links, calm notifications, escalation, grouping, mutes, auto-resolve, feedback and Trust page, health metrics and weekly digest, chaos tests); SEC Stages 6 to 8.
Done when: the chaos script shows zero lost events and every alert delivered exactly once.

### M7. Performance (only what measurements justify)
Build: PERF, using the M4 baseline. Apply the rule: if an optimization gains under 20 percent, keep the simple version.
Done when: the benchmark table shows the improvements, and the instant alert stays under 5 seconds p95 during a 5,000 events per second burst.

### M8. Usage metering and budgets
Build: USAGE stages 1 to 6 (meters, price book, budgets, usage API and page). No payments.
Done when: usage is recorded accurately, budgets pause only optional extras, and the safety-floor test passes with the hard limit already hit.

### M9. Owner view and opt-in telemetry
Build: the **Owner dashboard and telemetry** described in section 6.
Done when: telemetry is off by default, the preview screen shows the exact payload, and the owner view shows only aggregates.

## 5. Working protocol (follow exactly)

1. **Start by auditing.** Read the repo and every spec file present. Tell me which milestone and stage the repo is at. Do not redo finished and verified work. List anything the specs promised that is missing, and any conflicts you found.
2. **Then summarize this plan back in at most 15 lines** and wait for my "go" before building.
3. **One stage at a time.** After each stage: run all tests and the linter; update the README, `.env.example`, and `DECISIONS.md`; commit with a clear message; then **stop and report** in this format:
   - What was built (short)
   - How I verify it by hand (exact commands)
   - Test results (counts, anything skipped and why)
   - Deviations from the spec and why
   - Open questions (at most 3)
4. **Do not start the next stage until I say "continue".** Do not start a milestone early, and do not build features from later milestones.
5. **Never claim something works unless you ran it.** If you could not run something, say so plainly.
6. **Ambiguity:** choose the simplest reasonable option, record it in `DECISIONS.md`, and keep going, unless it affects security, data loss, or cost, in which case ask me one clear question.
7. **No scope creep, no new dependencies without asking,** no secrets in code, logs, or git, no placeholder TODOs left silently.
8. If an earlier stage turns out to be wrong, say so and propose a fix instead of building on top of it.
9. At each milestone gate, give me a short "what exists now" summary and the exact steps to try it end to end.

## 6. Dashboards and telemetry (requirements for M3 and M9)

**Team dashboard (M3), for the people using the tool**
- Header: app and environment, overall status (all normal, N open incidents, or no data), last-checked time.
- Four KPIs: releases this week, latest release verdict, median time to fix, share of alerts that were real (show the sample size).
- Open incident card: error, status (possible or confirmed), what broke and how many times, where (file and line, whether changed in this release), who changed it, owner, who is handling it, likely cause and suggested fix with confidence (once M5 exists), and actions (I'm on it, assign, mute, false alarm; the actions arrive with M6).
- Recent releases list with verdict badges.
- Timeline of the last 6 to 24 hours: errors line with release markers, flagged releases in red.
- System status row: app data flowing, notification delivery, AI provider, alert delay.
- Settings: channels, team members, critical routes. Later: quiet hours, mutes, budgets.
- Keep the design plain and consistent with the mockups I created (if I attach screenshots, follow them). No fake data in production views; use honest empty states.

**Owner view and opt-in telemetry (M9), for me as the maintainer**
- **Telemetry is off by default.** On first run, show a clear prompt explaining what would be shared and asking to opt in. A settings page has a **"Preview what would be sent"** screen that shows the exact payload.
- Payload allowlist only: random install ID, version, run mode, OS family, AI provider type, channel types, and bucketed counts of releases and incidents. Never code, errors, repo names, file paths, emails, URLs, or any customer data. Write a test that fails if any field outside the allowlist can be sent, and a test that no network call happens when telemetry is off.
- A small separate **collector service** that only I run receives payloads and stores aggregates. The owner dashboard reads from it and shows: opted-in installs, active this week, new this week, version mix, how it is run, AI provider mix, channel mix, releases tracked, and a panel stating what the page can never show. It is clearly labeled as a lower bound because many installs will not opt in. Public signals (GitHub stars, downloads) are entered manually or pulled from public APIs, not from telemetry.
- Document all of this in the README privacy section.

## 7. Global quality rules

- Small, typed functions; no dead code; plain SQL; money as integer micro-USD.
- Every query on customer data is project-scoped and covered by tenant isolation tests.
- Never log keys, webhook URLs, tokens, diffs, or stack traces.
- Anything the SDK does must be non-blocking and must never raise into the host app.
- Tests for detection, verdicts, scrubbing, encryption, and isolation are mandatory.
- Keep functions and files easy to read for a new contributor.

## 8. Your first reply

Do only this now: (a) audit the repo and spec files, (b) tell me which milestone and stage we are at, (c) list missing items and conflicts, (d) summarize the plan in at most 15 lines, then wait for my "go".
