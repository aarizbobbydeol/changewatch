"""Decision Catalog (Version 1).

Defines typed questions for incident triage, investigation, and verification.
"""
from __future__ import annotations

from typing import Dict, List, Optional
from app.decisions.schemas import Question

CATALOG_VERSION = "v1"

# Triage Question IDs
Q_REGRESSION = "is_regression_from_release"
Q_NOISE = "is_known_noise"
Q_TRANSIENT = "is_transient_infra"
Q_SEVERITY = "severity"
Q_CATEGORY = "error_category"
Q_OWNER = "owner_area"

ERROR_CATEGORIES = {
    "logic_bug": "Uncaught exception or bug in business logic",
    "missing_data_or_null": "KeyError, AttributeError, NoneType or null dereference",
    "config_or_env": "Missing environment variable, configuration error, or invalid settings",
    "dependency_or_version": "ImportError, package mismatch, or dependency incompatibility",
    "database_or_migration": "IntegrityError, relation not found, schema mismatch, or migration error",
    "external_service": "Downstream HTTP failure, 3rd party API error, or connection refusal",
    "performance_timeout": "Request timeout, gateway timeout, or slow query",
    "security_or_auth": "Authentication failure, 401/403, or invalid token",
    "other": "Other unclassified error",
}

SEVERITY_RUBRIC = [
    "0: cosmetic (harmless warning or cosmetic glitch)",
    "1: minor (low-impact error on non-essential route)",
    "2: degraded (noticeable user impact or transient issue)",
    "3: major (critical route error or broken primary flow)",
    "4: outage (widespread crash loop or service unavailability)",
]


def get_triage_questions(owner_candidates: Optional[List[str]] = None) -> Dict[str, Question]:
    """
    Returns standard triage questions dictionary for an incident.
    If owner_candidates are supplied, populates owner_area options.
    """
    owner_options = {c: f"Candidate owner {c}" for c in (owner_candidates or [])}
    if not owner_options:
        owner_options = {"unassigned": "No deterministic owner candidate"}

    return {
        Q_REGRESSION: Question(
            id=Q_REGRESSION,
            kind="noul",
            instructions="Is this error likely caused by the code changes in this release?",
        ),
        Q_NOISE: Question(
            id=Q_NOISE,
            kind="noul",
            instructions="Is this an expected or harmless error that does not need human attention (health checks, bots, client disconnects, harmless retries)?",
        ),
        Q_TRANSIENT: Question(
            id=Q_TRANSIENT,
            kind="noul",
            instructions="Is this more likely a temporary infrastructure problem (network, timeout, dependency outage) than a code bug?",
        ),
        Q_SEVERITY: Question(
            id=Q_SEVERITY,
            kind="score",
            instructions="Rate the incident severity on the rubric scale from 0 to 4.",
            rubric=SEVERITY_RUBRIC,
        ),
        Q_CATEGORY: Question(
            id=Q_CATEGORY,
            kind="choice",
            instructions="Select the primary category that explains this error.",
            options=ERROR_CATEGORIES,
        ),
        Q_OWNER: Question(
            id=Q_OWNER,
            kind="choice",
            instructions="Rank the most appropriate owner among the candidate options.",
            options=owner_options,
        ),
    }
