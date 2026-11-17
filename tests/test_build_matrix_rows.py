"""The sweep's build_matrix rows are twins of eval-matrix tasks, not copies.

Each row names its task (`from_task`) and grades with that task's own
ir_assertions (`behaves`). The prompt is the one thing the row must carry
itself (the conversation driver reads `steps`), so this pins it equal to the
task's -- a reworded prompt would grade a different request against the old
assertions, and the gap would read as a model regression.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(_SCRIPTS.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS.parent))

import chat_sweep  # noqa: E402

ROWS = sorted((_SCRIPTS / "conversations" / "sweep").glob("mx_*.json"))


def _task(name):
    try:
        return chat_sweep._matrix_task(name)
    except ValueError:
        pytest.skip("framework eval-matrix tasks not reachable from this env")


def test_there_are_build_matrix_rows():
    assert len(ROWS) >= 20


@pytest.mark.parametrize("path", ROWS, ids=lambda p: p.stem)
def test_row_is_a_faithful_twin_of_its_task(path):
    row = json.loads(path.read_text())
    task = _task(row["from_task"])
    assert row["name"] == "mx_" + task["name"]
    if "behaves" not in row["expect"]:
        # Dropping the grade is allowed only with the reason on the row.
        assert len(row.get("behaves_waived", "")) > 40, row["name"]
        return
    assert row["expect"]["behaves"] == row["from_task"]
    if task.get("mode") == "repair":
        # The designer's idiom replaces only the delivery sentence; the
        # symptom the analyst reports is the task's, verbatim.
        ask = "Find the defect and return the corrected complete YAML."
        assert row["playbook"] == "matrix:" + task["name"]
        assert [s["user"] for s in row["steps"]] == [
            task["prompt"].replace(ask, "Find the defect and fix it.")]
        assert ask in task["prompt"]
    else:
        assert [s["user"] for s in row["steps"]] == [task["prompt"]]


def test_a_repair_mount_does_not_say_what_is_broken():
    from scripts.local_conversation import mounted_yaml
    repairs = [json.loads(p.read_text()) for p in ROWS]
    repairs = [r for r in repairs if str(r.get("playbook", "")).startswith("matrix:")]
    assert repairs
    for r in repairs:
        try:
            text = mounted_yaml(r).lower()
        except ValueError:
            pytest.skip("framework eval-matrix tasks not reachable from this env")
        assert "#" not in "".join(l.strip()[:1] for l in text.splitlines())
        for leak in ("broken", "repair", "defect", "before-state"):
            assert leak not in text, (r["name"], leak)


def test_behaves_judges_the_last_delivered_playbook():
    task = _task("for_each_loop")
    grade = chat_sweep._behaves("for_each_loop")
    # The checkout, not the installed package: with a wheel, parents[1] of
    # fsr_playbooks is site-packages, where the gold examples don't exist.
    from framework_tooling import framework_root
    good = (framework_root() / task["gold_yaml_path"]).read_text()

    class C:
        yamls = ["collection: x\nplaybooks: []\n", good]
    ok, detail = grade(C)
    assert ok, detail

    class Empty:
        yamls: list = []
    assert grade(Empty) == (False, "no playbook delivered -- nothing to judge")


def test_slack_box_is_the_default_inventory_plus_slack():
    # A copy of the canned inventory would drift from it silently; pin it.
    import local_turn
    from scripts.fixture_box import load_bundle
    inv = load_bundle("soc_alert_surface_with_slack")["configured_connectors"]
    assert inv[:-1] == local_turn._CANNED_CONFIGURED_CONNECTORS
    assert inv[-1]["name"] == "slack"
