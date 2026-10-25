import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Locate .env file if available
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()


# Model families Anthropic no longer serves (Claude 3.x and earlier)
RETIRED_MODEL_PREFIXES = ("claude-instant", "claude-1", "claude-2", "claude-3-")
# Settings that named a retired model and were replaced: (variable, old value, new value)
RETIRED_MODEL_REPLACEMENTS: list = []


def _current_model(names: tuple, default: str) -> str:
    """The first of `names` that is set. A retired model would make every AI call fail, so it is
    replaced by `default` and reported at startup (log_llm_privacy_notice)."""
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            if value.startswith(RETIRED_MODEL_PREFIXES):
                RETIRED_MODEL_REPLACEMENTS.append((name, value, default))
                return default
            return value
    return default


class Settings(BaseModel):
    # Database and Cache
    DATABASE_URL: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql://changewatch:changewatch@localhost:5432/changewatch",
        )
    )
    REDIS_URL: str = Field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )

    # Project Seeding
    SEED_DEV_PROJECT: bool = Field(
        default_factory=lambda: os.getenv("SEED_DEV_PROJECT", "0") in ("1", "true", "True")
    )

    # LLM Settings (Addendum Section B)
    # Options: "anthropic", "ollama", "fake"
    LLM_PROVIDER: str = Field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "fake" if os.getenv("DRY_RUN_LLM", "0") in ("1", "true", "True") else "anthropic")
    )
    ANTHROPIC_API_KEY: str = Field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    # Fast model for automatic investigations and deploy summaries; the deep model is for "Dig deeper".
    # LLM_MODEL is the older name for the fast model and is still read.
    LLM_MODEL_FAST: str = Field(
        default_factory=lambda: _current_model(("LLM_MODEL_FAST", "LLM_MODEL"), "claude-sonnet-5-5")
    )
    LLM_MODEL_DEEP: str = Field(
        default_factory=lambda: _current_model(("LLM_MODEL_DEEP",), "claude-opus-5-5")
    )
    LLM_MODEL: str = Field(
        default_factory=lambda: _current_model(("LLM_MODEL_FAST", "LLM_MODEL"), "claude-sonnet-5-5")
    )
    # Reasoning effort (low, medium, high, xhigh, max): higher is more thorough, slower and costlier
    LLM_EFFORT_FAST: str = Field(default_factory=lambda: os.getenv("LLM_EFFORT_FAST", "medium"))
    LLM_EFFORT_DEEP: str = Field(default_factory=lambda: os.getenv("LLM_EFFORT_DEEP", "high"))
    OLLAMA_BASE_URL: str = Field(
        default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    )
    OLLAMA_MODEL: str = Field(
        default_factory=lambda: os.getenv("OLLAMA_MODEL", "llama3")
    )
    DRY_RUN_LLM: bool = Field(
        default_factory=lambda: os.getenv("DRY_RUN_LLM", "0") in ("1", "true", "True")
    )

    # Decision Provider Settings (Typed Decisions Addendum)
    DECISION_PROVIDER: str = Field(
        default_factory=lambda: os.getenv("DECISION_PROVIDER", "rules")
    )
    JEV_API_KEY: str = Field(
        default_factory=lambda: os.getenv("JEV_API_KEY", "")
    )
    JEV_BASE_URL: str = Field(
        default_factory=lambda: os.getenv("JEV_BASE_URL", "https://api.typesafe.ai/v1/systemone")
    )
    JEV_MODEL: str = Field(
        default_factory=lambda: os.getenv("JEV_MODEL", "jev-2024-10")
    )


    # Ingestion & Detection Settings
    # Each SDK process sends one heartbeat a minute, so this is the number of app processes one
    # ingest key can have; heartbeats over it are dropped (counted), never the errors with them
    HEARTBEAT_LIMIT_PER_MIN: int = Field(
        default_factory=lambda: int(os.getenv("HEARTBEAT_LIMIT_PER_MIN", "1200"))
    )
    RATE_LIMIT_PER_MIN: int = Field(
        default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_MIN", "600"))
    )
    SPIKE_MIN_EVENTS: int = Field(
        default_factory=lambda: int(os.getenv("SPIKE_MIN_EVENTS", "10"))
    )
    SPIKE_RATIO: float = Field(
        default_factory=lambda: float(os.getenv("SPIKE_RATIO", "3.0"))
    )

    # Verdict Checkpoint & Threshold Settings (Addendum Section C3)
    VERDICT_FIRST_MIN: int = Field(
        default_factory=lambda: int(os.getenv("VERDICT_FIRST_MIN", "10"))
    )
    VERDICT_FINAL_MIN: int = Field(
        default_factory=lambda: int(os.getenv("VERDICT_FINAL_MIN", "30"))
    )
    VERDICT_ROLLBACK_NEW_ISSUE_EVENTS: int = Field(
        default_factory=lambda: int(os.getenv("VERDICT_ROLLBACK_NEW_ISSUE_EVENTS", "20"))
    )
    VERDICT_ROLLBACK_ABS_RATE: float = Field(
        default_factory=lambda: float(os.getenv("VERDICT_ROLLBACK_ABS_RATE", "0.25"))
    )
    NOTIFY_ON_SAFE: bool = Field(
        default_factory=lambda: os.getenv("NOTIFY_ON_SAFE", "1") in ("1", "true", "True")
    )

    # Retention Settings (Stage 6 & Security Stage 7)
    RETENTION_DAYS: int = Field(
        # EVENT_RETENTION_DAYS is the brief's name for the same setting
        default_factory=lambda: int(os.getenv("RETENTION_DAYS") or os.getenv("EVENT_RETENTION_DAYS") or "30")
    )
    CODE_RETENTION_DAYS: int = Field(
        default_factory=lambda: int(os.getenv("CODE_RETENTION_DAYS", "7"))
    )
    SCRUB_PII: bool = Field(
        default_factory=lambda: os.getenv("SCRUB_PII", "1") in ("1", "true", "True")
    )


    # Encryption at Rest Settings (Envelope Encryption)
    MASTER_KEY: str = Field(
        default_factory=lambda: os.getenv("MASTER_KEY", "")
    )
    MASTER_KEY_FILE: str = Field(
        default_factory=lambda: os.getenv("MASTER_KEY_FILE", "")
    )
    MASTER_KEY_PREVIOUS: str = Field(
        default_factory=lambda: os.getenv("MASTER_KEY_PREVIOUS", "")
    )

    # Trust & Reliability Settings (Addendum Section A1)
    QUEUE_VISIBILITY_TIMEOUT_SEC: int = Field(
        default_factory=lambda: int(os.getenv("QUEUE_VISIBILITY_TIMEOUT_SEC", "60"))
    )
    OUTBOX_MAX_AGE_HOURS: int = Field(
        default_factory=lambda: int(os.getenv("OUTBOX_MAX_AGE_HOURS", "24"))
    )

    # Server Network & Security Settings
    HOST: str = Field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    PORT: int = Field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    DATABASE_SSL: bool = Field(
        default_factory=lambda: os.getenv("DATABASE_SSL", "0") in ("1", "true", "True")
    )
    REDIS_PASSWORD: str = Field(
        default_factory=lambda: os.getenv("REDIS_PASSWORD", "")
    )
    PUBLIC_HOSTNAME: str = Field(
        default_factory=lambda: os.getenv("PUBLIC_HOSTNAME", "localhost")
    )
    # Where people open the dashboard, for links in alerts. Empty: https://PUBLIC_HOSTNAME
    # (the Caddy tls profile), or http://localhost:3000 when PUBLIC_HOSTNAME is local.
    DASHBOARD_URL: str = Field(default_factory=lambda: os.getenv("DASHBOARD_URL", ""))

    def dashboard_link(self, path: str = "") -> str:
        base = self.DASHBOARD_URL.strip().rstrip("/")
        if not base:
            host = self.PUBLIC_HOSTNAME.strip()
            base = "http://localhost:3000" if host in ("", "localhost", "127.0.0.1", "::1") else f"https://{host}"
        return base + path

    # Performance & Concurrency Settings (Performance Addendum Section 1 & 6)
    WORKER_PROCESSES: str = Field(
        default_factory=lambda: os.getenv("WORKER_PROCESSES", "auto")
    )
    API_WORKERS: str = Field(
        default_factory=lambda: os.getenv("API_WORKERS", "auto")
    )
    INVESTIGATION_CONCURRENCY: int = Field(
        default_factory=lambda: int(os.getenv("INVESTIGATION_CONCURRENCY", "4"))
    )
    OLLAMA_CONCURRENCY: int = Field(
        default_factory=lambda: int(os.getenv("OLLAMA_CONCURRENCY", "1"))
    )
    DB_POOL_PER_PROCESS: int = Field(
        default_factory=lambda: int(os.getenv("DB_POOL_PER_PROCESS", "10"))
    )
    MAX_MEMORY_MB: int = Field(
        default_factory=lambda: int(os.getenv("MAX_MEMORY_MB", "512"))
    )
    STORE_FULL_PER_MIN: int = Field(
        default_factory=lambda: int(os.getenv("STORE_FULL_PER_MIN", "20"))
    )
    QUEUE_SOFT_LIMIT: int = Field(
        default_factory=lambda: int(os.getenv("QUEUE_SOFT_LIMIT", "10000"))
    )
    # How long an event_id is remembered, so a re-sent event is not counted twice
    DEDUPE_WINDOW_HOURS: int = Field(
        default_factory=lambda: int(os.getenv("DEDUPE_WINDOW_HOURS", "24"))
    )

    # Usage Metering Settings (Usage Metering Addendum Section I)
    METER_ENVIRONMENTS: str = Field(
        default_factory=lambda: os.getenv("METER_ENVIRONMENTS", "prod")
    )
    LLM_PRICES_JSON: str = Field(
        default_factory=lambda: os.getenv(
            "LLM_PRICES_JSON",
            # USD per million tokens (Claude Haiku 5.5: prompts up to 100K tokens)
            '{"claude-sonnet-5-5": {"input": 2.0, "output": 10.0}, "claude-opus-5-5": {"input": 4.0, "output": 20.0}, '
            '"claude-haiku-5-5": {"input": 0.1, "output": 0.5}}',
        )
    )
    FREE_CREDIT_MICROS: int = Field(
        default_factory=lambda: int(os.getenv("FREE_CREDIT_MICROS", "0"))
    )
    DEFAULT_SOFT_LIMIT_MICROS: Optional[int] = Field(
        default_factory=lambda: int(os.getenv("DEFAULT_SOFT_LIMIT_MICROS"))
        if os.getenv("DEFAULT_SOFT_LIMIT_MICROS")
        else None
    )
    DEFAULT_HARD_LIMIT_MICROS: Optional[int] = Field(
        default_factory=lambda: int(os.getenv("DEFAULT_HARD_LIMIT_MICROS"))
        if os.getenv("DEFAULT_HARD_LIMIT_MICROS")
        else None
    )
    USAGE_FLUSH_SEC: int = Field(
        default_factory=lambda: int(os.getenv("USAGE_FLUSH_SEC", "60"))
    )
    PRICE_BOOK_FILE: str = Field(
        default_factory=lambda: os.getenv("PRICE_BOOK_FILE", "config/price_book.default.yaml")
    )
    ADMIN_API_KEY: str = Field(
        default_factory=lambda: os.getenv("ADMIN_API_KEY", "")
    )
    # Trust & Reliability Settings (Trust Addendum Section G)
    SILENT_AFTER_MIN: int = Field(
        default_factory=lambda: int(os.getenv("SILENT_AFTER_MIN", "10"))
    )
    STALE_AFTER_SEC: int = Field(
        default_factory=lambda: int(os.getenv("STALE_AFTER_SEC", "180"))
    )
    WATCHDOG_PING_URL: str = Field(
        default_factory=lambda: os.getenv("WATCHDOG_PING_URL", "")
    )
    # Operator fallback channel for system alerts (Slack, Discord or Teams incoming webhook).
    # Sent directly, not through the outbox, so it still works when delivery itself is broken.
    # Signed one-click action links (/a/<token>); derived from MASTER_KEY when unset
    LINK_SIGNING_SECRET: str = Field(
        default_factory=lambda: os.getenv("LINK_SIGNING_SECRET", "")
    )
    # Slack app signing secret; the Slack interactions endpoint is disabled until it is set
    SLACK_SIGNING_SECRET: str = Field(
        default_factory=lambda: os.getenv("SLACK_SIGNING_SECRET", "")
    )
    # Weekly digest schedule (brief §19): day of week, hour (0-23), IANA timezone
    DIGEST_DAY: str = Field(
        default_factory=lambda: os.getenv("DIGEST_DAY", "mon").strip().lower()[:3]
    )
    DIGEST_HOUR: int = Field(
        default_factory=lambda: int(os.getenv("DIGEST_HOUR", "9"))
    )
    DIGEST_TIMEZONE: str = Field(
        default_factory=lambda: os.getenv("DIGEST_TIMEZONE", "UTC")
    )
    SYSTEM_ALERT_WEBHOOK_URL: str = Field(
        default_factory=lambda: os.getenv("SYSTEM_ALERT_WEBHOOK_URL", "")
    )

    # Ephemeral Code Mode & Decision Code Settings (TD Addendum Section I)
    CODE_MODE: str = Field(
        default_factory=lambda: os.getenv("CODE_MODE", "stored")
    )
    GITHUB_READ_TOKEN: str = Field(
        default_factory=lambda: os.getenv("GITHUB_READ_TOKEN", "")
    )
    DECISION_SEND_CODE: bool = Field(
        default_factory=lambda: os.getenv("DECISION_SEND_CODE", "0") in ("1", "true", "True")
    )

    # Opt-In Telemetry Settings (Milestone 9)
    TELEMETRY_ENABLED: bool = Field(
        default_factory=lambda: os.getenv("TELEMETRY_ENABLED", "0") in ("1", "true", "True")
    )
    TELEMETRY_COLLECTOR_URL: str = Field(
        default_factory=lambda: os.getenv("TELEMETRY_COLLECTOR_URL", "http://localhost:8001")
    )
    TELEMETRY_INSTALL_ID_PATH: str = Field(
        default_factory=lambda: os.getenv("TELEMETRY_INSTALL_ID_PATH", ".telemetry_id")
    )



settings = Settings()


def resolve_worker_processes(val: Optional[str] = None) -> int:
    """Calculate worker process count: max(1, min(cpu_count - 1, 8)) on 'auto'."""
    raw = val or settings.WORKER_PROCESSES
    cpu_count = os.cpu_count() or 1
    if raw == "auto":
        chosen = max(1, min(cpu_count - 1, 8))
    else:
        try:
            chosen = max(1, int(raw))
        except ValueError:
            chosen = max(1, min(cpu_count - 1, 8))
    return chosen


def resolve_api_workers(val: Optional[str] = None) -> int:
    """Calculate uvicorn API worker process count (2-4)."""
    raw = val or settings.API_WORKERS
    cpu_count = os.cpu_count() or 1
    if raw == "auto":
        chosen = max(1, min(cpu_count, 4))
    else:
        try:
            chosen = max(1, int(raw))
        except ValueError:
            chosen = max(1, min(cpu_count, 4))
    return chosen
