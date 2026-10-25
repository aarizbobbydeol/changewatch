#!/usr/bin/env python3
"""ChangeWatch MCP server: lets an AI assistant read your releases, incidents and verdicts.

Model Context Protocol over stdio (JSON-RPC 2.0, one message per line), Python standard library
only, nothing to install. Read-only: it makes GET requests with the project's read key and
nothing else (no acknowledging, resolving or muting).

Settings (environment): CHANGEWATCH_URL (https://..., or http://localhost), CHANGEWATCH_READ_KEY
(cw_read_...), CHANGEWATCH_ALLOW_INSECURE=1 to allow plain http to another host. See docs/MCP.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

SERVER_INFO = {"name": "changewatch", "version": "0.1.0"}
# Newest first; an older client gets the version it asked for if it is one of these
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 20000  # characters returned per tool call
TIMEOUT_SEC = 15

_ID = {"type": "integer", "minimum": 1}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_incidents",
        "description": "Recent incidents (regressions ChangeWatch alerted on): id, status, error title, "
                       "file and line, whether that line changed in the deploy, owners, event count.",
        "inputSchema": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["possible", "confirmed", "resolved"]},
            "limit": _LIMIT}, "additionalProperties": False},
    },
    {
        "name": "get_incident",
        "description": "One incident in full: where it failed, the deploy, owners, the code around the "
                       "failing line, the AI investigation result and the complete timeline.",
        "inputSchema": {"type": "object", "properties": {"incident_id": _ID}, "required": ["incident_id"],
                        "additionalProperties": False},
    },
    {
        "name": "get_postmortem",
        "description": "A blameless postmortem draft (Markdown) for an incident, written from ChangeWatch's records.",
        "inputSchema": {"type": "object", "properties": {"incident_id": _ID}, "required": ["incident_id"],
                        "additionalProperties": False},
    },
    {
        "name": "get_release_verdict",
        "description": "Whether a deploy is safe: safe, watch, rollback or no_data, with the reasons. "
                       "Use the commit SHA (or the release name the deploy was registered with).",
        "inputSchema": {"type": "object", "properties": {
            "commit_sha": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
            "environment": {"type": "string", "default": "prod"}},
            "required": ["commit_sha"], "additionalProperties": False},
    },
    {
        "name": "get_deploy_review",
        "description": "The risk review of a deploy's diff (rules, plus AI when configured): overall risk, "
                       "findings with file and line, and tests to add.",
        "inputSchema": {"type": "object", "properties": {"deploy_id": _ID}, "required": ["deploy_id"],
                        "additionalProperties": False},
    },
    {
        "name": "list_deploys",
        "description": "Recent deploys with their verdicts.",
        "inputSchema": {"type": "object", "properties": {"limit": _LIMIT, "environment": {"type": "string"}},
                        "additionalProperties": False},
    },
    {
        "name": "list_issues",
        "description": "Error groups (one per distinct error) with counts and when they were first and last seen.",
        "inputSchema": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["open", "resolved"]}, "limit": _LIMIT},
            "additionalProperties": False},
    },
    {
        "name": "get_issue",
        "description": "One error group with recent occurrences (stack traces and request paths, secrets removed).",
        "inputSchema": {"type": "object", "properties": {"issue_id": _ID}, "required": ["issue_id"],
                        "additionalProperties": False},
    },
    {
        "name": "get_status",
        "description": "Whether ChangeWatch itself is healthy, and whether the app's data is arriving.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class ToolError(Exception):
    """A problem to report to the assistant as a tool result (not a protocol error)."""


def _settings() -> Tuple[str, str]:
    url = (os.environ.get("CHANGEWATCH_URL") or "").rstrip("/")
    key = os.environ.get("CHANGEWATCH_READ_KEY") or ""
    if not url or not key:
        raise ToolError("Set CHANGEWATCH_URL and CHANGEWATCH_READ_KEY in the MCP server's environment.")
    parsed = urllib.parse.urlparse(url)
    local = (parsed.hostname or "") in ("localhost", "127.0.0.1", "::1")
    insecure_ok = (os.environ.get("CHANGEWATCH_ALLOW_INSECURE") or "").lower() in ("1", "true", "yes")
    if parsed.scheme != "https" and not local and not insecure_ok:
        raise ToolError("CHANGEWATCH_URL must use https:// (the read key would travel unencrypted).")
    return url, key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never send the key to another host


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url, key = _settings()
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    req = urllib.request.Request(f"{url}{path}" + (f"?{query}" if query else ""), method="GET",
                                 headers={"X-API-Key": key, "Accept": "application/json",
                                          "User-Agent": "changewatch-mcp/0.1.0"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT_SEC) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ToolError("ChangeWatch rejected the read key (check CHANGEWATCH_READ_KEY).")
        if exc.code == 404:
            raise ToolError("Not found in this project.")
        if 300 <= exc.code < 400:
            raise ToolError("ChangeWatch answered with a redirect: set CHANGEWATCH_URL to the final address.")
        raise ToolError(f"ChangeWatch answered HTTP {exc.code}.")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ToolError(f"Cannot reach ChangeWatch at {urllib.parse.urlparse(url).netloc} ({type(reason).__name__}).")


def _int(args: Dict[str, Any], name: str, default: Optional[int] = None, low: int = 1, high: int = 10**9) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"'{name}' must be a whole number from {low} to {high}.")
    return value


def call_tool(name: str, args: Dict[str, Any]) -> Any:
    """The data for one tool call (raises ToolError for problems the assistant should see)."""
    if name == "list_incidents":
        status = args.get("status")
        if status not in (None, "possible", "confirmed", "resolved"):
            raise ToolError("'status' must be possible, confirmed or resolved.")
        return _get("/api/incidents", {"status": status, "limit": _int(args, "limit", 10, 1, 50)})
    if name == "get_incident":
        return _get(f"/api/incidents/{_int(args, 'incident_id')}")
    if name == "get_postmortem":
        return _get(f"/api/incidents/{_int(args, 'incident_id')}/postmortem").get("markdown", "")
    if name == "get_release_verdict":
        sha = args.get("commit_sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", sha):
            raise ToolError("'commit_sha' must be a commit SHA or release name (letters, digits, . _ -).")
        env = args.get("environment", "prod")
        if not isinstance(env, str) or not 0 < len(env) <= 50:
            raise ToolError("'environment' must be a short name such as prod.")
        return _get("/v1/verdict", {"sha": sha, "environment": env})
    if name == "get_deploy_review":
        return _get(f"/api/deploys/{_int(args, 'deploy_id')}/review")
    if name == "list_deploys":
        env = args.get("environment")
        if env is not None and (not isinstance(env, str) or len(env) > 50):
            raise ToolError("'environment' must be a short name such as prod.")
        deploys = _get("/api/deploys", {"limit": _int(args, "limit", 10, 1, 50), "environment": env})
        # Whole diffs would flood the assistant's context; get_incident shows the code that failed
        return [{k: v for k, v in d.items() if k != "diff"} for d in deploys] if isinstance(deploys, list) else deploys
    if name == "list_issues":
        status = args.get("status")
        if status not in (None, "open", "resolved"):
            raise ToolError("'status' must be open or resolved.")
        return _get("/api/issues", {"status": status, "limit": _int(args, "limit", 10, 1, 50)})
    if name == "get_issue":
        return _get(f"/api/issues/{_int(args, 'issue_id')}")
    if name == "get_status":
        return _get("/v1/status")
    raise KeyError(name)


def _text(data: Any) -> str:
    text = data if isinstance(data, str) else json.dumps(data, indent=1, default=str)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n[cut: ask for fewer items]"


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The JSON-RPC response to one message, or None for a notification."""
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notifications (notifications/initialized, cancelled ...) need no answer

    def result(value: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": value}

    def error(code: int, text: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return result({
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "Read-only access to ChangeWatch: release verdicts, incidents, postmortem drafts, "
                            "deploys and error groups for one project.",
        })
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return error(-32602, "arguments must be an object")
        try:
            data = call_tool(name, args)
        except KeyError:
            return error(-32602, f"Unknown tool: {name}")
        except ToolError as exc:
            return result({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        return result({"content": [{"type": "text", "text": _text(data)}], "isError": False})
    return error(-32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response: Optional[Dict[str, Any]] = {"jsonrpc": "2.0", "id": None,
                                                   "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(message, list):  # a batch
                answers = [r for r in (handle(m) for m in message if isinstance(m, dict)) if r]
                response = answers or None  # type: ignore[assignment]
            elif isinstance(message, dict):
                response = handle(message)
            else:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
