"""#137: every write-failure handler must read the code where `_err` puts it.

`_err` nests the class one level down::

    {"ok": False, "error": {"code": "would_drop_fields", "message": "..."}}

A handler reading top-level ``result["code"]`` therefore never sees a code for
ANY `_err` failure, and reports its own default instead. The enhancement-offer
accept path did exactly that, so live probe A2's real refusal reached the
analyst as a bare ``apply_failed`` with the message -- the only actionable part
-- discarded. The bug is invisible to a test that only checks "failure is
reported as a failure", which is why the existing suites were green through it.

These tests pin the *content* of the report, not its existence, and assert the
property directly on the shape `_err` produces rather than on a hand-written
fixture that could inherit the same wrong assumption.
"""
from __future__ import annotations

import pytest

# The real builder and the real normaliser -- not local reimplementations, so
# these tests cannot pass against a copy of the shape while the product drifts.
from connector_fsr_agentic_assistant.operations import _err, _failure


def test_reads_the_code_out_of_the_err_envelope():
    """The shape the connector actually produces, not the one handlers assumed."""
    env = _err("would_drop_fields", "steps[Dead End] is missing", dropped=["x"])
    assert _failure(env) == ("would_drop_fields", "steps[Dead End] is missing")


def test_top_level_code_is_never_where_err_puts_it():
    """Guards the premise. If `_err` is ever flattened this test says so loudly,
    rather than letting the handlers' old assumption quietly become correct."""
    env = _err("would_drop_fields", "msg")
    assert "code" not in env, (
        "_err now sets a top-level code -- re-check every _failure() caller"
    )
    assert env["error"]["code"] == "would_drop_fields"


def test_reads_the_hand_rolled_inline_shape_too():
    """Some except/compile branches build {code, error: <str>} by hand."""
    assert _failure({"ok": False, "code": "push_failed", "error": "boom"}) == (
        "push_failed", "boom")


@pytest.mark.parametrize("junk", [None, {}, "nope", 7, {"ok": False}])
def test_falls_back_to_the_default_code_without_crashing(junk):
    code, msg = _failure(junk, "push_failed")
    assert code == "push_failed"
    assert isinstance(msg, str)


def test_the_default_is_only_used_when_there_is_no_real_code():
    """The regression itself: a real code must never be replaced by the default."""
    for real in ("would_drop_fields", "live_read_failed", "snapshot_failed",
                 "missing_workflow_target", "update_failed"):
        assert _failure(_err(real, "why"))[0] == real, (
            f"{real!r} was reported as the generic default -- the analyst is "
            f"told nothing they can act on"
        )


def test_a_refusal_message_survives_to_the_analyst():
    """`would_drop_fields` is only useful with its message: the message is the
    part that names the field it thinks is disappearing."""
    env = _err("would_drop_fields",
               "refusing to save: 1 field(s) ... - steps[Dead End]",
               dropped=["collection.workflows[w].steps[Dead End]"])
    code, msg = _failure(env)
    assert code == "would_drop_fields"
    assert "steps[Dead End]" in msg, "the actionable detail was dropped"


# --------------------------------------------------------------------------- #
# The red-proof that matters: the HANDLER, not just the helper.
#
# The tests above pin `_failure`, which did not exist before the fix -- they
# cannot go red against the old code, they can only fail to import. Per
# [[tests_inherit_the_fixs_blind_spots]] the regression has to be pinned where
# it actually lived: in what the analyst is told.
# --------------------------------------------------------------------------- #

import pytest
import ops_chat
import ops_resume


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    """_offer_end_turn now persists to the render-fidelity history via
    default_storage(); without isolation these tests would write their fake
    sessions into the developer's real ~/.connector_fsr_agentic_assistant.db (and
    broke test_chat_history_empty exactly that way)."""
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(tmp_path / "test.db"))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield
    storage._DEFAULT_STORAGE = None


class _Storage:
    def __init__(self, card):
        self._card = card

    def get_card(self, session_id, card_id):
        return self._card

    def __getattr__(self, _name):          # session persistence is incidental here
        return lambda *a, **k: None


def _run_enhancement_accept(monkeypatch, update_result):
    """Drive the accept handler with `update_playbook` failing a given way."""
    from connector_fsr_agentic_assistant import operations as o

    monkeypatch.setattr(ops_chat, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {"x": 1}})
    monkeypatch.setattr(o, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {"x": 1}})
    monkeypatch.setattr(ops_resume, "compile_yaml",
                        lambda *a, **k: {"ok": True, "workflow_json": {"x": 1}})
    monkeypatch.setattr(o, "update_playbook", lambda *a, **k: update_result)
    monkeypatch.setattr(ops_resume, "update_playbook", lambda *a, **k: update_result)

    storage = _Storage({"type": "enhancement_offer", "id": "off-1",
                        "final_yaml": "playbooks:\n- name: p\n"})
    resp = o._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "off-1",
             "workflow_iri": "/api/3/workflows/abc"}, storage)
    text = " ".join(
        str(b.get("text") or b.get("content") or "")
        for b in (resp.get("transcript") or []) if isinstance(b, dict))
    return resp, text


def test_a_refusal_reaches_the_analyst_as_its_real_code(monkeypatch):
    """THE #137 regression. A pre-write refusal was reported as `apply_failed`,
    the generic default, because the handler read `res["code"]` -- a key `_err`
    never sets. The analyst saw a shrug where a named field belonged."""
    refusal = _err("would_drop_fields",
                   "refusing to save: 1 field(s) ... - steps[Dead End]",
                   dropped=["collection.workflows[w].steps[Dead End]"])
    resp, text = _run_enhancement_accept(monkeypatch, refusal)

    assert resp["tags"]["saved"] is False
    assert resp["tags"]["code"] == "would_drop_fields", (
        f"the refusal's real class was replaced by a generic default: "
        f"{resp['tags']['code']!r}"
    )
    assert "apply_failed" not in text
    assert "steps[Dead End]" in text, (
        "the analyst was told the save failed but not what it thinks is "
        "disappearing -- the only actionable part of the refusal"
    )


def test_an_exception_shaped_failure_still_reports_its_own_code(monkeypatch):
    """The other envelope: `{code, error: <str>}` built inline by an except."""
    resp, text = _run_enhancement_accept(
        monkeypatch, {"ok": False, "code": "live_read_failed",
                      "error": "the appliance returned 503"})
    assert resp["tags"]["code"] == "live_read_failed"
    assert "503" in text


def test_a_codeless_failure_still_falls_back_rather_than_crashing(monkeypatch):
    resp, _ = _run_enhancement_accept(monkeypatch, {"ok": False})
    assert resp["tags"]["saved"] is False
    assert resp["tags"]["code"] == "apply_failed"
