"""App log lines attached to errors (opt-in capture_logs) and logged-only exceptions."""
import logging
import queue
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

sdk_root = Path(__file__).resolve().parent.parent
if str(sdk_root) not in sys.path:
    sys.path.insert(0, str(sdk_root))

import pytest  # noqa: E402

import changewatch  # noqa: E402
from changewatch import ASGIMiddleware, Client  # noqa: E402


@pytest.fixture(autouse=True)
def clean_root_logger():
    """Start from Python's default logging (other tests install the server's redacting handler,
    whose filter strips exception info from records before later handlers see them)."""
    root = logging.getLogger()
    saved_handlers, saved_filters, saved_level = root.handlers[:], root.filters[:], root.level
    saved_factory = logging.getLogRecordFactory()
    root.handlers, root.filters = [], []
    yield
    root.handlers, root.filters = saved_handlers, saved_filters
    root.setLevel(saved_level)
    logging.setLogRecordFactory(saved_factory)


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
    logging.getLogger("shop").warning("after stop")
    assert len(lines) == 50 and lines[0]["message"] == "line 30" and lines[-1]["message"] == "line 79"
    assert handler not in logging.getLogger().handlers
    assert handler.snapshot()[-1]["message"] == "line 79"


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


def test_capture_logs_leaves_the_apps_logging_setup_alone():
    """No handler goes on the root logger: Python's own output of errors (when logging is not
    configured) still prints, basicConfig() after init still configures logging, and a later
    dictConfig() does not switch the capture off. Run in a fresh interpreter, like an app."""
    script = textwrap.dedent(f"""
        import logging, logging.config, sys
        sys.path.insert(0, {str(sdk_root)!r})
        import changewatch
        client = changewatch.init(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9", capture_logs=True)
        client._running = False
        logging.getLogger("app").error("printed by python")
        logging.basicConfig(level=logging.INFO, format="BASIC %(message)s")
        logging.getLogger("app").info("configured after init")
        logging.config.dictConfig({{"version": 1, "disable_existing_loggers": False,
            "formatters": {{"f": {{"format": "DICT %(message)s"}}}},
            "handlers": {{"h": {{"class": "logging.StreamHandler", "formatter": "f"}}}},
            "root": {{"handlers": ["h"], "level": "INFO"}}}})
        logging.getLogger("app").warning("after dictConfig")
        print([line["message"] for line in client._log_handler.snapshot()])
        changewatch.shutdown()
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert "printed by python" in out.stderr
    assert "BASIC configured after init" in out.stderr and "DICT after dictConfig" in out.stderr
    assert "'printed by python'" in out.stdout and "'after dictConfig'" in out.stdout


def test_stop_puts_the_previous_record_factory_back_or_leaves_ours_passing_records_through():
    original = logging.getLogRecordFactory()
    client = _client(capture_logs=True)
    client.stop()
    assert logging.getLogRecordFactory() is original

    client = _client(capture_logs=True)
    handler, ours = client._log_handler, logging.getLogRecordFactory()

    def theirs(*args, **kwargs):  # another library wrapped ours after init
        record = ours(*args, **kwargs)
        record.tagged = True
        return record

    logging.setLogRecordFactory(theirs)
    client.stop()
    assert logging.getLogRecordFactory() is theirs
    logging.getLogger("shop").warning("after stop")
    assert handler.snapshot() == []
    assert logging.getLogRecordFactory()("shop", logging.INFO, "f", 1, "m", (), None).tagged


def test_only_lines_the_apps_logging_would_show_are_kept():
    client = _client(capture_logs=True)
    quiet = logging.getLogger("lib.quiet")
    quiet.propagate = False  # has its own output, never reaches the root logger
    off = logging.getLogger("lib.off")
    off.disabled = True  # what dictConfig(disable_existing_loggers=True) does
    try:
        quiet.warning("not the app's line")
        off.warning("switched off")
        logging.getLogger("changewatch.worker").warning("ours")
        logging.warning("from the root logger")
        logging.getLogger("shop").info("under the level")
        lines = [line["message"] for line in client._log_handler.snapshot()]
    finally:
        client.stop()
        quiet.propagate, off.disabled = True, False
    assert lines == ["from the root logger"]


def test_capture_logged_errors_works_without_capture_logs():
    client = _client(capture_logged_errors=True)
    try:
        try:
            raise ValueError("bad row")
        except ValueError:
            logging.getLogger("jobs").exception("import failed")
    finally:
        client.stop()
    [event] = _errors(client)
    assert event["exc_type"] == "ValueError" and event["extra"]["logger"] == "jobs"
    assert "logs" not in event


def test_values_split_by_the_line_cut_are_still_redacted():
    client = _client(capture_logs=True)
    log = logging.getLogger("shop")
    try:
        log.warning("x" * 485 + " jane.doe@example.com placed order")
        log.warning("x" * 475 + " using ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5")
        log.warning("x" * 484 + " 4111 1111 1111 1111")
        # Cut at 1000 characters when kept, and redacting the secret shortens the text before it
        log.warning("password=" + "p" * 600 + " " + "x" * 380 + " jane.doe@example.com")
        lines = client._log_handler.snapshot()
    finally:
        client.stop()
    text = " ".join(line["message"] for line in lines)
    assert "jane.doe" not in text and "ghp_aB3dE5" not in text and "4111 1111" not in text
    assert all("[REDACTED_" in line["message"] for line in lines[:3])  # the marker itself may be cut
    assert all(len(line["message"]) <= 500 for line in lines)


def test_an_exception_dropped_on_a_full_queue_can_still_be_reported():
    client = _client(capture_logs=True, capture_logged_errors=True)
    client._queue = queue.Queue(maxsize=1)  # a full queue the stopped worker never touches
    client._queue.put_nowait({"type": "heartbeat"})
    try:
        try:
            raise ConnectionError("db went away")
        except ConnectionError as exc:
            logging.getLogger("shop.jobs").exception("job failed")  # queue full: dropped
            _errors(client)  # the worker sends the queue
            client.capture_exception(exc)
    finally:
        client.stop()
    assert [e["exc_type"] for e in _errors(client)] == ["ConnectionError"]


def test_the_same_exception_logged_again_later_is_reported_again():
    """A cached error raised again (a failed connection kept by a library) is a new failure each
    time, even though Python grows the traceback of the same exception object."""
    client = _client(capture_logs=True, capture_logged_errors=True)
    try:
        raise ConnectionError("db unreachable")
    except ConnectionError as exc:
        cached = exc
    try:
        for _ in range(3):
            try:
                raise cached
            except ConnectionError:
                logging.getLogger("jobs").exception("job failed")
    finally:
        client.stop()
    assert len(_errors(client)) == 3


@pytest.mark.asyncio
async def test_logged_then_raised_in_a_request_is_reported_once_with_its_path_and_method():
    import asyncio

    client = _client(capture_logs=True, capture_logged_errors=True)
    log = logging.getLogger("shop.checkout")

    def pay():
        try:
            raise RuntimeError("card declined by provider")
        except RuntimeError:
            log.exception("checkout failed")
            raise

    async def app(scope, receive, send):
        if scope["path"] == "/checkout/pay":
            pay()
        else:
            await asyncio.to_thread(pay)  # a sync endpoint, run in a worker thread

    async def server(scope):
        """Like an ASGI server: logs what the app raised (here through the root logger)."""
        try:
            await ASGIMiddleware(app, client=client)(scope, None, None)
        except RuntimeError:
            logging.getLogger("server.error").exception("Exception in ASGI application")

    try:
        await server({"type": "http", "path": "/checkout/pay", "method": "POST"})
        await server({"type": "http", "path": "/checkout/sync", "method": "PUT"})
    finally:
        client.stop()
    events = _errors(client)
    assert sorted((e["request_path"], e["extra"]["method"]) for e in events) == [("/checkout/pay", "POST"), ("/checkout/sync", "PUT")]
    assert {e["extra"]["logger"] for e in events} == {"shop.checkout"}


@pytest.mark.asyncio
async def test_captures_inside_a_request_carry_its_path():
    client = _client()
    saved_client, changewatch._client = changewatch._client, client

    async def app(scope, receive, send):
        try:
            raise KeyError("sku")
        except KeyError as exc:
            changewatch.capture_exception(exc)

    try:
        await ASGIMiddleware(app, client=client)({"type": "http", "path": "/cart/42", "method": "PUT"}, None, None)
        try:
            raise ValueError("after the request")
        except ValueError as exc:
            client.capture_exception(exc)
    finally:
        changewatch._client = saved_client
        client.stop()
    events = {e["exc_type"]: e for e in _errors(client)}
    assert (events["KeyError"]["request_path"], events["KeyError"]["extra"].get("method")) == ("/cart/42", "PUT")
    assert (events["ValueError"]["request_path"], events["ValueError"]["extra"].get("method")) == (None, None)


def test_logged_then_raised_in_a_thread_is_reported_once():
    saved_hook = threading.excepthook
    threading.excepthook = lambda args: None  # pytest's own hook would warn about the thread
    client = changewatch.init(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9",
                              capture_logs=True, capture_logged_errors=True)
    client._running = False

    def job():
        try:
            raise ValueError("bad row")
        except ValueError:
            logging.getLogger("jobs").exception("import failed")
            raise

    try:
        worker = threading.Thread(target=job)
        worker.start()
        worker.join()
        errors = _errors(client)
    finally:
        changewatch.shutdown()
        threading.excepthook = saved_hook
    assert [e["extra"].get("logger") for e in errors] == ["jobs"]


def test_a_second_init_stops_the_first_clients_log_capture():
    original = logging.getLogRecordFactory()
    first = changewatch.init(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9",
                             capture_logs=True, capture_logged_errors=True)
    first._running = False
    second = changewatch.init(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9",
                              capture_logs=True, capture_logged_errors=True)
    second._running = False
    changewatch.shutdown()
    try:
        raise ValueError("after shutdown")
    except ValueError:
        logging.getLogger("app").exception("handled")
    assert _errors(first) == [] and first._log_handler is None
    assert logging.getLogRecordFactory() is original
