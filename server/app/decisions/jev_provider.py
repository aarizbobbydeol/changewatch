"""TypeSafe Jev Decision Provider Adapter (TD Addendum Section H).

Implements the DecisionProvider protocol against the TypeSafe systemone API.
Protocol:
- Endpoint: POST https://api.typesafe.ai/v1/systemone (configurable via JEV_BASE_URL)
- Auth: Bearer token via JEV_API_KEY
- Batched parallel evaluation of typed questions (choice, score, noul)
- Falls back gracefully to RulesDecisionProvider on timeout or network error.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Union
import httpx

from app.decisions.schemas import Answer, Question
from app.decisions.rules_provider import RulesDecisionProvider

logger = logging.getLogger("changewatch.decisions.jev")

DEFAULT_JEV_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-2024-10"


class JevDecisionProvider:
    """TypeSafe Jev hosted decision provider adapter."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_ms: int = 800,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.api_key = api_key or os.getenv("JEV_API_KEY", "")
        self.base_url = (base_url or os.getenv("JEV_BASE_URL", DEFAULT_JEV_BASE_URL)).strip()
        self.model = model or os.getenv("JEV_MODEL", DEFAULT_JEV_MODEL)
        self.timeout_sec = max(0.2, timeout_ms / 1000.0)
        self.transport = transport
        self.rules_fallback = RulesDecisionProvider()

    async def decide(
        self,
        state: Union[Dict[str, Any], str],
        questions: Union[Dict[str, Question], List[Question]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Answer]:
        """
        Send parallel batched questions to Jev endpoint.
        Returns map of question_id -> Answer.
        """
        q_dict: Dict[str, Question] = (
            questions if isinstance(questions, dict)
            else {q.id: q for q in questions}
        )

        if not self.api_key:
            logger.info("JEV_API_KEY not configured; falling back to rules provider")
            return await self.rules_fallback.decide(state, q_dict)

        # Cap state size
        if isinstance(state, dict):
            state_str = json.dumps(state)
        else:
            state_str = str(state)

        max_chars = int(os.getenv("DECISION_STATE_MAX_CHARS", "16000"))
        clipped_state = state_str[:max_chars]

        # Format questions according to Jev /systemone shape
        questions_payload: Dict[str, Any] = {}
        for q_id, q in q_dict.items():
            qd: Dict[str, Any] = {
                "type": q.kind,
                "instructions": q.instructions,
            }
            if q.kind == "choice" and q.options:
                qd["options"] = dict(list(q.options.items())[:255])
            elif q.kind == "score" and q.rubric:
                qd["criteria"] = q.rubric
            questions_payload[q.id] = qd

        body = {
            "model": self.model,
            "state": clipped_state,
            "questions": questions_payload,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec, transport=self.transport) as client:
                resp = await client.post(self.base_url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Jev provider call failed (%s); falling back to rules", exc)
            return await self.rules_fallback.decide(state, q_dict)

        answers_data = data.get("answers", data)

        answers: Dict[str, Answer] = {}
        for q_id, q in q_dict.items():
            q_res = answers_data.get(q.id)
            if not q_res:
                continue

            if q.kind == "noul":
                prob = float(q_res.get("probability", q_res.get("value", 0.5)))
                answers[q.id] = Answer(
                    id=q.id,
                    kind="noul",
                    probability=prob,
                    source="jev",
                    model=self.model,
                )
            elif q.kind == "score":
                score = float(q_res.get("score", q_res.get("value", 1.0)))
                conf = q_res.get("confidence")
                answers[q.id] = Answer(
                    id=q.id,
                    kind="score",
                    score=score,
                    confidence=float(conf) if conf is not None else None,
                    source="jev",
                    model=self.model,
                )
            elif q.kind == "choice":
                chosen = str(q_res.get("choice", q_res.get("option", "other")))
                probs = q_res.get("probabilities", {})
                conf = q_res.get("confidence")
                answers[q.id] = Answer(
                    id=q.id,
                    kind="choice",
                    choice=chosen,
                    probabilities=probs,
                    confidence=float(conf) if conf is not None else None,
                    source="jev",
                    model=self.model,
                )

        return answers
