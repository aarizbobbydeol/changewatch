"""Usage Metering, Price Book, and Cost-Plus Calculator.
Implements specifications from antigravity_usage_metering_addendum.md.
All monetary amounts are strictly integer micro-USD (1 USD = 1,000,000 micros).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import yaml

from app.config import settings

logger = logging.getLogger("app.metering")


class Meter(str, Enum):
    RELEASE_MONITORED = "release_monitored"
    AI_INVESTIGATION = "ai_investigation"
    AI_INVESTIGATION_DEEP = "ai_investigation_deep"
    STORED_EVENTS = "stored_events"
    DECISION_CALL = "decision_call"  # hosted typed-decision provider (Jev) calls only

    # Reserved names per spec Section A
    CODE_REVIEW_PR = "code_review_pr"
    COST_ACCOUNT_DAY = "cost_account_day"


@dataclass(frozen=True)
class PriceBookEntry:
    version: int
    meter: str
    pricing_mode: str  # 'fixed' | 'cost_plus'
    unit_price_micros: int
    margin_multiplier: float
    min_unit_price_micros: int
    free_units_per_month: int
    effective_from: Optional[datetime] = None


def load_price_book_yaml(file_path: Union[str, Path]) -> dict[str, Any]:
    """Load and validate price book YAML file."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Price book file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("Price book YAML must be a top-level mapping")

    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("Price book must specify an integer version >= 1")

    meters = data.get("meters")
    if not isinstance(meters, dict) or not meters:
        raise ValueError("Price book must specify a non-empty 'meters' mapping")

    for meter_name, config in meters.items():
        if not isinstance(config, dict):
            raise ValueError(f"Meter '{meter_name}' config must be a mapping")
        pricing_mode = config.get("pricing_mode")
        if pricing_mode not in ("fixed", "cost_plus"):
            raise ValueError(f"Meter '{meter_name}' invalid pricing_mode: {pricing_mode}")

        unit_price = config.get("unit_price_micros", 0)
        margin = config.get("margin_multiplier", 1.0)
        min_price = config.get("min_unit_price_micros", 0)
        free_units = config.get("free_units_per_month", 0)

        if not isinstance(unit_price, int) or unit_price < 0:
            raise ValueError(f"Meter '{meter_name}' unit_price_micros must be a non-negative int")
        if not isinstance(margin, (int, float)) or margin < 1.0:
            raise ValueError(f"Meter '{meter_name}' margin_multiplier must be >= 1.0")
        if not isinstance(min_price, int) or min_price < 0:
            raise ValueError(f"Meter '{meter_name}' min_unit_price_micros must be a non-negative int")
        if not isinstance(free_units, int) or free_units < 0:
            raise ValueError(f"Meter '{meter_name}' free_units_per_month must be a non-negative int")

    return data


async def publish_price_book(conn, price_book_data: dict[str, Any]) -> int:
    """Publish a new price book version to the database.
    Versions are strictly append-only and cannot overwrite existing versions.
    """
    version = price_book_data["version"]
    effective_from_str = price_book_data.get("effective_from")
    effective_from = (
        datetime.fromisoformat(effective_from_str)
        if effective_from_str
        else datetime.now(timezone.utc)
    )

    # Check for existing version
    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM price_book WHERE version = $1",
        version,
    )
    if existing and existing > 0:
        raise ValueError(
            f"Price book version {version} already exists. Publishing must use a new, unique version."
        )

    for meter_name, cfg in price_book_data["meters"].items():
        await conn.execute(
            """
            INSERT INTO price_book (
                version, meter, pricing_mode, unit_price_micros,
                margin_multiplier, min_unit_price_micros, free_units_per_month, effective_from
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            version,
            meter_name,
            cfg.get("pricing_mode", "fixed"),
            int(cfg.get("unit_price_micros", 0)),
            float(cfg.get("margin_multiplier", 1.0)),
            int(cfg.get("min_unit_price_micros", 0)),
            int(cfg.get("free_units_per_month", 0)),
            effective_from,
        )

    return version


def calculate_llm_cost_micros(
    model: str,
    input_tokens: int,
    output_tokens: int,
    provider: str = "anthropic",
) -> int:
    """Calculate our measured cost for LLM usage in integer micro-USD.
    Local models (ollama) and test fakes have 0 cost.
    1 USD = 1,000,000 micros.
    Price in USD per million tokens = micro-USD per token.
    """
    if provider in ("ollama", "fake") or input_tokens <= 0 and output_tokens <= 0:
        return 0

    # Prices are parsed as exact decimals: no float arithmetic on money
    try:
        rates_map = json.loads(settings.LLM_PRICES_JSON, parse_float=Decimal)
    except Exception:
        rates_map = {}

    model_rates = rates_map.get(model)
    if not model_rates:
        # Unknown model: charge the highest configured rate rather than guess low, and say so
        known = [r for r in rates_map.values() if isinstance(r, dict)]
        model_rates = max(known, key=lambda r: Decimal(r.get("output", 0)), default={"input": 4, "output": 20})
        logger.warning("No price for model %s in LLM_PRICES_JSON; using the highest configured rate", model)
    input_rate = Decimal(model_rates.get("input", 0))
    output_rate = Decimal(model_rates.get("output", 0))

    # Cost = ceil(input_tokens * input_rate + output_tokens * output_rate)
    total_micros = math.ceil(Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate)
    return max(0, int(total_micros))


def calculate_billed_micros(
    entry: PriceBookEntry,
    units: int,
    our_cost_micros: int = 0,
    free_units_available: int = 0,
) -> Tuple[int, int, int]:
    """Calculate customer billed amount in integer micro-USD according to the price book.
    Returns: (billed_micros, free_units_applied, billable_units).

    Rules:
    1. Free units are applied first per calendar month.
    2. Fixed mode: billed = billable_units * unit_price_micros.
    3. Cost-plus mode:
       - If our_cost_micros == 0 (e.g. ollama/fake), billed is 0.
       - Otherwise: billed = max(ceil(our_cost_micros * margin_multiplier), min_unit_price_micros * billable_units).
    4. All returned amounts are non-negative integers.
    """
    if units <= 0:
        return (0, 0, 0)

    free_units_applied = min(units, max(0, free_units_available))
    billable_units = units - free_units_applied

    if billable_units <= 0:
        return (0, free_units_applied, 0)

    if entry.pricing_mode == "fixed":
        billed = billable_units * entry.unit_price_micros
        return (max(0, int(billed)), free_units_applied, billable_units)

    if entry.pricing_mode == "cost_plus":
        if our_cost_micros <= 0:
            return (0, free_units_applied, billable_units)

        # Apply cost-plus markup with ceil rounding, in exact decimals (100 * 1.1 must be 110, not 111)
        marked_up = math.ceil(Decimal(our_cost_micros) * Decimal(str(entry.margin_multiplier)))
        floor = entry.min_unit_price_micros * billable_units
        billed = max(marked_up, floor)
        return (max(0, int(billed)), free_units_applied, billable_units)

    return (0, free_units_applied, billable_units)


async def get_active_price_book(
    conn,
    project_id: Optional[int] = None,
) -> Dict[str, PriceBookEntry]:
    """Retrieve the effective price book entries for a project.
    If project has an active price lock, uses its pinned version.
    Otherwise uses the latest published version in price_book.
    """
    target_version: Optional[int] = None

    if project_id is not None:
        project_row = await conn.fetchrow(
            """
            SELECT price_book_version, price_lock_until
            FROM projects
            WHERE id = $1
            """,
            project_id,
        )
        if project_row:
            lock_until = project_row["price_lock_until"]
            pinned_version = project_row["price_book_version"]
            now_utc = datetime.now(timezone.utc)
            if lock_until and lock_until > now_utc and pinned_version is not None:
                target_version = pinned_version

    if target_version is None:
        target_version = await conn.fetchval("SELECT MAX(version) FROM price_book")

    if target_version is None:
        # Fallback to version 1 default
        target_version = 1

    rows = await conn.fetch(
        """
        SELECT version, meter, pricing_mode, unit_price_micros,
               margin_multiplier, min_unit_price_micros, free_units_per_month, effective_from
        FROM price_book
        WHERE version = $1
        """,
        target_version,
    )

    result: Dict[str, PriceBookEntry] = {}
    for r in rows:
        result[r["meter"]] = PriceBookEntry(
            version=r["version"],
            meter=r["meter"],
            pricing_mode=r["pricing_mode"],
            unit_price_micros=r["unit_price_micros"],
            margin_multiplier=float(r["margin_multiplier"]),
            min_unit_price_micros=r["min_unit_price_micros"],
            free_units_per_month=r["free_units_per_month"],
            effective_from=r["effective_from"],
        )

    return result


async def set_price_lock(
    conn,
    project_id: int,
    days: int,
) -> Tuple[int, datetime]:
    """Pin project's rate card version for N days (founding rate lock)."""
    if days <= 0:
        raise ValueError("Price lock duration must be positive days")

    latest_version = await conn.fetchval("SELECT MAX(version) FROM price_book")
    if latest_version is None:
        latest_version = 1

    lock_until = datetime.now(timezone.utc) + timedelta(days=days)

    await conn.execute(
        """
        UPDATE projects
        SET price_book_version = COALESCE(price_book_version, $1),
            price_lock_until = $2
        WHERE id = $3
        """,
        latest_version,
        lock_until,
        project_id,
    )

    pinned_version = await conn.fetchval(
        "SELECT price_book_version FROM projects WHERE id = $1",
        project_id,
    )
    return (pinned_version or latest_version, lock_until)


async def record_usage(
    conn,
    project_id: int,
    meter: Union[str, Meter],
    units: int,
    ref_type: Optional[str] = None,
    ref_id: Optional[int] = None,
    our_cost_micros: int = 0,
    idempotency_key: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    price_book_version: Optional[int] = None,
    status: str = "done",  # "done", "failed", "partial"
    cached_from: Optional[Any] = None,
    result_delivered: bool = True,
) -> Optional[int]:
    """Record billable usage in usage_events, usage_daily, and budget_state in one transaction.
    Enforces all fairness rules (Section 0 & Section D):
    - Replaying the same key is strictly idempotent (DO NOTHING).
    - Failed investigations, unstored events, and duplicates record nothing.
    - Cached results ('cached_from' set) record nothing.
    - Free units allowance applied per project per calendar month before billing.
    - All money amounts are integers in micro-USD.
    """
    if units <= 0 or cached_from is not None or status == "failed":
        return None

    meter_str = meter.value if isinstance(meter, Meter) else str(meter)
    occurred_at_dt = occurred_at or datetime.now(timezone.utc)
    day = occurred_at_dt.date()
    month = occurred_at_dt.replace(day=1).date()

    # Retrieve effective rate card entry
    price_book = await get_active_price_book(conn, project_id)
    entry = price_book.get(meter_str)
    if not entry:
        entry = PriceBookEntry(
            version=1,
            meter=meter_str,
            pricing_mode="fixed",
            unit_price_micros=0,
            margin_multiplier=1.0,
            min_unit_price_micros=0,
            free_units_per_month=0,
        )

    pb_version = price_book_version or entry.version

    # Check free units already applied in the current calendar month
    month_start = occurred_at_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    free_used_row = await conn.fetchval(
        """
        SELECT COALESCE(SUM(free_units_applied), 0)
        FROM usage_daily
        WHERE project_id = $1 AND meter = $2 AND day >= $3
        """,
        project_id,
        meter_str,
        month_start.date(),
    )
    free_used_so_far = int(free_used_row or 0)
    free_available = max(0, entry.free_units_per_month - free_used_so_far)

    # Calculate billed micros
    billed_micros, free_applied, billable_units = calculate_billed_micros(
        entry=entry,
        units=units,
        our_cost_micros=our_cost_micros,
        free_units_available=free_available,
    )

    if status == "partial" and not result_delivered:
        billed_micros = 0

    idem_key = idempotency_key or f"{meter_str}:{ref_type or 'gen'}:{ref_id or int(occurred_at_dt.timestamp())}"

    # Insert usage_events
    row = await conn.fetchrow(
        """
        INSERT INTO usage_events (
            project_id, meter, units, occurred_at, ref_type, ref_id,
            our_cost_micros, billed_micros, free_units_applied,
            price_book_version, idempotency_key
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (project_id, idempotency_key) DO NOTHING
        RETURNING id
        """,
        project_id,
        meter_str,
        units,
        occurred_at_dt,
        ref_type,
        ref_id,
        our_cost_micros,
        billed_micros,
        free_applied,
        pb_version,
        idem_key,
    )

    if not row:
        # Idempotent replay: already recorded, do not double count
        return None

    usage_event_id = int(row["id"])

    # Update usage_daily in the SAME transaction
    await conn.execute(
        """
        INSERT INTO usage_daily (
            project_id, day, meter, units, our_cost_micros, billed_micros, free_units_applied
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (project_id, day, meter) DO UPDATE SET
            units = usage_daily.units + EXCLUDED.units,
            our_cost_micros = usage_daily.our_cost_micros + EXCLUDED.our_cost_micros,
            billed_micros = usage_daily.billed_micros + EXCLUDED.billed_micros,
            free_units_applied = usage_daily.free_units_applied + EXCLUDED.free_units_applied
        """,
        project_id,
        day,
        meter_str,
        units,
        our_cost_micros,
        billed_micros,
        free_applied,
    )

    # Update budget_state in the SAME transaction
    await conn.execute(
        """
        INSERT INTO budget_state (project_id, month, spent_micros)
        VALUES ($1, $2, $3)
        ON CONFLICT (project_id, month) DO UPDATE SET
            spent_micros = budget_state.spent_micros + EXCLUDED.spent_micros
        """,
        project_id,
        month,
        billed_micros,
    )

    return usage_event_id


async def record_investigation_usage(
    conn,
    project_id: int,
    investigation_id: int,
    status: str = "done",  # "done", "failed", "partial"
    is_deep: bool = False,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    cached_from: Optional[Any] = None,
    result_delivered: bool = True,
    occurred_at: Optional[datetime] = None,
) -> Optional[int]:
    """Record usage for AI investigation (fast or deep).
    Enforces all fairness rules (Section D):
    - Status 'failed' records nothing.
    - Cached result ('cached_from' set) records nothing.
    - Local / fake providers have 0 our cost and 0 billed.
    - Status 'partial' records real token cost with units=1 and billed cost-plus ONLY if result was delivered, otherwise 0.
    - Idempotency key: inv:<id>.
    """
    if status == "failed" or cached_from is not None:
        return None

    meter = Meter.AI_INVESTIGATION_DEEP if is_deep else Meter.AI_INVESTIGATION
    model_name = model or settings.LLM_MODEL
    prov = provider or settings.LLM_PROVIDER

    our_cost = calculate_llm_cost_micros(model_name, input_tokens, output_tokens, provider=prov)
    idempotency_key = f"inv:{investigation_id}" if not is_deep else f"inv_deep:{investigation_id}"

    return await record_usage(
        conn=conn,
        project_id=project_id,
        meter=meter,
        units=1,
        ref_type="investigation",
        ref_id=investigation_id,
        our_cost_micros=our_cost,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
        status=status,
        cached_from=cached_from,
        result_delivered=result_delivered,
    )


async def record_stored_event_metric(redis, project_id: int, occurred_at: datetime, count: int = 1):
    """Aggregate stored events in Redis per (project, minute) off the hot path."""
    if not redis or count <= 0:
        return
    m_epoch = int(occurred_at.replace(second=0, microsecond=0).timestamp())
    bucket_key = f"usage:events:{project_id}:{m_epoch}"
    try:
        await redis.incrby(bucket_key, count)
        await redis.expire(bucket_key, 7200)  # 2 hours TTL
        await redis.sadd("usage:pending_event_buckets", f"{project_id}:{m_epoch}")
    except Exception as exc:
        logger.warning("Failed updating Redis usage metric for stored events: %s", exc)


async def flush_usage_events(conn, redis, force: bool = False) -> int:
    """Flush aggregated stored_events counts from Redis to usage_events & usage_daily.
    Only flushes buckets older than 60s unless force=True.
    Returns total events flushed.
    """
    if not redis:
        return 0

    try:
        raw_buckets = await redis.smembers("usage:pending_event_buckets")
    except Exception as exc:
        logger.warning("Failed fetching pending usage buckets from Redis: %s", exc)
        return 0

    if not raw_buckets:
        return 0

    now_ts = int(datetime.now(timezone.utc).timestamp())
    total_flushed = 0

    for b in raw_buckets:
        b_str = b.decode("utf-8") if isinstance(b, bytes) else str(b)
        parts = b_str.split(":")
        if len(parts) != 2:
            continue
        try:
            project_id = int(parts[0])
            m_epoch = int(parts[1])
        except ValueError:
            continue

        if not force and (now_ts - m_epoch < 60):
            # Still in current minute window; wait for window to close
            continue

        bucket_key = f"usage:events:{project_id}:{m_epoch}"
        try:
            pipe = redis.pipeline()
            if hasattr(pipe, "__await__"):
                pipe = await pipe
            pipe.get(bucket_key)
            pipe.delete(bucket_key)
            pipe.srem("usage:pending_event_buckets", b_str)
            res = await pipe.execute()
            val = res[0]
        except Exception as exc:
            logger.warning("Error fetching bucket %s from Redis: %s", bucket_key, exc)
            continue

        if val is None:
            continue

        try:
            count = int(val)
        except (ValueError, TypeError):
            continue

        if count <= 0:
            continue

        occurred_at = datetime.fromtimestamp(m_epoch, tz=timezone.utc)
        idempotency_key = f"events:{project_id}:{m_epoch}"

        await record_usage(
            conn=conn,
            project_id=project_id,
            meter=Meter.STORED_EVENTS,
            units=count,
            ref_type="batch",
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )
        total_flushed += count

    return total_flushed


async def can_spend(
    conn,
    project_id: int,
    meter: Union[str, Meter],
    estimated_cost_micros: int = 0,
) -> Tuple[bool, str]:
    """Check whether a project can spend on an optional extra action.
    Returns (allowed: bool, reason: str).

    Safety floor rule (Section 0):
    Safety floor actions are NEVER paused, metered, or blocked:
    verdicts, instant alerts, dead-man's switch, health checks.

    Optional extras subject to hard limit:
    - ai_investigation
    - ai_investigation_deep
    - stored_events (full row inserts beyond free allowance)
    """
    meter_str = meter.value if isinstance(meter, Meter) else str(meter)

    # Safety floor actions always pass
    if meter_str in ("release_verdict", "instant_alert", "dead_mans_switch", "healthz"):
        return (True, "safety_floor")

    # Fetch budget settings
    budget_row = await conn.fetchrow(
        "SELECT soft_limit_micros, hard_limit_micros FROM budgets WHERE project_id = $1",
        project_id,
    )

    hard_limit: Optional[int] = None
    if budget_row:
        if isinstance(budget_row, dict) or hasattr(budget_row, "get"):
            hard_limit = budget_row.get("hard_limit_micros")
        else:
            try:
                hard_limit = budget_row["hard_limit_micros"]
            except (KeyError, IndexError):
                hard_limit = None

    if hard_limit is None and settings.DEFAULT_HARD_LIMIT_MICROS is not None:
        hard_limit = settings.DEFAULT_HARD_LIMIT_MICROS

    if hard_limit is None or hard_limit <= 0:
        return (True, "no_hard_limit")

    # Check current month spend
    now_dt = datetime.now(timezone.utc)
    month_date = now_dt.replace(day=1).date()

    spent_micros = await conn.fetchval(
        "SELECT spent_micros FROM budget_state WHERE project_id = $1 AND month = $2",
        project_id,
        month_date,
    )
    current_spent = int(spent_micros or 0)

    if current_spent + estimated_cost_micros > hard_limit:
        # Record hard_limit_hit_at if not already recorded
        await conn.execute(
            """
            INSERT INTO budget_state (project_id, month, spent_micros, hard_limit_hit_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (project_id, month) DO UPDATE SET
                hard_limit_hit_at = COALESCE(budget_state.hard_limit_hit_at, now())
            """,
            project_id,
            month_date,
            current_spent,
        )
        return (False, "budget_reached")

    return (True, "within_budget")


async def check_and_notify_budget_thresholds(
    conn,
    project_id: int,
    occurred_at: Optional[datetime] = None,
) -> Optional[int]:
    """Check if project spend crossed a warning threshold and enqueue notification to outbox.
    Warnings fire once per threshold per month (e.g. at 50%, 80%, 100%).
    Returns the newly triggered threshold percentage, or None.
    """
    now_dt = occurred_at or datetime.now(timezone.utc)
    month_date = now_dt.replace(day=1).date()

    budget_row = await conn.fetchrow(
        "SELECT soft_limit_micros, hard_limit_micros, warn_at FROM budgets WHERE project_id = $1",
        project_id,
    )
    soft_limit = None
    hard_limit = None
    raw_warn = None
    if budget_row:
        if isinstance(budget_row, dict) or hasattr(budget_row, "get"):
            soft_limit = budget_row.get("soft_limit_micros")
            hard_limit = budget_row.get("hard_limit_micros")
            raw_warn = budget_row.get("warn_at")
        else:
            try:
                soft_limit = budget_row["soft_limit_micros"]
            except (KeyError, IndexError):
                pass
            try:
                hard_limit = budget_row["hard_limit_micros"]
            except (KeyError, IndexError):
                pass
            try:
                raw_warn = budget_row["warn_at"]
            except (KeyError, IndexError):
                pass

    if soft_limit is None:
        soft_limit = settings.DEFAULT_SOFT_LIMIT_MICROS
    if hard_limit is None:
        hard_limit = settings.DEFAULT_HARD_LIMIT_MICROS

    # Warning threshold base: soft limit if set, otherwise hard limit
    effective_limit = soft_limit or hard_limit
    if not effective_limit or effective_limit <= 0:
        return None

    warn_at = [50, 80, 100]
    if raw_warn:
        try:
            warn_at = json.loads(raw_warn) if isinstance(raw_warn, str) else list(raw_warn)
        except Exception:
            warn_at = [50, 80, 100]

    state_row = await conn.fetchrow(
        "SELECT spent_micros, last_warned_pct FROM budget_state WHERE project_id = $1 AND month = $2",
        project_id,
        month_date,
    )
    spent = 0
    last_warned = 0
    if state_row:
        if isinstance(state_row, dict) or hasattr(state_row, "get"):
            spent = int(state_row.get("spent_micros") or 0)
            last_warned = int(state_row.get("last_warned_pct") or 0)
        else:
            try:
                spent = int(state_row["spent_micros"] or 0)
            except (KeyError, IndexError):
                spent = 0
            try:
                last_warned = int(state_row["last_warned_pct"] or 0)
            except (KeyError, IndexError):
                last_warned = 0

    pct = int((spent * 100) / effective_limit)

    # Find highest eligible threshold crossed
    eligible_thresholds = [t for t in sorted(warn_at) if t <= pct and t > last_warned]
    if not eligible_thresholds:
        return None

    crossed_threshold = eligible_thresholds[-1]

    # Update last_warned_pct
    await conn.execute(
        """
        INSERT INTO budget_state (project_id, month, spent_micros, last_warned_pct)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (project_id, month) DO UPDATE SET
            last_warned_pct = GREATEST(budget_state.last_warned_pct, EXCLUDED.last_warned_pct)
        """,
        project_id,
        month_date,
        spent,
        crossed_threshold,
    )

    # Enqueue low-severity notification into outbox
    dedupe_key = f"budget_warn:{project_id}:{month_date.isoformat()}:{crossed_threshold}"
    payload = {
        "kind": "budget_warning",
        "severity": "low",
        "project_id": project_id,
        "threshold_pct": crossed_threshold,
        "spent_micros": spent,
        "limit_micros": effective_limit,
        "message": f"ChangeWatch Budget Warning: Project {project_id} reached {crossed_threshold}% of spend limit (${spent / 1e6:.2f} / ${effective_limit / 1e6:.2f})",
        "change_budget_url": settings.dashboard_link("/usage"),
    }

    try:
        await conn.execute(
            """
            INSERT INTO outbox (project_id, dedupe_key, payload, status)
            VALUES ($1, $2, $3::jsonb, 'pending')
            ON CONFLICT (dedupe_key) DO NOTHING
            """,
            project_id,
            dedupe_key,
            json.dumps(payload),
        )
    except Exception as exc:
        logger.warning("Failed enqueueing budget warning outbox notification: %s", exc)

    return crossed_threshold


async def get_project_budget(conn, project_id: int) -> Dict[str, Any]:
    """Retrieve budget and current spend state for a project."""
    now_dt = datetime.now(timezone.utc)
    month_date = now_dt.replace(day=1).date()

    row = await conn.fetchrow(
        "SELECT soft_limit_micros, hard_limit_micros, warn_at FROM budgets WHERE project_id = $1",
        project_id,
    )
    state = await conn.fetchrow(
        "SELECT spent_micros, last_warned_pct, hard_limit_hit_at FROM budget_state WHERE project_id = $1 AND month = $2",
        project_id,
        month_date,
    )

    soft = None
    hard = None
    warn_at = [50, 80, 100]
    if row:
        if isinstance(row, dict) or hasattr(row, "get"):
            soft = row.get("soft_limit_micros")
            hard = row.get("hard_limit_micros")
            raw_warn = row.get("warn_at")
        else:
            try:
                soft = row["soft_limit_micros"]
            except (KeyError, IndexError):
                soft = None
            try:
                hard = row["hard_limit_micros"]
            except (KeyError, IndexError):
                hard = None
            try:
                raw_warn = row["warn_at"]
            except (KeyError, IndexError):
                raw_warn = None
        if raw_warn:
            try:
                warn_at = json.loads(raw_warn) if isinstance(raw_warn, str) else list(raw_warn)
            except Exception:
                warn_at = [50, 80, 100]

    if soft is None:
        soft = settings.DEFAULT_SOFT_LIMIT_MICROS
    if hard is None:
        hard = settings.DEFAULT_HARD_LIMIT_MICROS

    spent = 0
    last_warned = 0
    hit_at = None
    if state:
        if isinstance(state, dict) or hasattr(state, "get"):
            spent = state.get("spent_micros") or 0
            last_warned = state.get("last_warned_pct") or 0
            hit_at = state.get("hard_limit_hit_at")
        else:
            try:
                spent = state["spent_micros"] or 0
            except (KeyError, IndexError):
                spent = 0
            try:
                last_warned = state["last_warned_pct"] or 0
            except (KeyError, IndexError):
                last_warned = 0
            try:
                hit_at = state["hard_limit_hit_at"]
            except (KeyError, IndexError):
                hit_at = None

    return {
        "project_id": project_id,
        "month": month_date.isoformat(),
        "soft_limit_micros": soft,
        "hard_limit_micros": hard,
        "warn_at": warn_at,
        "spent_micros": spent,
        "last_warned_pct": last_warned,
        "hard_limit_hit_at": hit_at.isoformat() if hit_at else None,
        "optional_extras_paused": bool(hit_at is not None),
    }


async def set_project_budget(
    conn,
    project_id: int,
    soft_limit_micros: Optional[int] = None,
    hard_limit_micros: Optional[int] = None,
    warn_at: Optional[list] = None,
) -> Dict[str, Any]:
    """Upsert project spend limits and reset hard_limit_hit_at if limit is raised above spend."""
    warn_at_json = json.dumps(warn_at or [50, 80, 100])

    await conn.execute(
        """
        INSERT INTO budgets (project_id, soft_limit_micros, hard_limit_micros, warn_at)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (project_id) DO UPDATE SET
            soft_limit_micros = EXCLUDED.soft_limit_micros,
            hard_limit_micros = EXCLUDED.hard_limit_micros,
            warn_at = EXCLUDED.warn_at
        """,
        project_id,
        soft_limit_micros,
        hard_limit_micros,
        warn_at_json,
    )

    # If hard limit was raised above current spend, clear hard_limit_hit_at immediately
    now_dt = datetime.now(timezone.utc)
    month_date = now_dt.replace(day=1).date()
    spent_micros = await conn.fetchval(
        "SELECT spent_micros FROM budget_state WHERE project_id = $1 AND month = $2",
        project_id,
        month_date,
    )
    current_spent = int(spent_micros or 0)

    if hard_limit_micros is None or hard_limit_micros > current_spent:
        await conn.execute(
            """
            UPDATE budget_state
            SET hard_limit_hit_at = NULL
            WHERE project_id = $1 AND month = $2
            """,
            project_id,
            month_date,
        )

    return {
        "project_id": project_id,
        "soft_limit_micros": soft_limit_micros,
        "hard_limit_micros": hard_limit_micros,
        "warn_at": warn_at or [50, 80, 100],
    }


