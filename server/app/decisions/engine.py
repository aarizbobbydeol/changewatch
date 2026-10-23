"""Decision Engine, State Builder, Circuit Breaker, and Fallback Execution.

Specified in Typed Decisions Addendum Sections A, B, D, E.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional
import asyncpg

from app.config import settings
from app.decisions.catalog import CATALOG_VERSION
from app.decisions.rules_provider import RulesDecisionProvider, FakeDecisionProvider
from app.decisions.schemas import Answer, DecisionProvider, Question
from app.scrub import scrub_text

logger = logging.getLogger("changewatch.decisions.engine")


class CircuitBreaker:
    """Circuit breaker for hosted decision providers. Opens after 5 consecutive failures."""

    def __init__(self, failure_threshold: int = 5, cooldown_sec: float = 60.0):
        self.failure_threshold = failure_threshold
        self.cooldown_sec = cooldown_sec
        self.failure_count = 0
        self.opened_at: Optional[float] = None

    def record_success(self) -> None:
        self.failure_count = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold and self.opened_at is None:
            self.opened_at = time.time()
            logger.warning(
                "Decision circuit breaker OPEN after %d consecutive failures. Cooling down for %.1fs.",
                self.failure_count,
                self.cooldown_sec,
            )

    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at > self.cooldown_sec:
            # Half-open: attempt recovery
            logger.info("Decision circuit breaker entering HALF-OPEN state.")
            self.opened_at = None
            self.failure_count = 0
            return False
        return True


_global_circuit_breaker = CircuitBreaker()
_rules_provider = RulesDecisionProvider()


def get_decision_provider(provider_type: Optional[str] = None) -> DecisionProvider:
    """Retrieve decision provider based on config or override."""
    ptype = (provider_type or getattr(settings, "DECISION_PROVIDER", "rules")).lower()
    if ptype == "fake":
        return FakeDecisionProvider()
    elif ptype == "rules":
        return _rules_provider
    elif ptype == "jev":
        # Jev adapter will be loaded in Stage 4 / M5
        # If JEV_API_KEY is not configured, fall back to rules
        if not getattr(settings, "JEV_API_KEY", None):
            return _rules_provider
        from app.decisions.jev_provider import JevDecisionProvider
        return JevDecisionProvider()
    return _rules_provider


def build_decision_state(
    error_title: str,
    exc_type: str,
    frames: List[Dict[str, Any]],
    request_path: str,
    event_count: int,
    in_diff: bool,
    is_critical: bool,
    commit_message: Optional[str] = None,
    code_excerpt: Optional[str] = None,
    send_code: bool = False,
    max_chars: int = 20000,
) -> Dict[str, Any]:
    """
    Build a compact, pre-scrubbed state dictionary for decision providers.
    Enforces secret scrubbing, char limit, and privacy rules (no code unless send_code is True).
    """
    clean_title = scrub_text(error_title[:500])
    clean_exc = scrub_text(exc_type[:200])
    clean_path = scrub_text(request_path[:500])
    clean_msg = scrub_text(commit_message[:1000]) if commit_message else ""

    top_frames = []
    for f in frames[:5]:
        top_frames.append({
            "file": scrub_text(str(f.get("file", ""))[:200]),
            "line": f.get("line", 0),
            "function": scrub_text(str(f.get("function", ""))[:100]),
        })

    state: Dict[str, Any] = {
        "title": clean_title,
        "exc_type": clean_exc,
        "request_path": clean_path,
        "event_count": event_count,
        "in_diff": in_diff,
        "is_critical": is_critical,
        "commit_message": clean_msg,
        "top_frames": top_frames,
    }

    if send_code and code_excerpt:
        state["code_excerpt"] = scrub_text(code_excerpt[:2000])

    serialized = json.dumps(state)
    if len(serialized) > max_chars:
        # Truncate commit message and top frames if oversized
        state["commit_message"] = state["commit_message"][:200]
        state["top_frames"] = top_frames[:2]
        serialized = json.dumps(state)

    return state


async def decide_with_fallback(
    state: Dict[str, Any],
    questions: Dict[str, Question],
    provider: Optional[DecisionProvider] = None,
    timeout_ms: int = 800,
) -> Dict[str, Answer]:
    """
    Execute decision with strict timeout and automatic fallback to rules.
    Never raises an exception; always returns a complete set of answers.
    """
    active_provider = provider or get_decision_provider()

    if _global_circuit_breaker.is_open():
        logger.debug("Circuit breaker open: using deterministic rules provider directly")
        return await _rules_provider.decide(state, questions)

    try:
        timeout_sec = max(0.1, timeout_ms / 1000.0)
        answers = await asyncio.wait_for(
            active_provider.decide(state, questions),
            timeout=timeout_sec,
        )
        _global_circuit_breaker.record_success()
        return answers
    except Exception as exc:
        _global_circuit_breaker.record_failure()
        logger.warning(
            "Decision provider %s failed (falling back to rules): %s",
            getattr(active_provider, "__class__", type(active_provider)).__name__,
            exc,
        )
        return await _rules_provider.decide(state, questions)


async def persist_decisions(
    conn: asyncpg.Connection,
    project_id: int,
    incident_id: int,
    state: Dict[str, Any],
    answers: Dict[str, Answer],
    latency_ms: int = 0,
    cost_micros: int = 0,
) -> None:
    """
    Persist decision results into decisions table under project RLS.
    Stores hash of state text (never stores the state text itself).
    """
    state_str = json.dumps(state, sort_keys=True)
    state_hash = hashlib.sha256(state_str.encode("utf-8")).hexdigest()

    for q_id, ans in answers.items():
        ans_payload = {
            "choice": ans.choice,
            "score": ans.score,
            "probability": ans.probability,
            "confidence": ans.confidence,
            "probabilities": ans.probabilities,
        }
        await conn.execute(
            """
            INSERT INTO decisions (
                project_id, incident_id, question_id, kind,
                answer, probability, confidence, source, model,
                catalog_version, state_hash, latency_ms, cost_micros
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            project_id,
            incident_id,
            q_id,
            ans.kind,
            json.dumps(ans_payload),
            ans.probability,
            ans.confidence,
            ans.source,
            ans.model or "rules-v1",
            CATALOG_VERSION,
            state_hash,
            latency_ms,
            cost_micros,
        )
