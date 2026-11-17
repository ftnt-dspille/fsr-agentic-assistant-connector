"""analyst-sim grades whether the delivered playbook RUNS, not just its shape.

Requirements read structure, so a reputation gate whose condition reads a path
VirusTotal never returns (`... | default(0) | int >= 1`, always 0) met every
requirement in the sim and was counted good. `does_it_run` re-verifies the
final canvas and walks it offline down every branch; nothing executes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for p in (_SCRIPTS, _SCRIPTS.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# The offline walk ships in the framework after 0.6.63; against an older pinned
# wheel (the CI mirror) skip loudly instead of failing -- re-arms on upgrade.
_ta = pytest.importorskip("fsr_playbooks.mcp_server.tools_analysis")
if not hasattr(_ta, "walk_paths"):
    pytest.skip("installed fsr_playbooks has no offline walk (walk_paths) yet",
                allow_module_level=True)

from analyst_sim import does_it_run  # noqa: E402

_GATE = """
collection: C
playbooks:
  - name: P
    steps:
      - {name: Start, type: start_on_create, module: alerts, next: VT}
      - name: VT
        type: connector
        connector: virustotal
        operation: query_ip
        params: {ip: 192.0.2.10}
        next: Gate
      - name: Gate
        type: decision
        conditions:
          - {display: Bad, when: "{{ (vars.steps.VT.__PATH__ | default(0)) | int >= 1 }}", next: Done}
          - {display: Else, default: true, next: Done}
      - {name: Done, type: end}
"""


def test_a_gate_that_can_never_fire_does_not_run():
    r = does_it_run(_GATE.replace("__PATH__", "data.data.attributes.last_analysis_stats.malicious"))
    assert r["blocking"] or r["empty"]
    assert r["steps_reached"] == r["steps_total"] == 4


def test_the_recorded_path_runs():
    r = does_it_run(_GATE.replace("__PATH__", "data.attributes.last_analysis_stats.malicious"))
    assert r["empty"] == []
    assert not [b for b in r["blocking"] if b["layer"] != "environment"], r["blocking"]


_PARENT_AND_CHILD = """
collection: C
playbooks:
  - name: Block One IP
    parameters: [ip]
    steps:
      - {name: Start, type: start, next: Note}
      - {name: Note, type: set_variable, vars: {ip: "{{ vars.input.params.ip }}"}}
  - name: Alert Block
    steps:
      - {name: Start, type: start_on_create, module: alerts, next: Call}
      - {name: Call, type: workflow_reference, target: Block One IP, ip: "{{ vars.input.records[0].sourceIp }}"}
"""


def test_a_delivered_parent_and_child_is_flagged_as_dropped_by_apply():
    # The designer Apply stages collection.workflows[0] only: the child lands
    # on the canvas and the parent is gone, with ok:true.
    from analyst_sim import grade
    g = grade({"conv": {"steps": [], "session_id": "s"}, "canvas": _PARENT_AND_CHILD, "mounted": "x"}, {}, {})
    kinds = [(f["layer"], f.get("kind")) for f in g["findings"]]
    assert ("framework", "apply_drops_playbooks") in kinds, kinds


def test_one_playbook_is_not_flagged():
    from analyst_sim import grade
    one = _GATE.replace("__PATH__", "data.attributes.last_analysis_stats.malicious")
    g = grade({"conv": {"steps": [], "session_id": "s"}, "canvas": one, "mounted": "x"}, {}, {})
    assert "apply_drops_playbooks" not in [f.get("kind") for f in g["findings"]]


def test_a_step_the_walk_never_reaches_is_reported_not_crashed_on():
    # Live: grade() read runs['never_reached'], which does_it_run never set;
    # the first partially-walked canvas raised KeyError after an hour of
    # sessions and nothing was written.
    from analyst_sim import grade
    orphan = _GATE.replace("__PATH__", "data.attributes.last_analysis_stats.malicious") \
        + "      - {name: Orphan, type: set_variable, vars: {x: 1}}\n"
    r = does_it_run(orphan)
    assert "never_reached" in r
    g = grade({"conv": {"steps": [], "session_id": "s"}, "canvas": orphan, "mounted": ""}, {}, {})
    assert "grader_crashed" not in [f.get("kind") for f in g["findings"]]


def test_a_grader_crash_costs_one_session_not_the_run(monkeypatch):
    import analyst_sim
    monkeypatch.setattr(analyst_sim, "grade", lambda *a, **k: {}["boom"])
    g = analyst_sim._grade_safely({"goal": "g", "canvas": ""}, {}, {})
    assert [f["kind"] for f in g["findings"]] == ["grader_crashed"]


def test_sessions_the_gateway_never_answered_are_not_scored():
    # Live: an overnight VPN drop timed out 9/9 sessions and the report said
    # "requirements met 0/9", which reads like a result.
    from analyst_sim import grade, report
    dead = {"goal": "g", "voice": "vague", "model": "m", "run": 1, "secs": 0,
            "conv": None, "canvas": "", "mounted": "",
            "drive_error": "APITimeoutError: Request timed out.",
            "analyst": {"verdict": None, "moves": [], "model": "m"}}
    g = grade(dead, {}, {})
    assert g["excluded"] is True
    text = report([g])
    assert "0 session(s)" in text and "1 of 1 session(s) not scored" in text
    assert "APITimeoutError" in text


def test_an_unrecorded_box_call_is_the_harness_not_the_tool():
    # Live: why_did_playbook_fail died against the sealed port and the report
    # filed it under `tool`; the offline world simply had no runs recorded.
    from analyst_sim import _layer_for
    sealed = ("why_did_playbook_fail failed internally (ConnectionError: "
              "HTTPSConnectionPool(host='127.0.0.1', port=9): Max retries exceeded")
    assert _layer_for("tool_bug", sealed) == "harness"
    assert _layer_for("tool_bug", "REPLAY-MISS: no recorded response") == "harness"
    assert _layer_for("tool_bug", "KeyError: 'steps'") == "tool"


def test_the_report_totals_assistant_tokens_per_session_and_run():
    from analyst_sim import grade, report
    usage = lambda i, o, c: {"type": "usage", "input_tokens": i, "output_tokens": o, "cache_read": c}
    s = {"goal": "g", "voice": "vague", "model": "m", "run": 1, "secs": 3,
         "conv": {"session_id": "c", "steps": [
             {"transcript": [usage(10_000, 500, 8_000), {"type": "text"}]},
             {"transcript": {"ok": True, "transcript": [usage(30_000, 1_500, 20_000)]}}]},
         "canvas": "", "mounted": "", "drive_error": None,
         "analyst": {"verdict": None, "moves": [], "model": "m"}}
    text = report([grade(s, {}, {})])
    assert "- tokens: 40k in (28k cached) · 2k out · 2 LLM call(s)" in text
    assert "assistant tokens: 40k in (28k cached) · 2k out · 2 LLM call(s)" in text


def test_a_create_offer_after_the_analyst_picked_create_new_is_not_a_finding():
    from analyst_sim import grade
    def run(steps):
        r = {"goal": "g", "voice": "vague", "model": "m", "run": 1, "secs": 1,
             "conv": {"session_id": "c", "steps": steps}, "canvas": "",
             "mounted": "playbooks: []", "drive_error": None,
             "analyst": {"verdict": None, "moves": [], "model": "m"}}
        return [f["kind"] for f in grade(r, {}, {})["findings"]]
    offer = {"index": 2, "kind": "resume", "halt": {"kind": "playbook_offer"},
             "sent": {}, "transcript": {"transcript": []}}
    pick = {"index": 1, "kind": "resume", "halt": None, "transcript": {"transcript": []},
            "sent": {"decision": "choice", "choice_id": "playbook_scope", "value": "create_new"}}
    assert "create_offer_in_designer" in run([offer])
    assert "create_offer_in_designer" not in run([pick, offer])


# A playbook that computes a verdict and then writes a literal is the failure
# the effect inventory exists to catch: it compiles clean, walks clean, meets a
# structural "has an update_record" requirement -- and silently drops the work.
_DROPS_VERDICT = """
collection: C
playbooks:
  - name: P
    steps:
      - {name: Start, type: start_on_create, module: alerts, next: Decide}
      - {name: Decide, type: set_variable, vars: {verdict: "{{ vars.input.records[0].name }}"}, next: Write}
      - {name: Write, type: create_record, module: alerts, resource: {name: n, description: "malicious"}, next: Done}
      - {name: Done, type: end}
"""

_LINKS_VERDICT = _DROPS_VERDICT.replace(
    'description: "malicious"', 'description: "{{ vars.steps.Decide.verdict }}"')


def test_a_verdict_written_as_a_literal_is_an_effect_finding():
    from analyst_sim import grade
    def kinds(canvas, goal):
        r = {"goal": "g", "voice": "vague", "model": "m", "run": 1, "secs": 1,
             "conv": {"session_id": "c", "steps": []}, "canvas": canvas,
             "mounted": "", "drive_error": None,
             "analyst": {"verdict": None, "moves": [], "model": "m"}}
        return [f.get("kind") for f in grade(r, goal, {})["findings"]]
    write_goal = {"requirements": [{"kind": "step_type_present",
                                    "type": "create_record"}]}
    # drops the verdict -> an effect finding; links it -> none
    assert "effect" in kinds(_DROPS_VERDICT, write_goal)
    assert "effect" not in kinds(_LINKS_VERDICT, write_goal)


def test_no_effect_is_only_a_finding_when_the_goal_expects_one():
    from analyst_sim import grade
    # two non-trivial steps, no connector and no write -> genuinely no effect.
    triage = """
collection: C
playbooks:
  - name: P
    steps:
      - {name: Start, type: start_on_create, module: alerts, next: A}
      - {name: A, type: set_variable, vars: {v: "x"}, next: B}
      - {name: B, type: set_variable, vars: {w: "{{ vars.v }}"}, next: Done}
      - {name: Done, type: end}
"""
    def kinds(goal):
        r = {"goal": "g", "voice": "vague", "model": "m", "run": 1, "secs": 1,
             "conv": {"session_id": "c", "steps": []}, "canvas": triage,
             "mounted": "", "drive_error": None,
             "analyst": {"verdict": None, "moves": [], "model": "m"}}
        return [f.get("kind") for f in grade(r, goal, {})["findings"]]
    # a goal that asked for a write -> the missing effect is a finding
    assert "effect" in kinds({"requirements": [
        {"kind": "step_type_present", "type": "update_record"}]})
    # a pure-triage goal (decision only) -> no effect expected, no finding
    assert "effect" not in kinds({"requirements": [
        {"kind": "step_type_present", "type": "decision"}]})
