"""Persona-gated ``run_playbook``: authorization gate, dynamic tier (auto-run
vs approval card), and the thin pyfsr-typed trigger path.

No box -- a fake ``_shared._live_client()`` exposing a stub ``.playbooks`` whose
``trigger`` records the call and returns a shaped run. The persona is bound via
the same ContextVar the real turn uses (``profiles.set_active_profile``).
"""
from __future__ import annotations

import pytest

from fsr_playbooks.mcp_server import _shared
from fsr_playbooks.mcp_server import tools_playbook_run as tp
from fsr_soc_triage import tools_playbook as policy
from fsr_soc_triage import profiles
from fsr_soc_triage.profiles import Profile


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
class _StubPlaybooks:
    def __init__(self, result, *, definition=None, pb_uuid=None,
                 wait_for_run_result=None, history=None,
                 approval_result=None, execution=None,
                 manual_input_record=None):
        self.result = result                # trigger() return (manual path)
        self.definition = definition        # dict returned by get_definition
        self.pb_uuid = pb_uuid              # value _resolve_uuid returns
        self.wait_for_run_result = wait_for_run_result  # action follow: run or exc
        self.history = history or []         # execution_history return (awaiting seam)
        self.approval_result = approval_result  # approval() ack or exc
        self.execution = execution          # get_execution snapshot
        self.manual_input_record = manual_input_record  # retrieve_manual_input return
        self.calls = []
        self.action_calls = []
        self.approval_calls = []
    def trigger(self, playbook, *, records=None, inputs=None, follow=False,
                timeout=300):
        self.calls.append({"playbook": playbook, "records": records,
                           "inputs": inputs, "follow": follow, "timeout": timeout})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
    # --- record-action path (only present when the test provides a definition) --
    def _resolve_uuid(self, playbook):
        return self.pb_uuid
    def get_definition(self, uuid, *, relationships=True):
        if self.definition is None:
            raise AttributeError("no definition")  # emulate absence → manual path
        return self.definition
    def trigger_action(self, route_uuid, *, module, record_uuid,
                       playbook_uuid=None, env=None):
        self.action_calls.append({"route_uuid": route_uuid, "module": module,
                                  "record_uuid": record_uuid,
                                  "playbook_uuid": playbook_uuid, "env": env})
        return {"task_ids": ["run-token"]}
    def wait_for_run(self, *, playbook=None, playbook_uuid=None, since=None,
                     timeout=120, poll_interval=3):
        r = (self.wait_for_run_result if self.wait_for_run_result is not None
             else self.result)
        if isinstance(r, Exception):
            raise r
        return r
    def execution_history(self, *, playbook=None, playbook_uuid=None, limit=20):
        return self.history
    def approval(self, run_pk, *, decision="approve", comment=None, user=None):
        self.approval_calls.append({"run_pk": run_pk, "decision": decision})
        if isinstance(self.approval_result, Exception):
            raise self.approval_result
        return self.approval_result
    def get_execution(self, run_pk, *, step_detail=False):
        return self.execution
    def retrieve_manual_input(self, pk):
        return self.manual_input_record or {}


class _StubManualInput:
    """pyfsr ManualInputAPI surface resume_playbook(inputs=) uses: a no-owners
    retrieve (returns options + step ids) then resume."""
    def __init__(self, answer_result=None, options=None):
        self.resume_result = answer_result
        self.resume_calls = []
        self._options = options if options is not None else [
            {"option": "Ok", "primary": True, "step_iri": "/api/3/workflow_steps/ok"},
            {"option": "Cancel", "primary": False, "step_iri": "/api/3/workflow_steps/cx"}]
    def retrieve(self, input_id, *, typed=True, owners=None, **_kw):
        return {"workflow": 2971, "step_id": 555,
                "response_mapping": {"options": self._options}}
    def _resolve_user_iri(self):
        return "/api/3/people/admin"
    def resume(self, workflow_id, *, step_iri=None, step_id=None,
               manual_input_id=None, user=None, input=None, **_kw):
        self.resume_calls.append({"workflow_id": workflow_id, "step_iri": step_iri,
                                  "manual_input_id": manual_input_id, "input": input})
        if isinstance(self.resume_result, Exception):
            raise self.resume_result
        return self.resume_result


def _mi_record(*, title="Choose a Manager and a Device"):
    """A retrieve_manual_input record: one field + Ok(primary)/Cancel options."""
    return {
        "id": 126, "workflow": 2971, "is_approval": False,
        "input": {"schema": {"title": title, "description": "pick one",
                             "inputVariables": [
                                 {"name": "managerAndDevice", "type": "array",
                                  "label": "Manager and Device"}]}},
        "response_mapping": {"options": [
            {"option": "Ok", "primary": True},
            {"option": "Cancel"}]},
    }


def _action_def(route="81d0acd1-0000-0000-0000-000000000000"):
    """A workflow definition whose trigger step is a record-action."""
    return {"steps": [
        {"stepType": {"name": "cybersponse.action"}, "arguments": {"route": route}},
        {"stepType": {"name": "SetVariable"}, "arguments": {}},
    ]}


def _manual_def():
    """A workflow definition with no record-action trigger step."""
    return {"steps": [
        {"stepType": {"name": "cybersponse.trigger"}, "arguments": {}},
    ]}


class _StubClient:
    def __init__(self, playbooks, *, manual_input=None, mi_list=None):
        self.playbooks = playbooks
        self.manual_input = manual_input
        self._mi_list = mi_list  # hydra:member rows for the workflow=<pk> lookup
    def get(self, path, params=None, **_kw):
        # _pending_input's workflow=<pk> lookup on /manual-wf-input/
        if "manual-wf-input" in path:
            return {"hydra:member": self._mi_list or []}
        return {}


@pytest.fixture
def bind_persona(monkeypatch):
    tokens = []
    def _bind(**kw):
        p = Profile(module="ztpf_metadata_sources", prompt="p", **kw)
        tokens.append(profiles.set_active_profile(p))
        return p
    yield _bind
    for t in reversed(tokens):
        profiles.reset_active_profile(t)


def _patch_client(monkeypatch, result, *, manual_input=None, mi_list=None,
                  **stub_kw):
    pb = _StubPlaybooks(result, **stub_kw)
    client = _StubClient(pb, manual_input=manual_input, mi_list=mi_list)
    monkeypatch.setattr(_shared, "_live_client", lambda: client)
    return pb


# --------------------------------------------------------------------------- #
# authorization gate
# --------------------------------------------------------------------------- #
def test_no_persona_refused(monkeypatch):
    _patch_client(monkeypatch, {"status": "finished"})
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device")
    assert out["ok"] is False and out["code"] == "run_not_permitted"


def test_no_persona_but_approved_runs(monkeypatch):
    """A human clicked approve -- the missing persona must not veto them.

    run_playbook is in the *build* tool slice, which binds no persona, so the
    no-persona branch is reachable in normal use. Live on 8.0 it produced a
    correct approval card, an analyst approving it, and `run_not_permitted`
    with nothing run. `_execute_approved_card` wraps its dispatch in
    `approved_dispatch()`; that is the authorization the gate must honour.
    """
    _patch_client(monkeypatch, {"status": "finished", "ok": True})
    with tp.approved_dispatch():
        out = tp.run_playbook(playbook="Get Metadata Source Data on Device")
    assert out["ok"] is True and out["triggered"] is True
    assert out.get("code") != "run_not_permitted"


def test_no_persona_unattended_still_refused(monkeypatch):
    """The guard still does its real job: block an UNATTENDED run.

    Outside `approved_dispatch()` -- the auto/tier-2 path nobody approved. This
    is the case the persona check exists for, and loosening the approved path
    must not loosen this one.
    """
    _patch_client(monkeypatch, {"status": "finished", "ok": True})
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device")
    assert out["ok"] is False and out["code"] == "run_not_permitted"


def test_approved_dispatch_does_not_leak_past_the_call(monkeypatch):
    """The grant is scoped to one dispatch, never latched on.

    If it leaked, the next unattended run in the same worker would inherit an
    approval nobody gave it -- a far worse bug than the one being fixed.
    """
    _patch_client(monkeypatch, {"status": "finished", "ok": True})
    with tp.approved_dispatch():
        assert tp.approved_dispatch_active() is True
    assert tp.approved_dispatch_active() is False
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device")
    assert out["ok"] is False and out["code"] == "run_not_permitted"


def test_playbook_not_in_allow_runs_flagged_unlisted(monkeypatch, bind_persona):
    # Advisory allowlist: a not-listed playbook is NOT refused in the body
    # (reaching here means a human approved the cautionary card / or auto). It
    # runs and is flagged `unlisted` for the audit trail.
    bind_persona(run_playbook_allow=["Some Other PB"])
    _patch_client(monkeypatch, {"status": "finished", "ok": True})
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device")
    assert out["ok"] is True and out["triggered"] is True
    assert out.get("unlisted") is True


def test_listed_playbook_not_flagged_unlisted(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, {"status": "finished", "ok": True})
    out = tp.run_playbook(playbook="pb")
    assert out["ok"] is True and "unlisted" not in out


def test_allowed_playbook_runs(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    pb = _patch_client(monkeypatch, {"status": "finished", "ok": True})
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record="/api/3/ztpf_devices/u1")
    assert out["ok"] is True and out["triggered"] is True
    assert pb.calls[0]["playbook"] == "Get Metadata Source Data on Device"
    assert pb.calls[0]["records"] == "/api/3/ztpf_devices/u1"


def test_missing_playbook_arg(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["x"])
    _patch_client(monkeypatch, {"status": "finished"})
    out = tp.run_playbook(playbook="")
    assert out["ok"] is False and out["code"] == "missing_playbook"


# --------------------------------------------------------------------------- #
# record-action trigger routing (the fix for empty `input.records[0]`)
# --------------------------------------------------------------------------- #
_REC = "/api/3/ztpf_metadata_sources/ecaab084-c362-449e-bb6c-fd2b61d04a52"


def test_record_action_playbook_uses_trigger_action(monkeypatch, bind_persona):
    # A playbook with a cybersponse.action trigger + a record IRI must fire via
    # trigger_action (which populates input.records), NOT the manual notrigger,
    # and follow by polling the newest run (the /action/ task_id doesn't correlate).
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    pb = _patch_client(monkeypatch, None,
                       definition=_action_def(), pb_uuid="pbuuid",
                       history=[{"status": "finished", "ok": True,
                                 "pk": "2967"}])
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device", record=_REC)
    assert out["ok"] is True and out["triggered"] is True
    assert out["status"] == "finished"
    assert pb.calls == [], "must NOT use the manual notrigger trigger()"
    assert len(pb.action_calls) == 1
    a = pb.action_calls[0]
    assert a["route_uuid"] == "81d0acd1-0000-0000-0000-000000000000"
    assert a["module"] == "ztpf_metadata_sources"
    assert a["record_uuid"] == "ecaab084-c362-449e-bb6c-fd2b61d04a52"
    assert a["playbook_uuid"] == "pbuuid"


_MOVE_STEP_FORM = [{"name": "newStepNumber", "type": "integer", "required": False}]
_STEP = "/api/3/ztpf_automation_profile_steps/3edc97d2-91eb-45e6-b150-598296d6be0f"


def _form_def(form=_MOVE_STEP_FORM):
    d = _action_def()
    d["steps"][0]["arguments"]["inputVariables"] = form
    return d


def test_record_action_inputs_reach_the_run_typed(monkeypatch, bind_persona):
    # Live: "Move Step Number on Automation Profile" reads
    # vars.request.data["newStepNumber"]. The inputs were dropped on this route,
    # the run compared '' < 1, failed half way and left the profile at -1..-7.
    # They go in the trigger body, as the type the form declares ("2" -> 2).
    bind_persona(run_playbook_allow=["Move Step Number on Automation Profile"])
    pb = _patch_client(monkeypatch, None, definition=_form_def(), pb_uuid="pbuuid",
                       history=[{"status": "finished", "ok": True, "pk": "1"}])
    out = tp.run_playbook(playbook="Move Step Number on Automation Profile",
                          record=_STEP, inputs={"newStepNumber": "2"})
    assert out["ok"] is True
    assert pb.action_calls[0]["env"] == {"newStepNumber": 2}


@pytest.mark.parametrize("inputs,code", [
    ({"stepNumber": 2}, "unknown_input"),
    ({"newStepNumber": "two"}, "bad_input"),
    ({"newStepNumber": 2.5}, "bad_input"),
])
def test_a_form_the_run_cannot_use_is_refused_before_it_runs(monkeypatch, bind_persona,
                                                              inputs, code):
    bind_persona(run_playbook_allow=["Move Step Number on Automation Profile"])
    pb = _patch_client(monkeypatch, None, definition=_form_def(), pb_uuid="pbuuid",
                       history=[{"status": "finished", "ok": True, "pk": "1"}])
    out = tp.run_playbook(playbook="Move Step Number on Automation Profile",
                          record=_STEP, inputs=inputs)
    assert out["code"] == code and out["triggered"] is False
    assert "newStepNumber (integer)" in out["message"]
    assert pb.action_calls == []


def test_a_required_form_field_left_out_is_refused(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["Move Step Number on Automation Profile"])
    pb = _patch_client(monkeypatch, None,
                       definition=_form_def([{"name": "newStepNumber", "type": "integer",
                                              "required": True}]),
                       pb_uuid="pbuuid", history=[{"status": "finished", "ok": True, "pk": "1"}])
    out = tp.run_playbook(playbook="Move Step Number on Automation Profile", record=_STEP)
    assert out["code"] == "missing_input" and pb.action_calls == []


def test_action_awaiting_returns_resumable_run_pk(monkeypatch, bind_persona):
    # A run that pauses on a manual-input step surfaces the resumable awaiting_input
    # seam carrying run_pk -- and does so the instant it enters `awaiting`, without
    # blocking out the timeout (the poll settles on `awaiting`, not just terminal).
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}])
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device", record=_REC)
    assert out["ok"] is False and out["code"] == "awaiting_input"
    assert out["run_pk"] == "2971" and out["status"] == "awaiting"


def test_action_awaiting_settles_without_blocking_timeout(monkeypatch, bind_persona):
    # A paused run whose manual-input form is ready at once must settle on
    # `awaiting` immediately: NO sleep, and certainly not sleeping out the (large)
    # timeout waiting for a terminal status that will never come. Regression for
    # the "stuck on PENDING for the whole timeout" latency bug. (When the form
    # LAGS the pause, a bounded poll kicks in -- see the next test -- but a
    # ready form must never cost a single sleep.)
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    slept: list = []
    monkeypatch.setattr(tp.time, "sleep", lambda s: slept.append(s))
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}],
                  mi_list=[{"id": 126}], manual_input_record=_mi_record())
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record=_REC, timeout=999)
    assert out["code"] == "awaiting_input"
    assert out["awaiting"]["fields"], "the ready form must be attached"
    assert slept == [], "a ready form must settle at once, not poll or block"


def test_awaiting_form_poll_is_bounded_when_it_lags(monkeypatch, bind_persona):
    # The `manual-wf-input` record can lag the run reaching the manual_input step:
    # `execution_history` reads "awaiting" before `_pending_input` can see the
    # form. A single lookup then omits the form and the widget shows a plain text
    # ask instead of the fillable manual_input CARD. run_playbook must poll briefly
    # so the form is attached -- but the poll is BOUNDED (it can never sleep out a
    # large timeout). Here the form appears on the 3rd lookup.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    slept: list = []
    monkeypatch.setattr(tp.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}
    real_form = {"manual_input_id": 126, "title": "Enter a note",
                 "description": None, "is_approval": False,
                 "fields": [{"name": "note", "label": "Note", "type": "string",
                             "data_type": None, "tooltip": None}],
                 "options": [{"option": "Submit", "primary": True}]}

    def _lagging(_client, _pbs, _rpk):
        calls["n"] += 1
        return real_form if calls["n"] >= 3 else None

    monkeypatch.setattr(tp, "_pending_input", _lagging)
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}])
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record=_REC, timeout=999)
    assert out["code"] == "awaiting_input"
    assert out["awaiting"]["fields"], "the lagging form must be attached after the poll"
    assert calls["n"] == 3 and 0 < len(slept) < 8, "bounded poll, not the full timeout"


def test_action_slow_run_is_not_finished(monkeypatch, bind_persona):
    # Still running (not awaiting) after the wait → generic not_finished seam, still
    # with run_pk. timeout=0 makes the poll settle on the first non-terminal read.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "running", "pk": "3000"}])
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record=_REC, timeout=0)
    assert out["ok"] is False and out["code"] == "not_finished_awaiting_or_slow"
    assert out["run_pk"] == "3000"


def test_manual_playbook_falls_back_to_trigger(monkeypatch, bind_persona):
    # No record-action step in the definition → keep the manual notrigger path.
    bind_persona(run_playbook_allow=["pb"])
    pb = _patch_client(monkeypatch, {"status": "finished", "ok": True},
                       definition=_manual_def(), pb_uuid="u")
    out = tp.run_playbook(playbook="pb", record=_REC)
    assert out["ok"] is True
    assert pb.action_calls == [] and len(pb.calls) == 1
    assert pb.calls[0]["records"] == _REC


def test_no_record_on_action_playbook_refuses(monkeypatch, bind_persona):
    """A record-action playbook fired recordless is refused, not triggered.

    Superseded ``test_no_record_stays_manual``, which predated the
    `record_required` refusal and asserted the recordless manual trigger this
    path no longer takes. Down the `notrigger` route the run dies reading
    `input.records[0]`, so it has to be refused up front -- and nothing may be
    triggered on the way to that refusal.
    """
    bind_persona(run_playbook_allow=["pb"])
    pb = _patch_client(monkeypatch, {"status": "finished", "ok": True},
                       definition=_action_def(), pb_uuid="u")
    out = tp.run_playbook(playbook="pb")
    assert out["ok"] is False and out["code"] == "record_required"
    assert out["triggered"] is False
    assert pb.action_calls == [] and pb.calls == []


def test_no_record_on_manual_playbook_still_triggers(monkeypatch, bind_persona):
    # The refusal is scoped to record-action playbooks: a manual one still fires
    # recordless down the notrigger route.
    bind_persona(run_playbook_allow=["pb"])
    pb = _patch_client(monkeypatch, {"status": "finished", "ok": True},
                       definition=_manual_def(), pb_uuid="u")
    out = tp.run_playbook(playbook="pb")
    assert out["ok"] is True
    assert pb.action_calls == [] and len(pb.calls) == 1
    assert pb.calls[0]["records"] is None


# --------------------------------------------------------------------------- #
# outcome shaping
# --------------------------------------------------------------------------- #
def test_failed_run_reports_not_ok(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, {"status": "finished_with_error", "ok": False})
    out = tp.run_playbook(playbook="pb")
    assert out["ok"] is False and out["status"] == "finished_with_error"


def test_not_finished_awaiting_manual_input(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, TimeoutError("still awaiting"))
    out = tp.run_playbook(playbook="pb", timeout=5)
    assert out["ok"] is False and out["code"] == "not_finished_awaiting_or_slow"
    assert out["triggered"] is True


def test_not_triggerable(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, ValueError("playbook 'pb' not found"))
    out = tp.run_playbook(playbook="pb")
    assert out["ok"] is False and out["code"] == "not_triggerable"


# --------------------------------------------------------------------------- #
# dynamic tier: auto ⇒ 2 (no card), allow-but-not-auto ⇒ 3 (card)
# --------------------------------------------------------------------------- #
def test_tier_auto_is_2(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["valpb"], run_playbook_auto=["valpb"])
    from fsr_playbooks.llm.tools import _resolve_tier
    assert _resolve_tier("run_playbook", {"playbook": "valpb"}) == 2


def test_tier_allow_not_auto_is_3(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["mutpb"])  # allowed but not auto
    from fsr_playbooks.llm.tools import _resolve_tier
    assert _resolve_tier("run_playbook", {"playbook": "mutpb"}) == 3


def test_tier_not_listed_is_3(monkeypatch, bind_persona):
    # A playbook outside the allowlist still cards (tier 3) -- not auto.
    bind_persona(run_playbook_allow=["other"], run_playbook_auto=["other"])
    from fsr_playbooks.llm.tools import _resolve_tier
    assert _resolve_tier("run_playbook", {"playbook": "unlisted_pb"}) == 3


def test_tier_no_persona_is_3(monkeypatch):
    from fsr_playbooks.llm.tools import _resolve_tier
    assert _resolve_tier("run_playbook", {"playbook": "anything"}) == 3


# --------------------------------------------------------------------------- #
# auto ⊆ allow enforced at parse time
# --------------------------------------------------------------------------- #
def test_auto_intersected_with_allow_on_parse():
    rec = {"value": "prompt text", "jSONValue": {
        "run_playbook": {"allow": ["a"], "auto": ["a", "b"]}}}
    prof = profiles.parse_profile_record(rec, "ztpf_metadata_sources")
    assert prof.run_playbook_allow == ["a"]
    assert prof.run_playbook_auto == ["a"], "auto must drop 'b' (not in allow)"


def test_lint_flags_orphan_auto():
    rec = {"value": "p", "jSONValue": {
        "run_playbook": {"allow": ["a"], "auto": ["b"]}}}
    issues = profiles.validate_persona_record(rec)
    assert any(i.field == "run_playbook.auto" for i in issues)


# --------------------------------------------------------------------------- #
# resume_playbook -- thin wrapper over pyfsr approval()
# --------------------------------------------------------------------------- #
def test_resume_no_persona_refused(monkeypatch):
    _patch_client(monkeypatch, None)
    out = tp.resume_playbook(run="2971")
    assert out["ok"] is False and out["code"] == "resume_not_permitted"


def test_resume_missing_run(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, None)
    out = tp.resume_playbook(run="")
    assert out["ok"] is False and out["code"] == "missing_run"


def test_resume_calls_approval_with_decision(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    pb = _patch_client(monkeypatch, None,
                       approval_result={"task_id": "t1", "message": "resumed"},
                       execution={"status": "finished"})
    out = tp.resume_playbook(run="2971", decision="reject")
    assert out["ok"] is True and out["run_pk"] == "2971"
    assert out["resumed"] is True and out["status"] == "finished"
    assert pb.approval_calls == [{"run_pk": "2971", "decision": "reject"}]


def test_resume_accepts_run_iri(monkeypatch, bind_persona):
    # A run @id resolves to its bare pk before calling approval().
    bind_persona(run_playbook_allow=["pb"])
    pb = _patch_client(monkeypatch, None,
                       approval_result={"task_id": "t", "message": "ok"},
                       execution={"status": "awaiting"})
    out = tp.resume_playbook(run="/wf/api/workflows/2971/")
    assert out["ok"] is True
    assert pb.approval_calls[0]["run_pk"] == "2971"


def test_resume_not_awaiting(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, None,
                  approval_result=ValueError("no pending manual-input"))
    out = tp.resume_playbook(run="2971")
    assert out["ok"] is False and out["code"] == "not_awaiting"


def test_resume_bad_decision(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, None,
                  approval_result=LookupError("no option 'Nope'"))
    out = tp.resume_playbook(run="2971", decision="Nope")
    assert out["ok"] is False and out["code"] == "bad_decision"


# `awaiting_input` returns a top-level run_pk AND a nested
# awaiting.manual_input_id -- two bare ints side by side. Live on 206 the model
# passed the wrong one (resume_playbook(run='388') for run 6839); the platform
# rejects it as an invalid FORM CHOICE, which reads like the analyst picked
# something bad rather than a wrong-identifier bug. It must be named as such.
_INVALID_WORKFLOW_400 = (
    "400 Client Error: Bad Request for url: "
    "https://localhost/api/wf/api/manual-wf-input/?workflow=388&format=json"
    " :: {'workflow': ['Select a valid choice. That choice is not one of the "
    "available choices.']}")


def test_resume_wrong_id_is_named_not_a_generic_error(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, None,
                  approval_result=RuntimeError(_INVALID_WORKFLOW_400))
    out = tp.resume_playbook(run="388")
    assert out["ok"] is False
    assert out["code"] == "bad_run_reference"      # not the opaque resume_error
    assert "manual_input_id" in out["message"]     # names the actual confusion
    assert "run_pk" in out["message"]              # and what to pass instead


def test_resume_other_transport_failures_stay_resume_error(monkeypatch, bind_persona):
    # The re-map must be narrow: a genuine transport fault is still resume_error,
    # and must keep carrying its diagnosis.
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, None,
                  approval_result=RuntimeError("502 Bad Gateway"))
    out = tp.resume_playbook(run="2971")
    assert out["ok"] is False and out["code"] == "resume_error"
    assert "502 Bad Gateway" in out["message"]


# --------------------------------------------------------------------------- #
# awaiting form: surface fields + submit values
# --------------------------------------------------------------------------- #
def test_awaiting_input_surfaces_form_fields(monkeypatch, bind_persona):
    # run_playbook's awaiting_input carries the paused step's form: the fields to
    # fill (input.schema.inputVariables) + the response options.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}],
                  mi_list=[{"id": 126}], manual_input_record=_mi_record())
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device", record=_REC)
    assert out["code"] == "awaiting_input" and out["run_pk"] == "2971"
    form = out["awaiting"]
    assert form["manual_input_id"] == 126
    assert form["title"] == "Choose a Manager and a Device"
    assert form["fields"] == [{"name": "managerAndDevice", "label": "Manager and Device",
                               "type": "array", "data_type": None, "tooltip": None}]
    assert form["options"] == [{"option": "Ok", "primary": True},
                               {"option": "Cancel", "primary": False}]


def test_awaiting_form_carries_dynamiclist_options_and_datatype(monkeypatch, bind_persona):
    # A dynamicList field forwards its render hint (data_type) and the runtime-
    # COMPUTED options (manager→device groups), so the widget card has real
    # choices; a checkbox forwards its data_type with no options.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    groups = [{"value": "FMG (v8.0.0)", "options": ["CORE (VM64)", "FG1 (VM64)"]}]
    rec = _mi_record()
    rec["input"]["schema"]["inputVariables"] = [
        {"name": "managerAndDevice", "type": "array", "label": "Manager and Device",
         "dataType": "dynamicList", "options": groups},
        {"name": "populateInputParameters", "type": "boolean", "label": "Populate",
         "dataType": "checkbox"},
        # A raw template string (unrendered) must NOT leak as options.
        {"name": "other", "type": "array", "dataType": "dynamicList",
         "options": "{{vars.x}}"},
    ]
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}],
                  mi_list=[{"id": 126}], manual_input_record=rec)
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device", record=_REC)
    fields = {f["name"]: f for f in out["awaiting"]["fields"]}
    assert fields["managerAndDevice"]["data_type"] == "dynamicList"
    assert fields["managerAndDevice"]["options"] == groups
    assert fields["populateInputParameters"]["data_type"] == "checkbox"
    assert "options" not in fields["populateInputParameters"]
    assert "options" not in fields["other"]  # unrendered template string dropped


def test_shape_field_forwards_default_prefill():
    # FortiSOAR's inputVariables carry a prefill in `value` (a YAML default in a
    # textarea, a pre-checked checkbox) that its native panel shows. Forward it as
    # `default` so the card seeds it -- dropping it blanked textareas / unchecked boxes.
    assert tp._shape_field({"name": "params", "type": "textarea",
                            "value": "input:\n  lo_name:"})["default"] == "input:\n  lo_name:"
    assert tp._shape_field({"name": "flag", "type": "checkbox",
                            "value": True})["default"] is True
    # `default` key fallback, and absent → no key (card seeds empty, not "None").
    assert tp._shape_field({"name": "n", "type": "text", "default": "d"})["default"] == "d"
    assert "default" not in tp._shape_field({"name": "x", "type": "text"})


def test_resume_with_inputs_resumes_via_step_iri(monkeypatch, bind_persona):
    # inputs supplied → resolve the option (approve → primary 'Ok') and resume via
    # its step_iri with the field values (NOT owner-scoped answer()).
    bind_persona(run_playbook_allow=["pb"])
    mi = _StubManualInput(answer_result={"task_id": "t", "message": "resumed"})
    _patch_client(monkeypatch, None, manual_input=mi, mi_list=[{"id": 126}],
                  manual_input_record=_mi_record(), execution={"status": "finished"})
    out = tp.resume_playbook(run="2971", decision="approve",
                             inputs={"managerAndDevice": ["m", "d"]})
    assert out["ok"] is True and out["status"] == "finished"
    assert mi.resume_calls == [{"workflow_id": 2971,
                                "step_iri": "/api/3/workflow_steps/ok",
                                "manual_input_id": 126,
                                "input": {"managerAndDevice": ["m", "d"]}}]


def test_resume_inputs_reject_maps_to_nonprimary(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    mi = _StubManualInput(answer_result={"task_id": "t", "message": "ok"})
    _patch_client(monkeypatch, None, manual_input=mi, mi_list=[{"id": 126}],
                  manual_input_record=_mi_record(), execution={"status": "finished"})
    tp.resume_playbook(run="2971", decision="reject", inputs={"x": 1})
    assert mi.resume_calls[0]["step_iri"] == "/api/3/workflow_steps/cx"


def test_resume_inputs_no_pending_is_not_awaiting(monkeypatch, bind_persona):
    bind_persona(run_playbook_allow=["pb"])
    mi = _StubManualInput()
    _patch_client(monkeypatch, None, manual_input=mi, mi_list=[])  # nothing pending
    out = tp.resume_playbook(run="2971", inputs={"x": 1})
    assert out["ok"] is False and out["code"] == "not_awaiting"
    assert mi.resume_calls == []


def test_resume_repauses_on_next_gate_reemits_awaiting(monkeypatch, bind_persona):
    # A submit that lands the run on ANOTHER fillable manual_input gate re-emits
    # the awaiting_input seam (with the next form) so a CHAIN of gates keeps
    # surfacing a form to the widget instead of dead-ending after the first
    # submit. The run still reads `awaiting` and a fresh form is pending.
    bind_persona(run_playbook_allow=["pb"])
    mi = _StubManualInput(answer_result={"task_id": "t", "message": "resumed"})
    _patch_client(monkeypatch, None, manual_input=mi, mi_list=[{"id": 126}],
                  manual_input_record=_mi_record(), execution={"status": "awaiting"})
    # The next gate is a NEW manual input (its own id): the stub serves 126
    # throughout, and 126 is the gate just answered, which the re-await skips.
    real_pending = tp._pending_input
    calls = []

    def pending(client, playbooks, run_pk):
        form = real_pending(client, playbooks, run_pk)
        calls.append(1)
        return dict(form, manual_input_id=127) if form and len(calls) > 1 else form

    monkeypatch.setattr(tp, "_pending_input", pending)
    out = tp.resume_playbook(run="2971", decision="approve",
                             inputs={"managerAndDevice": ["m", "d"]})
    # gate 1 was submitted (mi.resume fired) ...
    assert mi.resume_calls and mi.resume_calls[0]["manual_input_id"] == 126
    # ... and the NEXT gate is re-surfaced as the same awaiting_input seam
    # run_playbook returns for the first gate (what _manual_input_card_from_
    # awaiting consumes to render the next manual_input card).
    assert out["ok"] is False and out["code"] == "awaiting_input"
    assert out["run_pk"] == "2971" and out["resumed"] is True
    assert out["awaiting"]["manual_input_id"] == 127
    assert out["awaiting"]["fields"]  # the next gate's fields, ready to fill


def test_resume_finished_does_not_reemit_awaiting(monkeypatch, bind_persona):
    # The happy path is unchanged: a resume that FINISHES the run reports a plain
    # ok:True with the terminal status -- no spurious awaiting_input seam.
    bind_persona(run_playbook_allow=["pb"])
    mi = _StubManualInput(answer_result={"task_id": "t", "message": "resumed"})
    _patch_client(monkeypatch, None, manual_input=mi, mi_list=[{"id": 126}],
                  manual_input_record=_mi_record(), execution={"status": "finished"})
    out = tp.resume_playbook(run="2971", decision="approve",
                             inputs={"managerAndDevice": ["m", "d"]})
    assert out["ok"] is True and out.get("code") is None
    assert out["status"] == "finished"


# --------------------------------------------------------------------------- #
# follow=False must not be able to hide a PARK
#
# Live on .206 (2026-08-05) the model called
# `run_playbook(playbook="Manual Input Demo - Collect Note", follow=false)`.
# The unfollowed branch returned `{"triggered": True}` and stopped, so the turn
# never learned the run had parked on its manual-input gate: no `awaiting_input`,
# no manual_input card, and an approval card that truthfully said "Approved and
# executed" because `ok` really was true. Every layer behaved as written and the
# analyst got no form -- the arc's visible behaviour hung on one model-supplied
# argument. A parked run is not a run you can decline to wait for: nobody else is
# coming to answer its gate.
# --------------------------------------------------------------------------- #
def test_unfollowed_action_run_that_parks_still_surfaces_awaiting(
        monkeypatch, bind_persona):
    # THE regression. follow=False, record-action route, the run parks -> the
    # resumable seam must come back anyway, with the run_pk that makes it
    # answerable.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}])
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record=_REC, follow=False)
    assert out["code"] == "awaiting_input", (
        "an unfollowed trigger dropped a parked run -- the gate is live on the "
        "box and unreachable from chat")
    assert out["run_pk"] == "2971" and out["status"] == "awaiting"


def test_unfollowed_park_attaches_the_form(monkeypatch, bind_persona):
    # Not merely the seam: the FIELDS. Without them the widget can't synthesize a
    # manual_input card and falls back to a plain text ask -- which is exactly the
    # degraded "a chatbot describes the form" behaviour seen live.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}],
                  mi_list=[{"id": 126}], manual_input_record=_mi_record())
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record=_REC, follow=False)
    assert out["code"] == "awaiting_input"
    assert out["awaiting"]["fields"], "the form must be attached, not just the pk"


def test_unfollowed_manual_run_that_parks_still_surfaces_awaiting(
        monkeypatch, bind_persona):
    # The same hole on the manual (`notrigger`) route -- a manual playbook can
    # park on a manual_input step too.
    bind_persona(run_playbook_allow=["pb"])
    _patch_client(monkeypatch, {"triggered": True}, pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}])
    out = tp.run_playbook(playbook="pb", follow=False)
    assert out["code"] == "awaiting_input" and out["run_pk"] == "2971"


def test_unfollowed_run_that_does_not_park_is_unchanged(monkeypatch, bind_persona):
    # The other half of the contract, and the one that keeps `follow=False`
    # honest: a run that is merely SLOW is still reported as "triggered, didn't
    # wait". The probe must not quietly turn follow=False into follow=True.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    monkeypatch.setattr(tp, "_PARK_PROBE_SECONDS", 0)
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "running", "pk": "2971"}])
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record=_REC, follow=False)
    assert out["ok"] is True and out["triggered"] is True
    assert out["followed"] is False and out.get("code") is None


def test_unfollowed_park_probe_is_bounded(monkeypatch, bind_persona):
    # The probe is a short watch, not a second follow: `follow=False` still means
    # "don't wait for this run" for everything that isn't a park. A run that never
    # parks must not cost the caller the full `timeout`.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    # A FAKE CLOCK, not a no-op sleep. The bound is a `time.monotonic()`
    # deadline, so stubbing sleep alone freezes time and the loop spins forever
    # -- the first cut of this test "measured" 34,783,973 simulated seconds and
    # would have reported an unbounded probe as unbounded for the wrong reason.
    # Advancing a clock the code actually reads tests the deadline itself.
    now = {"t": 0.0}
    monkeypatch.setattr(tp.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(tp.time, "sleep", lambda s: now.__setitem__("t", now["t"] + s))
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "running", "pk": "2971"}])
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record=_REC, follow=False, timeout=9999)
    assert out["triggered"] is True and out.get("code") is None
    assert now["t"] <= tp._PARK_PROBE_SECONDS, (
        f"the park probe waited {now['t']}s -- it must stay bounded by "
        f"_PARK_PROBE_SECONDS ({tp._PARK_PROBE_SECONDS}s), not the run timeout "
        f"(9999s)")


def test_unfollowed_park_probe_never_sinks_the_trigger(monkeypatch, bind_persona):
    # A diagnostic must never break the thing it observes. If the probe's history
    # read blows up, the trigger still succeeded and must still be reported so.
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    pb = _patch_client(monkeypatch, None, definition=_action_def(),
                       pb_uuid="pbuuid")

    def _boom(*_a, **_kw):
        raise RuntimeError("history read failed")

    monkeypatch.setattr(pb, "execution_history", _boom)
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device",
                          record=_REC, follow=False)
    assert out["ok"] is True and out["triggered"] is True


def test_the_result_names_the_record_the_run_got(monkeypatch, bind_persona):
    """Live: asked to render a template the chat had just created, the model
    passed the record on screen, and reported that output as the new
    template's. The result held only an IRI, so nothing showed the mismatch."""
    from fsr_playbooks.mcp_server import _record_lookup

    class _R:
        status_code = 200

        def json(self):
            return {"name": "Create Next Test Loopback Interface"}

    seen = []
    monkeypatch.setattr(_record_lookup._shared, "live_request_with_reauth",
                        lambda m, c, url, **k: seen.append(url) or _R())
    bind_persona(run_playbook_allow=["Get Metadata Source Data on Device"])
    _patch_client(monkeypatch, None, definition=_action_def(), pb_uuid="pbuuid",
                  history=[{"status": "awaiting", "pk": "2971"}])
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device", record=_REC)
    assert out["code"] == "awaiting_input"
    assert out["ran_on"] == {"iri": _REC, "name": "Create Next Test Loopback Interface"}
    assert seen and seen[-1].endswith(_REC)


def test_a_refused_run_names_no_record(monkeypatch):
    _patch_client(monkeypatch, {"status": "finished"})
    out = tp.run_playbook(playbook="Get Metadata Source Data on Device", record=_REC)
    assert out["code"] == "run_not_permitted" and "ran_on" not in out


# --------------------------------------------------------------------------- #
# auto-run vs. a record this conversation did not create (#179)
# --------------------------------------------------------------------------- #
_CREATED_A = "/api/3/ztpf_templates/6f1c2a10-0000-4000-8000-00000000000a"
_CREATED_B = "/api/3/ztpf_templates/6f1c2a10-0000-4000-8000-00000000000b"
_OTHER_MODULE = "/api/3/alerts/6f1c2a10-0000-4000-8000-00000000000a"


def _bind_created_source(source):
    """Bind SessionState.created_records, as the host does each turn. Skips
    on a framework that predates the field (the connector then leaves auto-run
    as it was, which the unbound test covers)."""
    import dataclasses

    from fsr_playbooks.llm import session_state
    if "created_records" not in {f.name for f in dataclasses.fields(session_state.SessionState)}:
        pytest.skip("framework SessionState has no created_records field")
    return session_state.update(created_records=source)


def _unbind_created_source(token):
    from fsr_playbooks.llm import session_state
    session_state.reset(token)


@pytest.fixture
def created_records():
    """Bind a created-records source for this test. Returns a setter for the IRIs."""
    state = {"iris": []}
    token = _bind_created_source(lambda: list(state["iris"]))

    def _set(*iris):
        state["iris"] = list(iris)
    yield _set
    _unbind_created_source(token)


def _auto_pb(bind_persona):
    bind_persona(run_playbook_allow=["Render"], run_playbook_auto=["Render"])


def test_run_at_other_record_of_created_module_is_carded(bind_persona, created_records):
    # Created A in this conversation; the run targets B (the one on screen).
    _auto_pb(bind_persona)
    created_records(_CREATED_A)
    assert policy._auto_resolver({"playbook": "Render", "record": _CREATED_B}) is False


def test_run_at_the_created_record_still_auto_runs(bind_persona, created_records):
    _auto_pb(bind_persona)
    created_records(_CREATED_A)
    assert policy._auto_resolver({"playbook": "Render", "record": _CREATED_A}) is True


def test_any_of_several_created_records_counts_as_created(bind_persona, created_records):
    _auto_pb(bind_persona)
    created_records(_CREATED_A, _CREATED_B)
    assert policy._auto_resolver({"playbook": "Render", "record": _CREATED_B}) is True
    stranger = "/api/3/ztpf_templates/6f1c2a10-0000-4000-8000-0000000000ff"
    assert policy._auto_resolver({"playbook": "Render", "record": stranger}) is False


def test_omitted_record_defaults_to_on_screen_record_and_cards(bind_persona, created_records):
    # The body runs against the on-screen record when `record` is omitted, so the
    # resolver must judge that record too.
    _auto_pb(bind_persona)
    created_records(_CREATED_A)
    tok = profiles.set_active_record_iri(_CREATED_B)
    try:
        assert policy._auto_resolver({"playbook": "Render"}) is False
    finally:
        profiles.reset_active_record_iri(tok)


def test_created_record_of_another_module_does_not_card(bind_persona, created_records):
    _auto_pb(bind_persona)
    created_records(_OTHER_MODULE)
    assert policy._auto_resolver({"playbook": "Render", "record": _CREATED_B}) is True


def test_no_created_records_leaves_auto_run_unchanged(bind_persona, created_records):
    _auto_pb(bind_persona)
    created_records()
    assert policy._auto_resolver({"playbook": "Render", "record": _CREATED_B}) is True


def test_unbound_created_records_leaves_auto_run_unchanged(bind_persona):
    _auto_pb(bind_persona)
    assert policy._auto_resolver({"playbook": "Render", "record": _CREATED_B}) is True


def test_a_failing_created_records_source_cards(bind_persona):
    _auto_pb(bind_persona)

    def _boom():
        raise RuntimeError("storage down")
    token = _bind_created_source(_boom)
    try:
        assert policy._auto_resolver({"playbook": "Render", "record": _CREATED_B}) is False
    finally:
        _unbind_created_source(token)


def test_unparseable_target_with_created_records_cards(bind_persona, created_records):
    _auto_pb(bind_persona)
    created_records(_CREATED_A)
    assert policy._auto_resolver({"playbook": "Render", "record": "ztpf template X"}) is False


def test_tier_is_3_for_the_other_record_and_2_for_the_created_one(bind_persona, created_records):
    from fsr_playbooks.llm.tools import _resolve_tier
    _auto_pb(bind_persona)
    created_records(_CREATED_A)
    assert _resolve_tier("run_playbook", {"playbook": "Render", "record": _CREATED_B}) == 3
    assert _resolve_tier("run_playbook", {"playbook": "Render", "record": _CREATED_A}) == 2
