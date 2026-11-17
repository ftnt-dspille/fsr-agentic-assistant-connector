"""session_health: failures are routed by CODE and by what the model did next.

The report exists to say whose problem a failing tool call is (model/prompt,
tool, box, guard) and whether the model got past it. These pin the two
structural rules it rests on, with no box and no LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from session_analyze import Call, Finding  # noqa: E402
from session_health import classify, failures, render  # noqa: E402


def _call(tool, seq, *, code=None, args=None, turn=0, session="s1"):
    f = [Finding(code=code, message=f"{code} happened")] if code else []
    return Call(session=session, tool=tool, args=args or {}, ok=code is None,
                findings=f, raw_result="{}", turn=turn, seq=seq)


def test_codes_route_to_the_owner():
    assert classify("bad_params") == "model_input"
    assert classify("bad_operation") == "model_input"
    assert classify("offer_not_verified") == "guard"
    assert classify("would_drop_fields") == "guard"
    assert classify("400") == "env"
    assert classify("connector_not_configured") == "env"
    assert classify("KeyError") == "tool_bug"
    assert classify("dispatch_failed") == "tool_bug"


def test_a_failure_fixed_later_in_the_turn_is_recovered_with_the_diff():
    calls = [_call("edit_playbook", 0, code="bad_operation", args={"operations": 1}),
             _call("find", 1),
             _call("edit_playbook", 2, code="bad_operation", args={"operations": 2}),
             _call("edit_playbook", 3, args={"operations": 3, "playbook": "P"})]
    fs = failures(calls)
    assert [f.recovered for f in fs] == [True, True]
    assert fs[0].retries == 2 and fs[0].fixed_by is calls[3]
    md = render(calls, examples=2, source="test")
    assert "+playbook" in md and "~operations: 1 -> 3" in md


def test_a_remote_rejection_fixed_by_new_args_is_an_input_problem():
    calls = [_call("siem_search", 0, code="400", args={"by": "raw", "value": "a OR b"}),
             _call("siem_search", 1, args={"by": "host", "value": "a"})]
    fs = failures(calls)
    assert fs[0].cls == "remote_input" and fs[0].recovered
    md = render(calls, examples=1, source="test")
    assert "~by: raw -> host" in md


def test_a_failure_never_fixed_in_its_turn_is_stuck():
    calls = [_call("siem_search", 0, code="400"),
             _call("siem_search", 1, turn=1)]       # a later TURN does not count
    fs = failures(calls)
    assert len(fs) == 1 and not fs[0].recovered
    md = render(calls, examples=1, source="test")
    assert "never got past" in md and "`s1`" in md
    assert "not a code or prompt fix" in md


def test_dispatch_codes_land_on_the_right_owner():
    """A bad CALL is the model's; a tool that raised after binding is ours."""
    from session_health import classify
    assert classify("invalid_tool_args") == "model_input"
    assert classify("tool_error") == "tool_bug"


def test_the_budget_reds_on_stuck_model_failures_but_not_on_env():
    from session_health import over_budget
    calls = [_call("edit_playbook", 0, code="bad_operation"),     # stuck, model
             _call("siem_search", 1, code="400")]                   # stuck, env
    assert over_budget(calls, max_stuck=1) == []                    # env excluded
    over = over_budget(calls, max_stuck=0)
    assert len(over) == 1 and "edit_playbook:bad_operation x1" in over[0]


def test_the_budget_reds_on_a_tool_failing_too_often():
    from session_health import over_budget
    calls = ([_call("find", i, code="invalid_tool_args") for i in range(2)]
             + [_call("find", i) for i in range(2, 6)])
    assert over_budget(calls, max_fail_rate=0.5) == []              # 2/6 = 33%
    over = over_budget(calls, max_fail_rate=0.25)
    assert over and over[0].startswith("find fails 2/6")
    assert over_budget(calls, max_fail_rate=0.25, min_calls=10) == []


def test_an_error_only_envelope_counts_as_a_failure():
    """`{"error": ...}` has no `ok` key; read as success, ~100 failing calls
    were invisible to every health report."""
    from session_analyze import _findings_of
    ok, fs = _findings_of({"error": "bad arguments for find: ..."})
    assert ok is False and [f.code for f in fs] == ["error_envelope"]
    ok, fs = _findings_of({"ok": False, "message": "no code"})
    assert ok is False and fs[0].code == "error_envelope"
    ok, fs = _findings_of({"ok": True, "result": 1})
    assert ok is True and fs == []


def test_a_guard_redirect_is_a_guard_not_a_stuck_model_input():
    """The duplicate/internal-IP search guards decline with `kind`, not
    `code`; filed as error_envelope they read as 3 stuck model failures."""
    from session_analyze import _findings_of
    ok, fs = _findings_of({"ok": False, "kind": "guard_redirect",
                           "duplicate_search_guard": True,
                           "error": "Skipped: you already searched ..."})
    assert ok is False and fs[0].code == "guard_redirect"
    assert classify(fs[0].code) == "guard"


def test_a_failed_verify_envelope_shows_its_first_required_fix():
    """edit_playbook's failed verify has no top-level error text; its rows
    printed an empty output."""
    from session_analyze import _findings_of
    ok, fs = _findings_of({"ok": False, "ready_to_push": False, "required_fixes": [
        {"code": "bad_value", "message": "`vars.input.params.rec` is not declared"},
        {"code": "bad_value", "message": "second"}]})
    assert ok is False
    assert fs[0].message == "bad_value: `vars.input.params.rec` is not declared (+1 more)"
