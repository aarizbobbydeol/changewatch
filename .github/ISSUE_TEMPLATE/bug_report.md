---
name: Bug report
about: Something in ChangeWatch did not work as expected
labels: bug
---

**What happened**
<!-- What you saw. Paste the exact error or alert text if you have it. -->

**What you expected**

**How to reproduce**
1.
2.
3.

**Environment**
- ChangeWatch version / commit:
- How you run it: Docker Compose / local (`uvicorn`) / other
- Postgres / Redis versions:
- LLM_PROVIDER and DECISION_PROVIDER:

**Logs**
<!-- Relevant lines from `docker compose logs api worker detector notifier investigator`.
     ChangeWatch redacts keys and webhook URLs in its logs, but please double-check
     that nothing secret (API keys, webhook URLs, stack traces with customer data) is pasted here. -->

> Found a security problem? Please do **not** open an issue; follow [SECURITY.md](../../SECURITY.md) instead.
