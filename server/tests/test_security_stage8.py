"""Security Addendum Stage 8 Tests: Security model and audit runner verification.

Verifies:
1. docs/SECURITY_MODEL.md exists and documents the 4 core promises and honest limits.
2. scripts/security_check.py runs and passes all compliance checks.
"""
from __future__ import annotations

import sys
from pathlib import Path
import pytest

# Ensure scripts dir is in sys.path
repo_root = Path(__file__).resolve().parent.parent.parent
scripts_dir = repo_root / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from security_check import SecurityComplianceRunner


def test_security_model_document_exists_and_covers_promises():
    """Verify docs/SECURITY_MODEL.md exists and articulates the 4 promises and limits."""
    doc_path = repo_root / "docs" / "SECURITY_MODEL.md"
    assert doc_path.exists(), "docs/SECURITY_MODEL.md must exist"

    content = doc_path.read_text(encoding="utf-8")
    # Verify core promises
    assert "We never modify the customer's code or repository" in content
    assert "We never disturb the customer's running app" in content
    assert "Data is encrypted in transit and at rest" in content
    assert "We collect the minimum" in content

    # Verify honest limits
    assert "What Encryption at Rest Protects Against" in content
    assert "What Encryption at Rest Does NOT Protect Against" in content
    assert "LLM_PROVIDER=anthropic" in content
    assert "LLM_PROVIDER=ollama" in content
    assert "MASTER_KEY" in content


def test_security_check_runner_passes_all_checks():
    """Verify SecurityComplianceRunner passes all 12 checks with 100% compliance."""
    runner = SecurityComplianceRunner()
    runner.run_all_checks()

    failed = [f"{cat} -> {name}: {detail}" for cat, name, passed, detail in runner.results if not passed]
    assert not failed, f"Security compliance checks failed: {failed}"
    assert len(runner.results) >= 12
