"""End-to-end data pipeline (ETL/ELT) integrity check against a running ChangeWatch.

Sends a known batch through the public ingest API, then checks every stage in Postgres:
  extract   accepted == sent (nothing dropped below the backpressure limit)
  transform one issue per fingerprint; storm control keeps at most STORE_FULL_PER_MIN full rows
  load      issue.event_count == events accepted; stack traces stored encrypted only
  ELT       sum(event_rollups.count) == issue.event_count; heartbeat requests land in traffic
  exactly-once  re-sending the same batch (an SDK retry) changes nothing
  export    GET /v1/export matches the database, stack traces decrypted (with --read-key and --admin-key)

Usage:
  DATABASE_URL=postgresql://... python scripts/verify_pipeline.py \\
      --api http://localhost:8000 --ingest-key cw_ingest_... [--read-key cw_read_... --admin-key ...] [--events 30]
Exit code 0 when every check passes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
import uuid

import asyncpg


def post_events(api: str, key: str, events: list) -> dict:
    req = urllib.request.Request(
        f"{api.rstrip('/')}/v1/events",
        data=json.dumps({"events": events}).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_json(api: str, path: str, key: str, admin_key: str = "") -> dict:
    headers = {"X-API-Key": key, **({"X-Admin-Key": admin_key} if admin_key else {})}
    req = urllib.request.Request(f"{api.rstrip('/')}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def build_batch(run_id: str, n_errors: int) -> list:
    """n_errors events of one new fingerprint plus one heartbeat, all with fixed event_ids."""
    now = time.time()
    trace = (
        "Traceback (most recent call last):\n"
        f'  File "/srv/app/app/pipeline_check.py", line 7, in check_{run_id}\n'
        "    raise ValueError('pipeline check')\n"
        "ValueError: pipeline check\n"
    )
    events = [
        {
            "event_id": f"{run_id}-err-{i}",
            "type": "error",
            "exc_type": "ValueError",
            "message": "pipeline check",
            "stack_trace": trace,
            "request_path": "/pipeline-check",
            "release": f"pipeline-check-{run_id}",
            "occurred_at": now - 1 + i * 0.001,
        }
        for i in range(n_errors)
    ]
    events.append(
        {"event_id": f"{run_id}-hb", "type": "heartbeat", "requests": 100, "errors": n_errors,
         "release": f"pipeline-check-{run_id}", "occurred_at": now}
    )
    return events


async def snapshot(conn: asyncpg.Connection, run_id: str) -> dict:
    issue = await conn.fetchrow(
        "SELECT id, event_count FROM issues WHERE title = 'ValueError: pipeline check' "
        "AND id IN (SELECT issue_id FROM events WHERE event_id LIKE $1)",
        f"{run_id}-err-%",
    )
    if not issue:
        return {}
    return {
        "issue_id": issue["id"],
        "event_count": issue["event_count"],
        "full_rows": await conn.fetchval("SELECT count(*) FROM events WHERE event_id LIKE $1", f"{run_id}-err-%"),
        "plaintext_traces": await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_id LIKE $1 AND stack_trace IS NOT NULL", f"{run_id}-err-%"
        ),
        "rollup_sum": await conn.fetchval(
            "SELECT coalesce(sum(count), 0) FROM event_rollups WHERE issue_id = $1", issue["id"]
        ),
    }


async def wait_for(conn, run_id: str, expected_count: int, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    snap: dict = {}
    while time.monotonic() < deadline:
        snap = await snapshot(conn, run_id)
        if snap and snap["event_count"] >= expected_count:
            return snap
        await asyncio.sleep(0.5)
    return snap


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.getenv("CHANGEWATCH_API", "http://localhost:8000"))
    parser.add_argument("--ingest-key", default=os.getenv("CHANGEWATCH_INGEST_KEY"), required=os.getenv("CHANGEWATCH_INGEST_KEY") is None)
    parser.add_argument("--read-key", default=os.getenv("CHANGEWATCH_READ_KEY"),
                        help="with --admin-key, also check GET /v1/export")
    parser.add_argument("--admin-key", default=os.getenv("CHANGEWATCH_ADMIN_KEY"), help="ADMIN_API_KEY of the server")
    parser.add_argument("--events", type=int, default=30, help="error events to send (default 30)")
    parser.add_argument("--store-full-per-min", type=int, default=int(os.getenv("STORE_FULL_PER_MIN", "20")))
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:10]
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    failures = 0

    def check(ok: bool, label: str):
        nonlocal failures
        print(("PASS " if ok else "FAIL ") + label)
        failures += 0 if ok else 1

    try:
        traffic_before = await conn.fetchval("SELECT coalesce(sum(requests), 0) FROM traffic")
        batch = build_batch(run_id, args.events)

        resp = post_events(args.api, args.ingest_key, batch)
        check(resp.get("accepted") == len(batch) and resp.get("dropped") == 0,
              f"extract: API accepted {resp.get('accepted')}/{len(batch)}, dropped {resp.get('dropped')}")

        snap = await wait_for(conn, run_id, args.events)
        check(bool(snap), "transform: events grouped into one issue")
        if not snap:
            return 1
        check(snap["event_count"] == args.events, f"load: issue.event_count {snap['event_count']} == {args.events} sent")
        expected_rows = min(args.events, args.store_full_per_min)
        check(snap["full_rows"] == expected_rows,
              f"storm control: {snap['full_rows']} full rows stored (cap {args.store_full_per_min}/min), counts stay exact")
        check(snap["plaintext_traces"] == 0, "load: stack traces stored encrypted only")
        check(snap["rollup_sum"] == snap["event_count"],
              f"ELT: rollups sum {snap['rollup_sum']} == issue.event_count {snap['event_count']}")
        await asyncio.sleep(1.5)
        traffic_after = await conn.fetchval("SELECT coalesce(sum(requests), 0) FROM traffic")
        check(traffic_after - traffic_before == 100, f"ELT: heartbeat added {traffic_after - traffic_before} requests to traffic (sent 100)")

        # Exactly-once: an SDK retry re-sends identical event_ids
        post_events(args.api, args.ingest_key, batch)
        await asyncio.sleep(5)
        again = await snapshot(conn, run_id)
        traffic_retry = await conn.fetchval("SELECT coalesce(sum(requests), 0) FROM traffic")
        check(again["full_rows"] == snap["full_rows"], f"exactly-once: full rows unchanged after re-send ({again['full_rows']})")
        check(again["event_count"] == snap["event_count"], f"exactly-once: issue.event_count unchanged after re-send ({again['event_count']})")
        check(again["rollup_sum"] == snap["rollup_sum"], f"exactly-once: rollups unchanged after re-send ({again['rollup_sum']})")
        check(traffic_retry == traffic_after, f"exactly-once: traffic unchanged after re-send (+{traffic_retry - traffic_after})")

        if args.read_key and args.admin_key:
            export = get_json(args.api, "/v1/export", args.read_key, args.admin_key)
            issue = next((i for i in export["issues"] if i["id"] == snap["issue_id"]), None)
            check(bool(issue) and issue["event_count"] == again["event_count"],
                  f"export: issue event_count {issue and issue['event_count']} == database {again['event_count']}")
            exported = [e for e in export["events"] if (e["event_id"] or "").startswith(f"{run_id}-err-")]
            check(len(exported) == again["full_rows"] and all("pipeline check" in (e["stack_trace"] or "") for e in exported),
                  f"export: {len(exported)} stored events exported with decrypted stack traces")
    finally:
        await conn.close()

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
