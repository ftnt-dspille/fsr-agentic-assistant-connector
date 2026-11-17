"""The shared conversation driver: halts are answered by policy, a turn that
did not answer is never graded as one, and history is what the next turn sees."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import drive as D  # noqa: E402


class _Scripted:
    """A transport that replays canned envelopes and records what it was sent."""

    def __init__(self, turns, resumes=()):
        self.session_id = "s-test"
        self._turns, self._resumes = list(turns), list(resumes)
        self.sent: list[tuple[str, dict]] = []

    def turn(self, params):
        self.sent.append(("turn", dict(params)))
        return self._turns.pop(0)

    def resume(self, params):
        self.sent.append(("resume", dict(params)))
        return self._resumes.pop(0)


_HALT = {"ok": True, "stop_reason": "approval_required", "transcript": [
    {"type": "text", "text": "Running it."},
    {"type": "tool_use", "id": "t1", "name": "run_playbook", "input": {}},
    {"type": "approval_request", "approval_id": "ap1", "tool": "run_playbook"}]}
_DONE = {"ok": True, "stop_reason": "end_turn",
         "transcript": [{"type": "text", "text": "Done."}]}


def test_a_halt_is_answered_by_the_policy_and_both_envelopes_are_kept():
    t = _Scripted([_HALT], [_DONE])

    def approve(halts, env):
        assert [h["type"] for h in halts] == ["approval_request"]
        return t.resume({"decision": "approve", "approval_id": halts[0]["approval_id"]})

    trace = D.drive(t, {"steps": [{"user": "run it"}]}, answer=approve)
    (step,) = trace
    assert step.answered_halts == 1
    assert [e["stop_reason"] for e in step.envelopes] == ["approval_required", "end_turn"]
    assert step.tools == ["run_playbook"]
    assert step.text == "Running it.Done."
    assert step.outcome.kind == "answered"


def test_without_a_policy_a_halt_stops_the_step_and_is_recorded():
    trace = D.drive(_Scripted([_HALT]), {"steps": [{"user": "run it"}]})
    assert trace[0].halt == {"key": "approval_id", "value": "ap1", "kind": "approval"}
    assert trace[0].outcome.kind == "halted"


def test_a_resume_step_with_nothing_to_resume_is_an_outcome_not_a_crash():
    trace = D.drive(_Scripted([_DONE]), {"steps": [{"user": "hi"}, {"resume": {}}]})
    assert trace[1].error == "no pending halt to resume"


def test_a_resume_step_answers_the_last_halt_with_its_decision():
    t = _Scripted([_HALT], [_DONE])
    D.drive(t, {"steps": [{"user": "run it"}, {"resume": {"decision": "approve"}}]})
    op, params = t.sent[-1]
    assert op == "resume"
    assert params == {"session_id": "s-test", "decision": "approve", "approval_id": "ap1"}


def test_history_is_the_block_list_by_default_and_prose_on_request():
    blocks = _Scripted([_DONE, _DONE])
    D.drive(blocks, {"steps": [{"user": "a"}, {"user": "b"}]})
    assert blocks.sent[1][1]["messages"][1]["content"] == [{"type": "text", "text": "Done."}]

    prose = _Scripted([_DONE, _DONE])
    D.drive(prose, {"steps": [{"user": "a"}, {"user": "b"}]}, history="text")
    assert prose.sent[1][1]["messages"][1]["content"] == "Done."


def test_a_history_policy_sees_one_summary_per_step():
    t = _Scripted([_HALT, _DONE], [_DONE])
    seen = []

    def approve(halts, env):
        return t.resume({"decision": "approve", "approval_id": "ap1"})

    D.drive(t, {"steps": [{"user": "run it"}, {"user": "next"}]}, answer=approve,
            history=lambda st: (seen.append(st.index), "(summary)")[1])
    assert seen == [1, 2]  # one summary per step, not one per envelope
    assert t.sent[2][1]["messages"][1] == {"role": "assistant", "content": "(summary)"}


def test_a_bad_history_mode_is_refused():
    with pytest.raises(ValueError):
        D.drive(_Scripted([]), {"steps": []}, history="markdown")


def test_detached_turn_refuses_a_turn_that_did_not_answer(monkeypatch):
    """A provider rejection is a drive error, never a graded answer."""
    calls = []

    def execute(op, params, timeout=None):
        calls.append(op)
        if op == "chat_turn":
            return SimpleNamespace(data={"ok": True, "accepted": True}, ok=True, error=None)
        return SimpleNamespace(data={"done": True, "turn": 1, "stop_reason": "error",
                                     "frames": [{"type": "error", "message": "403 model_blocked"}]},
                               ok=True, error=None)

    fsr = SimpleNamespace(execute=execute)
    with pytest.raises(D.DriveError, match="did not answer"):
        D.detached_turn(fsr, session_id="s", intent="build", messages=[])
    assert calls == ["chat_turn", "chat_poll"]


def test_a_failed_ack_is_a_drive_error():
    fsr = SimpleNamespace(execute=lambda op, p, timeout=None: SimpleNamespace(
        data={"ok": False, "error": "no"}, ok=False, error="no"))
    with pytest.raises(D.DriveError, match="ack failed"):
        D.detached_turn(fsr, session_id="s", intent="build", messages=[])


# ---- connector-only pieces (the shared driver is tested in fsr_playbooks) ----

def test_scripts_drive_reexports_the_shared_driver():
    """Every `from drive import ...` in the connector scripts resolves to the one
    framework copy, so there is no second loop to drift."""
    import fsr_playbooks.harness.drive as shared
    for name in ("drive", "FsrTransport", "DriveError", "TraceStep", "Transport",
                 "poll_to_done", "detached_turn", "HALT_TYPES"):
        assert getattr(D, name) is getattr(shared, name), name


def test_mount_entity_missing_record_is_drive_error():
    fetched = []

    def fetch(module, uuid):
        fetched.append((module, uuid))
        return None

    spec = {"mount": {"module": "alerts", "uuid": "u-1"}}
    with pytest.raises(D.DriveError, match="no alerts record 'u-1' to mount"):
        D.mount_entity(spec, fetch)
    assert fetched == [("alerts", "u-1")]


def test_mount_entity_uses_record_entity_shape():
    from scripts.t1_scenarios import record_entity

    rec = {"@id": "/api/3/alerts/a1", "uuid": "a1", "id": 7, "name": "x", "empty": ""}
    spec = {"mount": {"module": "alerts", "uuid": "a1"}}
    assert D.mount_entity(spec, lambda module, uuid: rec) == record_entity(rec, "alerts")
    entity = D.mount_entity(spec, lambda module, uuid: rec)
    assert entity["module"] == "alerts" and entity["iri"] == "/api/3/alerts/a1"
    assert "empty" not in entity["fields"]
