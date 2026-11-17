"""Turn diagnosis (scripts/turn_diagnosis.py): the report a person or a coding
agent reads to see where one agent turn lost time -- and the `make local-turn`
front door that prints it.

Each test builds a transcript with a known defect and asserts the diagnosis
names THAT defect, not merely that a report came out.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import turn_diagnosis as td  # noqa: E402


def _use(i, name, **args):
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}


def _res(i, body):
    return {"type": "tool_result", "tool_use_id": f"t{i}", "content": json.dumps(body)}


def _usage(tokens=1000, stop="tool_calls"):
    return {"type": "usage", "total_tokens": tokens, "stop_reason": stop}


# A build turn with every defect class in it, one per round.
BAD_TURN = [
    _use(1, "find", kind="operation", connector="VirusTotal"),
    _res(1, {"matches": [], "suggestion": "check the connector name"}),
    _usage(),
    _use(2, "find", kind="step", query="update record"),
    _res(2, {"kind": "step", "results": []}),
    _usage(),
    _use(3, "find", kind="step", query="record"),
    _res(3, {"kind": "step", "results": []}),
    _usage(),
    _use(4, "find", kind="step", query="set variable"),
    _res(4, {"kind": "step", "results": []}),
    _usage(),
    _use(5, "compose_playbook", steps=[{"kind": "start"}]),
    _res(5, {"ok": False, "code": "bad_plan", "message": "unknown kind 'start'"}),
    _usage(),
    _use(6, "compose_playbook", steps=[{"kind": "connector"}]),
    _res(6, {"ok": True, "yaml": "playbooks: []"}),
    _usage(),
    _use(7, "find", kind="operation", connector="virustotal"),
    _res(7, {"matches": [{"op_name": "query_ip"}]}),
    _usage(stop="create_delivery_forced"),
    {"type": "playbook_offer", "id": "offer-1"},
    {"type": "text", "text": "Here is the playbook."},
]


def _by_round(dx):
    return {c.round: c for c in dx.calls}


def test_each_call_is_classed_ok_empty_or_failed():
    calls = _by_round(td.diagnose(BAD_TURN))
    assert {n: c.status for n, c in calls.items()} == {
        1: "empty", 2: "empty", 3: "empty", 4: "empty",
        5: "failed", 6: "ok", 7: "ok"}
    assert (calls[5].code, calls[5].layer) == ("bad_plan", "model_input")


def test_recovery_is_per_tool_and_only_counts_a_later_success():
    calls = _by_round(td.diagnose(BAD_TURN))
    # find and compose each succeed LATER, so their earlier misses recovered.
    assert all(calls[n].recovered for n in (1, 2, 3, 4, 5))
    # Cut the turn before either success: nothing recovered.
    cut = _by_round(td.diagnose(BAD_TURN[:15]))
    assert not any(cut[n].recovered for n in (1, 2, 3, 4, 5))


def test_totals_and_wasted_share():
    dx = td.diagnose(BAD_TURN, offered=["find", "compose_playbook", "picklist"])
    assert dx.model_rounds == 7
    assert dx.tokens == 7000
    assert dx.cards == ["playbook_offer"]
    assert dx.wasted_share == 5 / 7
    assert dx.unused == ["picklist"]


def test_a_loop_intervention_is_reported_and_a_provider_stop_is_not():
    dx = td.diagnose(BAD_TURN)
    assert dx.interventions == ["create_delivery_forced"]
    clean = [f if f.get("stop_reason") != "create_delivery_forced" else _usage()
             for f in BAD_TURN]
    assert td.diagnose(clean).interventions == []


def test_envelope_guards_are_interventions_too():
    dx = td.diagnose({"transcript": [_usage()],
                      "tags": {"guards_fired": ["verdict_guard_forced"]}})
    assert dx.interventions == ["verdict_guard_forced"]


def test_report_names_every_defect():
    dx = td.diagnose(BAD_TURN, offered=["find", "compose_playbook", "picklist"],
                     env_gaps=["https://fixture-box.invalid/api/3/x"])
    out = td.render(dx, title="t")
    # The four empty step/operation searches: three identical-status in a row
    # is the streak (round 1 is also empty, so the run is 4 long).
    assert "REPEAT: find came back empty 4 times in a row" in out
    assert "FAILED: compose_playbook [bad_plan] x1, layer: model_input, recovered" in out
    assert "EMPTY: find x4" in out
    assert "LOOP STEPPED IN: create_delivery_forced" in out
    assert "Never used: picklist" in out
    assert "https://fixture-box.invalid/api/3/x" in out
    assert "OUTCOME" not in out            # a card WAS delivered


def test_no_card_is_called_out():
    out = td.render(td.diagnose(BAD_TURN[:6]), title="t")
    assert "delivered: NOTHING" in out
    assert "OUTCOME: the turn ended without delivering a card" in out


def test_an_errored_turn_is_not_reported_as_a_delivery():
    """With the model gateway down the report said `delivered: error`."""
    dx = td.diagnose([{"type": "error", "message": "Connection error."}])
    assert dx.cards == [] and dx.errors == ["Connection error."]
    out = td.render(dx, title="t")
    assert "delivered: NOTHING" in out
    assert "THE TURN ERRORED" in out and "Connection error." in out


def _verdict_turn(*dispositions):
    return [{"type": "verdict_card", "disposition": d} for d in dispositions]


def test_the_answer_is_checked_against_the_known_answer():
    right = td.diagnose(_verdict_turn("true_positive"))
    assert right.answer(["true_positive"]) == (
        True, "verdict 'true_positive' is RIGHT; expected one of ['true_positive']")
    wrong = td.diagnose(_verdict_turn("false_positive"))
    assert wrong.answer(["true_positive"])[0] is False
    assert "WRONG" in td.render(wrong, title="t", expect_verdict=["true_positive"])


def test_the_last_delivered_verdict_is_the_answer():
    dx = td.diagnose(_verdict_turn("false_positive", "True_Positive"))
    assert dx.verdict == "true_positive"


def test_no_verdict_is_a_wrong_answer_not_a_pass():
    ok, why = td.diagnose(BAD_TURN).answer(["true_positive"])
    assert ok is False and "no verdict delivered" in why


def test_an_emit_card_call_is_not_a_delivered_verdict():
    """A refused emit_card delivers nothing; only the wire frame counts."""
    turn = [_use(1, "emit_card", card_type="verdict",
                 payload={"disposition": "true_positive"}),
            _res(1, {"ok": False, "code": "invalid_evidence_ids"}), _usage()]
    assert td.diagnose(turn).verdict is None


def test_a_call_the_world_could_not_serve_is_not_charged_to_the_agent():
    """A strict known-answer world refuses ops it has no recording for. Those
    were listed as model mistakes and counted into the wasted share."""
    turn = [_use(1, "edr_host", host="x"),
            _res(1, {"ok": False, "code": "unserved_op", "message": "not recorded"}),
            _use(2, "get_record", module="alerts"),
            _res(2, {"ok": True, "record": {"a": 1}}), _usage()]
    dx = td.diagnose(turn)
    assert dx.calls[0].layer == "env"
    assert dx.wasted_share == 0.0
    out = td.render(dx, title="t")
    assert "ENVIRONMENT: edr_host [unserved_op] x1" in out
    assert "FAILED: edr_host" not in out


def test_empty_means_no_rows_not_no_lists():
    assert td.is_empty({"results": []})
    assert td.is_empty({"count": 0, "records": []})
    assert td.is_empty({"matches": [], "suggestion": "x" * 500})
    assert td.is_empty([])
    # Rows came back.
    assert not td.is_empty({"results": [{"a": 1}]})
    # No list at all: an acknowledgement or a document, not a search miss.
    assert not td.is_empty({"ok": True, "yaml": "playbooks: []"})
    # Advisory lists do not count as rows, in either direction.
    assert td.is_empty({"results": [], "warnings": [{"code": "x"}]})
    assert not td.is_empty({"ok": True, "warnings": []})
    # An empty list beside a real payload is not an empty result.
    assert not td.is_empty({"steps": [], "markdown": "# doc\n" + "x" * 400})
    assert not td.is_empty({"steps": [], "resolved": {"type": "start"}})


def test_result_without_an_id_pairs_first_in_first_out():
    turn = [{"type": "tool_use", "name": "find", "input": {}},
            {"type": "tool_use", "name": "get_record", "input": {}},
            {"type": "tool_result", "content": json.dumps({"results": []})},
            {"type": "tool_result", "content": json.dumps({"ok": True, "record": {"a": 1}})},
            _usage()]
    assert [(c.name, c.status) for c in td.diagnose(turn).calls] == [
        ("find", "empty"), ("get_record", "ok")]


def test_a_call_that_never_got_a_result_is_not_counted_ok():
    dx = td.diagnose([_use(1, "find", kind="x"), _usage()])
    assert dx.calls[0].status == "no_result"


def test_budget():
    dx = td.diagnose(BAD_TURN)
    assert td.over_budget(dx, max_rounds=7, max_wasted=0.8) == []
    assert len(td.over_budget(dx, max_rounds=6, max_wasted=0.5)) == 2


def _run_front_door(tmp_path, *extra):
    """Run the script the way `make local-turn` does: a fresh interpreter, the
    script path as argv[0], cwd NOT the repo. (Importing it, as the other
    local_turn tests do, puts the repo on sys.path and hides an import that
    only works from there.)"""
    env = {**os.environ, "FSRPB_DEV": "1"}
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "local_turn.py"), "--intent", "build",
         "--llm", "fake", "--persona", "none", "--tools", "cassette",
         "--message", "build a playbook", "--diagnose", *extra],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)


def test_front_door_runs_as_a_script_and_prints_the_diagnosis(tmp_path):
    p = _run_front_door(tmp_path)
    assert p.returncode == 0, p.stderr[-2000:]
    assert "# Turn diagnosis: local intent=build" in p.stdout
    assert "## Findings" in p.stdout


def test_front_door_runs_a_scenario_and_fails_a_turn_with_no_verdict(tmp_path):
    """The fake model delivers no verdict, so a known-answer scenario must
    exit non-zero: the scenario's world and ask were loaded, and the answer
    was checked."""
    p = _run_front_door(tmp_path, "--scenario", "ka_c2_beacon")
    assert "scenario=ka_c2_beacon" in p.stdout, p.stderr[-1500:]
    assert "intent=triage" in p.stdout
    assert "ANSWER: no verdict delivered" in p.stdout
    assert p.returncode == 1


def test_front_door_exits_nonzero_over_budget(tmp_path):
    # The fake turn makes at least one model round; a budget of 0 is broken.
    p = _run_front_door(tmp_path, "--max-rounds", "0")
    assert p.returncode == 1, p.stdout[-1500:] + p.stderr[-1500:]
    assert "OVER BUDGET: rounds" in p.stdout


# ── what the model was given, and every guard that fired ────────────────────

def _inputs(*tools, model="m"):
    return {"type": "turn_inputs", "model": model, "tools": list(tools),
            "intent": "build", "system_chars": 10, "slices": []}


def test_offered_is_read_from_the_transcript_when_not_passed():
    dx = td.diagnose([_inputs("find", "compose_playbook", "picklist")]
                     + BAD_TURN)
    assert dx.offered == ["find", "compose_playbook", "picklist"]
    assert dx.unused == ["picklist"]
    assert dx.model == "m"
    out = td.render(dx, title="t")
    assert "Never used: picklist" in out
    assert "model m | 3 tools offered" in out


def test_an_explicit_offer_wins_over_the_recorded_one():
    dx = td.diagnose([_inputs("find")] + BAD_TURN, offered=["picklist"])
    assert dx.offered == ["picklist"]


def test_the_turn_inputs_frame_is_not_a_delivered_card():
    dx = td.diagnose([_inputs("find")] + BAD_TURN)
    assert dx.cards == ["playbook_offer"]


def test_a_transcript_without_the_record_reports_the_offer_as_unknown():
    """Stored before the frame existed: unknown, not 'no tools were unused'."""
    dx = td.diagnose(BAD_TURN)
    assert dx.offered == [] and not dx.offered_known
    out = td.render(dx, title="t")
    assert "tools offered: unknown" in out
    assert "TOOLS:" not in out


def test_an_empty_recorded_offer_is_known_and_empty():
    dx = td.diagnose([_inputs()] + BAD_TURN[:3])
    assert dx.offered_known and dx.offered == []


def test_every_guard_firing_is_listed_with_its_count_and_kind():
    env = {"transcript": [_usage()],
           "tags": {"guards_fired": ["dispatch.not_in_slice", "triage.hunt_floor",
                                     "dispatch.not_in_slice",
                                     "connector.salvage_build_offer"]}}
    dx = td.diagnose(env)
    assert dx.guard_counts == [("dispatch.not_in_slice", 2),
                               ("triage.hunt_floor", 1),
                               ("connector.salvage_build_offer", 1)]
    out = td.render(dx, title="t")
    assert "GUARD FIRED: dispatch.not_in_slice x2 (dispatch_refusal:" in out
    assert "GUARD FIRED: triage.hunt_floor (triage_redirect:" in out
    # Connector-side names are not in the framework registry: listed, no note.
    assert "GUARD FIRED: connector.salvage_build_offer." in out


def test_a_guard_and_a_loop_marker_are_not_listed_as_one_thing():
    env = {"transcript": BAD_TURN, "tags": {"guards_fired": ["CreateDeliveryGuard"]}}
    out = td.render(td.diagnose(env), title="t")
    assert "LOOP STEPPED IN: create_delivery_forced" in out
    assert "GUARD FIRED: CreateDeliveryGuard" in out


def test_a_clean_check_with_no_findings_is_not_an_empty_lookup():
    """test_template returns `findings: []` next to a rendered document; it was
    reported EMPTY and charged to the agent as a wasted call."""
    ok = {"ok": True, "findings": [], "has_errors": False,
          "rendered": "snmp community rotate"}
    dx = td.diagnose([_use(1, "test_template", script="x"), _res(1, ok), _usage()])
    assert dx.calls[0].status == "ok"
