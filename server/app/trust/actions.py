"""Incident Lifecycle Actions, Audit Events, Signed Links, and Slack Interactivity.
Specified in TRUST Addendum Stage 4.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
import asyncpg

from app.config import settings

logger = logging.getLogger("changewatch.trust.actions")

VALID_ACTION_TYPES = {
    "created",
    "alerted",
    "confirmed",
    "acked",
    "assigned",
    "resolved",
    "reopened",
    "escalated",
    "muted",
    "feedback",
    "undo_pr",  # a draft revert pull request was opened (app/undo_pr.py)
}


async def record_incident_event(
    conn: asyncpg.Connection,
    project_id: int,
    incident_id: int,
    event_type: str,
    actor: str = "system",
    detail: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
) -> int:
    """Insert an audit event into incident_events table."""
    at_dt = occurred_at or datetime.now(timezone.utc)
    detail_payload = detail or {}

    row = await conn.fetchrow(
        """
        INSERT INTO incident_events (
            incident_id, project_id, type, actor, detail, at
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        incident_id,
        project_id,
        event_type,
        actor,
        json.dumps(detail_payload),
        at_dt,
    )
    event_id = int(row["id"]) if row else 0
    logger.info("Recorded incident_event #%d: incident=%d type=%s actor=%s", event_id, incident_id, event_type, actor)
    return event_id


async def execute_incident_action(
    conn: asyncpg.Connection,
    project_id: int,
    incident_id: int,
    action: str,
    actor: str = "anonymous",
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute an action on an incident (ack, assign, resolve, reopen) and record audit event."""
    action = action.lower().strip()
    if action not in ("ack", "assign", "resolve", "reopen"):
        raise ValueError(f"Invalid incident action: '{action}'. Must be ack, assign, resolve, or reopen.")

    inc = await conn.fetchrow(
        "SELECT id, status, acked_at, assigned_to FROM incidents WHERE id = $1 AND project_id = $2",
        incident_id,
        project_id,
    )
    if not inc:
        raise ValueError(f"Incident {incident_id} not found for project {project_id}")

    now = datetime.now(timezone.utc)
    target_assignee = (detail or {}).get("assignee") or actor

    if action == "ack":
        await conn.execute(
            """
            UPDATE incidents
            SET acked_at = COALESCE(acked_at, $1),
                acked_by = COALESCE(acked_by, $2)
            WHERE id = $3 AND project_id = $4
            """,
            now,
            actor,
            incident_id,
            project_id,
        )
        await record_incident_event(
            conn, project_id, incident_id, "acked", actor=actor, detail={"acked_at": now.isoformat()}
        )
        return {"status": "ok", "action": "ack", "acked_by": actor, "acked_at": now.isoformat()}

    elif action == "assign":
        await conn.execute(
            """
            UPDATE incidents
            SET assigned_to = $1
            WHERE id = $2 AND project_id = $3
            """,
            target_assignee,
            incident_id,
            project_id,
        )
        await record_incident_event(
            conn, project_id, incident_id, "assigned", actor=actor, detail={"assigned_to": target_assignee}
        )
        return {"status": "ok", "action": "assign", "assigned_to": target_assignee}

    elif action == "resolve":
        await conn.execute(
            """
            UPDATE incidents
            SET status = 'resolved',
                resolved_at = $1
            WHERE id = $2 AND project_id = $3
            """,
            now,
            incident_id,
            project_id,
        )
        await record_incident_event(
            conn, project_id, incident_id, "resolved", actor=actor, detail={"resolved_at": now.isoformat()}
        )
        return {"status": "ok", "action": "resolve", "resolved_at": now.isoformat()}

    elif action == "reopen":
        await conn.execute(
            """
            UPDATE incidents
            SET status = 'confirmed',
                resolved_at = NULL,
                -- Auto-resolve waits for 30 quiet minutes from now, not from the last old error
                last_event_at = GREATEST(last_event_at, now())
            WHERE id = $1 AND project_id = $2
            """,
            incident_id,
            project_id,
        )
        await record_incident_event(
            conn, project_id, incident_id, "reopened", actor=actor, detail={"reopened_at": now.isoformat()}
        )
        return {"status": "ok", "action": "reopen", "incident_status": "confirmed"}

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# SIGNED ACTION LINKS (/a/<token>)
# ---------------------------------------------------------------------------

def _get_link_secret() -> bytes:
    """HMAC key for action links: LINK_SIGNING_SECRET, else a key derived from MASTER_KEY.

    The master key is never used directly (key separation), and there is no built-in
    fallback secret: anyone could forge links with a value published in the source code.
    """
    if settings.LINK_SIGNING_SECRET:
        return settings.LINK_SIGNING_SECRET.encode("utf-8")
    from app.crypto import get_master_key_bytes

    master = get_master_key_bytes()  # raises MasterKeyMissingError when unconfigured
    return hmac.new(master, b"changewatch.action-links.v1", hashlib.sha256).digest()


def generate_signed_action_token(
    incident_id: int,
    action: str,
    project_id: int,
    expires_in_sec: int = 86400,
) -> str:
    """Generate a tamper-proof HMAC-signed token for one-click action landing links."""
    expiry_ts = int(time.time()) + expires_in_sec
    payload = f"{incident_id}:{action}:{project_id}:{expiry_ts}"
    secret = _get_link_secret()
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def verify_signed_action_token(token: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """Verify HMAC signature and expiry for signed action token. Returns (is_valid, payload, error_reason)."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) != 5:
            return False, None, "Malformed token structure"

        inc_id_str, action, proj_id_str, exp_str, sig = parts
        incident_id = int(inc_id_str)
        project_id = int(proj_id_str)
        expiry_ts = int(exp_str)

        # Check signature
        payload = f"{incident_id}:{action}:{project_id}:{expiry_ts}"
        secret = _get_link_secret()
        expected_sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(sig, expected_sig):
            return False, None, "Invalid or tampered token signature"

        # Check expiry
        if time.time() > expiry_ts:
            return False, None, "Token has expired"

        return True, {
            "incident_id": incident_id,
            "action": action,
            "project_id": project_id,
            "expires_at": expiry_ts,
        }, "OK"
    except Exception as exc:
        return False, None, f"Token verification error: {exc}"


# ---------------------------------------------------------------------------
# SLACK INTERACTION WEBHOOK VERIFIER
# ---------------------------------------------------------------------------

def verify_slack_signature(
    body_bytes: bytes,
    timestamp: Optional[str],
    signature: Optional[str],
    signing_secret: Optional[str] = None,
) -> bool:
    """Verify Slack webhook signature (v0=...) with replay protection (5-minute window)."""
    secret = signing_secret or settings.SLACK_SIGNING_SECRET
    if not secret or not timestamp or not signature:
        return False

    try:
        ts = int(timestamp)
        # Replay prevention
        if abs(time.time() - ts) > 300:
            logger.warning("Slack signature verification rejected: timestamp %d drift > 300s", ts)
            return False

        sig_basestring = f"v0:{timestamp}:{body_bytes.decode('utf-8', errors='replace')}"
        computed = "v0=" + hmac.new(
            secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed, signature)
    except Exception as exc:
        logger.warning("Slack signature verification failed with error: %s", exc)
        return False
