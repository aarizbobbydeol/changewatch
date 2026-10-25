"""App log lines attached to errors (opt-in capture_logs) and logged-only exceptions."""
import logging
import sys
from pathlib import Path

sdk_root = Path(__file__).resolve().parent.parent
if str(sdk_root) not in sys.path:
    sys.path.insert(0, str(sdk_root))

import pytest  # noqa: E402

from changewatch import Client  # noqa: E402


@pytest.fixture(autouse=True)
def clean_root_logger():
    """Start from Python's default logging (other tests install the server's redacting handler,
    whose filter strips exception info from records before later handlers see them)."""
    root = logging.getLogger()
    saved_handlers, saved_filters, saved_level = root.handlers[:], root.filters[:], root.level
    root.handlers, root.filters = [], []
    yield
    root.handlers, root.filters = saved_handlers, saved_filters
    root.setLevel(saved_level)


def _client(**kwargs):
    client = Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9", **kwargs)
    client._running = False
    return client


def _errors(client):
    events = []
    while not client._queue.empty():
        events.append(client._queue.get_nowait())
    return [e for e in events if e["type"] == "error"]


def test_errors_carry_the_last_log_lines_scrubbed_and_cut():
    client = _client(capture_logs=True)
    log = logging.getLogger("shop.billing")
    log.setLevel(logging.INFO)
    try:
        log.info("routine detail")  # under the WARNING default: not kept
        log.warning("payment provider slow for jane.doe@example.com")
        log.error("retrying with password=hunter2hunter2 and " + "x" * 2000)
        logging.getLogger("changewatch").warning("our own warning")  # never kept
        try:
            raise ValueError("boom")
        except ValueError as exc:
            client.capture_exception(exc)
    finally:
        client.stop()
    [event] = _errors(client)
    assert [(line["level"], line["logger"]) for line in event["logs"]] == [("WARNING", "shop.billing"), ("ERROR", "shop.billing")]
    text = " ".join(line["message"] for line in event["logs"])
    assert "jane.doe@example.com" not in text and "hunter2hunter2" not in text
    assert all(len(line["message"]) <= 500 for line in event["logs"])


def test_without_capture_logs_nothing_is_attached_or_installed():
    before = list(logging.getLogger().handlers)
    client = _client()
    logging.getLogger("shop").warning("hello")
    try:
        raise KeyError("k")
    except KeyError as exc:
        client.capture_exception(exc)
    client.stop()
    assert "logs" not in _errors(client)[0]
    assert logging.getLogger().handlers == before


def test_only_the_last_fifty_lines_are_kept_and_the_handler_is_removed_on_stop():
    client = _client(capture_logs=True)
    handler = client._log_handler
    for i in range(80):
        logging.getLogger("shop").warning("line %d", i)
    lines = handler.snapshot()
    client.stop()
    assert len(lines) == 50 and lines[0]["message"] == "line 30" and lines[-1]["message"] == "line 79"
    assert handler not in logging.getLogger().handlers


def test_logged_exceptions_are_reported_once():
    client = _client(capture_logs=True, capture_logged_errors=True)
    try:
        try:
            raise ConnectionError("db went away")
        except ConnectionError as exc:
            logging.getLogger("shop.jobs").exception("job failed")
            client.capture_exception(exc)  # the same exception reaching another hook later
    finally:
        client.stop()
    errors = _errors(client)
    assert len(errors) == 1 and errors[0]["exc_type"] == "ConnectionError"
    assert errors[0]["extra"].get("logger") == "shop.jobs"
    assert errors[0]["logs"][-1]["message"] == "job failed"  # the log line that reported it
