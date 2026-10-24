"""Typed Decisions Abstraction for ChangeWatch.

Provides typed questions (choice, score, noul), answers, and decision providers.
Specified in Typed Decisions Addendum (TD).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Protocol

logger = logging.getLogger("changewatch.decisions")


@dataclass
class Question:
    id: str
    kind: Literal["choice", "score", "noul"]
    instructions: str
    options: Optional[Dict[str, str]] = None  # choice: option id -> description (max 255)
    rubric: Optional[List[str]] = None        # score: ordered levels, low to high


@dataclass
class Answer:
    id: str
    kind: str
    choice: Optional[str] = None
    score: Optional[float] = None
    probability: Optional[float] = None       # noul is P(yes)
    confidence: Optional[float] = None
    probabilities: Optional[Dict[str, float]] = None
    source: Literal["rules", "jev", "fake"] = "rules"
    model: Optional[str] = None


class DecisionProvider(Protocol):
    async def decide(self, state: Dict[str, Any] | str, questions: Dict[str, Question]) -> Dict[str, Answer]:
        ...
