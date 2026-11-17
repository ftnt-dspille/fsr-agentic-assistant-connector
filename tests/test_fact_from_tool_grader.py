"""`fact_from_tool` must be able to FAIL, case by case.

A provenance grader that cannot go red manufactures confidence. Each case is a
synthetic one-turn conversation with a known answer:
  PASS  the value is in a hunt result, shown to the analyst, absent from the record;
  FAIL  recited (shown, in no result), dropped (in a result, never shown),
        record-only (in the mounted alert), and in-record-and-result.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

from scripts.local_conversation import Conversation, Step  # noqa: E402
from scripts import conv_scenarios as CS  # noqa: E402
from scripts import chat_sweep  # noqa: E402

NONCE = "FAZ-ONLY-9b4e27d1c08f"
TOOLS = ("faz_search", "run_op")
RECORD = {"@id": "/api/3/alerts/abc", "title": "Traffic from 10.50.60.70",
          "sourceIp": "10.50.60.70", "destinationIp": "198.51.100.9"}
HIT = json.dumps({"ok": True, "events": [{"src": "10.50.60.70",
                                          "dst": "198.51.100.77",
                                          "msg": f"staged upload {NONCE}"}]})
MISS = json.dumps({"ok": True, "events": []})


def _arc(text, *, results=(), tool="faz_search", mounted=RECORD):
    """One step: a tool_use, one tool_result per body, then the prose answer."""
    frames = [{"type": "tool_use", "id": "t1", "name": tool, "input": {"by": "ip"}}]
    for body in results:
        frames.append({"type": "tool_result", "tool_use_id": "t1", "content": body})
    frames.append({"type": "text", "text": text})
    s = Step(index=1, kind="user", sent="ask")
    s.transcript = {"transcript": frames, "stop_reason": "end_turn"}
    s.text = text
    s.tools = [tool]
    return Conversation(name="t", steps=[s], mounted=mounted)


def _grade(conv, **kw):
    kw.setdefault("values", (NONCE,))
    kw.setdefault("tools", TOOLS)
    ok, why = CS.fact_from_tool(**kw)(CS.ConvCtx(conv=conv, mounted_yaml=""))
    return ok, why


# -- PASS ------------------------------------------------------------------
def test_pass_a_hunt_fact_that_was_shown_and_is_not_in_the_record():
    ok, why = _grade(_arc(f"The FAZ hunt found {NONCE} on 198.51.100.77.",
                          results=[HIT]))
    assert ok, why


# -- FAIL: recited (shown, but no tool returned it) -------------------------
def test_fail_recited_value_shown_with_no_tool_result():
    ok, why = _grade(_arc(f"The logs show {NONCE}.", results=[MISS]))
    assert not ok and "recited" in why


def test_fail_recited_value_with_no_tool_call_at_all():
    conv = _arc(f"The logs show {NONCE}.", results=[])
    ok, why = _grade(conv)
    assert not ok and "recited" in why


# -- FAIL: dropped (a tool returned it, the analyst never saw it) ------------
def test_fail_value_returned_by_a_tool_but_never_shown():
    ok, why = _grade(_arc("Nothing notable in the logs.", results=[HIT]))
    assert not ok and "never shown" in why


# -- FAIL: record-only (the alert already said it) ---------------------------
def test_fail_value_only_in_the_mounted_record():
    rec = dict(RECORD, comment=f"analyst note {NONCE}")
    ok, why = _grade(_arc(f"The alert notes {NONCE}.", results=[MISS], mounted=rec))
    assert not ok


def test_fail_value_in_both_record_and_hunt_result():
    rec = dict(RECORD, comment=NONCE)
    ok, why = _grade(_arc(f"Found {NONCE}.", results=[HIT], mounted=rec))
    assert not ok and "mounted record" in why


# -- FAIL: the result came from a tool that is not one of the named tools -----
def test_fail_result_from_an_unnamed_tool_does_not_count():
    ok, why = _grade(_arc(f"Found {NONCE}.", results=[HIT], tool="get_record"),
                     tools=("faz_search",))
    assert not ok and "recited" in why


# -- Mounted-record requirement ---------------------------------------------
def test_fail_loudly_without_a_mounted_record_when_absence_is_asked():
    ok, why = _grade(_arc(f"Found {NONCE}.", results=[HIT], mounted=None))
    assert not ok and "no mounted record" in why


def test_absence_check_can_be_switched_off():
    rec = dict(RECORD, comment=NONCE)
    ok, why = _grade(_arc(f"Found {NONCE}.", results=[HIT], mounted=rec),
                     absent_from_record=False)
    assert ok, why


def test_any_one_of_several_values_is_enough():
    ok, why = _grade(_arc(f"Found {NONCE}.", results=[HIT]),
                     values=("198.51.100.9", NONCE))
    assert ok, why


# -- Construction and the sweep vocabulary -----------------------------------
def test_empty_values_or_tools_are_refused():
    with pytest.raises(ValueError):
        CS.fact_from_tool(values=(), tools=TOOLS)
    with pytest.raises(ValueError):
        CS.fact_from_tool(values=(NONCE,), tools=())


def test_sweep_expectation_parses_and_rejects_bad_shapes():
    grader = chat_sweep._fact_from_tool({"values": [NONCE], "tools": ["faz_search"]})
    assert callable(grader)
    with pytest.raises(ValueError):
        chat_sweep._fact_from_tool({"values": [NONCE]})          # no tools
    with pytest.raises(ValueError):
        chat_sweep._fact_from_tool({"values": [], "tools": ["x"]})
    with pytest.raises(ValueError):
        chat_sweep._fact_from_tool({"values": [NONCE], "tools": ["x"], "oops": 1})
    assert "fact_from_tool" in chat_sweep.EXPECTATIONS


def test_s5_pivot_reports_are_not_already_in_the_mounted_alert():
    """The S5 invest-pivot row passed from the record alone when its reports
    list named a value the mounted alert already carried. A `reports` value
    must be one only the hunt could return."""
    import json as _json

    from scripts.fixture_box import FixtureBox, load_bundle
    row = _json.loads((_ROOT / "scripts/conversations/sweep/"
                       "invest_pivot_on_source_ip.json").read_text())
    box = FixtureBox(load_bundle(row["records"]))
    record = _json.dumps(box.record(row["mount"]["module"], row["mount"]["uuid"]),
                         default=str).lower()
    expect = row["expect"]["reports"]
    values = expect.get("any_of", []) + expect.get("all_of", [])
    assert values
    leaked = [v for v in values if v.lower() in record]
    assert not leaked, f"reports values already in the mounted alert: {leaked}"
