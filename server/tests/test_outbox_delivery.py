"""Outbox delivery (live Postgres): a crash re-sends at most the message in flight, and a slow
channel holds up only its own messages."""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool
from app.notifiers.channels import DiscordNotifier
from app.outbox import deliver_outbox_batch


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def project():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")
    await init_db_pool()
    async with get_connection() as conn:
        pid = await conn.fetchval("INSERT INTO projects (name) VALUES ($1) RETURNING id", f"outbox-{uuid.uuid4().hex[:8]}")
    yield pid
    async with get_connection() as conn:
        await conn.execute("DELETE FROM outbox WHERE project_id = $1", pid)
        await conn.execute("DELETE FROM projects WHERE id = $1", pid)
    await close_db_pool()


async def _queue(pid: int, rows) -> list:
    """rows: [(channel_id, seconds_ago)]; returns ids in due order."""
    ids = []
    async with get_connection() as conn:
        for i, (channel_id, ago) in enumerate(rows):
            ids.append(await conn.fetchval(
                """INSERT INTO outbox (project_id, channel_id, dedupe_key, payload, next_attempt_at)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                pid, channel_id, f"test:{pid}:{i}", json.dumps({"text": f"msg {i}"}),
                datetime.now(timezone.utc) - timedelta(seconds=ago),
            ))
    return ids


async def _statuses(ids) -> list:
    async with get_connection() as conn:
        rows = await conn.fetch("SELECT id, status, attempts FROM outbox WHERE id = ANY($1)", ids)
    by_id = {r["id"]: (r["status"], r["attempts"]) for r in rows}
    return [by_id[i] for i in ids]


@pytest.mark.asyncio
async def test_live_crash_mid_batch_keeps_what_was_already_sent(project):
    ids = await _queue(project, [(None, 30), (None, 20), (None, 10)])
    mine = []

    async def sender(pid, payload):
        if pid != project:
            return  # leftovers of other tests
        mine.append(payload["text"])
        if len(mine) == 2:
            await asyncio.sleep(30)  # a slow send, during which the process is stopped

    task = asyncio.create_task(deliver_outbox_batch(sender_func=sender))
    for _ in range(100):
        if len(mine) == 2:
            break
        await asyncio.sleep(0.05)
    task.cancel()  # docker stop / OOM kill: every lane stops at once
    with pytest.raises(asyncio.CancelledError):
        await task

    # Only the message in flight is sent again; the one already delivered stays sent
    assert await _statuses(ids) == [("sent", 0), ("pending", 0), ("pending", 0)]


@pytest.mark.asyncio
async def test_live_slow_channel_does_not_hold_up_other_channels(project):
    # Channel 901 answers slowly (1 s per message, like a host that times out); 902 is healthy.
    # The slow channel's messages are due first.
    slow_ids = await _queue(project, [(901, 40), (901, 30), (901, 20)])
    async with get_connection() as conn:
        fast_id = await conn.fetchval(
            """INSERT INTO outbox (project_id, channel_id, dedupe_key, payload, next_attempt_at)
               VALUES ($1, 902, $2, '{"text": "fast"}', now() - interval '10 seconds') RETURNING id""",
            project, f"test:{project}:fast",
        )
    sent_at = {}
    start = time.monotonic()

    async def sender(pid, payload):
        if pid != project:
            return  # leftovers of other tests
        await asyncio.sleep(0 if payload["text"] == "fast" else 1.0)
        sent_at[payload["text"]] = time.monotonic() - start

    delivered = await deliver_outbox_batch(sender_func=sender)
    fast_ids = [fast_id]

    assert delivered >= 4
    assert sent_at["fast"] < 0.8  # not after the three slow messages (about 3 s)
    assert max(sent_at.values()) >= 2.9  # the slow channel's messages went one at a time
    assert await _statuses(slow_ids + fast_ids) == [("sent", 0)] * 4


@pytest.mark.asyncio
async def test_long_discord_messages_are_clipped_to_discord_limits(monkeypatch):
    posted = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted.update(json)

            class R:
                def raise_for_status(self):
                    pass
            return R()

    monkeypatch.setattr("app.notifiers.channels.httpx.AsyncClient", FakeClient)
    await DiscordNotifier().send(
        {"text": "x" * 2500, "details": {"title": "t" * 300, "where": "w" * 1500, "owners": "@a"}},
        {"webhook_url": "https://discord.example/api/webhooks/1/x"},
    )
    assert len(posted["content"]) == 2000 and posted["content"].endswith("…")
    assert len(posted["embeds"][0]["title"]) == 256
    assert len(posted["embeds"][0]["fields"][0]["value"]) == 1024
