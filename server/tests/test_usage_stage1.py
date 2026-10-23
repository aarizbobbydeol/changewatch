"""Unit tests for Usage Metering Addendum Stage 1.
Tests price book schema, YAML loading, cost-plus integer micro-USD math,
price book publishing/versioning, and founding rate locks.
"""

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.metering import (
    Meter,
    PriceBookEntry,
    calculate_billed_micros,
    calculate_llm_cost_micros,
    get_active_price_book,
    load_price_book_yaml,
    publish_price_book,
    set_price_lock,
)


def test_price_book_yaml_loading():
    """Verify loading of default starter price book YAML."""
    path = Path("config/price_book.default.yaml")
    assert path.exists()

    data = load_price_book_yaml(path)
    assert data["version"] == 1
    assert "meters" in data

    meters = data["meters"]
    assert "release_monitored" in meters
    assert meters["release_monitored"]["pricing_mode"] == "fixed"
    assert meters["release_monitored"]["unit_price_micros"] == 20000
    assert meters["release_monitored"]["free_units_per_month"] == 200

    assert "ai_investigation" in meters
    assert meters["ai_investigation"]["pricing_mode"] == "cost_plus"
    assert meters["ai_investigation"]["margin_multiplier"] == 1.3
    assert meters["ai_investigation"]["min_unit_price_micros"] == 1000
    assert meters["ai_investigation"]["free_units_per_month"] == 20

    assert "stored_events" in meters
    assert meters["stored_events"]["pricing_mode"] == "fixed"
    assert meters["stored_events"]["unit_price_micros"] == 2
    assert meters["stored_events"]["free_units_per_month"] == 100000


def test_price_book_yaml_validation_errors(tmp_path):
    """Test validation errors on invalid YAML configurations."""
    # Bad version
    bad_v = tmp_path / "bad_v.yaml"
    bad_v.write_text("version: 0\nmeters: {release_monitored: {pricing_mode: fixed}}")
    with pytest.raises(ValueError, match="version >= 1"):
        load_price_book_yaml(bad_v)

    # Invalid pricing mode
    bad_mode = tmp_path / "bad_mode.yaml"
    bad_mode.write_text("version: 2\nmeters: {release_monitored: {pricing_mode: auction}}")
    with pytest.raises(ValueError, match="invalid pricing_mode"):
        load_price_book_yaml(bad_mode)

    # Negative prices
    bad_price = tmp_path / "bad_price.yaml"
    bad_price.write_text(
        "version: 2\nmeters: {release_monitored: {pricing_mode: fixed, unit_price_micros: -100}}"
    )
    with pytest.raises(ValueError, match="non-negative int"):
        load_price_book_yaml(bad_price)


def test_calculate_llm_cost_micros():
    """Verify integer micro-USD token cost calculation with ceil rounding."""
    # Ollama and Fake always return 0 cost
    assert calculate_llm_cost_micros("llama3", 1000, 500, provider="ollama") == 0
    assert calculate_llm_cost_micros("fake-model", 1000, 500, provider="fake") == 0

    # Anthropic models with mocked price dictionary:
    # 1 USD per million tokens = 1 micro-USD per token
    prices = {
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0},
    }
    with patch("app.metering.settings.LLM_PRICES_JSON", json.dumps(prices)):
        # Haiku: 1,000 input tokens * 0.25 = 250 micros; 200 output tokens * 1.25 = 250 micros -> 500 micros ($0.0005)
        cost = calculate_llm_cost_micros("claude-3-haiku-20240307", 1000, 200, provider="anthropic")
        assert cost == 500
        assert isinstance(cost, int)

        # Sonnet: 3 input tokens * 3.0 = 9; 1 output token * 15.0 = 15 -> 24 micros
        cost_sonnet = calculate_llm_cost_micros("claude-3-5-sonnet-20241022", 3, 1, provider="anthropic")
        assert cost_sonnet == 24
        assert isinstance(cost_sonnet, int)

        # Fractional micro ceil rounding: 1 input token * 0.25 = 0.25 -> ceil = 1 micro
        fractional = calculate_llm_cost_micros("claude-3-haiku-20240307", 1, 0, provider="anthropic")
        assert fractional == 1
        assert isinstance(fractional, int)


def test_calculate_billed_micros_fixed_pricing():
    """Verify fixed meter pricing (e.g. releases, stored events) with free allowance."""
    entry = PriceBookEntry(
        version=1,
        meter="release_monitored",
        pricing_mode="fixed",
        unit_price_micros=20000,  # $0.02
        margin_multiplier=1.0,
        min_unit_price_micros=0,
        free_units_per_month=200,
    )

    # 1. Within free allowance: 50 releases with 200 free units available
    billed, free_applied, billable = calculate_billed_micros(
        entry, units=50, our_cost_micros=0, free_units_available=200
    )
    assert billed == 0
    assert free_applied == 50
    assert billable == 0

    # 2. Exceeding free allowance: 250 releases with 200 free units available -> 50 billable
    billed, free_applied, billable = calculate_billed_micros(
        entry, units=250, our_cost_micros=0, free_units_available=200
    )
    assert free_applied == 200
    assert billable == 50
    assert billed == 50 * 20000  # 1,000,000 micros ($1.00)
    assert isinstance(billed, int)

    # 3. No free units remaining: 10 releases with 0 free units available
    billed, free_applied, billable = calculate_billed_micros(
        entry, units=10, our_cost_micros=0, free_units_available=0
    )
    assert free_applied == 0
    assert billable == 10
    assert billed == 200000  # $0.20


def test_calculate_billed_micros_cost_plus():
    """Verify cost-plus pricing with margin multiplier, ceil rounding, and floor."""
    entry = PriceBookEntry(
        version=1,
        meter="ai_investigation",
        pricing_mode="cost_plus",
        unit_price_micros=0,
        margin_multiplier=1.300,  # 30% margin
        min_unit_price_micros=1000,  # $0.001 minimum floor
        free_units_per_month=20,
    )

    # 1. Free units available: 1 investigation, 20 free available -> 0 billed
    billed, free_applied, billable = calculate_billed_micros(
        entry, units=1, our_cost_micros=10000, free_units_available=20
    )
    assert billed == 0
    assert free_applied == 1
    assert billable == 0

    # 2. Billable investigation with standard cost:
    # our_cost = 10,000 micros ($0.01) -> 10,000 * 1.30 = 13,000 micros ($0.013)
    billed, free_applied, billable = calculate_billed_micros(
        entry, units=1, our_cost_micros=10000, free_units_available=0
    )
    assert billed == 13000
    assert free_applied == 0
    assert billable == 1
    assert isinstance(billed, int)

    # 3. Minimum floor enforcement:
    # our_cost = 50 micros -> 50 * 1.3 = 65 micros < 1000 floor -> billed is 1000
    billed, free_applied, billable = calculate_billed_micros(
        entry, units=1, our_cost_micros=50, free_units_available=0
    )
    assert billed == 1000
    assert isinstance(billed, int)

    # 4. Ceil rounding for non-integer multiplication:
    # our_cost = 1001 micros * 1.3 = 1301.3 -> ceil = 1302
    billed, free_applied, billable = calculate_billed_micros(
        entry, units=1, our_cost_micros=1001, free_units_available=0
    )
    assert billed == 1302

    # 5. Local / fake model (our_cost = 0):
    # As mandated by spec: ollama and fake providers have our cost 0 and billed 0
    billed, free_applied, billable = calculate_billed_micros(
        entry, units=1, our_cost_micros=0, free_units_available=0
    )
    assert billed == 0


@pytest.mark.asyncio
async def test_publish_price_book_versioning():
    """Verify rate card publishing: append-only versions, no overwrites."""
    mock_conn = AsyncMock()
    # Check existing version: 0 (doesn't exist yet)
    mock_conn.fetchval.return_value = 0

    yaml_data = {
        "version": 2,
        "effective_from": "2026-06-01T00:00:00Z",
        "meters": {
            "release_monitored": {
                "pricing_mode": "fixed",
                "unit_price_micros": 25000,
                "margin_multiplier": 1.0,
                "min_unit_price_micros": 0,
                "free_units_per_month": 150,
            }
        },
    }

    published_version = await publish_price_book(mock_conn, yaml_data)
    assert published_version == 2
    assert mock_conn.execute.call_count == 1

    # Attempt to re-publish version 2 must raise ValueError
    mock_conn.fetchval.return_value = 1  # already exists
    with pytest.raises(ValueError, match="already exists"):
        await publish_price_book(mock_conn, yaml_data)


@pytest.mark.asyncio
async def test_price_lock_pinning_and_expiry():
    """Verify founding-rate lock pins rate card until expiry date."""
    mock_conn = AsyncMock()
    mock_conn.fetchval.side_effect = [
        1,  # latest version from price_book
        1,  # pinned version in projects
    ]

    # Lock project 42 for 365 days
    version, lock_until = await set_price_lock(mock_conn, project_id=42, days=365)
    assert version == 1
    assert lock_until > datetime.now(timezone.utc) + timedelta(days=364)

    # 1. Project with active lock queries active price book
    mock_conn.fetchrow.return_value = {
        "price_book_version": 1,
        "price_lock_until": datetime.now(timezone.utc) + timedelta(days=300),
    }
    mock_conn.fetch.return_value = [
        {
            "version": 1,
            "meter": "release_monitored",
            "pricing_mode": "fixed",
            "unit_price_micros": 20000,
            "margin_multiplier": 1.0,
            "min_unit_price_micros": 0,
            "free_units_per_month": 200,
            "effective_from": datetime.now(timezone.utc),
        }
    ]

    pb = await get_active_price_book(mock_conn, project_id=42)
    assert "release_monitored" in pb
    assert pb["release_monitored"].version == 1

    # 2. Project with expired lock falls back to latest version (e.g. version 2)
    mock_conn.fetchrow.return_value = {
        "price_book_version": 1,
        "price_lock_until": datetime.now(timezone.utc) - timedelta(days=1),  # expired!
    }
    mock_conn.fetchval.side_effect = None
    mock_conn.fetchval.return_value = 2  # MAX(version) is 2
    mock_conn.fetch.return_value = [
        {
            "version": 2,
            "meter": "release_monitored",
            "pricing_mode": "fixed",
            "unit_price_micros": 25000,
            "margin_multiplier": 1.0,
            "min_unit_price_micros": 0,
            "free_units_per_month": 150,
            "effective_from": datetime.now(timezone.utc),
        }
    ]

    pb_expired = await get_active_price_book(mock_conn, project_id=42)
    assert pb_expired["release_monitored"].version == 2
    assert pb_expired["release_monitored"].unit_price_micros == 25000


def test_meter_enum_and_types():
    """Verify Meter enum contains all active and reserved meter names."""
    assert Meter.RELEASE_MONITORED.value == "release_monitored"
    assert Meter.AI_INVESTIGATION.value == "ai_investigation"
    assert Meter.AI_INVESTIGATION_DEEP.value == "ai_investigation_deep"
    assert Meter.STORED_EVENTS.value == "stored_events"
    assert Meter.CODE_REVIEW_PR.value == "code_review_pr"
    assert Meter.COST_ACCOUNT_DAY.value == "cost_account_day"


@pytest.mark.asyncio
async def test_cli_price_commands():
    """Verify CLI price-book publish and price-lock command execution."""
    from app.cli import price_lock_cmd, publish_price_book_cmd

    with patch("app.cli.init_db_pool", new_callable=AsyncMock), \
         patch("app.cli.close_db_pool", new_callable=AsyncMock), \
         patch("app.cli.get_connection") as mock_get_conn:

        mock_conn = AsyncMock()
        mock_get_conn.return_value.__aenter__.return_value = mock_conn

        # Test price-book publish
        mock_conn.fetchval.return_value = 0  # not existing
        version = await publish_price_book_cmd("config/price_book.default.yaml")
        assert version == 1

        # Test price-lock
        mock_conn.fetchval.side_effect = [1, 1]
        v_lock, until = await price_lock_cmd(project_id=10, days=90)
        assert v_lock == 1
        assert until > datetime.now(timezone.utc)

