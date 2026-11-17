"""The deterministic diagnose-before-fix lever (S6).

A weak box model (e.g. gpt-4.1) does not reliably follow the build prompt's
"call why_did_playbook_fail before editing a failed playbook" rule -- it guesses a
cosmetic change and leaves the real bug. So when the analyst reports a runtime
failure on an OPEN playbook, the connector runs the diagnosis itself and folds
the failing step + cause into the turn. These tests pin that it fires on exactly
the right turns and is a no-op otherwise, and that a diagnosis failure never
breaks the turn.
"""
from __future__ import annotations

import pytest

from connector_fsr_agentic_assistant import operations as ops
from fsr_playbooks.llm.provider import Message

OPEN_YAML = "playbooks:\n- name: X\n  steps: []\n"
ENTITY = {"module": "workflows", "fields": {"name": "My Broken Playbook"},
          "playbook_yaml": OPEN_YAML}

# A SUCCESSFUL diagnosis of a FAILED run: why_did_playbook_fail returns ok=False
# ("the run is not healthy") WITH the run + failure surfaced. The lever must treat
# THIS as a hit (the box bug was gating on `ok` and discarding exactly this).
_DIAG_OK = {
    "ok": False,
    "pb_execution": "4431",
    "run_status": "failed",
    "failing_step": "Emit",
    "error_message": "insert_data() takes at least 2 positional arguments",
    "step_diagnostics": [{"step": "Emit", "issue": "create_record ran with no data"}],
    "hints": ["Add module: alerts to the create_record step"],
}
# A RESOLUTION failure (no provider / no matching run): carries a `code`.
_DIAG_NO_RUN = {"ok": False, "code": "no_failed_runs",
                "message": "no recent failed run found"}


def _msgs(text: str) -> list:
    return [Message(role="user", content=text)]


def _lever(msgs, entity, intent="build", emit=None):
    """Call the lever the way the chat turn does: the analyst's own words are
    passed as ``analyst_text`` (the turn captures them before entity folding).
    In these detector tests the single message IS the analyst's text."""
    return ops._maybe_inject_failure_diagnosis(
        msgs, entity, intent, emit=emit, analyst_text=msgs[-1].content)


def _patch_diag(monkeypatch, result):
    import fsr_playbooks.mcp_server.tools_recipe as tr
    calls = {}

    def fake(name, yaml_text=None):
        calls["name"] = name
        calls["yaml_text"] = yaml_text
        return result

    monkeypatch.setattr(tr, "why_did_playbook_fail", fake)
    return calls


# --- the failure-report detector ---------------------------------------------

@pytest.mark.parametrize("text", [
    "The last run failed and it didn't create the record it's supposed to.",
    "why did it error?",
    "the playbook broke",
    "it ran but created nothing",
    "this isn't creating the alert",
    "no record was created",
])
def test_regex_matches_failure_reports(text):
    assert ops._FAILURE_REPORT_RE.search(text)


@pytest.mark.parametrize("text", [
    "add a step that sends an email",
    "explain what this playbook does",
    "rename the Set Inputs step to Prepare",
])
def test_regex_ignores_normal_edits(text):
    assert not ops._FAILURE_REPORT_RE.search(text)


# --- the lever ----------------------------------------------------------------

def test_injects_diagnosis_on_failure_report_over_open_playbook(monkeypatch):
    calls = _patch_diag(monkeypatch, _DIAG_OK)
    msgs = _msgs("The last run failed -- it didn't create the record. Fix it.")
    activity = []
    step = _lever(
        msgs, ENTITY, "build", emit=lambda ev: activity.append(ev))

    assert step == "Emit"
    # Transparency: the analyst sees the connector consult the failed run, and
    # what it found (a "looking…" frame and a result frame naming the step).
    joined = " ".join(a.get("message", "") for a in activity)
    assert "Checking why the last run" in joined
    assert "Emit" in joined and "insert_data()" in joined
    # it diagnosed the OPEN playbook by name, passing the open YAML
    assert calls["name"] == "My Broken Playbook"
    assert calls["yaml_text"] == OPEN_YAML
    body = msgs[0].content
    assert "LAST FAILED RUN -- DIAGNOSIS" in body
    assert "Emit" in body and "insert_data()" in body
    assert "create_record ran with no data" in body  # step_diagnostics
    assert "Add module: alerts" in body               # hint
    assert "Fix EXACTLY this defect" in body
    # the analyst's original text is preserved
    assert "Fix it." in body


def test_noop_when_not_build_intent(monkeypatch):
    _patch_diag(monkeypatch, _DIAG_OK)
    msgs = _msgs("the run failed, fix it")
    assert _lever(msgs, ENTITY, "triage") is None
    assert "DIAGNOSIS" not in msgs[0].content


def test_noop_when_no_open_playbook(monkeypatch):
    _patch_diag(monkeypatch, _DIAG_OK)
    entity = {"module": "workflows", "fields": {"name": "X"}}  # no playbook_yaml
    msgs = _msgs("the run failed, fix it")
    assert _lever(msgs, entity, "build") is None
    assert "DIAGNOSIS" not in msgs[0].content


def test_noop_when_message_is_not_a_failure_report(monkeypatch):
    _patch_diag(monkeypatch, _DIAG_OK)
    msgs = _msgs("add an error-handling branch to the Notify step")
    assert _lever(msgs, ENTITY, "build") is None
    assert "DIAGNOSIS" not in msgs[0].content


def test_noop_when_no_failed_run_found(monkeypatch):
    # A resolution failure carries a `code` -- the lever must NOT inject.
    _patch_diag(monkeypatch, _DIAG_NO_RUN)
    msgs = _msgs("the run failed, fix it")
    assert _lever(msgs, ENTITY, "build") is None
    assert "DIAGNOSIS" not in msgs[0].content


def test_injects_even_though_ok_is_false(monkeypatch):
    # Regression for the box bug: ok=False is the SUCCESS shape for a failed run;
    # gating on `ok` no-op'd the whole lever. It must inject here.
    _patch_diag(monkeypatch, _DIAG_OK)  # ok=False, but has pb_execution + failure
    msgs = _msgs("the last run failed, fix it")
    assert _lever(msgs, ENTITY, "build") == "Emit"
    assert "LAST FAILED RUN -- DIAGNOSIS" in msgs[0].content


def test_diagnosis_exception_never_breaks_the_turn(monkeypatch):
    import fsr_playbooks.mcp_server.tools_recipe as tr

    def boom(name, yaml_text=None):
        raise RuntimeError("diagnosis backend down")

    monkeypatch.setattr(tr, "why_did_playbook_fail", boom)
    msgs = _msgs("the run failed, fix it")
    # swallowed → no injection, no raise
    assert _lever(msgs, ENTITY, "build") is None
    assert msgs[0].content == "the run failed, fix it"


# --- the trigger reads the ANALYST's words, not the folded playbook ----------

# The OPEN PLAYBOOK is folded into the analyst's message before the lever runs.
# Its YAML comment says "would fail", which the detector matches. That text is
# the playbook author's, not a failure the analyst reported.
WOULD_FAIL_YAML = (
    "# step reads records[0]; without it the step would fail with CS-WF-35\n"
    "playbooks:\n- name: X\n  steps: []\n")
WOULD_FAIL_ENTITY = {"module": "workflows", "fields": {"name": "X"},
                     "playbook_yaml": WOULD_FAIL_YAML}


def test_go_turn_does_not_consult_the_lever_for_playbook_text(monkeypatch):
    """A bare "go" over a playbook whose text says "would fail" is not a failure
    report. The lever must not run (no diagnosis call, no injection)."""
    calls = _patch_diag(monkeypatch, _DIAG_OK)
    folded = f"{WOULD_FAIL_YAML}\n\n---\n\ngo"
    msgs = _msgs(folded)
    assert ops._maybe_inject_failure_diagnosis(
        msgs, WOULD_FAIL_ENTITY, "build", analyst_text="go") is None
    assert calls == {}, "the lever ran on the playbook's text, not the analyst's"
    assert msgs[0].content == folded


def test_failure_report_from_analyst_still_consults_the_lever(monkeypatch):
    """The analyst's own failure report triggers the lever even when the folded
    playbook carries no failure words at all."""
    calls = _patch_diag(monkeypatch, _DIAG_OK)
    msgs = _msgs("the analyst's words\n\n---\n\nwhy did this playbook fail")
    assert ops._maybe_inject_failure_diagnosis(
        msgs, ENTITY, "build",
        analyst_text="why did this playbook fail") == "Emit"
    assert calls["name"] == "My Broken Playbook"


def test_diagnosis_lands_on_the_latest_analyst_turn(monkeypatch):
    """Multi-turn: the diagnosis rides on the message that reported the failure
    (the latest analyst turn), not on turn 1 where the record context lives."""
    _patch_diag(monkeypatch, _DIAG_OK)
    first = Message(role="user", content="build me an alert playbook")
    reply = Message(role="assistant", content="done")
    latest = Message(role="user", content="it failed, fix it")
    msgs = [first, reply, latest]
    assert ops._maybe_inject_failure_diagnosis(
        msgs, ENTITY, "build", analyst_text="it failed, fix it") == "Emit"
    assert msgs[0].content == "build me an alert playbook"
    assert "LAST FAILED RUN -- DIAGNOSIS" in msgs[2].content
