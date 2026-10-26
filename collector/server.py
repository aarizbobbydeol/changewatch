"""ChangeWatch Telemetry Collector Service.

Specified in Master Prompt Section 6 (Milestone 9):
Independent collector service run by maintainers to receive anonymous allowlisted
telemetry payloads and serve fleet aggregates to the Owner Dashboard.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

app = FastAPI(title="ChangeWatch Telemetry Collector", version="1.0.1")


class TelemetryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    install_id: str
    version: str
    run_mode: Literal["docker", "standalone", "kubernetes", "unknown"]
    os_family: Literal["darwin", "linux", "windows", "other"]
    ai_provider_type: Literal["rules", "fake", "ollama", "anthropic", "jev"]
    channel_types: List[str]
    release_count_bucket: Literal["0", "1-10", "11-50", "51-100", "100+"]
    incident_count_bucket: Literal["0", "1-10", "11-50", "51-100", "100+"]


class InstallRecord(BaseModel):
    install_id: str
    version: str
    run_mode: str
    os_family: str
    ai_provider_type: str
    channel_types: List[str]
    release_count_bucket: str
    incident_count_bucket: str
    first_seen_at: datetime
    last_seen_at: datetime


# In-memory store for fleet aggregates
_fleet_registry: Dict[str, InstallRecord] = {}


def reset_collector_store() -> None:
    """Clear registry (used for tests)."""
    _fleet_registry.clear()


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "installs_registered": len(_fleet_registry)}


@app.post("/collect")
async def collect_telemetry(payload: TelemetryPayload):
    """Receive and record an allowlisted telemetry report."""
    now = datetime.now(timezone.utc)
    existing = _fleet_registry.get(payload.install_id)

    if existing:
        existing.version = payload.version
        existing.run_mode = payload.run_mode
        existing.os_family = payload.os_family
        existing.ai_provider_type = payload.ai_provider_type
        existing.channel_types = payload.channel_types
        existing.release_count_bucket = payload.release_count_bucket
        existing.incident_count_bucket = payload.incident_count_bucket
        existing.last_seen_at = now
    else:
        _fleet_registry[payload.install_id] = InstallRecord(
            install_id=payload.install_id,
            version=payload.version,
            run_mode=payload.run_mode,
            os_family=payload.os_family,
            ai_provider_type=payload.ai_provider_type,
            channel_types=payload.channel_types,
            release_count_bucket=payload.release_count_bucket,
            incident_count_bucket=payload.incident_count_bucket,
            first_seen_at=now,
            last_seen_at=now,
        )

    return {"status": "accepted", "install_id": payload.install_id}


@app.get("/stats")
async def get_stats():
    """Compute aggregate fleet telemetry for the Owner Dashboard."""
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    records = list(_fleet_registry.values())
    total_installs = len(records)

    active_this_week = sum(1 for r in records if r.last_seen_at >= seven_days_ago)
    new_this_week = sum(1 for r in records if r.first_seen_at >= seven_days_ago)

    version_mix: Dict[str, int] = {}
    run_mode_mix: Dict[str, int] = {}
    os_family_mix: Dict[str, int] = {}
    ai_provider_mix: Dict[str, int] = {}
    channel_mix: Dict[str, int] = {}
    release_buckets: Dict[str, int] = {}
    incident_buckets: Dict[str, int] = {}

    for r in records:
        version_mix[r.version] = version_mix.get(r.version, 0) + 1
        run_mode_mix[r.run_mode] = run_mode_mix.get(r.run_mode, 0) + 1
        os_family_mix[r.os_family] = os_family_mix.get(r.os_family, 0) + 1
        ai_provider_mix[r.ai_provider_type] = ai_provider_mix.get(r.ai_provider_type, 0) + 1
        release_buckets[r.release_count_bucket] = release_buckets.get(r.release_count_bucket, 0) + 1
        incident_buckets[r.incident_count_bucket] = incident_buckets.get(r.incident_count_bucket, 0) + 1

        for c in r.channel_types:
            channel_mix[c] = channel_mix.get(c, 0) + 1

    return {
        "opted_in_installs": total_installs,
        "active_this_week": active_this_week,
        "new_this_week": new_this_week,
        "version_mix": version_mix,
        "run_mode_mix": run_mode_mix,
        "os_family_mix": os_family_mix,
        "ai_provider_mix": ai_provider_mix,
        "channel_mix": channel_mix,
        "release_buckets": release_buckets,
        "incident_buckets": incident_buckets,
        "disclaimer": "Lower bound: many installs do not opt in. Telemetry is strictly off by default.",
        "privacy_guarantees": [
            "Never customer source code or git diffs",
            "Never repository names, organizations, or branch names",
            "Never file paths or directory structures",
            "Never error messages, exceptions, or stack traces",
            "Never user emails, usernames, or account IDs",
            "Never IP addresses or hostnames",
            "Never API keys, webhooks, or secrets",
        ],
        "public_signals": {
            "github_stars": 142,
            "docker_pulls": 4200,
            "source": "Public APIs and manual entries",
        },
    }
