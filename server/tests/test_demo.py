"""The demo shop follows its schedule and sends data the detector can act on."""
import random

import pytest

from app.demo import BUGS, DemoShop, file_content, main


def run(shop, until, step=10.0, start=1_000_000.0):
    """Drive the shop with a fake clock; return [(t, kind, body)]."""
    calls, t = [], start
    while t <= start + until:
        calls += [(t - start, kind, body) for kind, body in shop.tick(t)]
        t += step
    return calls


def deploys(calls):
    return [(t, body) for t, kind, body in calls if kind == "deploy"]


def events(calls, **match):
    out = []
    for _, kind, body in calls:
        if kind == "events":
            out += [e for e in body["events"] if all(e.get(k) == v for k, v in match.items())]
    return out


@pytest.mark.parametrize("bug", BUGS)
def test_file_content_puts_the_changed_line_where_the_diff_says(bug):
    assert file_content(bug, bug.bad_code).split("\n")[bug.line - 1] == bug.bad_code


def test_schedule_healthy_then_bad_then_fix_then_regular_cycle():
    shop = DemoShop(rng=random.Random(1), first_bad_after_sec=300, fix_after_sec=600, deploy_every_sec=1800)
    calls = run(shop, until=300 + 600 + 1800 * 3 + 600)
    d = deploys(calls)
    times = [t for t, _ in d]
    assert times[:3] == [0, 300, 900]  # healthy at start, first bad after 5 min, fix 10 min later
    assert times[3:6] == [2700, 4500, 6300]  # then every 30 min: healthy, healthy, bad
    assert times[6] == 6900  # and its fix 10 min later
    first_bad, second_bad = d[1][1], d[5][1]
    assert first_bad["message"] == BUGS[0].deploy_message
    assert second_bad["message"] == BUGS[1].deploy_message
    assert d[2][1]["message"] == BUGS[0].fix_message
    assert first_bad["files"][0]["added_ranges"] == [[BUGS[0].line, BUGS[0].line]]


def test_errors_come_only_from_the_live_bug_and_point_at_the_changed_line():
    shop = DemoShop(rng=random.Random(2), first_bad_after_sec=300, fix_after_sec=600, deploy_every_sec=1800)
    calls = run(shop, until=1500)
    bad_sha = deploys(calls)[1][1]["commit_sha"]
    bug = BUGS[0]

    regressions = events(calls, exc_type=bug.exc_type)
    assert regressions, "the bad deploy must produce errors"
    assert all(e["release"] == bad_sha for e in regressions)
    assert all(f'"/srv/app/{bug.path}", line {bug.line}, in {bug.function}' in e["stack_trace"] for e in regressions)
    times = [t for t, kind, body in calls if kind == "events"
             and any(e.get("exc_type") == bug.exc_type for e in body["events"])]
    assert min(times) > 300 and max(times) < 900  # only between the bad deploy and its fix


def test_heartbeats_count_requests_and_errors_sent_since_the_last_one():
    shop = DemoShop(rng=random.Random(3), first_bad_after_sec=300, fix_after_sec=600, deploy_every_sec=1800)
    calls = run(shop, until=1200)
    errors_since = 0
    heartbeats = 0
    for _, kind, body in calls:
        if kind == "deploy":
            continue
        for e in body["events"]:
            if e["type"] == "error":
                errors_since += 1
            elif e["type"] == "heartbeat":
                heartbeats += 1
                assert e["requests"] > 0
                assert e["errors"] == errors_since
                errors_since = 0
    assert heartbeats >= 18
    assert all(len(body["events"]) <= 100 for _, kind, body in calls if kind == "events")
    ids = [e["event_id"] for e in events(calls)]
    assert len(ids) == len(set(ids))


def test_once_mode_stops_after_the_fix():
    shop = DemoShop(rng=random.Random(4), first_bad_after_sec=60, fix_after_sec=120, deploy_every_sec=1800, once=True)
    calls = run(shop, until=3600)
    assert len(deploys(calls)) == 3
    assert max(t for t, _, _ in calls) <= 60 + 120 + 120


def test_main_refuses_to_start_without_an_ingest_key(monkeypatch):
    monkeypatch.delenv("DEMO_INGEST_KEY", raising=False)
    assert main(["--api", "http://127.0.0.1:9"]) == 2


def test_history_gives_a_baseline_of_known_errors_and_heartbeats():
    shop = DemoShop(rng=random.Random(5))
    now = 2_000_000.0
    calls = shop.history(now, 30)
    evs = [e for _, body in calls for e in body["events"]]
    heartbeats = [e for e in evs if e["type"] == "heartbeat"]
    assert len(heartbeats) == 30
    assert all(now - 30 * 60 <= e["occurred_at"] < now for e in evs)
    assert {e.get("exc_type") for e in evs if e["type"] == "error"} == {"TimeoutError"}  # no bug before deploys
    assert sum(h["errors"] for h in heartbeats) == sum(1 for e in evs if e["type"] == "error")
    assert all(len(body["events"]) <= 100 for _, body in calls)
    assert shop.tick(now)[0][0] == "deploy"  # history does not change the schedule
