"""Time to first alert is set by the first alert; the later "confirmed" alert must not overwrite it."""
import time
import uuid

import asyncpg
import pytest

from app.config import settings
from app.db import close_db_pool, init_db_pool, project_transaction
from app.notifiers.incident_alert import dispatch_incident_alert


def test_dashboard_link_follows_public_hostname(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_URL", "")
    monkeypatch.setattr(settings, "PUBLIC_HOSTNAME", "localhost")
    assert settings.dashboard_link("/incidents/7") == "http://localhost:3000/incidents/7"
    monkeypatch.setattr(settings, "PUBLIC_HOSTNAME", "changewatch.example.com")
    assert settings.dashboard_link("/incidents/7") == "https://changewatch.example.com/incidents/7"
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://cw.internal:8443/")
    assert settings.dashboard_link("/usage") == "https://cw.internal:8443/usage"


async def _db_up() -> bool:
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL, timeout=1.0)
        await conn.close()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_live_confirmed_alert_keeps_the_first_alert_time():
    if not await _db_up():
        pytest.skip("Live PostgreSQL not running. Skipping live integration test.")

    await init_db_pool()
    try:
        run = uuid.uuid4().hex[:10]
        async with project_transaction(1) as conn:
            deploy = await conn.fetchval(
                "INSERT INTO deploys (project_id, commit_sha, environment) VALUES (1, $1, 'prod') RETURNING id", f"fa{run}"
            )
            issue = await conn.fetchval(
                "INSERT INTO issues (project_id, fingerprint, title, first_seen, last_seen, status, event_count) "
                "VALUES (1, $1, 'ValueError: first alert', now(), now(), 'open', 3) RETURNING id", f"fp-fa-{run}"
            )
            incident = await conn.fetchval(
                "INSERT INTO incidents (project_id, deploy_id, issue_id, status) VALUES (1, $1, $2, 'possible') RETURNING id",
                deploy, issue,
            )
            args = dict(commit_sha=f"fa{run}", author="dev@example.com", message="m", error_title="ValueError: first alert",
                        event_count=3, where_file="app/x.py", where_line=1, where_function="f", in_diff=True, raw_owners=[])
            first_event_at = time.time() - 2.0
            await dispatch_incident_alert(conn, 1, incident, "possible", first_event_at=first_event_at, **args)
            first = await conn.fetchrow("SELECT alerted_at, first_alert_ms FROM incidents WHERE id = $1", incident)

            # 35 s later the incident is confirmed and alerted again
            await dispatch_incident_alert(conn, 1, incident, "confirmed", first_event_at=first_event_at - 35, **args)
            again = await conn.fetchrow("SELECT alerted_at, first_alert_ms FROM incidents WHERE id = $1", incident)
            links = await conn.fetch(
                "SELECT payload::text AS payload FROM outbox WHERE project_id = 1 AND dedupe_key LIKE $1",
                f"incident:{incident}:%",
            )

            await conn.execute("DELETE FROM outbox WHERE project_id = 1 AND dedupe_key LIKE $1", f"incident:{incident}:%")
            await conn.execute("DELETE FROM deploys WHERE id = $1", deploy)
            await conn.execute("DELETE FROM issues WHERE id = $1", issue)

        assert 1500 <= first["first_alert_ms"] < 10000
        assert again["first_alert_ms"] == first["first_alert_ms"]
        assert again["alerted_at"] == first["alerted_at"]
        assert len(links) == 2  # one per alert: possible, then confirmed
        assert all(settings.dashboard_link(f"/incidents/{incident}") in r["payload"] for r in links)
    finally:
        await close_db_pool()
