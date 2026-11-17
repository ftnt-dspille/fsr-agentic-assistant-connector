"""Eval graders see the constituent a union call ran (B4 bug class).

The model calls `emit_card(card_type=...)` / `find(kind=...)`; S1/S3/conv
graders ask for `emit_playbook_offer` / `find_containment_actions`. Reading
raw names made the grader blind to the call it grades.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import eval_canon  # noqa: E402

pytestmark = pytest.mark.skipif(
    eval_canon.canonicalizer() is None,
    reason="framework tooling/evals/scoring.py not importable (pinned wheel)")

_YAML = "name: Block IP\nsteps:\n  start: {type: start}\n"


def _call(name, args):
    return {"type": "tool_call", "name": name, "args": args}


def test_emit_card_offer_is_seen_as_playbook_offer():
    t = [_call("emit_card", {"card_type": "playbook_offer",
                             "payload": {"yaml": _YAML, "title": "Block IP"}})]
    assert "emit_playbook_offer" in eval_canon.tool_names(t)
    assert eval_canon.offered_yaml(t) == _YAML.strip()


def test_find_action_is_seen_as_containment_lookup():
    t = [_call("find", {"kind": "action", "query": "block ip"})]
    assert eval_canon.tool_names(t) == ["find_containment_actions"]


def test_last_offer_wins_and_no_offer_is_empty():
    first = _call("emit_card", {"card_type": "playbook_offer",
                                "payload": {"yaml": "name: old\n"}})
    last = _call("emit_card", {"card_type": "playbook_offer",
                               "payload": {"yaml": _YAML}})
    assert eval_canon.offered_yaml([first, last]) == _YAML.strip()
    assert eval_canon.offered_yaml([_call("find", {"kind": "action"})]) == ""
