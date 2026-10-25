"""Outbox delivery (live Postgres): a crash re-sends at most the message in flight, a slow
channel holds up only its own messages, messages are stored encrypted and not kept once sent,
and a message with nowhere to go is not reported as delivered."""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

from app import outbox
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool, project_transaction
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


async def _row(outbox_id: int):
    async with get_connection() as conn:
        return await conn.fetchrow("SELECT * FROM outbox WHERE id = $1", outbox_id)


async def _deliver_one(outbox_id: int, sender=None) -> bool:
    """Deliver one message the way the notifier does, without touching other tests' rows."""
    async with get_connection() as conn:
        async with conn.transaction():
            item = await conn.fetchrow("SELECT * FROM outbox WHERE id = $1 FOR UPDATE", outbox_id)
            return await outbox.process_outbox_item(conn, item, sender)


async def _queue_message(pid: int, key: str, payload: dict, **kwargs) -> int:
    async with project_transaction(pid) as conn:
        assert await outbox.queue_notification(conn, pid, key, payload, **kwargs) is True
        return await conn.fetchval("SELECT id FROM outbox WHERE dedupe_key = $1", key)


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
async def test_live_messages_are_stored_encrypted_and_not_kept_once_sent(project):
    # A risk review line quotes the customer's code; the AI's suggested change does too
    text = "Risk review: MEDIUM: Removes a check: `if not user.is_admin:` (app/views.py)"
    oid = await _queue_message(project, f"test:{project}:review", {"text": text})
    async with get_connection() as conn:
        stored = await conn.fetchval("SELECT payload::text FROM outbox WHERE id = $1", oid)
    assert "is_admin" not in stored  # not readable in the table, or in a backup of it
    row = await _row(oid)
    assert row["payload_enc"] and (await outbox.read_payload(row))["text"] == text

    got = []

    async def sender(pid, payload):
        got.append(payload["text"])

    assert await _deliver_one(oid, sender) is True
    assert got == [text]
    row = await _row(oid)
    assert row["status"] == "sent" and row["sent_at"] is not None
    assert json.loads(row["payload"]) == {} and row["payload_enc"] is None


@pytest.mark.asyncio
async def test_live_message_queued_before_encryption_is_delivered_then_cleared(project):
    ids = await _queue(project, [(None, 5)])  # a plain payload, as stored before migration 031
    got = []

    async def sender(pid, payload):
        got.append(payload["text"])

    assert await _deliver_one(ids[0], sender) is True
    assert got == ["msg 0"]
    assert json.loads((await _row(ids[0]))["payload"]) == {}


@pytest.mark.asyncio
async def test_live_permanently_failed_message_is_not_kept(project):
    oid = await _queue_message(project, f"test:{project}:failing", {"text": "Possible regression in app/views.py"})
    async with get_connection() as conn:
        await conn.execute("UPDATE outbox SET attempts = 9 WHERE id = $1", oid)

    def failing(pid, payload):
        raise RuntimeError("503 Service Unavailable")

    with patch("app.system_alerts.send_fallback", new=AsyncMock(return_value=True)):
        assert await _deliver_one(oid, failing) is False
    row = await _row(oid)
    assert row["status"] == "failed"
    assert json.loads(row["payload"]) == {} and row["payload_enc"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["error", "hang"])
async def test_live_alert_is_queued_in_plain_text_when_encryption_fails(project, monkeypatch, caplog, problem):
    async def broken(*args, **kwargs):
        if problem == "hang":
            await asyncio.sleep(30)
        raise RuntimeError("data key unavailable")

    monkeypatch.setattr("app.outbox.encrypt_or_keep", broken)
    monkeypatch.setattr("app.outbox.ENCRYPT_TIMEOUT_SEC", 0.2)
    started = time.monotonic()
    oid = await _queue_message(project, f"test:{project}:{problem}", {"text": "Possible regression"})
    assert time.monotonic() - started < 5  # the alert is never held up by encryption
    row = await _row(oid)
    assert row["payload_enc"] is None and json.loads(row["payload"]) == {"text": "Possible regression"}
    assert "queued as plain text" in caplog.text
    got = []

    async def sender(pid, payload):
        got.append(payload["text"])

    assert await _deliver_one(oid, sender) is True and got == ["Possible regression"]


@pytest.mark.asyncio
async def test_live_message_with_nowhere_to_go_is_not_marked_sent(project):
    async with get_connection() as conn:
        off = await conn.fetchval(
            "INSERT INTO notification_channels (project_id, type, enabled) VALUES ($1, 'slack', false) RETURNING id", project)
    # No enabled channel and no Slack webhook; and a message for a channel that was turned off
    ids = [
        await _queue_message(project, f"test:{project}:default", {"text": "Possible regression"}),
        await _queue_message(project, f"test:{project}:off", {"text": "Possible regression"}, channel_id=off),
    ]
    for oid in ids:
        assert await _deliver_one(oid) is False
    rows = [await _row(oid) for oid in ids]
    assert [(r["status"], r["sent_at"], r["last_error"]) for r in rows] == [
        ("skipped", None, "no channel configured"),
        ("skipped", None, "the channel is turned off or was removed"),
    ]
    assert all(json.loads(r["payload"]) == {} and r["payload_enc"] is None for r in rows)


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
