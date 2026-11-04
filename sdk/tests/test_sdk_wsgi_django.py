"""WSGI apps (WSGIMiddleware, instrument_flask) and Django (DjangoMiddleware).

The Flask and Django tests run the real frameworks when they are installed and are skipped
otherwise; the rest use stand-ins, so the SDK itself stays dependency-free.
"""
import subprocess
import sys
import textwrap
import time
from pathlib import Path

sdk_root = Path(__file__).resolve().parent.parent
if str(sdk_root) not in sys.path:
    sys.path.insert(0, str(sdk_root))

import pytest  # noqa: E402

import changewatch  # noqa: E402
from changewatch import Client, DjangoMiddleware, WSGIMiddleware  # noqa: E402


def _client(**kwargs):
    client = Client(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9", **kwargs)
    client._running = False
    return client


def _events(client):
    events = []
    while not client._queue.empty():
        events.append(client._queue.get_nowait())
    return events


def _wsgi_app(status="200 OK", body=b"ok", raises=None, sleep=0.0):
    def app(environ, start_response):
        if sleep:
            time.sleep(sleep)
        if raises is not None:
            raise raises
        start_response(status, [("Content-Type", "text/plain")])
        return [body]
    return app


def _call(app, path="/checkout", method="POST", script_name=""):
    seen = {}

    def start_response(status, headers, exc_info=None):
        seen["status"] = status
        return lambda data: None

    environ = {"PATH_INFO": path, "REQUEST_METHOD": method, "SCRIPT_NAME": script_name}
    return app(environ, start_response), seen


def test_wsgi_exception_is_reported_with_path_and_method_and_raised_unchanged():
    client = _client()
    error = ZeroDivisionError("division by zero")
    app = WSGIMiddleware(_wsgi_app(raises=error), client=client)
    with pytest.raises(ZeroDivisionError) as raised:
        _call(app, path="/pay", script_name="/shop")
    assert raised.value is error
    [event] = _events(client)
    assert event["type"] == "error" and event["exc_type"] == "ZeroDivisionError"
    assert event["request_path"] == "/shop/pay" and event["extra"]["method"] == "POST"
    assert (client._heartbeat_requests, client._heartbeat_errors) == (1, 1)


def test_wsgi_5xx_counts_as_failed_and_the_response_is_untouched():
    client = _client()
    app = WSGIMiddleware(_wsgi_app(status="503 Service Unavailable", body=b"down"), client=client)
    body, seen = _call(app)
    assert body == [b"down"] and seen["status"] == "503 Service Unavailable"
    assert _events(client) == []  # no exception to report, only the count
    assert (client._heartbeat_requests, client._heartbeat_errors) == (1, 1)

    _call(WSGIMiddleware(_wsgi_app(status="404 Not Found"), client=client))
    assert (client._heartbeat_requests, client._heartbeat_errors) == (2, 1)  # 4xx is not a failure


def test_wsgi_slow_request_is_reported():
    client = _client(slow_ms=30.0)
    _call(WSGIMiddleware(_wsgi_app(sleep=0.05), client=client), path="/report", method="GET")
    [event] = _events(client)
    assert event["type"] == "slow_request" and event["request_path"] == "/report"
    assert event["duration_ms"] >= 30.0


def test_wsgi_without_a_client_or_disabled_is_a_pass_through(monkeypatch):
    monkeypatch.setattr(changewatch, "_client", None)
    inner = _wsgi_app(body=b"same")
    assert _call(WSGIMiddleware(inner))[0] == [b"same"]
    client = _client()
    client.disabled = True
    assert _call(WSGIMiddleware(inner, client=client))[0] == [b"same"]
    assert client._heartbeat_requests == 0


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _Request:
    path, method = "/orders/7", "GET"


def test_django_middleware_reports_the_view_exception_once_and_counts_the_500(monkeypatch):
    client = _client(capture_logged_errors=True)
    monkeypatch.setattr(changewatch, "_client", client)
    error = KeyError("sku")

    def get_response(request):
        # What Django does inside: the view raises, process_exception runs, a 500 comes back
        middleware.process_exception(request, error)
        middleware.process_exception(request, error)  # seen twice in one request: sent once
        return _Response(500)

    middleware = DjangoMiddleware(get_response)
    assert middleware(_Request()).status_code == 500
    [event] = _events(client)
    assert event["exc_type"] == "KeyError" and event["request_path"] == "/orders/7"
    assert event["extra"]["method"] == "GET"
    assert (client._heartbeat_requests, client._heartbeat_errors) == (1, 1)
    client.stop()


def test_django_middleware_without_init_changes_nothing(monkeypatch):
    monkeypatch.setattr(changewatch, "_client", None)
    response = _Response(200)
    middleware = DjangoMiddleware(lambda request: response)
    assert middleware(_Request()) is response
    assert middleware.process_exception(_Request(), ValueError("x")) is None


def test_django_middleware_async(monkeypatch):
    pytest.importorskip("asgiref")
    import asyncio

    client = _client()
    monkeypatch.setattr(changewatch, "_client", client)

    async def get_response(request):
        return _Response(502)

    middleware = DjangoMiddleware(get_response)
    assert asyncio.iscoroutinefunction(middleware) or middleware._async
    response = asyncio.run(middleware(_Request()))
    assert response.status_code == 502
    assert (client._heartbeat_requests, client._heartbeat_errors) == (1, 1)


def test_flask_errors_404_and_logged_exception_are_each_handled_right(monkeypatch):
    flask = pytest.importorskip("flask")
    client = _client(capture_logged_errors=True)
    monkeypatch.setattr(changewatch, "_client", client)
    app = flask.Flask("shop")

    @app.route("/boom", methods=["POST"])
    def boom():
        return 1 / 0

    @app.route("/ok")
    def ok():
        return "fine"

    assert changewatch.instrument_flask(app) is app
    web = app.test_client()
    assert web.post("/boom").status_code == 500  # Flask still answers as it always does
    assert web.get("/ok").data == b"fine"
    assert web.get("/missing").status_code == 404
    errors = [e for e in _events(client) if e["type"] == "error"]
    assert len(errors) == 1  # Flask logs it too: still reported once
    assert errors[0]["exc_type"] == "ZeroDivisionError" and errors[0]["request_path"] == "/boom"
    assert errors[0]["extra"]["method"] == "POST"
    assert (client._heartbeat_requests, client._heartbeat_errors) == (3, 1)
    client.stop()


DJANGO_APP = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {sdk!r})
    import django
    from django.conf import settings
    from django.http import HttpResponse
    from django.urls import path

    def boom(request):
        return 1 / 0

    async def aboom(request):
        raise KeyError("async view")

    def ok(request):
        return HttpResponse("fine")

    urlpatterns = [path("boom", boom), path("aboom", aboom), path("ok", ok)]
    settings.configure(
        ROOT_URLCONF=__name__, ALLOWED_HOSTS=["*"], DEBUG=False,
        MIDDLEWARE=["changewatch.DjangoMiddleware"],
        LOGGING_CONFIG=None,
    )
    django.setup()

    import asyncio
    import changewatch
    from django.test import AsyncClient, Client as Web

    cw = changewatch.init(api_key="cw_ingest_test", endpoint="http://127.0.0.1:9", capture_logged_errors=True)
    cw._running = False
    web = Web(raise_request_exception=False)
    statuses = [web.get("/boom").status_code, web.get("/ok").status_code, web.get("/nope").status_code]
    statuses.append(asyncio.run(AsyncClient(raise_request_exception=False).get("/aboom")).status_code)
    events = []
    while not cw._queue.empty():
        events.append(cw._queue.get_nowait())
    errors = [(e["exc_type"], e["request_path"], e["extra"].get("method")) for e in events if e["type"] == "error"]
    print(statuses, errors, cw._heartbeat_requests, cw._heartbeat_errors)
    """
)


def test_django_app_sync_and_async_views(tmp_path):
    pytest.importorskip("django")
    script = tmp_path / "django_app.py"
    script.write_text(DJANGO_APP.format(sdk=str(sdk_root)))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == (
        "[500, 200, 404, 500] [('ZeroDivisionError', '/boom', 'GET'), ('KeyError', '/aboom', 'GET')] 4 2"
    )
