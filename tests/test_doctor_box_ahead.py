"""doctor's connector version check: a box ahead of the tree that runs this
tree's build is the expected result of a dev-push copy, not a stale tree."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("doctor_mod", ROOT / "scripts" / "doctor.py")
doctor = importlib.util.module_from_spec(_spec)
sys.modules["doctor_mod"] = doctor
_spec.loader.exec_module(doctor)


def test_box_ahead_running_this_tree_is_not_a_failure():
    c = doctor.connector_version_check("0.6.27", "0.6.26", runs_tree=True)
    assert c.status == "WARN"
    assert "runs this tree" in c.detail


def test_box_ahead_running_other_code_still_fails():
    c = doctor.connector_version_check("0.6.27", "0.6.26", runs_tree=False)
    assert c.status == "FAIL"


def test_equal_and_behind_unchanged():
    assert doctor.connector_version_check("0.6.26", "0.6.26", runs_tree=False).status == "PASS"
    assert doctor.connector_version_check("0.6.25", "0.6.26", runs_tree=True).status == "WARN"
