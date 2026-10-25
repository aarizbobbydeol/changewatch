"""The MCP server, run as a real subprocess speaking JSON-RPC over stdio to a recording HTTP server."""
import importlib.util
import io
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2] / "mcp-server" / "changewatch_mcp.py"

RESPONSES = {
    "/api/incidents": (200, [{"id": 7, "status": "confirmed", "title": "ZeroDivisionError: division by zero",
                              "where_file": "app/billing.py", "where_line": 42}]),
    "/api/incidents/7/postmortem": (200, {"markdown": "# Postmortem (draft): ZeroDivisionError", "draft": True}),
    "/v1/verdict": (200, {"verdict": "rollback", "reasons": ["error rate 25.0% vs 0.0% before"]}),
    "/api/incidents/99": (404, {"detail": "Incident not found"}),
}


class Recorder:
    def __init__(self, status_override=None):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                outer.requests.append({"method": self.command, "path": self.path, "key": self.headers.get("X-API-Key")})
                path = self.path.split("?", 1)[0]
                code, body = status_override or RESPONSES.get(path, (404, {"detail": "not found"}))
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _answer

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def _session(messages, env):
    """Send JSON-RPC lines to a fresh server process; return its responses by id."""
    proc = subprocess.run([sys.executable, "-I", str(SERVER)], input="\n".join(json.dumps(m) for m in messages) + "\n",
                          capture_output=True, text=True, timeout=30, env=env)
    out = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert "cw_read_secret" not in proc.stderr + proc.stdout.replace('"cw_read_secret"', "")
    return {r.get("id"): r for r in out}, out


def _call(i, name, args=None):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


@pytest.fixture
def api():
    rec = Recorder()
    yield rec
    rec.close()


def _env(url):
    return {"CHANGEWATCH_URL": url, "CHANGEWATCH_READ_KEY": "cw_read_secret", "PATH": "/usr/bin:/bin"}


def test_handshake_and_tool_list(api):
    by_id, out = _session([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ], _env(api.url))
    assert len(out) == 3  # the notification gets no answer
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert by_id[1]["result"]["capabilities"] == {"tools": {"listChanged": False}}
    names = [t["name"] for t in by_id[2]["result"]["tools"]]
    assert names == ["list_incidents", "get_incident", "get_postmortem", "get_release_verdict",
                     "get_deploy_review", "list_deploys", "list_issues", "get_issue", "list_runbooks", "get_status"]
    assert by_id[3]["result"] == {}


def test_tools_read_with_the_key_and_only_ever_get(api):
    by_id, _ = _session([
        _call(1, "list_incidents", {"status": "confirmed", "limit": 5}),
        _call(2, "get_postmortem", {"incident_id": 7}),
        _call(3, "get_release_verdict", {"commit_sha": "abc123", "environment": "prod"}),
        _call(4, "get_incident", {"incident_id": 99}),
    ], _env(api.url))
    assert "app/billing.py" in by_id[1]["result"]["content"][0]["text"] and by_id[1]["result"]["isError"] is False
    assert by_id[2]["result"]["content"][0]["text"].startswith("# Postmortem (draft)")
    assert '"rollback"' in by_id[3]["result"]["content"][0]["text"]
    assert by_id[4]["result"] == {"content": [{"type": "text", "text": "Not found in this project."}], "isError": True}
    assert {r["method"] for r in api.requests} == {"GET"}
    assert all(r["key"] == "cw_read_secret" for r in api.requests)
    assert api.requests[0]["path"] == "/api/incidents?status=confirmed&limit=5"


def test_bad_input_and_unknown_tools_never_reach_the_api(api):
    by_id, _ = _session([
        _call(1, "get_incident", {"incident_id": "7; DROP"}),
        _call(2, "get_release_verdict", {"commit_sha": "../../admin"}),
        _call(3, "list_incidents", {"limit": 500}),
        _call(4, "resolve_incident", {"incident_id": 7}),
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    ], _env(api.url))
    for i in (1, 2, 3):
        assert by_id[i]["result"]["isError"] is True
    assert by_id[4]["error"]["code"] == -32602
    assert by_id[5]["error"]["code"] == -32601
    assert api.requests == []


def test_rejected_key_and_plain_http_to_another_host():
    rejected = Recorder(status_override=(401, {"detail": "Invalid API key"}))
    try:
        by_id, _ = _session([_call(1, "get_status")], _env(rejected.url))
    finally:
        rejected.close()
    assert "rejected the read key" in by_id[1]["result"]["content"][0]["text"]

    by_id, _ = _session([_call(1, "get_status")], _env("http://203.0.113.10:8000"))
    assert "must use https://" in by_id[1]["result"]["content"][0]["text"]


class RawServer:
    """Answers every connection with the same raw bytes: a cut-off body, a garbage status line ..."""

    def __init__(self, answer: bytes):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"

        def loop():
            while True:
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    return
                with conn:
                    conn.recv(65536)
                    conn.sendall(answer)

        threading.Thread(target=loop, daemon=True).start()

    def close(self):
        self.sock.close()


PING = {"jsonrpc": "2.0", "id": 9, "method": "ping"}


def _json_answer(body: bytes) -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)


def test_params_that_are_not_an_object_get_an_error_and_the_server_keeps_answering(api):
    by_id, _ = _session([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["list_runbooks"]},  # positional params
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": "x"},
        PING,
    ], _env(api.url))
    assert by_id[1]["error"]["code"] == -32602 and by_id[2]["error"]["code"] == -32602
    assert by_id[9]["result"] == {}
    assert api.requests == []


@pytest.mark.parametrize("answer", [
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"markdown\":",
    b"GARBAGE\r\n\r\n",
    _json_answer(b"null"),
    _json_answer(b"[1, 2]"),
], ids=["cut-off body", "garbage status line", "null body", "list of numbers"])
def test_a_broken_answer_is_a_tool_error_and_the_server_keeps_answering(answer):
    raw = RawServer(answer)
    try:
        by_id, _ = _session([_call(1, "get_postmortem", {"incident_id": 7}), _call(2, "list_deploys"), PING], _env(raw.url))
    finally:
        raw.close()
    assert by_id[1]["result"]["isError"] is True
    assert "result" in by_id[2]
    assert by_id[9]["result"] == {}


def test_a_bad_port_in_the_address_is_a_tool_error():
    by_id, _ = _session([_call(1, "get_status"), PING], _env("https://changewatch.example:80a"))
    assert by_id[1]["result"]["isError"] is True and "changewatch.example:80a" in by_id[1]["result"]["content"][0]["text"]
    assert by_id[9]["result"] == {}


def test_any_other_failure_is_an_internal_error_and_the_next_request_is_answered(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("changewatch_mcp", SERVER)
    mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp)
    real = mcp.handle

    def flaky(message):
        if message.get("id") in (1, 3):
            raise RuntimeError("cw_read_secret at https://changewatch.example")
        return real(message)

    monkeypatch.setattr(mcp, "handle", flaky)
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, [{"jsonrpc": "2.0", "id": 3, "method": "ping"}, PING],
             {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in lines)))
    mcp.main()
    out = capsys.readouterr().out
    assert "cw_read_secret" not in out and "changewatch.example" not in out
    answers = [json.loads(line) for line in out.splitlines()]
    assert answers[0] == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "Internal error"}}
    assert answers[1] == [{"jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "Internal error"}},
                          {"jsonrpc": "2.0", "id": 9, "result": {}}]
    assert answers[2] == {"jsonrpc": "2.0", "id": 2, "result": {}}
