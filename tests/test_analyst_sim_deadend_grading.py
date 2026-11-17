"""A dead-end goal is graded by its terminal, not its (impossible) canvas.

An unfinished build should end on a choice card ("not ready yet ... keep going
or stop"). If it instead trails off in prose or dies on a guard refusal, that is
a framework defect -- the one thing this goal measures.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for p in (_SCRIPTS, _SCRIPTS.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from analyst_sim import _grade_deadend  # noqa: E402


def _conv(last_step: dict) -> dict:
    return {"session_id": "s", "steps": [{"index": 0, "kind": "user"}, last_step]}


def test_incomplete_choice_card_is_accepted():
    g = _grade_deadend(
        {"conv": None},
        _conv({"index": 1, "kind": "resume",
               "halt": {"kind": "choice", "key": "build_incomplete"},
               "text": "The playbook is not ready to save yet. 1 item still "
                       "needs attention. Keep working on it, or stop here?"}))
    assert g["excluded"] is True
    assert g["deadend_terminal"]["delivered_card"] is True
    assert g["deadend_terminal"]["incomplete_choice"] is True
    assert g["findings"] == []


def test_capability_gap_card_is_also_accepted():
    # The model that never gets a draft going recognises the missing connector
    # and hands back a capability_gap card -- a clean dead end, not a defect.
    g = _grade_deadend(
        {"conv": None},
        _conv({"index": 1, "kind": "resume",
               "halt": {"kind": "capability_gap", "key": "missing_connector"},
               "text": "No CrowdStrike Falcon connector is in this instance's "
                       "catalog, so host isolation can't be built."}))
    assert g["deadend_terminal"]["delivered_card"] is True
    assert g["findings"] == []


def test_bare_prose_terminal_is_a_framework_finding():
    g = _grade_deadend(
        {"conv": None},
        _conv({"index": 1, "kind": "user",
               "halt": None,
               "text": "I could not find a CrowdStrike Falcon connector, so "
                       "here is the YAML so far:\n```yaml\n...\n```"}))
    assert g["deadend_terminal"]["delivered_card"] is False
    assert any(f["kind"] == "deadend_no_card" for f in g["findings"])
