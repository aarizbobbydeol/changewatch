# ChangeWatch Security & Threat Model

ChangeWatch is designed for developer teams who care about security, transparency, and data privacy. This document outlines our core security promises, our threat model, encryption architecture, operational best practices, and honest limits about what our security architecture does and does not protect against.

---

## 1. Core Security Promises

1. **We never modify the customer's code or repository.**
   - All code access is provably read-only (`ReadOnlyGitHubClient`).
   - We never create commits, branches, pull requests, tags, issues, or PR comments.
   - The GitHub Action declares `permissions: contents: read` and `persist-credentials: false`.
   - Agent analysis queries are restricted to SQL `SELECT` operations.

2. **We never disturb the customer's running app.**
   - The SDK cannot crash the host app: every public function is wrapped in failure handlers, internal errors are swallowed and counted, and exceptions are never raised into customer code.
   - The SDK never alters behavior: original exceptions are re-raised unchanged. Request and response bodies, cookies, and authorization headers are never read, stored, or inspected.
   - Non-blocking execution: network deliveries occur on a single background daemon thread with a bounded queue (1,000 max).
   - Strict performance budget: added latency is guaranteed under 1.0 ms at p95 (measured at ~0.005 ms) and memory footprint is under 20 MB.
   - Resiliency: includes automatic circuit breakers after 5 delivery failures and an emergency kill switch (`CHANGEWATCH_DISABLED=1`).

3. **Data is encrypted in transit and at rest.**
   - **In Transit:** SDK and Action enforce HTTPS with strict TLS certificate verification. Reverse proxy profiles provide automatic TLS (via Caddy with Let's Encrypt or local CA). Outbound webhooks are cryptographically signed with HMAC-SHA256 (`X-ChangeWatch-Signature`).
   - **At Rest:** Sensitive columns (`deploys.diff`, `events.stack_trace`, `events.extra`, notification configs) are protected using AES-256-GCM envelope encryption with per-project data keys and Associated Authenticated Data (AAD).

4. **We collect the minimum and the customer controls retention and purging.**
   - Ingestion excludes request bodies, headers, and secrets (`.env*`, `*.pem`, `*.key`, `id_rsa`).
   - Automatic secret scrubbing redacts API keys, cloud tokens, and passwords.
   - Configurable PII scrubbing (`SCRUB_PII=1`) redacts emails, IPv4/IPv6 addresses, JWT tokens, and card numbers.
   - Automated retention pruning removes code diffs after `CODE_RETENTION_DAYS` (default 7 days) and events after `RETENTION_DAYS` (default 30 days).
   - Customers can export (`GET /v1/export`) or purge (`DELETE /v1/project-data`) their data at any time under tenant-isolated API keys.

---

## 2. Honest Limits & Threat Model

Security requires honesty about boundaries and failure modes:

### What Encryption at Rest Protects Against
- **Physical disk theft:** A decommissioned or stolen physical drive cannot reveal customer code, stack traces, or webhook secrets.
- **Backup leaks:** Backups, snapshots, and raw SQL dumps of the database contain only AES-256-GCM ciphertexts. Without the `MASTER_KEY`, data cannot be decrypted.
- **SQL injection / SQL inspection:** A compromised SQL query or unauthorized database observer seeing `stack_trace_enc` or `diff_enc` sees only raw byte sequences.

### What Encryption at Rest Does NOT Protect Against
- **Root / Host Compromise:** The entity running the ChangeWatch server holds the master key and runtime memory. Encryption at rest does **not** protect against an attacker who has root access to the ChangeWatch host or container memory.
- **LLM Plaintext Processing:** An LLM must see plaintext to reason about root causes. While our automated secret and PII scrubbers sanitize the input before submission:
  - If `LLM_PROVIDER=anthropic`, scrubbed diffs and stack traces leave your network over encrypted TLS to Anthropic's API.
  - If `LLM_PROVIDER=ollama`, all data remains entirely on your internal host without leaving the network.

---

## 3. Envelope Encryption Architecture

ChangeWatch utilizes a two-tier envelope encryption architecture:

```
+-----------------------------------------------------------+
| MASTER_KEY (256-bit AES-GCM Key in Env or Docker Secret)  |
+-----------------------------------------------------------+
                             |
         encrypts at creation / wraps during rotation
                             v
+-----------------------------------------------------------+
| Project Data Key (projects.data_key_enc)                  |
+-----------------------------------------------------------+
                             |
               decrypts in memory per project
                             v
+-----------------------------------------------------------+
| Row-Level Ciphertext (BYTEA)                              |
| Format: version(1B) | key_id(2B) | nonce(12B) | tag(16B)  |
| AAD: "{table}.{column}.{project_id}"                      |
+-----------------------------------------------------------+
```

### Context Binding via Associated Authenticated Data (AAD)
Every ciphertext is cryptographically bound to its table, column, and tenant project ID:
$$\text{AAD} = \texttt{"\{table\}.\{column\}.\{project\_id\}"}$$
If an adversary copies ciphertext from `events.stack_trace_enc` of Project 1 to `deploys.diff_enc` of Project 2, decryption will fail with a cryptographic authentication error.

### Key Rotation & Crypto-Shredding
- **Rotate Master Key:** Re-wraps all project data keys using `MASTER_KEY_PREVIOUS` without requiring re-encryption of millions of historical database rows:
  ```bash
  python -m app.cli rotate-master-key
  ```
- **Rotate Project Key:** Re-encrypts all data for a specific project under a fresh project data key:
  ```bash
  python -m app.cli rotate-project-key --project-id 1
  ```
- **Crypto-Shredding:** Permanently destroys a project's data key and associated records:
  ```bash
  python -m app.cli delete-project --project-id 1 --purge
  ```

---

## 4. API Key Role Separation

ChangeWatch splits credentials into two distinct permission scopes:

| Key Role | Prefix | Permissions | Intended For |
|---|---|---|---|
| **Ingest Key** | `cw_ingest_...` | `POST /v1/events`<br>`POST /v1/deploys`<br>`POST /v1/heartbeat` | Python SDK, GitHub Actions CI/CD |
| **Read Key** | `cw_read_...` | `GET /v1/verdict`<br>`GET /api/*`<br>`GET /v1/export`<br>`DELETE /v1/project-data` | Web Dashboard, SRE Audit Scripts |

- **Storage:** Keys are hashed with SHA-256 before storage; plaintext keys are never stored in the database.
- **Verification:** Role checks and key matching are verified in constant time (`hmac.compare_digest`).
- **Blast Radius:** If an Ingest Key leaks from client-side code or CI/CD logs, the key **cannot** be used to read or exfiltrate customer code, error histories, or release verdicts (HTTP 403 Forbidden).

---

## 5. Production Best Practices for Small Teams

1. **Full-Disk Encryption (FDE):** Ensure your host machine runs with disk encryption enabled:
   - macOS: Turn on **FileVault** in System Settings.
   - Linux: Use **LUKS** encryption on the root partition or data volumes.
   - Cloud (AWS/GCP/Azure): Enable KMS-managed volume encryption on EBS/Persistent Disks.
2. **Master Key Separation:**
   - Store `MASTER_KEY` in your secret manager (AWS Secrets Manager, Doppler, 1Password, or Docker secrets).
   - **Never** commit `.env` or `MASTER_KEY` to git repositories.
   - Keep backups of `MASTER_KEY` separate from database backups. A database backup stolen along with its master key provides zero protection.
3. **GitHub Token Minimization:**
   - If using `GITHUB_READ_TOKEN`, issue a **Fine-Grained Personal Access Token** with **Contents: Read-only** permissions exclusively.
4. **Internal Network Isolation:**
   - In production, PostgreSQL and Redis should never expose external host ports. Use internal Docker networks and require passwords (`REDIS_PASSWORD`).
