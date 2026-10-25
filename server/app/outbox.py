import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple
import asyncpg
import httpx
from app.config import settings
from app.crypto import decrypt, decrypt_or_plain, encrypt_or_keep
from app.logging_filter import redact_delivery_error
from app.db import get_connection

logger = logging.getLogger("changewatch.outbox")

ENCRYPT_TIMEOUT_SEC = 5.0  # an alert waits at most this long for encryption, then goes in plain text


async def _encrypted_payload(project_id: int, payload: Dict[str, Any]) -> Tuple[str, Optional[bytes]]:
    """(payload column, payload_enc) for a new message.

    Messages can quote customer code (a risk review finding, the AI's suggested change), so only
    the ciphertext is stored. If encryption fails or hangs the message is stored in plain text:
    an alert is never held back by encryption.
    """
    text = json.dumps(payload)
    try:
        _, payload_enc = await asyncio.wait_for(encrypt_or_keep(project_id, "outbox", "payload", text), ENCRYPT_TIMEOUT_SEC)
    except Exception as exc:
        logger.warning(
            "Could not encrypt a notification for project %d, queued as plain text: %s",
            project_id,
            exc or type(exc).__name__,
        )
        return text, None
    return "{}", payload_enc


async def read_payload(item: Any) -> Dict[str, Any]:
    """The message of an outbox row (it needs project_id, payload and payload_enc): decrypted, or
    the plain payload of a row queued before encryption (or while it failed). Empty once the
    message was sent, skipped or permanently failed."""
    if item.get("payload_enc"):
        return json.loads(await decrypt(item["project_id"], "outbox", "payload", item["payload_enc"]))
    raw = item["payload"]
    return raw if isinstance(raw, dict) else json.loads(raw)


async def queue_notification(
    conn: asyncpg.Connection,
    project_id: int,
    dedupe_key: str,
    payload: Dict[str, Any],
    channel_id: Optional[int] = None,
    fan_out: bool = True,
) -> bool:
    """
    Queue an alert notification in the durable outbox within the caller's transaction.
    Returns True if newly queued, False if deduplicated.

    Without a channel_id the message goes to every enabled channel of the project
    (one outbox row each), or to the project's default Slack webhook if it has none.
    """
    if channel_id is None and fan_out:
        channel_rows = await conn.fetch(
            "SELECT id FROM notification_channels WHERE project_id = $1 AND enabled = true ORDER BY id",
            project_id,
        )
        channel_ids = [r["id"] for r in channel_rows] if isinstance(channel_rows, list) else []
        if channel_ids:
            queued = [
                await queue_notification(conn, project_id, f"{dedupe_key}:channel:{ch_id}", payload, channel_id=ch_id)
                for ch_id in channel_ids
            ]
            return any(queued)

    stored, payload_enc = await _encrypted_payload(project_id, payload)
    row = await conn.fetchrow(
        """
        INSERT INTO outbox (project_id, channel_id, dedupe_key, payload, payload_enc, next_attempt_at, status)
        VALUES ($1, $2, $3, $4::jsonb, $5, now(), 'pending')
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        project_id,
        channel_id,
        dedupe_key,
        stored,
        payload_enc,
    )
    if row:
        logger.info(
            "Notification queued in outbox: project=%d, dedupe_key=%s, outbox_id=%d",
            project_id,
            dedupe_key,
            row["id"],
        )
        return True
    else:
        logger.debug(
            "Notification deduplicated by key %s; not re-queued",
            dedupe_key,
        )
        return False


def annotate_delayed_message(payload: Dict[str, Any], delay_sec: float) -> Dict[str, Any]:
    """Annotate message if delivery was delayed by Slack / channel outages."""
    annotated = dict(payload)
    if delay_sec >= 60.0:
        delay_min = int(delay_sec // 60)
        annotation = f"[delayed by {delay_min} min (channel was unreachable)]"

        # If payload has text or title, prepend or append annotation
        if "text" in annotated:
            annotated["text"] = f"{annotation} {annotated['text']}"
        elif "summary" in annotated:
            annotated["summary"] = f"{annotation} {annotated['summary']}"
        else:
            annotated["_delivery_delay_note"] = annotation

    return annotated


def to_chat_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure a notification has the plain `text` every chat channel needs.

    Incident alerts carry Slack-formatted `text` and `blocks`; other notifications
    (AI replies, verdicts, silence warnings, budget pauses) carry `title` and `body`.
    """
    if payload.get("text"):
        return payload
    title = payload.get("title") or "ChangeWatch"
    body = payload.get("body") or payload.get("summary") or payload.get("_delivery_delay_note") or ""
    message = dict(payload)
    message["text"] = f"*{title}*\n{body}".strip()
    return message


async def _send_to_channel(conn: asyncpg.Connection, project_id: int, channel_id: int, payload: Dict[str, Any]) -> Optional[bool]:
    """Deliver through the project's configured channel (Slack, Discord, Teams, email or webhook).
    False when there was nothing to send to."""
    from app.notifiers.channels import get_notifier

    ch = await conn.fetchrow(
        "SELECT type, config, config_enc, enabled FROM notification_channels WHERE id = $1 AND project_id = $2",
        channel_id,
        project_id,
    )
    if not ch or not ch["enabled"]:
        logger.warning("Channel %d for project %d is missing or disabled; skipping delivery", channel_id, project_id)
        return False
    notifier = get_notifier(ch["type"])
    if notifier is None:
        raise ValueError(f"Unsupported channel type: {ch['type']}")
    raw_config = await decrypt_or_plain(project_id, "notification_channels", "config", ch.get("config_enc"), ch.get("config"))
    config = raw_config if isinstance(raw_config, dict) else json.loads(raw_config or "{}")
    await notifier.send(payload, config)


async def _default_send(project_id: int, payload: Dict[str, Any]) -> Optional[bool]:
    """Send payload to project's configured Slack webhook if present (False when there is none)."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT slack_webhook_url, slack_webhook_url_enc FROM projects WHERE id = $1",
            project_id,
        )
    url = (
        await decrypt_or_plain(project_id, "projects", "slack_webhook_url", row.get("slack_webhook_url_enc"), row.get("slack_webhook_url"))
        if row
        else None
    )
    if not url:
        logger.warning("Project %d has no channel or webhook URL configured; notification not delivered", project_id)
        return False

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()


async def process_outbox_item(
    conn: asyncpg.Connection,
    item: asyncpg.Record,
    sender_func: Optional[Callable[[int, Dict[str, Any]], Any]] = None,
) -> bool:
    """Process a single outbox row with retry backoff and expiration.

    Once a message is sent, skipped or permanently failed, its content is cleared (not kept)."""
    item_id = item["id"]
    project_id = item["project_id"]
    attempts = item["attempts"]
    created_at = item["created_at"]

    now = datetime.now(timezone.utc)
    delay_sec = (now - created_at).total_seconds()
    max_age_sec = settings.OUTBOX_MAX_AGE_HOURS * 3600.0

    send_fn = sender_func or _default_send
    channel_id = item["channel_id"] if "channel_id" in item.keys() else None

    try:
        annotated_payload = to_chat_message(annotate_delayed_message(await read_payload(item), delay_sec))
        if sender_func is None and channel_id is not None:
            res = await _send_to_channel(conn, project_id, channel_id, annotated_payload)
        else:
            res = send_fn(project_id, annotated_payload)
            if asyncio.iscoroutine(res):
                res = await res

        if res is False:
            # Nowhere to send it: recorded as skipped, never as delivered
            await conn.execute(
                """
                UPDATE outbox
                SET status = 'skipped', last_error = $2, payload = '{}'::jsonb, payload_enc = NULL
                WHERE id = $1
                """,
                item_id,
                "no channel configured" if channel_id is None else "the channel is turned off or was removed",
            )
            return False

        # Mark sent
        await conn.execute(
            """
            UPDATE outbox
            SET status = 'sent', last_error = NULL, sent_at = now(), payload = '{}'::jsonb, payload_enc = NULL
            WHERE id = $1
            """,
            item_id,
        )
        logger.info("Outbox message %d delivered successfully for project %d", item_id, project_id)
        return True

    except Exception as exc:
        new_attempts = attempts + 1
        # Errors quote the target URL; for webhooks that is the credential
        err_msg = redact_delivery_error(str(exc))[:500]
        logger.warning(
            "Outbox delivery failed for item %d (attempt %d): %s",
            item_id,
            new_attempts,
            err_msg,
        )

        if delay_sec >= max_age_sec or new_attempts >= 10:
            # Exceeded maximum age or attempt budget
            await conn.execute(
                """
                UPDATE outbox
                SET status = 'failed', attempts = $1, last_error = $2, payload = '{}'::jsonb, payload_enc = NULL
                WHERE id = $3
                """,
                new_attempts,
                f"Expired after {int(delay_sec // 60)}m: {err_msg}",
                item_id,
            )
            logger.error("Outbox item %d permanently failed", item_id)
            # Tell the operator directly: this alert never reached the team
            from app.system_alerts import send_fallback

            kind = (item["dedupe_key"] or "").split(":", 1)[0] or "notification"
            await send_fallback(
                f"⚠️ ChangeWatch could not deliver a {kind} alert for project {project_id} "
                f"after {new_attempts} attempts.\nLast error: {err_msg}"
            )
        else:
            # Exponential backoff: 10s, 20s, 40s, 80s ... capped at 1 hour
            backoff_sec = min(3600.0, (2 ** new_attempts) * 10.0)
            next_attempt_at = now + timedelta(seconds=backoff_sec)
            await conn.execute(
                """
                UPDATE outbox
                SET attempts = $1, next_attempt_at = $2, last_error = $3
                WHERE id = $4
                """,
                new_attempts,
                next_attempt_at,
                err_msg,
                item_id,
            )

        return False


OUTBOX_LANES = 4  # messages sent at the same time by one process
OUTBOX_BATCH_MAX_SEC = 15.0  # a batch stops claiming new messages after this long


def _channel_key(project_id: int, channel_id: Optional[int]) -> str:
    return f"{project_id}:{'*' if channel_id is None else channel_id}"


async def _claim_and_deliver(sender_func, busy: set) -> Any:
    """Claim one due message (of a channel no other lane is sending to), send it and record the
    result, all in its own transaction. None when nothing is due, "busy" when the claimed
    message's channel turned out to be in use by another lane (it is left for later)."""
    async with get_connection() as conn:
        async with conn.transaction():
            item = await conn.fetchrow(
                """
                SELECT id, project_id, channel_id, dedupe_key, payload, payload_enc, attempts, created_at
                FROM outbox
                WHERE status = 'pending' AND next_attempt_at <= now()
                  AND (project_id::text || ':' || COALESCE(channel_id::text, '*')) <> ALL($1::text[])
                ORDER BY next_attempt_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                list(busy),
            )
            if item is None:
                return None
            key = _channel_key(item["project_id"], item["channel_id"])
            if key in busy:
                return "busy"  # lanes claimed at the same moment; ending the transaction frees the row
            busy.add(key)
            try:
                return await process_outbox_item(conn, item, sender_func)
            finally:
                busy.discard(key)


async def deliver_outbox_batch(
    limit: int = 50,
    sender_func: Optional[Callable[[int, Dict[str, Any]], Any]] = None,
) -> int:
    """Deliver up to `limit` pending outbox messages.

    Rows are claimed with FOR UPDATE SKIP LOCKED, so the notifier and the detector
    (fallback sender) can run at the same time without ever sending a row twice. Each message
    is committed as sent right after its send, so a crash or a database drop re-sends at most
    the message in flight, not the whole batch. Up to OUTBOX_LANES messages go out at once,
    at most one per channel, so a channel that times out holds up only its own messages.
    """
    deadline = time.monotonic() + OUTBOX_BATCH_MAX_SEC
    busy: set = set()
    state = {"left": limit, "delivered": 0}

    async def lane() -> None:
        while state["left"] > 0 and time.monotonic() < deadline:
            state["left"] -= 1
            ok = await _claim_and_deliver(sender_func, busy)
            if ok is None or ok == "busy":
                state["left"] += 1
                if ok is None and not busy:
                    return  # nothing due and nothing in flight
                await asyncio.sleep(0.2)  # another lane is on a slow channel: keep serving the rest
                continue
            state["delivered"] += int(ok)

    results = await asyncio.gather(*(lane() for _ in range(max(1, min(OUTBOX_LANES, limit)))), return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException):
            raise res
    return state["delivered"]
