"""The Anthropic client sends a current request and reads current responses (no network: mock transport)."""
import json
import logging
from decimal import Decimal

import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient

from app.config import settings
from app.llm import AnthropicClient, get_llm_client, log_llm_privacy_notice
from app.metering import PriceBookEntry, calculate_billed_micros, calculate_llm_cost_micros


def _client(response: dict, seen: list, **kwargs) -> AnthropicClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=response)

    http_client = DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return AnthropicClient(api_key="sk-ant-test", http_client=http_client, **kwargs)


def _message(content, stop_reason="end_turn", stop_details=None):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": content, "stop_reason": stop_reason, "stop_details": stop_details, "stop_sequence": None,
        "usage": {"input_tokens": 812, "output_tokens": 145},
    }


@pytest.mark.asyncio
async def test_request_uses_effort_and_refusal_fallback_and_no_sampling_params():
    seen = []
    client = _client(_message([{"type": "text", "text": "ok"}]), seen, model="claude-sonnet-5-5", effort="medium")
    await client.complete(system="You are an SRE.", prompt="token=abc123secret broke prod", max_tokens=8000)

    request = seen[0]
    body = json.loads(request.content)
    assert request.url.path == "/v1/messages"
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 8000
    assert body["output_config"] == {"effort": "medium"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    assert not {"temperature", "top_p", "top_k", "thinking"} & body.keys()
    assert "abc123secret" not in body["messages"][0]["content"]  # scrubbed before sending
    # SDK 2.x: the key travels as a bearer credential, on API version 2026-10-21
    assert request.headers["authorization"] == "Bearer sk-ant-test"
    assert request.headers["anthropic-version"] == "2026-10-21"


@pytest.mark.asyncio
async def test_reads_text_after_thinking_blocks_and_records_usage():
    seen = []
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Likely cause: "},
        {"type": "text", "text": "division by zero in billing.py"},
    ]
    client = _client(_message(content), seen)
    answer = await client.complete(system="s", prompt="p", max_tokens=4000)
    assert answer == "Likely cause: division by zero in billing.py"
    assert client.last_usage == (812, 145)


@pytest.mark.asyncio
async def test_refusal_returns_no_text(caplog):
    seen = []
    client = _client(_message([], stop_reason="refusal",
                              stop_details={"type": "refusal", "category": "cyber", "explanation": None}), seen)
    assert await client.complete(system="s", prompt="p", max_tokens=4000) == ""
    assert "declined" in caplog.text


def test_dig_deeper_uses_the_deep_model(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "DRY_RUN_LLM", False)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-test")
    fast, deep = get_llm_client(), get_llm_client(deep=True)
    assert (fast.model, fast.effort) == (settings.LLM_MODEL_FAST, settings.LLM_EFFORT_FAST)
    assert (deep.model, deep.effort) == (settings.LLM_MODEL_DEEP, settings.LLM_EFFORT_DEEP)


def test_retired_model_is_reported_at_startup(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "DRY_RUN_LLM", False)
    monkeypatch.setattr(settings, "LLM_MODEL_FAST", "claude-3-5-sonnet-20241022")
    log_llm_privacy_notice()
    assert "has been retired" in caplog.text


def test_llm_cost_is_exact_for_fractional_rates_and_unknown_models_use_the_highest_rate(caplog):
    # Haiku 5.5: 1,000 in * $0.10/MTok = 100 micros; 999 out * $0.50/MTok = 499.5 -> 599.5 -> 600
    assert calculate_llm_cost_micros("claude-haiku-5-5", 1000, 999) == 600
    # Opus 5.5 is the highest configured rate: 1,000 * 4 + 1,000 * 20
    assert calculate_llm_cost_micros("claude-unknown", 1000, 1000) == 24000
    assert "No price for model claude-unknown" in caplog.text


def test_cost_plus_markup_has_no_float_rounding_error():
    entry = PriceBookEntry(1, "ai_investigation", "cost_plus", 0, 1.1, 0, 0)
    # As floats, 100 * 1.1 = 110.00000000000001, which rounded up to 111
    assert calculate_billed_micros(entry, 1, our_cost_micros=100)[0] == 110
    assert all(calculate_billed_micros(entry, 1, our_cost_micros=c)[0] == -(-c * 11 // 10) for c in range(1, 5000))
    assert Decimal("1.1") * 100 == 110
