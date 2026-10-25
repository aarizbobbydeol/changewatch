"""End-to-end check of a running ChangeWatch, from the customer's side (standard library only).

What it does, in order, with an assertion at each step:
  1. waits for the API, the dashboard and a webhook receiver (stand-in for Slack)
  2. creates two projects with the CLI (A gets alerts at the receiver; B is the other tenant)
  3. sends 20 minutes of normal traffic for A (the baseline)
  4. deploys a real git change through the GitHub Action script (action/notify.py)
  5. sends errors from the changed line, tagged with the commit, as the SDK would
  6. expects: the instant alert (file:line, "changed in this release", owner, author), the AI
     investigation reply, and a rollback/watch verdict at the receiver
  7. checks the read API, tenant isolation, the dashboard and its API proxy, export (admin key),
     and that re-sending the same events changes nothing

Usage (Docker Compose, with docker-compose.e2e.yml providing the receiver):
  python scripts/e2e_check.py --admin-key "$ADMIN_API_KEY" --keys-out /tmp/e2e_keys.json
Exit code 0 when every check passes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list = []


def check(ok: bool, label: str) -> bool:
    print(("PASS " if ok else "FAIL ") + label, flush=True)
    if not ok:
        FAILURES.append(label)
    return ok


def http(method: str, url: str, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and raw[:1] in b"[{" else raw)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def wait_for(label: str, fn, timeout: float, every: float = 2.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            check(True, label)
            return last
        time.sleep(every)
    check(False, f"{label} (gave up after {timeout:.0f} s)")
    return None


def create_project(cli: list, name: str, webhook: str) -> dict:
    out = subprocess.run(cli + ["create-project", name, "--slack-webhook", webhook],
                         capture_output=True, text=True, timeout=120)
    text = out.stdout + out.stderr
    ingest = re.search(r"Ingest Key:\s+(cw_ingest_\S+)", text)
    read = re.search(r"Read Key:\s+(cw_read_\S+)", text)
    pid = re.search(r"Project ID:\s+(\d+)", text)
    if not (ingest and read and pid):
        print(text[-1500:])
        return {}
    return {"id": int(pid.group(1)), "ingest": ingest.group(1), "read": read.group(1)}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path) -> tuple:
    """A small app repository: CODEOWNERS, then a commit that changes app/billing.py line 42."""
    repo = root / "shop"
    (repo / "app").mkdir(parents=True)
    (repo / ".github").mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "dev@acme.example")
    git(repo, "config", "user.name", "Dev")
    lines = ['"""Billing."""', ""] + [f"# line {i}" for i in range(3, 40)] + [
        "def total_cents(order):", '    """Total in cents."""', "    return sum(i.price for i in order.items)", ""]
    (repo / "app" / "billing.py").write_text("\n".join(lines))
    (repo / ".github" / "CODEOWNERS").write_text("* @acme/core\napp/billing.py @acme/payments\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Initial")
    first = git(repo, "rev-parse", "HEAD")
    lines[41] = "    return sum(i.price for i in order.items) / order.discount"
    (repo / "app" / "billing.py").write_text("\n".join(lines))
    git(repo, "commit", "-qam", "Apply discount to totals")
    return repo, first, git(repo, "rev-parse", "HEAD")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--dashboard", default="http://localhost:3000")
    p.add_argument("--receiver", default="http://localhost:8099", help="where this script reads received alerts")
    p.add_argument("--hook-base", default="http://receiver:8099", help="receiver address as the server sees it")
    p.add_argument("--cli", default="docker compose exec -T api python -m app.cli")
    p.add_argument("--admin-key", default=os.getenv("ADMIN_API_KEY", ""))
    p.add_argument("--keys-out", default="", help="write the created projects' keys here (for later steps)")
    p.add_argument("--verdict-timeout", type=float, default=300)
    args = p.parse_args()
    api, run = args.api.rstrip("/"), uuid.uuid4().hex[:8]

    # 1. Everything is up
    wait_for("API healthy", lambda: http("GET", f"{api}/healthz")[0] == 200, 180)
    wait_for("dashboard serving", lambda: http("GET", args.dashboard)[0] == 200, 180)
    wait_for("webhook receiver up", lambda: http("GET", f"{args.receiver}/messages")[0] == 200, 60)

    # 2. Two projects
    cli = shlex.split(args.cli)
    a = create_project(cli, f"e2e-shop-{run}", f"{args.hook_base}/hook/{run}")
    b = create_project(cli, f"e2e-other-{run}", f"{args.hook_base}/other/{run}")
    if not check(bool(a and b), "two projects created with the CLI"):
        return 1
    if args.keys_out:
        Path(args.keys_out).write_text(json.dumps({"a": a, "b": b}))
    ingest = {"X-API-Key": a["ingest"]}
    read = {"X-API-Key": a["read"]}

    def messages(prefix: str) -> list:
        status, data = http("GET", f"{args.receiver}/messages")
        return [m["body"] for m in (data if status == 200 else []) if m["path"].startswith(prefix)]

    def text_of(body: dict) -> str:
        return str(body.get("text") or body.get("body") or body.get("content") or body)

    # The team's runbook for this kind of error, added with the read key like other settings
    status, _ = http("POST", f"{api}/api/runbooks", {
        "title": "Billing totals fail", "steps": "1. Check discounts\n2. Roll back if checkouts fail",
        "match_exception": "ZeroDivisionError", "match_path": "app/billing.py",
        "url": "https://wiki.acme.example/runbooks/billing"}, read)
    check(status == 201, "runbook added")

    # 3. Baseline: 20 minutes of normal traffic before the deploy
    now = time.time()
    beats = [{"event_id": f"{run}-hb-{m}", "type": "heartbeat", "requests": 300, "errors": 0,
              "environment": "prod", "occurred_at": now - m * 60} for m in range(20, 0, -1)]
    check(http("POST", f"{api}/v1/events", {"events": beats}, ingest)[0] == 200, "baseline traffic accepted")

    # 4. Deploy through the real GitHub Action script
    with tempfile.TemporaryDirectory() as tmp:
        repo, first, sha = make_repo(Path(tmp))
        env = {**os.environ, "CW_URL": api, "CW_KEY": a["ingest"], "CW_ENV": "prod", "CW_MODE": "full",
               "CW_EXCLUDE": "", "CW_SHA": sha, "CW_BEFORE": first, "GITHUB_REPOSITORY": "acme/shop"}
        out = subprocess.run([sys.executable, str(REPO / "action" / "notify.py")], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=60)
    check("registered" in out.stdout and "1 changed files" in out.stdout and "CODEOWNERS sent" in out.stdout,
          f"GitHub Action registered the deploy ({out.stdout.strip()[:120]})")
    deployed_at = time.time()

    # 5. Errors from the changed line, as the SDK sends them
    trace = ('Traceback (most recent call last):\n  File "/srv/app/api/orders.py", line 18, in checkout\n'
             '    total = total_cents(order)\n  File "/srv/app/app/billing.py", line 42, in total_cents\n'
             '    return sum(i.price for i in order.items) / order.discount\nZeroDivisionError: division by zero\n')

    def errors(n: int, tag: str) -> list:
        t = time.time()
        # As the SDK sends them with capture_logs: the app's last log lines before the error
        logs = [{"ts": t - 2, "level": "WARNING", "logger": "shop.billing", "message": "order has no discount"}]
        return [{"event_id": f"{run}-{tag}-{i}", "type": "error", "exc_type": "ZeroDivisionError",
                 "message": "division by zero", "stack_trace": trace, "request_path": "/checkout",
                 "release": sha, "environment": "prod", "occurred_at": t - 1 + i * 0.01, "logs": logs}
                for i in range(n)]

    first_batch = errors(15, "e1")
    for batch in (first_batch, errors(15, "e2")):
        check(http("POST", f"{api}/v1/events", {"events": batch}, ingest)[0] == 200, "errors accepted")
        time.sleep(2)
    hb = [{"event_id": f"{run}-hb-now", "type": "heartbeat", "requests": 120, "errors": 30, "release": sha,
           "environment": "prod", "occurred_at": time.time()}]
    http("POST", f"{api}/v1/events", {"events": hb}, ingest)

    # 6. What the team sees
    alert = wait_for("instant alert delivered", lambda: next(
        (t for t in map(text_of, messages(f"/hook/{run}")) if "regression" in t.lower() and "billing" in t), None), 90)
    if alert:
        check("app/billing.py:42" in alert, "alert names the file and line")
        check("this line was changed in this release" in alert, "alert says the line changed in this release")
        check("@acme/payments" in alert, "alert names the owner from CODEOWNERS")
        check("dev@acme.example" in alert, "alert names the commit author")
        check("Runbook: Billing totals fail (https://wiki.acme.example/runbooks/billing)" in alert, "alert links the runbook")
    wait_for("AI investigation reply delivered", lambda: next(
        (t for t in map(text_of, messages(f"/hook/{run}")) if "nvestigation" in t), None), 120)
    verdict = wait_for("rollback or watch verdict delivered", lambda: next(
        (t for t in map(text_of, messages(f"/hook/{run}")) if "verdict" in t.lower() and sha[:8] in t
         and ("ROLLBACK" in t or "WATCH" in t)), None), args.verdict_timeout, every=5)
    print(f"  verdict after {time.time() - deployed_at:.0f} s: {(verdict or '')[:80]}")
    if verdict:
        check("Risk review: MEDIUM: Divides by a value that may be zero (app/billing.py:42)" in verdict,
              "verdict alert carries the deploy's risk review")
        if "ROLLBACK" in verdict:
            check(f"Undo: `git revert --no-edit {sha}`" in verdict, "ROLLBACK verdict says how to undo the deploy")

    # 7. Read API, isolation, dashboard, export, exactly-once
    status, incidents = http("GET", f"{api}/api/incidents", headers=read)
    mine = [i for i in (incidents if isinstance(incidents, list) else incidents.get("items", []) if isinstance(incidents, dict) else [])
            if (i.get("where_file") == "app/billing.py")]
    check(status == 200 and bool(mine), "read API lists the incident")
    if mine:
        status, detail = http("GET", f"{api}/api/incidents/{mine[0]['id']}", headers=read)
        logs = detail.get("logs_before_error", []) if isinstance(detail, dict) else []
        check(any(line.get("message") == "order has no discount" for line in logs),
              "incident shows the app's log lines before the error")
        timeline = detail.get("timeline", []) if isinstance(detail, dict) else []
        check(any(t.get("event") == "alert_sent" for t in timeline), "incident timeline shows the alert delivery")
        runbooks = detail.get("runbooks", []) if isinstance(detail, dict) else []
        check([r.get("title") for r in runbooks] == ["Billing totals fail"], "incident shows the matching runbook")
        commands = [c.get("command") for c in ((detail.get("fix_suggestions") or {}).get("commands") or [])]
        check(f"git restore --source={sha}^ -- app/billing.py" in commands, "incident suggests undoing only the failing file")
        status, review = http("GET", f"{api}/api/deploys/{mine[0]['deploy_id']}/review", headers=read)
        check(status == 200 and isinstance(review, dict) and review.get("risk") == "medium"
              and any(f.get("file") == "app/billing.py" and f.get("line") == 42 for f in review.get("findings", [])),
              "risk review of the deploy flags the changed line")
    check(http("GET", f"{api}/api/incidents/99999999999", headers=read)[0] == 422, "an out-of-range id is a 422, not a 500")
    status, v = http("GET", f"{api}/v1/verdict?sha={sha}&environment=prod", headers=read)
    check(status == 200 and isinstance(v, dict) and v.get("verdict") in ("rollback", "watch"),
          f"verdict API answers for the commit ({v.get('verdict') if isinstance(v, dict) else status})")
    status, other = http("GET", f"{api}/api/incidents", headers={"X-API-Key": b["read"]})
    other_list = other if isinstance(other, list) else other.get("items", []) if isinstance(other, dict) else []
    check(status == 200 and not any(i.get("where_file") == "app/billing.py" for i in other_list),
          "the other project cannot see this incident")
    check(http("GET", f"{api}/api/incidents", headers=ingest)[0] == 403, "ingest key cannot read")

    # The MCP server (what an AI assistant would use) sees the same incident and its postmortem
    calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25",
              "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_incidents", "arguments": {"limit": 5}}}]
    if mine:
        calls.append({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "get_postmortem", "arguments": {"incident_id": mine[0]["id"]}}})
    mcp = subprocess.run([sys.executable, "-I", str(REPO / "mcp-server" / "changewatch_mcp.py")],
                         input="\n".join(json.dumps(c) for c in calls) + "\n", capture_output=True, text=True, timeout=60,
                         env={**os.environ, "CHANGEWATCH_URL": api, "CHANGEWATCH_READ_KEY": a["read"]})
    answers = {r.get("id"): r for r in (json.loads(x) for x in mcp.stdout.splitlines() if x.strip())}
    listed = answers.get(2, {}).get("result", {}).get("content", [{}])[0].get("text", "")
    check("app/billing.py" in listed, "MCP server lists the incident")
    draft = answers.get(3, {}).get("result", {}).get("content", [{}])[0].get("text", "")
    check(draft.startswith("# Postmortem (draft)") and "## Timeline (UTC)" in draft, "MCP server returns the postmortem draft")
    for page in ("/", "/incidents", "/analytics", "/usage", "/settings", "/trust"):
        check(http("GET", f"{args.dashboard}{page}")[0] == 200, f"dashboard page {page} loads")
    status, proxied = http("GET", f"{args.dashboard}/api/incidents", headers=read)
    check(status == 200, "dashboard proxies the API")

    def billing_count():
        if not args.admin_key:
            return None
        status, data = http("GET", f"{api}/v1/export", headers={**read, "X-Admin-Key": args.admin_key}, timeout=60)
        if status != 200:
            return None
        return sum(i["event_count"] for i in data["issues"] if "ZeroDivisionError" in i["title"])

    if args.admin_key:
        before = billing_count()
        check(before == 30, f"export (admin key) counts all 30 errors ({before})")
        http("POST", f"{api}/v1/events", {"events": first_batch}, ingest)  # an SDK retry
        time.sleep(6)
        check(billing_count() == before, "re-sent events are not counted twice")

    print(f"\n{len(FAILURES)} failure(s)")
    for f in FAILURES:
        print("  - " + f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
