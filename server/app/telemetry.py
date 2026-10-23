"""ChangeWatch Opt-In Telemetry & Privacy Allowlist Specification.

Specified in Master Prompt Section 6 (Milestone 9):
1. Telemetry is OFF by default (TELEMETRY_ENABLED=False).
2. Strict allowlist-only schema: random install ID, version, run mode, OS family,
   AI provider type, channel types, and bucketed counts of releases and incidents.
3. Extra attributes are strictly forbidden (extra="forbid").
4. Negative guarantees: NEVER collect or send code, diffs, repository names, file paths,
   emails, URLs, IP addresses, tokens, or customer data.
5. Preview screen and API endpoint display the exact payload before opting in.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings

logger = logging.getLogger("changewatch.telemetry")

APP_VERSION = "0.1.0"

# Set of strictly forbidden keywords in telemetry payloads
FORBIDDEN_KEYWORDS = {
    "code",
    "diff",
    "repo",
    "repository",
    "file",
    "path",
    "filename",
    "email",
    "url",
    "uri",
    "ip",
    "ip_address",
    "token",
    "key",
    "secret",
    "password",
    "stack",
    "trace",
    "error",
    "message",
    "customer",
    "tenant",
    "user",
}


class TelemetryPayload(BaseModel):
    """Strictly allowlisted telemetry payload.
    
    Any additional or unrecognized fields will cause validation to fail immediately.
    """
    model_config = ConfigDict(extra="forbid")

    install_id: str
    version: str
    run_mode: Literal["docker", "standalone", "kubernetes", "unknown"]
    os_family: Literal["darwin", "linux", "windows", "other"]
    ai_provider_type: Literal["rules", "fake", "ollama", "anthropic", "jev"]
    channel_types: List[str]
    release_count_bucket: Literal["0", "1-10", "11-50", "51-100", "100+"]
    incident_count_bucket: Literal["0", "1-10", "11-50", "51-100", "100+"]


def bucket_count(n: int) -> Literal["0", "1-10", "11-50", "51-100", "100+"]:
    """Map raw integers to coarse buckets for privacy preservation."""
    if n <= 0:
        return "0"
    elif n <= 10:
        return "1-10"
    elif n <= 50:
        return "11-50"
    elif n <= 100:
        return "51-100"
    else:
        return "100+"


def get_or_create_install_id(filepath: Optional[str] = None) -> str:
    """Retrieve or generate a random anonymous installation UUID."""
    path_str = filepath or settings.TELEMETRY_INSTALL_ID_PATH
    path = Path(path_str)
    try:
        if path.exists():
            content = path.read_text().strip()
            if content:
                return content
    except Exception as e:
        logger.debug("Could not read install ID file: %s", e)

    new_id = uuid.uuid4().hex
    try:
        path.write_text(new_id)
    except Exception as e:
        logger.debug("Could not persist install ID: %s", e)
    return new_id


def detect_os_family() -> Literal["darwin", "linux", "windows", "other"]:
    """Detect host OS family."""
    p = sys.platform.lower()
    if p.startswith("darwin"):
        return "darwin"
    elif p.startswith("linux"):
        return "linux"
    elif p.startswith("win"):
        return "windows"
    return "other"


def detect_run_mode() -> Literal["docker", "standalone", "kubernetes", "unknown"]:
    """Detect if running inside Docker container, Kubernetes, or standalone process."""
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    if Path("/.dockerenv").exists() or os.getenv("RUN_MODE") == "docker":
        return "docker"
    return "standalone"


def sanitize_channel_type(channel_type: str) -> str:
    """Map channel string to standard type name, omitting any private destination."""
    c = channel_type.lower()
    if "slack" in c:
        return "slack"
    if "discord" in c:
        return "discord"
    if "teams" in c:
        return "teams"
    if "email" in c:
        return "email"
    if "webhook" in c:
        return "webhook"
    return "other"


async def build_telemetry_payload(conn_or_pool: Optional[Any] = None) -> TelemetryPayload:
    """Construct the anonymous allowlisted telemetry payload."""
    install_id = get_or_create_install_id()
    os_family = detect_os_family()
    run_mode = detect_run_mode()

    # Determine configured AI provider
    raw_provider = getattr(settings, "DECISION_PROVIDER", "rules")
    if raw_provider not in ("rules", "fake", "ollama", "anthropic", "jev"):
        raw_provider = "rules"

    release_count = 0
    incident_count = 0
    channel_types: List[str] = []

    async def _query(conn):
        nonlocal release_count, incident_count, channel_types
        r_row = await conn.fetchrow("SELECT count(*) as cnt FROM deploys")
        if r_row:
            release_count = int(r_row["cnt"])
        i_row = await conn.fetchrow("SELECT count(*) as cnt FROM incidents")
        if i_row:
            incident_count = int(i_row["cnt"])
        c_rows = await conn.fetch("SELECT DISTINCT type FROM notification_channels")
        raw_channels = [r["type"] for r in c_rows if r.get("type")]
        channel_types = sorted(list({sanitize_channel_type(c) for c in raw_channels}))

    try:
        if conn_or_pool is not None:
            if hasattr(conn_or_pool, "acquire"):
                async with conn_or_pool.acquire() as conn:
                    await _query(conn)
            else:
                await _query(conn_or_pool)
        else:
            from app.db import get_connection
            async with get_connection() as conn:
                await _query(conn)
    except Exception as e:
        logger.debug("Could not gather database counts for telemetry: %s", e)

    return TelemetryPayload(
        install_id=install_id,
        version=APP_VERSION,
        run_mode=run_mode,
        os_family=os_family,
        ai_provider_type=raw_provider,
        channel_types=channel_types,
        release_count_bucket=bucket_count(release_count),
        incident_count_bucket=bucket_count(incident_count),
    )


def assert_allowlist_compliance(payload_dict: Dict[str, Any]) -> None:
    """Validate that payload adheres strictly to the allowlist with zero forbidden keys."""
    # 1. Schema check via strict Pydantic model
    TelemetryPayload.model_validate(payload_dict)

    # 2. Key inspection: no forbidden substring
    for key in payload_dict.keys():
        lower_key = key.lower()
        for forbidden in FORBIDDEN_KEYWORDS:
            if forbidden in lower_key:
                raise ValueError(f"Forbidden field '{key}' detected in telemetry payload!")


async def send_telemetry_report(
    conn_or_pool: Optional[Any] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """Send telemetry payload if and only if TELEMETRY_ENABLED is True.
    
    GUARANTEE: If TELEMETRY_ENABLED is False, zero network requests occur.
    """
    if not settings.TELEMETRY_ENABLED:
        logger.debug("Telemetry is disabled. Zero network activity.")
        return False

    payload = await build_telemetry_payload(conn_or_pool)
    payload_data = payload.model_dump()
    assert_allowlist_compliance(payload_data)

    target_url = f"{settings.TELEMETRY_COLLECTOR_URL.rstrip('/')}/collect"

    try:
        if client:
            resp = await client.post(target_url, json=payload_data, timeout=5.0)
            return resp.status_code == 200
        else:
            async with httpx.AsyncClient() as c:
                resp = await c.post(target_url, json=payload_data, timeout=5.0)
                return resp.status_code == 200
    except Exception as e:
        logger.warning("Failed to submit telemetry report: %s", e)
        return False
