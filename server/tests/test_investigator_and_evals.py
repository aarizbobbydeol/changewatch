"""Comprehensive Tests for Milestone 5 (AI SRE Investigator & Evals) and TD Addendum Stages 4, 5, 7.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.decisions.jev_provider import JevDecisionProvider
from app.decisions.schemas import Question
from app.decisions.verifier import (
    verify_evidence_supports_conclusion,
    verify_suggestion_safety,
)
from app.investigator.agent import (
    run_investigation,
)
from app.investigator.schemas import (
    InvestigationOutput,
    SuggestedFix,
    WhereLocation,
)
from app.investigator.tools import (
    get_diff_files,
)
from app.investigator.worker import execute_investigation
from app.main import app
from app.auth import require_read_project_id


# ---------------------------------------------------------------------------
# 1. READ-ONLY TOOL INVARIANTS & SELECT-ONLY CHECKS
# ---------------------------------------------------------------------------

def test_investigator_tools_are_strictly_readonly():
    """Verify that investigator tools contain NO write operations (INSERT, UPDATE, DELETE, DROP)."""
    tools_file = Path("server/app/investigator/tools.py")
    content = tools_file.read_text(encoding="utf-8")

    # Clean comments and docstrings for query inspection
    write_patterns = [
        "INSERT INTO",
        "UPDATE ",
        "DELETE FROM",
        "DROP TABLE",
        "ALTER TABLE",
        "TRUNCATE",
    ]
    for pattern in write_patterns:
        # Should not have any active SQL write statements
        assert pattern not in content, f"Forbidden write pattern '{pattern}' found in tools.py"


@pytest.mark.asyncio
async def test_tools_return_tenant_scoped_results():
    """Verify tools fetch data scoped by project_id and deploy_id."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {
            "path": "server/app/main.py",
            "status": "modified",
            "added_ranges": "[[10, 20]]",
            "is_dependency_file": False,
            "is_config_file": False,
        }
    ]

    files = await get_diff_files(mock_conn, project_id=1, deploy_id=42)
    assert len(files) == 1
    assert files[0]["path"] == "server/app/main.py"
    assert files[0]["added_ranges"] == [[10, 20]]

    # Check that SQL was executed with project_id
    call_args = mock_conn.fetch.call_args[0]
    assert 1 in call_args
    assert 42 in call_args


# ---------------------------------------------------------------------------
# 2. VERIFIERS (TD STAGE 5)
# ---------------------------------------------------------------------------

def test_verifier_evidence_supports_conclusion():
    """Verify that evidence correlating with cause passes, weak evidence is downgraded."""
    cause = "KeyError in fetch_profile inside server/app/services/user_service.py"
    strong_evidence = [
        {"kind": "diff", "detail": "Modified fetch_profile in server/app/services/user_service.py"},
        {"kind": "events", "detail": "KeyError at server/app/services/user_service.py:84"},
    ]
    is_sup, score, note = verify_evidence_supports_conclusion(
        cause=cause,
        evidence=strong_evidence,
        where_file="server/app/services/user_service.py",
    )
    assert is_sup is True
    assert score >= 0.50
    assert note == "evidence is valid"

    # Weak / unrelated evidence
    weak_evidence = [
        {"kind": "diff", "detail": "Updated docs/README.md and styles.css"}
    ]
    is_sup_weak, score_weak, note_weak = verify_evidence_supports_conclusion(
        cause="Database connection timeout in billing worker",
        evidence=weak_evidence,
        where_file="server/app/billing.py",
    )
    assert is_sup_weak is False
    assert score_weak < 0.50
    assert note_weak == "evidence is weak"


def test_verifier_suggestion_safety():
    """Verify destructive commands in suggested fixes receive a warning label."""
    safe_fix = "Use dict.get('key', default) to handle missing dictionary entries safely."
    is_safe, prob, warning = verify_suggestion_safety(safe_fix)
    assert is_safe is True
    assert warning is None

    unsafe_fix = "Run rm -rf /var/cache/app and drop table sessions to reset cache."
    is_unsafe, prob_unsafe, warning_unsafe = verify_suggestion_safety(unsafe_fix)
    assert is_unsafe is False
    assert prob_unsafe >= 0.90
    assert "Warning" in warning_unsafe


# ---------------------------------------------------------------------------
# 3. JEV ADAPTER (TD STAGE 4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jev_provider_format_and_offline_fallback():
    """Verify Jev provider payload shaping and graceful offline fallback."""
    provider = JevDecisionProvider(api_key="mock_jev_key")
    questions = {
        "is_regression_from_release": Question(
            id="is_regression_from_release",
            kind="noul",
            instructions="Is this error likely caused by the code changes?",
        )
    }
    state = {"error": "TypeError", "file": "app/main.py"}

    # Mock response matching TypeSafe systemone shape
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "model": "jev-2024-10",
        "answers": {
            "is_regression_from_release": {
                "probability": 0.92,
            }
        },
    }

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        answers = await provider.decide(state, questions)
        assert "is_regression_from_release" in answers
        ans = answers["is_regression_from_release"]
        assert ans.probability == 0.92
        assert ans.source == "jev"

    # Test failure fallback (e.g. timeout / network error)
    with patch("httpx.AsyncClient.post", side_effect=Exception("Connection refused")):
        fallback_answers = await provider.decide(state, questions)
        assert "is_regression_from_release" in fallback_answers
        fb_ans = fallback_answers["is_regression_from_release"]
        assert fb_ans.source == "rules"  # Fallback to rules on error


# ---------------------------------------------------------------------------
# 4. INVESTIGATOR AGENT & WORKER
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_investigation_and_caching():
    """Test investigation agent execution, verifier enforcement, and cache behavior."""
    mock_conn = AsyncMock()
    now_dt = datetime.now(timezone.utc)

    async def mock_fetchrow(query, *args):
        q = query.lower()
        if "from investigations" in q:
            return None
        elif "from incidents i" in q:
            return {
                "id": 1, "deploy_id": 10, "issue_id": 5, "where_file": "app/user.py",
                "where_line": 20, "where_function": "login", "title": "KeyError: 'token'",
                "event_count": 12, "created_at": now_dt,
                "commit_sha": "abc12345", "commit_author": "dev", "commit_message": "Add auth",
                "deployed_at": now_dt, "environment": "prod", "fingerprint": "fp1",
                "in_diff": True, "category": "logic_bug",
            }
        elif "from deploy_files" in q:
            return {"content": "def login():\n    token = request.headers['token']\n"}
        elif "from deploys" in q:
            return {"deployed_at": now_dt, "commit_sha": "prev1"}
        elif "insert into investigations" in q:
            return {"id": 99}
        return None

    async def mock_fetch(query, *args):
        q = query.lower()
        if "from deploy_files" in q:
            return [{"path": "app/user.py", "status": "modified", "added_ranges": "[[15, 25]]", "is_dependency_file": False, "is_config_file": False}]
        elif "from events" in q:
            return [{"id": 100, "occurred_at": now_dt, "request_path": "/login", "duration_ms": 150, "stack_trace": "KeyError"}]
        return []

    mock_conn.fetchrow.side_effect = mock_fetchrow
    mock_conn.fetch.side_effect = mock_fetch

    with patch("app.crypto.encrypt", new=AsyncMock(return_value=b"enc")), \
         patch("app.investigator.agent.get_llm_client") as mock_llm_factory, \
         patch("app.investigator.agent.queue_notification", new=AsyncMock()), \
         patch("app.metering.record_investigation_usage", new=AsyncMock()) as mock_usage:
        mock_llm = AsyncMock()
        mock_llm.model = "fake-model"
        mock_llm.last_usage = (1234, 567)  # tokens the provider reported for this call
        # Return valid JSON
        mock_llm.complete.return_value = json.dumps({
            "summary": "KeyError in login header parsing",
            "what_broke": "Missing token header throws unhandled KeyError",
            "where": {"file": "app/user.py", "line": 20, "function": "login"},
            "cause": "Login handler accesses request.headers['token'] directly without fallback",
            "evidence": [{"kind": "diff", "detail": "Modified app/user.py in this deploy", "ref": "deploy_files"}],
            "suggested_fix": {"description": "Use request.headers.get('token')", "rollback_recommended": False},
            "confidence": "high",
            "not_sure_reason": None,
        })
        mock_llm_factory.return_value = mock_llm

        res = await run_investigation(mock_conn, project_id=1, incident_id=1, deep_mode=False)
        assert res.confidence in ("high", "medium")
        assert "login" in res.cause.lower()

        # The fast model is used, and the real token counts are stored and metered (not fixed numbers)
        mock_llm_factory.assert_called_once_with(deep=False)
        insert_args = next(c.args for c in mock_conn.fetchrow.call_args_list if "insert into investigations" in c.args[0].lower())
        assert insert_args[-2:] == (1234, 567)
        assert mock_usage.call_args.kwargs["input_tokens"] == 1234
        assert mock_usage.call_args.kwargs["output_tokens"] == 567

    # 2nd run: Cache hit
    mock_conn_cached = AsyncMock()
    mock_conn_cached.fetchrow.return_value = {
        "id": 99,
        "result": json.dumps(res.model_dump()),
    }
    cached_res = await run_investigation(mock_conn_cached, project_id=1, incident_id=1, deep_mode=False)
    assert cached_res.summary == res.summary
    assert cached_res.cause == res.cause


@pytest.mark.asyncio
async def test_worker_budget_pause_behavior():
    """Verify that when project hard limit is hit, investigator worker pauses and notifies."""
    mock_conn = AsyncMock()
    # Mock budget hard limit exceeded
    with patch("app.investigator.worker.can_spend", new=AsyncMock(return_value=(False, "hard_limit_exceeded"))), \
         patch("app.investigator.worker.queue_notification", new=AsyncMock()) as mock_notify:
        mock_conn.fetchrow.return_value = {"id": 101}
        result = await execute_investigation(mock_conn, project_id=1, incident_id=1, deep_mode=False)
        assert result is None  # Paused!

        # Check notification queued
        assert mock_notify.called
        call_payload = mock_notify.call_args[1]["payload"]
        assert call_payload["type"] == "investigation_paused"
        assert "budget reached" in call_payload["title"]


# ---------------------------------------------------------------------------
# 5. API ENDPOINTS INTEGRATION
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_incident_returns_ai_investigation():
    """Verify GET /api/incidents/{id} returns real investigation object from DB."""
    app.dependency_overrides[require_read_project_id] = lambda: 1
    mock_conn = AsyncMock()

    now = datetime.now(timezone.utc)
    mock_conn.fetchrow.side_effect = [
        {
            "id": 10, "deploy_id": 1, "issue_id": 2, "status": "possible",
            "title": "IndexError: list index out of range", "where_file": "app/paginate.py",
            "where_line": 63, "where_function": "paginate", "in_diff": True,
            "owners": "[]", "event_count": 5, "category": "logic_bug",
            "first_alert_ms": 1200, "created_at": now, "confirmed_at": None,
            "resolved_at": None, "commit_sha": "abc999", "commit_author": "alice",
            "commit_message": "Fix list", "deployed_at": now,
        },
        {"content": "def paginate():\n    return items[10]\n"}, # code excerpt
        { # investigation row
            "id": 45, "status": "done", "model": "fake-model",
            "started_at": now, "finished_at": now,
            "result": json.dumps({
                "summary": "Off-by-one error in paginate",
                "what_broke": "IndexError on empty page",
                "where": {"file": "app/paginate.py", "line": 63, "function": "paginate"},
                "cause": "Slice boundary off-by-one",
                "evidence": [],
                "suggested_fix": {"description": "Check len(items)", "rollback_recommended": False},
                "confidence": "high",
                "not_sure_reason": None,
            }),
            "not_sure_reason": None,
        },
    ]

    with patch("app.main.read_project_transaction") as mock_tx:
        mock_tx.return_value.__aenter__.return_value = mock_conn
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/incidents/10", headers={"Authorization": "Bearer test-key"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == 10
            assert "ai_investigation" in data
            ai_inv = data["ai_investigation"]
            assert ai_inv["enabled"] is True
            assert ai_inv["status"] == "done"
            assert ai_inv["result"]["summary"] == "Off-by-one error in paginate"

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_post_incident_investigate_endpoint():
    """Verify POST /api/incidents/{id}/investigate triggers investigation."""
    app.dependency_overrides[require_read_project_id] = lambda: 1
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 10}

    mock_inv_output = InvestigationOutput(
        summary="Test deep investigation",
        what_broke="Broke something",
        where=WhereLocation(file="test.py", line=1, function="test"),
        cause="Identified cause",
        evidence=[],
        suggested_fix=SuggestedFix(description="Fix it", rollback_recommended=False),
        confidence="high",
        not_sure_reason=None,
    )

    with patch("app.main.project_transaction") as mock_tx, \
         patch("app.investigator.worker.execute_investigation", new=AsyncMock(return_value=mock_inv_output)):
        mock_tx.return_value.__aenter__.return_value = mock_conn
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/incidents/10/investigate?deep=true", headers={"Authorization": "Bearer test-key"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "done"
            assert data["result"]["summary"] == "Test deep investigation"

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 6. EVAL RUNNER INTEGRATION TEST
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eval_suite_and_unexplainable_handling():
    """Verify that all 12 scenarios run and unexplainable returns confidence='low' with reason."""
    from evals.run import evaluate_decision_triage, evaluate_investigator, load_scenarios

    scenarios_dir = Path("evals/scenarios")
    scenarios = load_scenarios(scenarios_dir)
    assert len(scenarios) == 12

    for sc in scenarios:
        dt_ok, dt_res, dt_msg = await evaluate_decision_triage(sc, provider="rules")
        assert dt_ok is True, f"Decision triage failed for {sc['id']}: {dt_msg}"

        inv_ok, inv_res, inv_msg = await evaluate_investigator(sc, provider="fake")
        assert inv_ok is True, f"Investigator eval failed for {sc['id']}: {inv_msg}"

        if sc["id"] == "unexplainable":
            assert inv_res.confidence == "low"
            assert inv_res.not_sure_reason is not None
            assert len(inv_res.not_sure_reason) > 0
