"""Owners must resolve from repo-relative paths even though stack frames are absolute."""
from app.incident_manager import _same_file
from app.sre_prep import resolve_owners

CODEOWNERS = "* @core-team\napp/billing.py @billing-team\n"


def test_same_file_matches_absolute_frame_to_repo_path():
    assert _same_file("/srv/app/app/billing.py", "app/billing.py")
    assert _same_file("app/billing.py", "app/billing.py")
    assert _same_file("C:\\srv\\app\\app\\billing.py", "app/billing.py")


def test_same_file_respects_path_boundaries():
    assert not _same_file("/srv/app/app/xbilling.py", "billing.py")
    assert not _same_file("/srv/app/other/billing.py", "app/billing.py")


def test_last_matching_codeowners_rule_wins_for_repo_path():
    assert resolve_owners("app/billing.py", CODEOWNERS, []) == ["@billing-team"]
    # The absolute frame path alone only hits the catch-all rule, which is the bug this guards
    assert resolve_owners("/srv/app/app/billing.py", CODEOWNERS, []) == ["@core-team"]
