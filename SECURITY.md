# Security Policy

ChangeWatch protects teams from broken releases while upholding the highest standards of data isolation, privacy, and code safety.

---

## Non-Negotiable Security Guarantees

1. **Read-Only to Customer Repositories:** ChangeWatch only ever reads deploy diffs and stack traces. It never modifies customer code, merges pull requests, creates rollback commits, or disturbs running customer services.
2. **Envelope Encryption at Rest:** Sensitive event payloads, diff snippets, and stack frames are encrypted per-project using AES-256-GCM with Additional Authenticated Data (AAD) bound to the `project_id`.
3. **No Plaintext Secrets:** API keys are hashed with SHA-256 prior to storage and compared using constant-time string equality. Secrets, bearer tokens, AWS credentials, and PII are scrubbed prior to storage and before any LLM inference call.
4. **Zero Unconsented Telemetry:** Telemetry is disabled by default (`TELEMETRY_ENABLED=0`). When enabled, only coarse aggregates (anonymous install UUID, OS family, bucketed release counts) are shared under a strict allowlist (`extra="forbid"`). Zero customer code or personal data is ever collected.

---

## Reporting a Vulnerability

If you discover a security vulnerability in ChangeWatch, please report it privately:

- **Email:** `security@changewatch.dev` (or open a confidential security advisory on GitHub).
- **PGP Key:** Available upon request or via public key directory.

Please include:
- A description of the issue and potential impact
- Detailed steps to reproduce (PoC scripts or curl commands)
- Any proposed remediation

We aim to:
- Acknowledge receipt within **24 hours**.
- Provide an initial assessment and patch timeline within **72 hours**.
- Publicly credit reporters (unless anonymity is requested) in release notes after a fix has been verified and shipped.
