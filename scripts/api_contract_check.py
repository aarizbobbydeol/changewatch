"""REST API contract check against a running ChangeWatch, driven by its own /openapi.json.

For every operation it checks, without changing any data:
  auth        no key -> 401, unknown key -> 401, wrong key type -> 403 (ingest vs read, admin-only)
  reads       read key with valid parameters -> 2xx (404 for ids that do not exist), never 5xx
  validation  out-of-range or invalid parameters and malformed JSON bodies -> 422, never 5xx
  isolation   project A's read key on project B's incident and issue ids -> 404
  errors      every 4xx answer is JSON with a "detail" field

Endpoints that change data (settings, budget, mutes, incident actions, purge, telemetry opt-in)
only get the auth and malformed-body checks, so the script is safe to run against real projects.

Usage:
  python scripts/api_contract_check.py --api http://localhost:8000 \\
      --ingest-key cw_ingest_A --read-key cw_read_A --other-read-key cw_read_B [--admin-key ...]
Exit code 0 when every check passes.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

PUBLIC = {("GET", "/healthz"), ("GET", "/a/{token}"), ("POST", "/a/{token}"), ("POST", "/v1/integrations/slack/interactions")}
INGEST_ONLY = {("POST", "/v1/events"), ("POST", "/v1/deploys")}
ANY_KEY = {("GET", "/v1/auth/verify")}
CHANGES_DATA = {
    ("PUT", "/api/settings"), ("PUT", "/v1/budget"), ("POST", "/api/mutes"), ("DELETE", "/api/mutes/{id}"),
    ("POST", "/api/incidents/{id}/actions"), ("POST", "/api/incidents/{id}/feedback"),
    ("POST", "/api/incidents/{id}/investigate"), ("DELETE", "/v1/project-data"), ("POST", "/v1/telemetry/opt-in"),
    ("POST", "/v1/events"), ("POST", "/v1/deploys"),
    ("POST", "/api/runbooks"), ("PUT", "/api/runbooks/{id}"), ("DELETE", "/api/runbooks/{id}"),
}
SAMPLES = {"sha": "deadbeefcafe", "metric": "overview", "meter": "ai_investigation", "token": "not-a-real-token"}
NOT_FOUND_OK = {("GET", "/v1/verdict")}  # the sample sha has no deploy
# Admin endpoints that act on one project: the read key says which project
ADMIN_WITH_PROJECT = {("GET", "/v1/export"), ("DELETE", "/v1/project-data")}
MISSING_ID = 2_000_000_000


def call(api: str, method: str, path: str, key: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
         body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    url = api.rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    h = {"Content-Type": "application/json", **(headers or {})}
    if key:
        h["X-API-Key"] = key
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw[:200]


class Report:
    def __init__(self) -> None:
        self.failures: List[str] = []
        self.checks = 0

    def expect(self, ok: bool, label: str) -> None:
        self.checks += 1
        if not ok:
            self.failures.append(label)
            print("FAIL " + label)


def schema_of(spec: Dict[str, Any], param: Dict[str, Any]) -> Dict[str, Any]:
    schema = param.get("schema", {})
    for option in schema.get("anyOf", []):
        if option.get("type") != "null":
            return {**option, **{k: v for k, v in schema.items() if k != "anyOf"}}
    return schema


def valid_params(spec: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    params = {}
    for p in op.get("parameters", []):
        if p["in"] != "query" or not p.get("required"):
            continue
        s = schema_of(spec, p)
        params[p["name"]] = s.get("default", s.get("enum", [SAMPLES.get(p["name"], "x")])[0])
    return params


def invalid_params(spec: Dict[str, Any], op: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One request per constrained query parameter, with a value outside its constraint."""
    cases = []
    base = valid_params(spec, op)
    for p in op.get("parameters", []):
        if p["in"] != "query":
            continue
        s = schema_of(spec, p)
        if "maximum" in s:
            cases.append({**base, p["name"]: s["maximum"] + 1})
        elif "minimum" in s:
            cases.append({**base, p["name"]: s["minimum"] - 1})
        elif "enum" in s:
            cases.append({**base, p["name"]: "not-a-valid-choice"})
        elif s.get("type") == "integer":
            cases.append({**base, p["name"]: "abc"})
    return cases


def fill_path(path: str, ident: int) -> str:
    return path.replace("{id}", str(ident)).replace("{token}", SAMPLES["token"])


def first_id(api: str, key: str, path: str) -> Optional[int]:
    status, data = call(api, "GET", path, key, {"limit": 1})
    items = data if isinstance(data, list) else (data or {}).get("items") or (data or {}).get("incidents") or (data or {}).get("issues") or []
    return int(items[0]["id"]) if status == 200 and items else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--ingest-key", required=True, help="project A ingest key")
    parser.add_argument("--read-key", required=True, help="project A read key")
    parser.add_argument("--other-read-key", required=True, help="project B read key (tenant isolation)")
    parser.add_argument("--admin-key", default=None)
    args = parser.parse_args()
    api, ingest, read, other = args.api, args.ingest_key, args.read_key, args.other_read_key
    r = Report()

    status, spec = call(api, "GET", "/openapi.json")
    if status != 200:
        print(f"Cannot read {api}/openapi.json (HTTP {status})")
        return 1
    r.expect(call(api, "GET", "/docs")[0] == 200, "GET /docs: interactive docs reachable")

    other_ids = {p: first_id(api, other, p) for p in ("/api/incidents", "/api/issues")}

    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            method = method.upper()
            name = f"{method} {path}"
            needs_admin = any(p["name"] == "X-Admin-Key" for p in op.get("parameters", []))
            has_body = "requestBody" in op
            target = fill_path(path, MISSING_ID)
            params = valid_params(spec, op)

            def req(key: Optional[str] = None, **kw: Any) -> Tuple[int, Any]:
                return call(api, method, target, key, kw.pop("params", params), kw.pop("body", b"{}" if has_body else None), **kw)

            if (method, path) in PUBLIC:
                status, _ = req()
                r.expect(status < 500, f"{name}: public endpoint returned {status}")
                continue

            # Auth: missing and unknown keys (admin endpoints answer 403 when no admin key is configured)
            allowed = (401, 403) if needs_admin and not args.admin_key else (401,)
            for label, key in (("no key", None), ("unknown key", "cw_read_" + "0" * 40)):
                status, data = req(key)
                r.expect(status in allowed, f"{name}: {label} -> expected {allowed}, got {status}")
                r.expect(isinstance(data, dict) and "detail" in data, f"{name}: {label} error body has no 'detail'")

            if needs_admin:
                status, _ = req(read)
                r.expect(status in (401, 403), f"{name}: read key on admin endpoint -> expected 401/403, got {status}")
                if args.admin_key and (method, path) in ADMIN_WITH_PROJECT:
                    status, _ = call(api, method, target, None, params, headers={"X-Admin-Key": args.admin_key})
                    r.expect(status == 401, f"{name}: admin key without a project key -> expected 401, got {status}")
                    if (method, path) not in CHANGES_DATA:
                        status, _ = call(api, method, target, read, params, headers={"X-Admin-Key": args.admin_key})
                        r.expect(status == 200, f"{name}: read key + admin key -> expected 200, got {status}")
                elif args.admin_key and (method, path) not in CHANGES_DATA:
                    status, _ = call(api, method, target, None, params, headers={"X-Admin-Key": args.admin_key})
                    r.expect(status == 200, f"{name}: admin key -> expected 200, got {status}")
                continue

            if (method, path) in ANY_KEY:
                for label, key in (("ingest", ingest), ("read", read)):
                    r.expect(req(key)[0] == 200, f"{name}: {label} key -> expected 200")
                continue

            wrong, right = (read, ingest) if (method, path) in INGEST_ONLY else (ingest, read)
            status, _ = req(wrong)
            r.expect(status == 403, f"{name}: wrong key type -> expected 403, got {status}")

            if has_body:
                status, data = req(right, body=b"{not json")
                r.expect(status == 422, f"{name}: malformed JSON -> expected 422, got {status}")

            if (method, path) in CHANGES_DATA:
                continue

            status, data = req(right)
            expected = (404,) if "{id}" in path else (200, 404) if (method, path) in NOT_FOUND_OK else (200,)
            r.expect(status in expected, f"{name}: valid request -> expected {expected}, got {status}: {str(data)[:120]}")

            for bad in invalid_params(spec, op):
                status, _ = req(right, params=bad)
                r.expect(status == 422, f"{name}: invalid params {bad} -> expected 422, got {status}")

            if "{id}" in path:
                collection = "/api/incidents" if "incidents" in path else "/api/issues"
                other_id = other_ids.get(collection)
                if other_id is not None:
                    status, _ = call(api, method, fill_path(path, other_id), read, params)
                    r.expect(status == 404, f"{name}: other project's id {other_id} -> expected 404, got {status}")

    print(f"\n{r.checks} checks, {len(r.failures)} failure(s)")
    if not all(other_ids.values()):
        print("Note: the other project has no incidents or issues, so some isolation checks were skipped.")
    return 1 if r.failures else 0


if __name__ == "__main__":
    sys.exit(main())
