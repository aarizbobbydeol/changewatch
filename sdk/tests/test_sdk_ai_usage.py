"""Counting the app's own AI calls for cost tracking: summed per minute, never raising."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sdk_root = Path(__file__).resolve().parent.parent
if str(sdk_root) not in sys.path:
    sys.path.insert(0, str(sdk_root))

import changewatch  # noqa: E402


def _flushed(client):
    with client._lock:
        client._flush_ai_usage_locked(time.time())
    events = []
    while not client._queue.empty():
        events.append(client._queue.get_nowait())
    return [e for e in events if e["type"] == "ai_usage"]


def test_token_counts_are_read_from_anthropic_and_openai_responses():
    client = changewatch.init(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9")
    client._running = False
    try:
        anthropic_like = SimpleNamespace(model="claude-sonnet-5-5", usage=SimpleNamespace(
            input_tokens=1000, output_tokens=200, cache_creation_input_tokens=300, cache_read_input_tokens=5000))
        openai_like = {"model": "gpt-x", "usage": {"prompt_tokens": 40, "completion_tokens": 7}}
        changewatch.record_ai_usage_from_response(anthropic_like, feature="search")
        changewatch.record_ai_usage_from_response(openai_like)
        changewatch.record_ai_usage_from_response(object())  # nothing usable: counted as an unknown call, never raises
        events = {e["model"]: e for e in _flushed(client)}
    finally:
        changewatch.shutdown()
    assert (events["claude-sonnet-5-5"]["input_tokens"], events["claude-sonnet-5-5"]["output_tokens"]) == (1300, 200)
    assert events["claude-sonnet-5-5"]["feature"] == "search"
    assert (events["gpt-x"]["input_tokens"], events["gpt-x"]["output_tokens"]) == (40, 7)
    assert events["unknown"]["calls"] == 1


def test_sums_stay_within_the_servers_limits():
    """One sum over the server's limits made it reject the whole batch, error events included
    (here: micro-USD passed as dollars, 400 times in a minute)."""
    client = changewatch.Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9")
    client._running = False
    for _ in range(400):
        client.record_ai_usage("claude-x", 10**10, 10**10, cost_usd=3000, calls=10**7)
    client.record_ai_usage("other", input_tokens=10**15)
    events = {e["model"]: e for e in _flushed(client)}
    client.stop()
    big = events["claude-x"]
    assert (big["input_tokens"], big["output_tokens"], big["cost_micros"]) == (10**12, 10**12, 10**12)
    assert big["calls"] == 2_147_483_647
    assert events["other"]["input_tokens"] == 10**12
    assert client._internal_errors > 0


def test_bad_input_never_raises_into_the_app():
    client = changewatch.Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9")
    client._running = False
    client.record_ai_usage("m", input_tokens="lots", output_tokens=None)
    client.record_ai_usage("m", cost_usd="not money")
    client.stop()
    assert client._internal_errors == 2
