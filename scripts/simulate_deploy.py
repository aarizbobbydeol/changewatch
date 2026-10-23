import argparse
import json
import os
import time
import urllib.request

API_KEY = os.getenv("CHANGEWATCH_API_KEY", "cw_test_key_123")
ENDPOINT = os.getenv("CHANGEWATCH_ENDPOINT", "http://localhost:8000").rstrip("/")


def post_json(path: str, data: dict, api_key: str):
    url = f"{ENDPOINT}{path}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def simulate(mode: str, api_key: str):
    sha = f"sim_{mode}_{int(time.time())}"
    print(f"\n--- Simulating {mode.upper()} Deploy (SHA: {sha}) ---")

    # 1. Create Deploy
    deploy_payload = {
        "commit_sha": sha,
        "author": f"Dev {mode.capitalize()}",
        "message": f"feat: simulation deploy ({mode})",
        "diff": (
            "diff --git a/services/checkout.py b/services/checkout.py\n"
            "--- a/services/checkout.py\n"
            "+++ b/services/checkout.py\n"
            "@@ -25,3 +25,4 @@\n"
            "+    # Potential breaking change\n"
            "+    raise KeyError('missing_config_token')\n"
        ),
        "environment": "prod",
    }
    dep_res = post_json("/v1/deploys", deploy_payload, api_key)
    print(f"Deploy created: ID {dep_res.get('deploy_id')}")

    now = time.time()

    if mode == "no_data":
        print("Mode is 'no_data': Sending no events or heartbeats.")
        print(f"Simulation complete. Release verdict will be 'no_data'.\n")
        return

    if mode == "safe":
        print("Mode is 'safe': Sending healthy request heartbeats without errors.")
        heartbeat = {
            "type": "heartbeat",
            "requests": 250,
            "errors": 0,
            "release": sha,
            "environment": "prod",
            "occurred_at": now,
        }
        post_json("/v1/events", {"events": [heartbeat]}, api_key)
        print("Heartbeat posted (250 requests, 0 errors). Verdict will evaluate to 'safe'.\n")
        return

    if mode == "bad":
        print("Mode is 'bad': Sending 25 regression errors (above 20 threshold) to trigger rollback verdict.")
        events = []
        for i in range(25):
            events.append({
                "type": "error",
                "exc_type": "KeyError",
                "message": f"missing_config_token during checkout iteration {i}",
                "stack_trace": (
                    "Traceback (most recent call last):\n"
                    "  File 'services/checkout.py', line 26, in process_payment\n"
                    "    raise KeyError('missing_config_token')\n"
                    "KeyError: missing_config_token"
                ),
                "request_path": "/api/checkout",
                "release": sha,
                "environment": "prod",
                "occurred_at": now + (i * 0.1),
                "extra": {"attempt": i},
            })

        # Also add a heartbeat showing failing error rate
        events.append({
            "type": "heartbeat",
            "requests": 30,
            "errors": 25,
            "release": sha,
            "environment": "prod",
            "occurred_at": now,
        })

        post_json("/v1/events", {"events": events}, api_key)
        print(f"Posted {len(events)} events (25 errors).")
        print("Release verdict will evaluate to 'rollback' with root-cause analysis.\n")


def main():
    parser = argparse.ArgumentParser(description="Simulate deploys for ChangeWatch testing")
    parser.add_argument(
        "--mode",
        choices=["bad", "safe", "no_data"],
        default="bad",
        help="Type of deploy to simulate (default: bad)",
    )
    parser.add_argument(
        "--api-key",
        default=API_KEY,
        help="ChangeWatch API Key",
    )
    args = parser.parse_args()
    simulate(args.mode, args.api_key)


if __name__ == "__main__":
    main()
