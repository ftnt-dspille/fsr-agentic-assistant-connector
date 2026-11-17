"""A1 outside-agent vs widget A/B (scripts/eval_ab.py).

The comparison is only worth anything if both sides are asked the same thing,
their playbooks are read out of their own transcripts correctly, ONE grader
scores them, and the table says what the rows said. Each test builds an input
with a known answer and asserts that answer, not that a report came out.
No model, no box.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import eval_ab  # noqa: E402
import eval_canon  # noqa: E402

pytestmark = pytest.mark.skipif(
    eval_canon.canonicalizer() is None,
    reason="framework tooling/ not importable (pinned wheel): no golden tasks")

OUTSIDE_SERVER = "fsrtk"


@pytest.fixture(scope="module")
def tasks():
    return {t["id"]: t for t in eval_ab.load_tasks()}


@pytest.fixture(scope="module")
def g1(tasks):
    return tasks["g1_alert_severity_triage"]


# A playbook that compiles but does not do what g1 asks (no decision at all).
WRONG_YAML = """collection: Wrong
playbooks:
  - name: Wrong
    steps:
      - name: Start
        type: start
        next: Close
      - name: Close
        type: update_record
        module: alerts
        record: "{{ vars.input.records[0]['@id'] }}"
        resource:
          status: Closed
"""


def _widget_transcript(yaml_text):
    return {"transcript": [
        {"type": "tool_use", "id": "t1", "name": "emit_card",
         "input": {"card_type": "playbook_offer", "payload": {"yaml": yaml_text}}},
        {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"ok": True})},
        {"type": "usage", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
         "stop_reason": "end_turn"}]}


def _outside_events(yaml_text, via="tool"):
    name = f"mcp__{OUTSIDE_SERVER}__build_playbook"
    if via == "tool":
        body = [{"type": "tool_use", "id": "u1", "name": name,
                 "input": {"goal": "g", "yaml_text": yaml_text}}]
    else:
        body = [{"type": "text", "text": f"Here it is:\n```yaml\n{yaml_text}\n```"}]
    return [{"type": "assistant", "message": {"content": body}},
            {"type": "result", "num_turns": 1, "usage": {"input_tokens": 1,
                                                          "output_tokens": 1}}]


# ---------------------------------------------------------------- the ask
def test_widget_ask_is_the_spec_as_one_user_message(g1):
    ask = eval_ab.widget_ask(g1)
    assert ask == [{"role": "user", "content": g1["spec"].strip()}]


def test_outside_prompt_carries_the_same_spec_after_the_recipe_preamble(g1):
    p = eval_ab.outside_prompt(g1)
    assert p.endswith(g1["spec"].strip())
    assert "Do NOT" in p and "```yaml" in p        # the recipe's own instructions


def test_a_task_without_a_spec_is_refused():
    with pytest.raises(ValueError):
        eval_ab.widget_ask({"id": "x", "spec": " "})
    with pytest.raises(ValueError):
        eval_ab.outside_prompt({"id": "x"})


# ------------------------------------------------------------- extraction
def test_widget_yaml_prefers_the_offer_card_over_a_prose_fence(g1):
    t = _widget_transcript(g1["real_yaml"])
    t["transcript"].append({"type": "text", "text": "```yaml\nname: prose\n```"})
    y, how = eval_ab.widget_yaml(t)
    assert how == "offer" and y == g1["real_yaml"].strip()


def test_widget_yaml_falls_back_to_the_last_fence_then_to_nothing():
    fenced = [{"type": "text", "text": "a\n```yaml\nname: one\n```\nb\n```yaml\nname: two\n```"}]
    assert eval_ab.widget_yaml(fenced) == ("name: two", "fence")
    assert eval_ab.widget_yaml([{"type": "text", "text": "I could not."}]) == ("", "none")


def test_outside_yaml_takes_the_last_yaml_written_in_document_order(g1):
    ev = _outside_events("name: draft\n")
    ev.insert(1, {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "```yaml\n" + g1["real_yaml"] + "```"}]}})
    y, tools = eval_ab.outside_yaml(ev)
    assert y.strip() == g1["real_yaml"].strip()
    assert tools == ["build_playbook"]


# ------------------------------------------------------------ grader parity
@pytest.mark.parametrize("which", ["right", "wrong"])
def test_the_same_yaml_gets_the_same_grade_through_both_sides(g1, which):
    y = g1["real_yaml"] if which == "right" else WRONG_YAML
    w = eval_ab.row_from_transcript("widget", g1, 1, _widget_transcript(y), {})
    o = eval_ab.row_from_transcript("outside", g1, 1, _outside_events(y), {})
    keys = ("delivered", "draft", "structural", "verified", "verified_strict", "fair_pass")
    assert {k: w[k] for k in keys} == {k: o[k] for k in keys}
    # and the grader is not vacuous: the golden passes, the wrong playbook does not
    assert w["structural"] is (which == "right")


def test_outside_fence_only_delivery_grades_like_a_tool_delivery(g1):
    a = eval_ab.row_from_transcript("outside", g1, 1, _outside_events(g1["real_yaml"]), {})
    b = eval_ab.row_from_transcript("outside", g1, 1,
                                    _outside_events(g1["real_yaml"], via="fence"), {})
    assert (a["structural"], a["verified"]) == (b["structural"], b["verified"]) == (True, True)


def test_no_playbook_is_not_delivered_and_passes_nothing(g1):
    r = eval_ab.row_from_transcript("widget", g1, 1,
                                    {"transcript": [{"type": "text", "text": "sorry"}]}, {})
    assert (r["delivered"], r["draft"], r["structural"], r["verified"]) == (
        False, False, False, False)


# ---------------------------------------------------------------- metrics
def test_widget_and_outside_count_failed_and_empty_calls_the_same_way():
    failed = {"ok": False, "error": {"code": "unknown_connector", "message": "no such"}}
    empty = {"matches": [], "suggestion": "try another name"}
    good = {"matches": [{"name": "x"}]}
    widget = {"transcript": [
        {"type": "tool_use", "id": "a", "name": "find", "input": {}},
        {"type": "tool_result", "tool_use_id": "a", "content": json.dumps(failed)},
        {"type": "tool_use", "id": "b", "name": "find", "input": {}},
        {"type": "tool_result", "tool_use_id": "b", "content": json.dumps(empty)},
        {"type": "tool_use", "id": "c", "name": "find", "input": {}},
        {"type": "tool_result", "tool_use_id": "c", "content": json.dumps(good)},
        {"type": "usage", "input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
         "stop_reason": "end_turn"}]}

    def blocks(i, body):
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": i, "name": f"mcp__{OUTSIDE_SERVER}__find",
                     "input": {}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": i,
                     "content": [{"type": "text", "text": json.dumps(body)}]}]}}]
    outside = (blocks("a", failed) + blocks("b", empty) + blocks("c", good)
               + [{"type": "result", "num_turns": 4, "usage": {
                   "input_tokens": 7, "output_tokens": 3,
                   "cache_read_input_tokens": 100}}])
    w, o = eval_ab.widget_metrics(widget), eval_ab.outside_metrics(outside)
    for m in (w, o):
        assert (m["tool_calls"], m["failed"], m["empty"]) == (3, 1, 1)
        assert m["waste_share"] == pytest.approx(0.667, abs=0.001)
    assert (w["in_tokens"], w["out_tokens"]) == (7, 3)
    # outside input counts cache reads: they are input the model was billed for
    assert (o["in_tokens"], o["in_tokens_uncached"], o["out_tokens"]) == (107, 7, 3)
    assert o["rounds"] == 4


def test_widget_input_tokens_use_the_provider_neutral_total():
    t = [{"type": "usage", "input_tokens": 50, "total_input": 950, "cache_read": 900,
          "output_tokens": 5, "total_tokens": 955, "stop_reason": "end_turn"},
         {"type": "usage", "input_tokens": 20, "output_tokens": 5, "total_tokens": 25,
          "stop_reason": "end_turn"}]
    m = eval_ab.widget_metrics(t)
    assert (m["in_tokens"], m["out_tokens"], m["rounds"]) == (970, 10, 2)


def test_a_tool_error_flag_is_a_failed_call_outside():
    ev = [{"type": "assistant", "message": {"content": [
              {"type": "tool_use", "id": "a", "name": "mcp__x__y", "input": {}}]}},
          {"type": "user", "message": {"content": [
              {"type": "tool_result", "tool_use_id": "a", "is_error": True,
               "content": "boom"}]}}]
    assert eval_ab.outside_metrics(ev)["failed"] == 1


# ---------------------------------------------------------------- the table
def _row(side, task, run, **kw):
    base = dict(delivered=True, delivered_via="offer", draft=True, structural=True,
                verified=True, verified_strict=True, fair_pass=True, rounds=3,
                tool_calls=4, failed=1, empty=0, waste_share=0.25, in_tokens=100,
                out_tokens=20, wall_s=5.0, errors=[])
    base.update(kw)
    return {"side": side, "task": task, "run": run, **base}


def test_table_has_one_line_per_side_task_run_and_the_right_totals():
    rows = [_row("widget", "t1", 1, structural=False, fair_pass=False),
            _row("widget", "t1", 2), _row("outside", "t1", 1),
            _row("outside", "t1", 2, delivered=False, structural=False, verified=False,
                 fair_pass=False)]
    md = eval_ab.render_table(rows, {"model": "m", "runs": 2})
    body = [l for l in md.splitlines() if l.startswith("| widget | t1") or
            l.startswith("| outside | t1")]
    assert len(body) == 4
    s = eval_ab.summarize(rows)
    assert s["widget"]["structural"] == "1/2" and s["outside"]["delivered"] == "1/2"
    assert s["widget"]["tool_calls_total"] == 8 and s["outside"]["in_tokens_total"] == 200
    assert "| widget | 2 | 2/2 | 1/2 |" in md and "| outside | 2 | 1/2 | 1/2 |" in md


def test_a_side_that_was_not_run_has_no_row_and_no_summary():
    s = eval_ab.summarize([_row("widget", "t1", 1)])
    assert list(s) == ["widget"]


# ---------------------------------------------------- the outside command
def test_outside_command_is_strict_scoped_and_write_denied(g1, tmp_path):
    cmd = eval_ab.outside_command(g1, "some-model", tmp_path / ".mcp.json")
    assert cmd[:2] == ["claude", "-p"] and cmd[2] == eval_ab.outside_prompt(g1)
    assert cmd[cmd.index("--model") + 1] == "some-model"
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == f"mcp__{OUTSIDE_SERVER}__*"
    deny = cmd[cmd.index("--disallowedTools") + 1].split(",")
    for w in ("publish_playbook", "create_record", "update_record", "run_playbook_yaml",
              "install_connector", "delete_record"):
        assert f"mcp__{OUTSIDE_SERVER}__{w}" in deny
    assert "build_playbook" not in " ".join(deny)      # the authoring path stays open


def test_box_env_forwards_only_auth_keys_and_demands_creds(tmp_path):
    f = tmp_path / "e"
    f.write_text("FSR_BASE_URL=https://fortisoar.example.com\nFSR_USERNAME=u\n"
                 "FSR_PASSWORD=p\nAGENTLAB_PASSWORD=zzz\nFSR_SSH_KEY_PATH=/k\n")
    got = eval_ab.read_box_env(f)
    assert set(got) == {"FSR_BASE_URL", "FSR_USERNAME", "FSR_PASSWORD"}
    f.write_text("FSR_BASE_URL=https://fortisoar.example.com\n")
    with pytest.raises(SystemExit):
        eval_ab.read_box_env(f)


def test_widget_llm_kind_follows_the_model_unless_told():
    assert eval_ab.widget_llm_kind("claude-haiku-5-5", None) == "anthropic"
    assert eval_ab.widget_llm_kind("coding-b200/eco", None) == "frank"
    assert eval_ab.widget_llm_kind(None, None) == "fake"
    assert eval_ab.widget_llm_kind("claude-x", "frank") == "frank"


# ------------------------------------------- store -> regrade, dry run
def test_dry_outside_run_stores_transcripts_and_regrades_without_rerun(tasks, tmp_path):
    rows = eval_ab.run_all(sides=["outside"], tasks=[tasks["g2_tag_each_indicator"]],
                           runs=2, model=None, llm=None, tools="cassette", out=tmp_path,
                           box_env=None, dry=True)
    assert [(r["side"], r["run"], r["structural"]) for r in rows] == [
        ("outside", 1, True), ("outside", 2, True)]
    again = eval_ab.regrade(tmp_path, list(tasks.values()))
    assert [(r["task"], r["run"], r["structural"], r["verified"]) for r in again] == [
        (r["task"], r["run"], r["structural"], r["verified"]) for r in rows]
    eval_ab.write_results(tmp_path, rows, {"model": "m", "runs": 2})
    doc = json.loads((tmp_path / "results.json").read_text())
    assert len(doc["rows"]) == 2 and (tmp_path / "results.md").read_text().count("outside") >= 3


def test_outside_without_a_box_is_refused_on_a_real_run():
    with pytest.raises(SystemExit) as e:
        eval_ab._main(["--side", "outside", "--model", "m", "--tasks",
                       "g1_alert_severity_triage"])
    assert "no default box" in str(e.value)
