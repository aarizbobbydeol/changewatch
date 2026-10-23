# ADDENDUM: Typed decisions (Jev), verification, calibration, and ephemeral code mode

Goal: add a fast, cheap **decision layer** next to the rules and the LLM investigator. It answers small typed questions (a choice, a score, or a probability) in one parallel call, and our code owns the policy (thresholds), not a prompt. We use TypeSafe's **Jev** as the first hosted provider, behind our own interface so providers can change. We also add verification of AI answers, calibration on the Trust page, and an optional ephemeral code mode.

This addendum fits into the milestones of the master prompt (see section J). Do not build it early. If something earlier prompts promised is missing, tell me first. I work on macOS with no Homebrew; use `python3` and a venv. When this conflicts with earlier prompts, this one wins; write the change in `DECISIONS.md`.

## 0. Why this is efficient (and the rules that keep it safe)

Efficiency wins we are taking:
- One request answers many questions in parallel, so triage adds well under a second.
- Typed answers mean no free-text parsing, no retries from malformed output, and deterministic tests.
- Policy lives in config thresholds, so changing risk tolerance is a config change, not a prompt rewrite.
- Cache by fingerprint, skip the call when rules already decide, and keep inputs small, so cost per incident stays tiny.
- Break work into small steps: deterministic rules, then small typed decisions, then a single LLM explanation. Never one giant prompt for a decision.

Hard rules:
1. **The decision layer annotates and prioritizes. It never decides the release verdict, and it never fully silences an incident.** The verdict stays rule-based. A "probably noise" answer can at most lower severity to `low` (digest only, still stored and visible); it can never delete, hide, or skip an incident.
2. **It must never delay the safety floor.** Instant alerts must go out on time without it. Use a short timeout; on timeout or failure, fall back to rules-only values and label the alert "triage unavailable".
3. **Disclose data egress.** Using a hosted provider sends scrubbed text to that provider. It requires explicit opt-in, shows in the startup log, and is written in the README privacy section. Code excerpts are never sent unless `DECISION_SEND_CODE=true`.
4. **Everything is logged as data, not prose:** question id, answer, probability, source, model, latency, cost.

## A. The decision abstraction

Create `server/app/decisions/` with:

```python
class Question:  # one of three kinds
    id: str; kind: Literal["choice","score","noul"]; instructions: str
    options: dict[str,str] | None      # choice: option id -> description (max 255)
    rubric: list[str] | None           # score: ordered levels, low to high
class Answer:
    id: str; kind: str
    choice: str | None; score: float | None; probability: float | None   # noul is P(yes)
    confidence: float | None; probabilities: dict | None
    source: Literal["rules","jev","fake"]; model: str | None
class DecisionProvider(Protocol):
    async def decide(self, state: dict | str, questions: dict[str, Question]) -> dict[str, Answer]
```

Providers, selected by `DECISION_PROVIDER` (default `rules`):
- `rules`: deterministic fallback computed in code (for example regression probability 1.0 if the stack frame is inside the diff, 0.5 otherwise; noise probability from a small known-patterns list such as health checks and client disconnects). Always available.
- `fake`: scripted answers for tests.
- `jev`: the hosted TypeSafe API (section H).
All providers return the same `Answer` shape and mark `source`. Add a thin wrapper `decide_with_fallback(...)` that applies the timeout, a circuit breaker (open after 5 consecutive failures, retry later), and falls back to `rules`.

## B. Building the state sent to a provider

- Build a compact `state` from already-scrubbed data: error title, exception type, the top 5 stack frames (paths and function names), request path, event counts and rate before and after, whether the frame is inside the diff, the commit message, new-fingerprint flag, and previous occurrences. No code unless `DECISION_SEND_CODE=true`, and then only a small excerpt (at most 20 lines) around the frame.
- Cap the state at `DECISION_STATE_MAX_CHARS` (default 20,000; the provider's context limit is about 64k tokens).
- Run the existing secret and PII scrubbers first.

## C. Decision catalog (version 1)

Define each question in code with its id, kind, instructions, options or rubric, and thresholds from config. Store the catalog version with every decision.

**Triage (at incident open, one batched call, parallel):**
- `is_regression_from_release` (noul): "Is this error likely caused by the code changes in this release?"
- `is_known_noise` (noul): "Is this an expected or harmless error that does not need human attention (health checks, bots, client disconnects, harmless retries)?"
- `is_transient_infra` (noul): "Is this more likely a temporary infrastructure problem (network, timeout, dependency outage) than a code bug?"
- `severity` (score, ordered rubric 0 to 4: cosmetic, minor, degraded, major, outage).
- `error_category` (choice): logic_bug, missing_data_or_null, config_or_env, dependency_or_version, database_or_migration, external_service, performance_timeout, security_or_auth, other.
- `owner_area` (choice): the options are the deterministic owner candidates only (from CODEOWNERS and authors). It may rank them but can never invent an owner. Deterministic owners stay authoritative.

**Investigation support (milestone M5):**
- `rollback_would_fix` (noul): "Would reverting this release likely stop these errors?"
- `suggested_action` (choice): rollback, hotfix, config_change, monitor, ignore.
- `same_root_cause` (noul): for pairs of issues in one deploy (at most `DECISION_MAX_PAIRS=10` pairs), used to group alerts into one incident.

**Verification (milestone M5):**
- `evidence_supports_conclusion` (noul): given the investigator's stated cause and its evidence list, "Does the evidence support this conclusion?" If below `VERIFY_MIN=0.5`, force confidence to `low` and add "evidence is weak". This is a verifier on the AI's own answer.
- `suggestion_is_unsafe` (noul): applied to any suggested fix text. At or above `UNSAFE_MAX=0.5`, show it only with a warning label. (The tool is read-only, but humans may run what it suggests.)

**Optional guardrail (off by default, `SECRET_GUARD_ENABLED=false`):**
- `contains_secret_or_credential` (noul): a second pass on outbound text after regex scrubbing. At or above `SECRET_GUARD_BLOCK=0.5`, replace that field with a redaction placeholder and log a guard event (never the content). Document honestly in the README that this check itself sends the already-scrubbed text to the provider, so use it only where egress to that provider is already accepted.

## D. Policy in config, not prompts

Defaults (all configurable globally and per project, with an audit trail of changes):

| Setting | Default | Effect |
|---|---|---|
| `TRIAGE_REGRESSION_CONFIRM` | 0.80 | At or above: label "likely caused by this release" |
| `TRIAGE_REGRESSION_POSSIBLE` | 0.30 | Between possible and confirm: "possible". Below: "probably unrelated to this release" (new fingerprints still alert) |
| `NOISE_DOWNGRADE` | 0.95 | At or above, and not a critical route, and verdict is not rollback: severity becomes `low` (digest only). Never suppress or delete |
| `SEVERITY_CRITICAL` | 3.0 | Score at or above: `critical` (escalation rules apply). At or above 2.0: `high`. Otherwise `low` |

Show the probabilities in the instant alert and on the incident page (for example "regression probability 0.91, severity 3.2, category: missing data"), and label values that came from `rules` rather than a model.

## E. Efficiency mechanics

- **One call per incident** with all triage questions batched; questions share one `state`.
- **Cache** answers by (project, fingerprint, deploy id, catalog version, provider, model) with a TTL. For questions that do not depend on the diff (`is_known_noise`, `error_category`), cache by fingerprint across deploys.
- **Skip the call** when rules already decide (for example the same fingerprint was triaged within the TTL).
- **Concurrency limits:** `DECISION_CONCURRENCY`, `DECISION_TIMEOUT_MS` (default 800 for the instant path, longer for the investigator), one retry, then fallback.
- **Metering:** record each call as a `decision_call` meter (units 1, our cost from input tokens times `DECISION_PRICE_PER_MTOK`, default from the provider's published price, editable). If the usage metering addendum is present, register the meter in the price book as cost-plus with a generous free allowance.
- Instant alert path target stays under 5 seconds p95 including the decision call.

## F. Calibration and evals (the trust win)

- Table `decisions(id, project_id, incident_id, question_id, kind, answer JSONB, probability, confidence, source, model, catalog_version, state_hash, latency_ms, cost_micros, created_at)` with row-level security. Never store the state text, only its hash.
- **Calibration job** (nightly): join `decisions` with outcomes (alert feedback real or false, resolution) and compute, for `is_regression_from_release` and `is_known_noise`, a reliability table (bins of predicted probability versus observed rate), the Brier score, and sample sizes. On the **Trust page** show "when we said about 90 percent, we were right X percent", hide bins with fewer than 10 samples, and always show the sample size.
- **Eval extension:** add decision-level labels to the 12 existing eval scenarios. Score accuracy per question at the configured thresholds, compare providers (`rules`, `fake`, `jev`) side by side, run a **determinism check** (same input five times with a pinned model must give identical or near-identical answers), and measure the end-to-end instant path with the decision layer on versus off (correctness, severity, time to first alert). Do not change thresholds without an eval run, and record the result in `DECISIONS.md`.

## G. Ephemeral code mode (optional, for privacy-sensitive teams)

Add `CODE_MODE=stored|ephemeral`, default `stored`.
- `stored`: current behavior (code content kept for `CODE_RETENTION_DAYS`).
- `ephemeral`: the server never stores code. The GitHub Action sends only metadata (paths, changed line ranges, authors, CODEOWNERS), no file contents and no diff body. When the investigator needs a file, it fetches it with the GET-only GitHub client using `GITHUB_READ_TOKEN`, holds it in memory for the duration of that investigation, and discards it. Incident bundles keep only the path, line numbers, and a short excerpt (at most 10 lines) around the error. The instant alert must still work from metadata alone.
- Document the trade-offs in the README: needs a read-only token, slower, depends on GitHub availability and rate limits, and "dig deeper" later works only if the commit is still reachable.
- Test: in ephemeral mode, inspect the database, the blob store, logs, and the queue after a full incident; no file content may appear anywhere.

## H. Jev provider adapter (`DECISION_PROVIDER=jev`)

Implement with `httpx` (already a dependency; do not add the vendor SDK). Use the public API shape below, but **verify it against the official docs (docs.typesafe.ai, primitives and quick start) and the playground before coding, record real request and response samples as test fixtures, and follow the docs where they differ from this description; note differences in `DECISIONS.md`. Do not guess fields.**

- Endpoint: `POST https://api.typesafe.ai/v1/systemone` (configurable with `JEV_BASE_URL`, since compatible gateways exist). Header `Authorization: Bearer $JEV_API_KEY`.
- Body: `{ "model": ..., "state": ..., "questions": { "<id>": { "type": "choice"|"score"|"noul", "instructions": ..., ...options or criteria } } }`.
- Responses are keyed by question id: choice returns the chosen option plus probabilities and confidence; score returns an expected (possibly fractional) score on the ordered rubric plus confidence; noul returns the probability that the proposition is true and no confidence.
- Limits: up to 255 choice options per question; about 64k tokens of context per request. Enforce `DECISION_STATE_MAX_CHARS` and the option limit before sending.
- `JEV_MODEL`: default to a **pinned** release (not the moving `latest` alias) so tests and calibration are stable; allow `jev-latest` by configuration.
- Write good instructions: each question should be a snap judgment a knowledgeable person could make quickly, stated as one clear proposition (noul) or a small set of distinct options (choice). Keep them in the catalog and version them.
- Use the key only from env, never log it, never send secrets, and honor the egress rules in section 0.

## I. Config (add to `.env.example` and the README table)

`DECISION_PROVIDER`, `DECISION_TIMEOUT_MS`, `DECISION_CONCURRENCY`, `DECISION_STATE_MAX_CHARS`, `DECISION_SEND_CODE`, `DECISION_MAX_PAIRS`, `DECISION_PRICE_PER_MTOK`, `JEV_API_KEY`, `JEV_BASE_URL`, `JEV_MODEL`, `TRIAGE_REGRESSION_CONFIRM`, `TRIAGE_REGRESSION_POSSIBLE`, `NOISE_DOWNGRADE`, `SEVERITY_CRITICAL`, `VERIFY_MIN`, `UNSAFE_MAX`, `SECRET_GUARD_ENABLED`, `SECRET_GUARD_BLOCK`, `CODE_MODE`, `GITHUB_READ_TOKEN`.

## J. Where it fits in the milestones, and stages (stop after each and report)

| Stage | Build | Milestone |
|---|---|---|
| 1 | Provider interface, `rules` and `fake` providers, `decisions` table, catalog, thresholds config, fallback wrapper | M2 |
| 2 | Triage questions on the incident-open path with timeouts, labels, severity, and the "never suppress" invariant | M2 |
| 3 | `CODE_MODE=ephemeral` and its tests | M4 |
| 4 | Jev adapter with fixtures, caching, metering, startup disclosure | M5 (needs my Jev API key) |
| 5 | Investigator verifier (`evidence_supports_conclusion`, `suggestion_is_unsafe`), `rollback_would_fix`, `suggested_action`, `same_root_cause` grouping | M5 |
| 6 | Calibration job and Trust page panel | M6 |
| 7 | Eval extension and determinism check | M5/M6 |
| 8 | Optional secret guard | M6, off by default |

## K. Tests (all required)

- Provider contract tests for `rules`, `fake`, and `jev` (with recorded fixtures through a mocked HTTP layer); identical `Answer` shape.
- **Timeout and failure:** the instant alert is still sent on time with rules-only values and the "triage unavailable" label; the circuit breaker opens and recovers.
- **Invariants:** with a noise probability of 0.99, the incident is still recorded, visible, and notified at `low` severity or in the digest; the release verdict is byte-for-byte identical with the decision layer on, off, and failing; `owner_area` can never return a person outside the deterministic candidates.
- Threshold boundary tests (exactly at 0.30, 0.80, 0.95, 3.0).
- State builder: scrubbing applied, size cap enforced, no code unless `DECISION_SEND_CODE=true`.
- No network call is made unless `DECISION_PROVIDER=jev`; the startup log states where data goes.
- Cache hits and skips, concurrency limit, metering records, tenant isolation on `decisions`.
- Calibration math against a fixture with known answers; small bins hidden.
- Determinism test and decision-level evals.
- Verifier: weak evidence forces `low` confidence; unsafe suggestions get the warning label.
- Ephemeral mode: no file content in the database, blob store, logs, or queue.
- Secret guard (when enabled): blocks the field, logs no content.

## L. Definition of done

A simulated bad deploy produces the instant alert on time with probabilities, severity, category, and owner ranking, labeled by source. Turning the provider off or breaking it changes nothing about alert timing or the release verdict. The Trust page shows a calibration view with sample sizes. The investigator's low-evidence answers are downgraded automatically. In ephemeral mode, no code content exists anywhere on the server after an incident.
