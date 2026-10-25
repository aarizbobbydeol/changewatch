"""Pydantic schemas and output validation for AI SRE Investigator (Phase 2 Stage C4).
"""
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, model_validator


class WhereLocation(BaseModel):
    file: str = Field(..., description="File path of innermost project frame")
    line: int = Field(0, description="Line number of innermost frame")
    function: str = Field("unknown", description="Function or method name")


class EvidenceItem(BaseModel):
    kind: Literal[
        "diff",
        "file",
        "dependency",
        "config",
        "events",
        "error_rate",
        "previous_deploy",
        "request",
        "timeline",
        "past_incident",
        "logs",
    ] = Field(..., description="Evidence category")
    detail: str = Field(..., description="Concrete factual finding")
    ref: Optional[str] = Field(None, description="Tool call id or data source reference")


class SuggestedFix(BaseModel):
    description: str = Field(..., description="Actionable fix or mitigation steps")
    code_hint: Optional[str] = Field(None, description="Short code snippet illustrating the fix")
    rollback_recommended: bool = Field(False, description="Whether immediate release rollback is recommended")
    rollback_to_sha: Optional[str] = Field(None, description="Target commit SHA to revert to")
    warning_label: Optional[str] = Field(None, description="Safety warning if fix involves risky actions")


class InvestigationOutput(BaseModel):
    summary: str = Field(..., description="One-sentence executive summary")
    what_broke: str = Field(..., description="Specific symptom, exception, and broken flow")
    where: WhereLocation = Field(..., description="Location of the failure in code")
    cause: str = Field(..., description="Hypothesized root cause backed by retrieved evidence")
    evidence: List[EvidenceItem] = Field(default_factory=list, description="Concrete evidence items")
    suggested_fix: SuggestedFix = Field(..., description="Recommended fix or rollback")
    confidence: Literal["high", "medium", "low"] = Field(..., description="Confidence assessment")
    not_sure_reason: Optional[str] = Field(None, description="Mandatory explanation when confidence is low")

    @model_validator(mode="after")
    def enforce_investigation_invariants(self) -> "InvestigationOutput":
        # Rule 1: If evidence is empty, confidence MUST be low
        if not self.evidence:
            self.confidence = "low"
            if not self.not_sure_reason:
                self.not_sure_reason = "No retrieved evidence directly explains this failure."

        # Rule 2: If confidence is low, not_sure_reason is required
        if self.confidence == "low" and not self.not_sure_reason:
            self.not_sure_reason = "Confidence is low based on inconclusive evidence."

        return self
