"""The end-to-end script's own checks must fail when what they check is wrong."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "e2e_check.py"


def _e2e():
    spec = importlib.util.spec_from_file_location("e2e_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_timeline_check_needs_the_alert_delivered_not_just_mentioned():
    e2e = _e2e()
    deployed = {"event": "deployed", "detail": "Deploy abc12345 to prod"}

    def timeline(detail):  # the three ways build_incident_timeline reports an alert
        return [deployed, {"event": "alert_sent", "detail": detail}]

    assert e2e.alert_delivered(timeline("Possible-regression alert delivered to 1 channel"))
    assert e2e.alert_delivered(timeline("Confirmed-regression alert delivered to 2 channels"))
    assert not e2e.alert_delivered(timeline("Possible-regression alert could not be delivered"))
    assert not e2e.alert_delivered(timeline("Possible-regression alert waiting to be delivered"))
    assert not e2e.alert_delivered(timeline("Resolved message delivered to 1 channel"))  # not the incident alert
    assert not e2e.alert_delivered([deployed, {"event": "alert_sent", "detail": None}])
    assert not e2e.alert_delivered([deployed])
