"""Deterministic Rules Decision Provider and Test Fake Provider.

Specified in Typed Decisions Addendum Section A & C.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from app.decisions.catalog import (
    Q_REGRESSION,
    Q_NOISE,
    Q_TRANSIENT,
    Q_SEVERITY,
    Q_CATEGORY,
    Q_OWNER,
)
from app.decisions.schemas import Answer, Question

logger = logging.getLogger("changewatch.decisions.rules")

NOISE_PATH_PATTERNS = ("/healthz", "/health", "/ping", "/status", "/favicon.ico", "/robots.txt")
NOISE_EXC_TYPES = ("ClientDisconnect", "ConnectionResetError", "BrokenPipeError")


class RulesDecisionProvider:
    """
    100% local, deterministic decision provider based on code heuristics.
    Never calls external networks or hosted APIs.
    """

    async def decide(
        self, state: Dict[str, Any] | str, questions: Dict[str, Question]
    ) -> Dict[str, Answer]:
        if isinstance(state, str):
            import json
            try:
                state_dict = json.loads(state)
            except Exception:
                state_dict = {}
        else:
            state_dict = state or {}

        in_diff = bool(state_dict.get("in_diff", False))
        is_critical = bool(state_dict.get("is_critical", False))
        request_path = str(state_dict.get("request_path", "")).lower()
        exc_type = str(state_dict.get("exc_type", ""))
        message = str(state_dict.get("message", "")).lower()
        event_count = int(state_dict.get("event_count", 1))

        answers: Dict[str, Answer] = {}

        for q_id, q in questions.items():
            if q_id == Q_REGRESSION:
                # 1.0 if inside diff, 0.5 otherwise
                prob = 1.0 if in_diff else 0.40
                answers[q_id] = Answer(
                    id=q_id,
                    kind="noul",
                    probability=prob,
                    confidence=0.9 if in_diff else 0.6,
                    source="rules",
                    model="rules-v1",
                )

            elif q_id == Q_NOISE:
                is_noise = any(p in request_path for p in NOISE_PATH_PATTERNS) or any(
                    e in exc_type for e in NOISE_EXC_TYPES
                )
                prob = 0.95 if is_noise else 0.05
                answers[q_id] = Answer(
                    id=q_id,
                    kind="noul",
                    probability=prob,
                    confidence=0.95 if is_noise else 0.90,
                    source="rules",
                    model="rules-v1",
                )

            elif q_id == Q_TRANSIENT:
                is_transient = any(
                    t in exc_type
                    for t in ("Timeout", "OperationalError", "ConnectionRefused", "NetworkError")
                ) or ("timeout" in message or "temporarily unavailable" in message)
                prob = 0.85 if is_transient else 0.10
                answers[q_id] = Answer(
                    id=q_id,
                    kind="noul",
                    probability=prob,
                    confidence=0.85,
                    source="rules",
                    model="rules-v1",
                )

            elif q_id == Q_SEVERITY:
                # 0: cosmetic, 1: minor, 2: degraded, 3: major, 4: outage
                if is_critical or event_count >= 50:
                    score = 4.0
                elif in_diff or event_count >= 10:
                    score = 3.0
                elif any(t in exc_type for t in ("Timeout", "OperationalError")):
                    score = 2.0
                else:
                    score = 1.5

                answers[q_id] = Answer(
                    id=q_id,
                    kind="score",
                    score=score,
                    confidence=0.9,
                    source="rules",
                    model="rules-v1",
                )

            elif q_id == Q_CATEGORY:
                choice = "logic_bug"
                if any(k in exc_type for k in ("KeyError", "AttributeError", "TypeError", "IndexError")):
                    choice = "missing_data_or_null"
                elif any(k in exc_type for k in ("IntegrityError", "ProgrammingError", "OperationalError")):
                    choice = "database_or_migration"
                elif any(k in exc_type for k in ("ImportError", "ModuleNotFoundError")):
                    choice = "dependency_or_version"
                elif any(k in exc_type for k in ("Timeout", "GatewayTimeout")):
                    choice = "performance_timeout"
                elif any(k in exc_type for k in ("Auth", "Permission", "Forbidden", "Unauthorized")):
                    choice = "security_or_auth"
                elif "config" in message or "env" in message or "settings" in message:
                    choice = "config_or_env"

                answers[q_id] = Answer(
                    id=q_id,
                    kind="choice",
                    choice=choice,
                    confidence=0.85,
                    source="rules",
                    model="rules-v1",
                )

            elif q_id == Q_OWNER:
                # Pick the first deterministic candidate option
                choice = "unassigned"
                if q.options:
                    choice = next(iter(q.options.keys()))
                answers[q_id] = Answer(
                    id=q_id,
                    kind="choice",
                    choice=choice,
                    confidence=0.8,
                    source="rules",
                    model="rules-v1",
                )

            else:
                # Generic fallback for uncataloged questions
                answers[q_id] = Answer(
                    id=q_id,
                    kind=q.kind,
                    choice=next(iter(q.options.keys())) if q.options else None,
                    score=2.0 if q.kind == "score" else None,
                    probability=0.5 if q.kind == "noul" else None,
                    confidence=0.5,
                    source="rules",
                    model="rules-v1",
                )

        return answers


class FakeDecisionProvider:
    """Scripted or configurable fake provider for tests."""

    def __init__(self, canned_answers: Optional[Dict[str, Answer]] = None, fail: bool = False):
        self.canned_answers = canned_answers or {}
        self.fail = fail
        self.call_count = 0

    async def decide(
        self, state: Dict[str, Any] | str, questions: Dict[str, Question]
    ) -> Dict[str, Answer]:
        self.call_count += 1
        if self.fail:
            raise RuntimeError("FakeDecisionProvider simulated failure")

        answers: Dict[str, Answer] = {}
        for q_id, q in questions.items():
            if q_id in self.canned_answers:
                answers[q_id] = self.canned_answers[q_id]
            else:
                answers[q_id] = Answer(
                    id=q_id,
                    kind=q.kind,
                    choice=next(iter(q.options.keys())) if q.options else None,
                    score=2.0 if q.kind == "score" else None,
                    probability=0.7 if q.kind == "noul" else None,
                    confidence=0.9,
                    source="fake",
                    model="fake-v1",
                )
        return answers
