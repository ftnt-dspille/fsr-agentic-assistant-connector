"""premise_advisory_rate: the live rate of the card advisory is read from the
stored verdict cards, per turn that ended in a verdict."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from premise_advisory_rate import collect, render  # noqa: E402


def _card(adv=None, disp="true_positive", status="unread_exists"):
    premise = {"status": status}
    if adv is not None:
        premise["advisory"] = adv
    return {"type": "verdict_card", "disposition": disp, "premise": premise}


def _hist(*turns):
    return {"turns": [{"transcript": list(t)} for t in turns]}


def test_the_rate_counts_turns_that_ended_in_a_verdict():
    h = [("s1", _hist(
        [{"type": "text"}, _card([{"kind": "change", "state": "unread"}])],
        [{"type": "text"}],                                   # no verdict: not counted
        [_card(None, status="read")],
        [_card([{"kind": "asset", "state": "conflict"}], status="contradicted")])),
        ("s2", _hist([_card([{"kind": "change", "state": "unread"}], disp="benign")]))]
    res = collect(h)
    assert res["verdict_turns"] == 4 and res["shown"] == 3
    assert res["rate"] == 0.75
    assert res["by_kind_state"] == {"asset/conflict": 1, "change/unread": 2}
    assert res["by_disposition"] == {"benign": 1, "true_positive": 2}


def test_a_turn_with_two_cards_counts_the_last_one():
    res = collect([("s", _hist([_card([{"kind": "change", "state": "unread"}]),
                                _card(None, status="read")]))])
    assert res["verdict_turns"] == 1 and res["shown"] == 0


def test_no_verdict_turns_is_no_rate_not_zero_division():
    res = collect([("s", _hist([{"type": "text"}]))])
    assert res["rate"] is None and "verdict turns: 0" in render(res)


def test_malformed_advisories_are_not_counted():
    for bad in ("x", [None, {"kind": "change"}, {"state": "unread"}]):
        assert collect([("s", _hist([_card(bad)]))])["shown"] == 0


def _pre(card, **review):
    card["premise"]["preread"] = review
    return card


def test_the_shadow_pre_read_hold_is_counted_by_verdict_and_names_its_record():
    res = collect([("s", _hist(
        [_pre(_card(disp="true_positive"), hold=True, relevant=["change:CHG1"])],
        [_pre(_card(disp="benign"), hold=False, relevant=[])],
        [_pre(_card(disp="false_positive"), hold=True, relevant=[], conflict=["change"])],
        [_card(disp="benign")]))])
    assert res["preread_turns"] == 3 and res["would_hold"] == 2
    assert res["hold_by_disposition"] == {"false_positive": 1, "true_positive": 1}
    out = render(res)
    assert "would hold (shadow): 2" in out
    assert "on change:CHG1" in out and "on conflict:change" in out


def test_a_hold_with_no_record_is_called_out():
    res = collect([("s", _hist([_pre(_card(), hold=True, relevant=[])]))])
    assert "NO REASON" in render(res)
