# Contributing to ChangeWatch

Thank you for your interest in contributing to ChangeWatch! We welcome contributions to our open-core self-hosted release monitoring system.

---

## Development Principles

Before contributing code, please review our core architectural rules:

1. **Safety Floor Exemption:** Core alerts, verdicts, watchdog health checks, and the dead-man's switch must NEVER be gated, metered, paused, or delayed.
2. **Customer Code is Strictly Read-Only:** Zero mutating verbs against customer code or repositories.
3. **Pure Python SDK:** The SDK in `sdk/` must rely strictly on standard library modules (zero external third-party dependencies) and maintain an execution budget of \(< 1.0\text{ ms } p95\) latency and \(< 20\text{ MB}\) memory overhead.
4. **No Developer Rankings:** Never implement views, metrics, or endpoints that rank or score individual engineers.
5. **Architectural Decisions:** Any schema change, protocol modification, or invariant change must be documented in `DECISIONS.md`.

---

## Local Setup

```bash
# 1. Clone repository
git clone https://github.com/your-org/changewatch.git
cd changewatch

# 2. Start PostgreSQL and Redis via Docker Compose
docker compose -f docker-compose.dev.yml up -d

# 3. Create and activate a Python 3.12 virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 4. Install dependencies
pip install -r server/requirements.txt -r sdk/requirements.txt
```

---

## Running Verification & Tests

Always run the full suite before submitting a Pull Request:

```bash
# Run unit and integration tests
pytest server/tests sdk/tests

# Run AI SRE & Decisions eval benchmark
python -m evals.run --provider fake

# Run automated security & AST read-only compliance checks
python scripts/security_check.py

# Run chaos test suite
python scripts/chaos.py
```

All 288+ tests and compliance scripts must be 100% green.
