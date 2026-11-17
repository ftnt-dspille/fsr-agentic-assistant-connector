"""N1: the LIVE grader checks the answer, with the offline checks unchanged.

`live_rows.run_row` builds a Conversation from the real turn transcript and
`grade` runs the offline `verdict_is` / `planted_token` / `fact_from_tool`
implementations on it. The planted-token rule: the token must be in a tool
RESULT of the turn and in what the analyst was shown. No model is involved:
the fake box returns a recorded envelope.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts import live_rows as LR  # noqa: E402

TOKEN = "NDL-0a1b2c3d4e5f"
SPEC = {"name": "snow_needle_row", "intent": "triage", "planted": {"scenario": "snow_incident_needle"},
        "steps": [{"user": "Did anyone report a large transfer from build-07?"}],
        "expect": {"planted_token": {"labels": ["needle"]},
                   "verdict_is": {"any_of": ["true_positive", "suspicious"]}}}
PLANTED = {"needle": TOKEN}


class _Box:
    """Answers the single chat turn with a recorded frame list."""
    def __init__(self, frames):
        self._frames = frames
        self.client = SimpleNamespace(get=lambda path: {})

    def execute(self, op, params, timeout=None):
        return SimpleNamespace(error=None, data={"ok": True, "stop_reason": "end_turn",
                                                 "transcript": self._frames})


def _turn(*, tool_returns, answer_has_token=True, disposition="true_positive"):
    body = f"incident INC001 {TOKEN} outbound transfer" if tool_returns else "no incidents found"
    text = f"A ticket {TOKEN} reports it." if answer_has_token else "Nothing to report."
    return [
        {"type": "tool_use", "id": "t1", "name": "run_op", "input": {"operation": "search_record"}},
        {"type": "tool_result", "tool_use_id": "t1", "content": body},
        {"type": "text", "text": text},
        {"type": "verdict_card", "disposition": disposition, "summary": text,
         "findings": [{"claim": text}]},
    ]


def _grade(frames, spec=SPEC, planted=PLANTED):
    run = LR.run_row(_Box(frames), spec, planted)
    assert run["error"] is None
    return LR.grade(spec, run["said"], run["tools"], run["conv"])


def test_token_through_a_tool_result_passes():
    g = _grade(_turn(tool_returns=True))
    assert g["ok"], g["answer_checks"]
    assert g["answer_checks"]["planted_token"]["ok"] and g["answer_checks"]["verdict_is"]["ok"]


def test_same_row_fails_when_the_tool_is_withheld():
    """The answer still names the token (recited), but no tool result holds it."""
    g = _grade(_turn(tool_returns=False))
    assert not g["ok"]
    why = g["answer_checks"]["planted_token"]["why"]
    assert not g["answer_checks"]["planted_token"]["ok"] and "recited" in why


def test_a_token_the_tool_returned_but_the_answer_dropped_fails():
    g = _grade(_turn(tool_returns=True, answer_has_token=False))
    assert not g["answer_checks"]["planted_token"]["ok"]
    assert "never shown" in g["answer_checks"]["planted_token"]["why"]


def test_the_verdict_check_runs_live_on_the_live_transcript():
    g = _grade(_turn(tool_returns=True, disposition="false_positive"))
    assert g["answer_checks"]["planted_token"]["ok"]
    assert not g["answer_checks"]["verdict_is"]["ok"] and not g["ok"]


def test_a_row_with_no_planted_tokens_fails_loudly_not_silently():
    g = _grade(_turn(tool_returns=True), planted={})
    assert not g["ok"] and "no planted token" in g["answer_checks"]["planted_token"]["why"]


def test_no_tool_call_at_all_cannot_pass():
    frames = [f for f in _turn(tool_returns=True) if f["type"] in ("text", "verdict_card")]
    g = _grade(frames)
    assert not g["answer_checks"]["planted_token"]["ok"]


def test_rows_without_live_answer_checks_grade_as_before():
    spec = {"expect": {"reports": {"all_of": ["hello"]}}}
    assert LR.grade(spec, "hello there", [])["ok"] is True
    assert LR.grade(spec, "hello there", [])["answer_checks"] == {}


def test_a_planted_row_needs_no_mounted_record():
    rows, skipped = LR.load_rows(None, ["no_such"])
    assert rows == []
    import json, tempfile
    d = Path(tempfile.mkdtemp())
    (d / "p.json").write_text(json.dumps({**SPEC, "category": "needle"}))
    old = LR.SWEEP
    LR.SWEEP = d
    try:
        rows, _ = LR.load_rows("needle", [])
    finally:
        LR.SWEEP = old
    assert [r["name"] for r in rows] == ["snow_needle_row"]


def test_the_expectation_is_registered_for_the_sweep():
    from scripts import chat_sweep
    assert "planted_token" in chat_sweep.EXPECTATIONS


# ---- distractors: a near miss must not be shown ---------------------------------
DISTRACTOR = "NDL-ffffffffffff"
PLANTED_ALL = {"needle": TOKEN, "distractor_similar": DISTRACTOR}
SPEC_D = {**SPEC, "expect": {**SPEC["expect"],
                             "planted_not_shown": {"labels": ["distractor_similar"]}}}


def _turn_with_distractor(*, shown):
    frames = _turn(tool_returns=True)
    frames[1]["content"] += f" | incident INC002 {DISTRACTOR} routine backup"
    if shown:
        frames[2]["text"] += f" Also {DISTRACTOR} is a backup."
    return frames


def test_a_distractor_the_tool_returned_but_the_answer_left_out_passes():
    g = _grade(_turn_with_distractor(shown=False), SPEC_D, PLANTED_ALL)
    assert g["answer_checks"]["planted_not_shown"]["ok"], g["answer_checks"]
    assert g["ok"]


def test_a_distractor_shown_to_the_analyst_fails_the_row():
    g = _grade(_turn_with_distractor(shown=True), SPEC_D, PLANTED_ALL)
    assert g["answer_checks"]["planted_token"]["ok"]
    nd = g["answer_checks"]["planted_not_shown"]
    assert not nd["ok"] and "distractor_similar" in nd["why"] and not g["ok"]


def test_a_distractor_check_with_no_token_fails_loudly():
    g = _grade(_turn_with_distractor(shown=False), SPEC_D, PLANTED)
    assert not g["answer_checks"]["planted_not_shown"]["ok"]
    assert "no planted token" in g["answer_checks"]["planted_not_shown"]["why"]


def test_planted_not_shown_is_registered_and_validated():
    import pytest
    from scripts import chat_sweep
    assert "planted_not_shown" in chat_sweep.EXPECTATIONS
    with pytest.raises(ValueError):
        chat_sweep._planted_not_shown({"labels": []})


# ---- the live-only planted rows ---------------------------------------------------
def _planted_rows():
    return sorted(LR.LIVE_PLANTED.glob("*.json"))


def test_planted_rows_are_live_only_well_formed_and_token_free():
    import json
    from scripts import chat_sweep, planter
    scenario = planter.load_scenario("snow_incident_needle")
    labels = {i["label"]: i["role"] for i in scenario["items"]}
    files = _planted_rows()
    assert len(files) >= 3
    for p in files:
        spec = json.loads(p.read_text())
        assert spec["name"] == p.stem and spec["why"] and spec["steps"]
        assert spec["planted"]["scenario"] == "snow_incident_needle"
        assert not spec.get("mount")
        chat_sweep.build_graders(spec["expect"], where=p.name)  # every expectation is registered
        want = spec["expect"].get("planted_token", {}).get("labels", [])  # a row may grade a host + incident instead
        hidden = spec["expect"]["planted_not_shown"]["labels"]
        assert all(labels[l] == "needle" for l in want)
        assert set(hidden) == {l for l, r in labels.items() if r == "distractor"}
        for st in spec["steps"]:
            assert "{token}" not in st["user"] and "NDL-" not in st["user"]
    # the offline sweep never loads them: a planted row cannot fail offline for want of a token
    assert not any(p.parent == chat_sweep.CORPUS for p in files)
    assert {r["name"] for r in LR.load_rows("planted_needle", [])[0]} == {p.stem for p in files}


def test_planted_rows_ask_for_what_the_token_lives_in():
    """The token is only in the ticket's wording, so a row that grades
    `planted_token` must ask the analyst-facing question that makes a correct
    answer quote it. snow_planted_affected_host asked only 'which host', so a
    correct answer (host named, ticket found) was graded 0 for never quoting
    the token."""
    import json
    cues = ("what does it say", "what was reported", "quote", "title", "wording", "short description")
    bad = []
    for p in sorted(LR.LIVE_PLANTED.glob("*.json")):
        spec = json.loads(p.read_text())
        if "planted_token" not in spec.get("expect", {}):
            continue
        asks = " ".join(s.get("user", "") for s in spec["steps"]).lower()
        if not any(c in asks for c in cues):
            bad.append(spec["name"])
    assert not bad, f"rows grade the token but never ask for the ticket's wording: {bad}"


# ---- affected host: the host plus an incident number a tool returned ----------
# The owner's direction: the row keeps its original question (which host does it
# name), so a correct answer need not quote the planted token. It is graded on
# the host and the incident number, and the number must come from a tool result.
AFFECTED_ROW = LR.LIVE_PLANTED / "snow_planted_affected_host.json"
INCIDENT = "INC0010001"
OTHER_HOST_TOKEN = "NDL-111122223333"


def _affected_frames(answer: str, *, tool_body: str | None = None):
    body = tool_body if tool_body is not None else (
        f"{INCIDENT} {TOKEN} outbound transfer, host build-07, external archive")
    return [
        {"type": "tool_use", "id": "t1", "name": "run_op", "input": {"operation": "search_record"}},
        {"type": "tool_result", "tool_use_id": "t1", "content": body},
        {"type": "text", "text": answer},
        {"type": "verdict_card", "disposition": "true_positive", "summary": answer,
         "findings": [{"claim": answer}]},
    ]


def _affected_spec():
    import json
    return json.loads(AFFECTED_ROW.read_text())


AFFECTED_PLANTED = {"needle": TOKEN, "distractor_similar": DISTRACTOR,
                    "distractor_other_host": OTHER_HOST_TOKEN}


def test_affected_host_row_passes_host_and_incident_without_the_token():
    """A correct answer names the host and the incident and does not quote the
    token. The ticket's record (in the tool result) carries the token, so the
    old grader's failure below is a grading artefact, not a wrong answer."""
    spec = _affected_spec()
    assert "planted_token" not in spec["expect"] and "host_and_incident" in spec["expect"]
    g = _grade(_affected_frames(f"The ticket {INCIDENT} names build-07 as the host."),
               spec, AFFECTED_PLANTED)
    assert g["answer_checks"]["host_and_incident"]["ok"], g["answer_checks"]
    assert g["ok"], g


def test_old_planted_token_grader_fails_that_same_correct_answer():
    """The red-without-fix proof: the planted-token rule, which the row used to
    grade on, rejects the correct answer because it never quotes the token."""
    spec = {**_affected_spec(), "expect": {"planted_token": {"labels": ["needle"]}}}
    g = _grade(_affected_frames(f"The ticket {INCIDENT} names build-07 as the host."),
               spec, AFFECTED_PLANTED)
    assert not g["answer_checks"]["planted_token"]["ok"]
    assert "never shown" in g["answer_checks"]["planted_token"]["why"]


def test_host_and_incident_needs_the_host():
    g = _grade(_affected_frames(f"The ticket {INCIDENT} is open."),
               _affected_spec(), AFFECTED_PLANTED)
    assert not g["answer_checks"]["host_and_incident"]["ok"]
    assert "host" in g["answer_checks"]["host_and_incident"]["why"]


def test_host_and_incident_needs_an_incident_number():
    g = _grade(_affected_frames("It names build-07 as the host."),
               _affected_spec(), AFFECTED_PLANTED)
    assert not g["answer_checks"]["host_and_incident"]["ok"]
    assert "no incident number" in g["answer_checks"]["host_and_incident"]["why"]


def test_an_incident_number_no_tool_returned_is_recited_and_fails():
    g = _grade(_affected_frames("The ticket INC9999999 names build-07."),
               _affected_spec(), AFFECTED_PLANTED)
    assert not g["answer_checks"]["host_and_incident"]["ok"]
    assert "no tool result" in g["answer_checks"]["host_and_incident"]["why"]


def test_the_affected_host_expectation_is_registered():
    from scripts import chat_sweep
    assert "host_and_incident" in chat_sweep.EXPECTATIONS
    with __import__("pytest").raises(ValueError):
        chat_sweep.EXPECTATIONS["host_and_incident"][0]({"host": ""})
