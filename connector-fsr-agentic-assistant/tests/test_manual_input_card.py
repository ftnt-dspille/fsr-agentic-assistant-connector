"""The awaiting-form manual_input card: a run_playbook/resume_playbook result that
paused on a fillable form is rendered as a gating manual_input card that truncates
the turn (stop_reason awaiting_manual_input). Covers the field→control mapping and
the _wire_transcript splice."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from fsr_playbooks.llm.provider import ToolUseEvent, ToolResultEvent, TextEvent

ROOT = Path(__file__).resolve().parent.parent



def _joined(ops, *args, **kw) -> str:
    """Facts + turn-scoped directive, joined the way `_resume_conversation`
    joins them for the resumed turn. Only the facts are persisted; see
    `tests/test_resume_directive_is_turn_scoped.py` for why the two are
    separate now. These assertions are about what the model reads."""
    durable, directive = ops._manual_input_user_message(*args, **kw)
    return f"{durable}\n{directive}" if directive else durable


@pytest.fixture(scope="module")
def ops():
    # The connector dir name has a hyphen so `operations` isn't importable
    # normally; stub the FSR platform import and load it under a synthetic pkg.
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


_AWAITING = {
    "ok": False, "code": "awaiting_input", "run_pk": "2971", "triggered": True,
    "awaiting": {
        "manual_input_id": 126,
        "title": "Choose a Manager and a Device",
        "description": "1. Choose a Manager and then a Device.",
        "is_approval": False,
        "fields": [
            {"name": "managerAndDevice", "label": "Manager and Device",
             "type": "array", "data_type": "dynamicList",
             "options": [{"value": "FMG (v8.0.0)",
                          "options": ["CORE (VM64)", "FG1 (VM64)"]}]},
            {"name": "populateInputParameters", "label": "Populate",
             "type": "boolean", "data_type": "checkbox"},
            {"name": "inputParameters", "label": "Input Parameters",
             "type": "text", "data_type": "text"},
        ],
        "options": [{"option": "Ok", "primary": True},
                    {"option": "Cancel", "primary": False}],
    },
}


# --------------------------------------------------------------------------- #
# field → widget control mapping
# --------------------------------------------------------------------------- #
def test_dynamiclist_maps_to_dependent_groups(ops):
    f = _AWAITING["awaiting"]["fields"][0]
    out = ops._manual_field_to_widget(f)
    assert out["type"] == "dynamic_list"
    assert out["groups"] == [{"value": "FMG (v8.0.0)",
                              "options": ["CORE (VM64)", "FG1 (VM64)"]}]
    assert out["name"] == "managerAndDevice"


def test_boolean_and_checkbox_map_to_checkbox(ops):
    assert ops._manual_field_to_widget(
        {"name": "b", "type": "boolean"})["type"] == "checkbox"
    assert ops._manual_field_to_widget(
        {"name": "c", "data_type": "checkbox"})["type"] == "checkbox"


def test_plain_field_maps_to_text(ops):
    assert ops._manual_field_to_widget(
        {"name": "t", "type": "text"})["type"] == "text"


def test_flat_dynamiclist_maps_to_a_single_select(ops):
    """A dynamicList over SCALARS is one dropdown, not the dependent pair.

    Live regression (.206, "Add Steps to Device from Profile"): this field's
    `options` render to a flat `["ProfileA", …]`, but it was mapped to
    `dynamic_list`, so the widget drew two dropdowns, the dependent one had
    nothing to offer, and the submit collapsed to `[]` -- the run resumed with
    an empty selection and failed downstream."""
    out = ops._manual_field_to_widget(
        {"name": "profile", "label": "Profile", "type": "array",
         "data_type": "dynamicList",
         "options": ["ZTP Base", "ZTP Branch", "ZTP Edge"]})
    assert out["type"] == "select"
    assert out["options"] == ["ZTP Base", "ZTP Branch", "ZTP Edge"]
    assert "groups" not in out


def test_dynamiclist_without_rendered_options_is_not_a_dead_picker(ops):
    """Unrendered options (the definition's Jinja string is never forwarded)
    leave no choices at all -- a text box the analyst can answer beats two
    empty dropdowns they cannot."""
    out = ops._manual_field_to_widget(
        {"name": "profile", "type": "array", "data_type": "dynamicList"})
    assert out["type"] == "text"


def test_required_flag_is_forwarded(ops):
    assert ops._manual_field_to_widget(
        {"name": "p", "type": "text", "required": True})["required"] is True
    assert "required" not in ops._manual_field_to_widget(
        {"name": "p", "type": "text"})


# --------------------------------------------------------------------------- #
# card synthesis
# --------------------------------------------------------------------------- #
def test_card_from_awaiting_carries_run_iri_and_fields(ops):
    card = ops._manual_input_card_from_awaiting(_AWAITING, "tu-run", 5)
    assert card["type"] == "manual_input"
    assert card["id"] == "126"
    assert card["workflow_run_iri"] == "/wf/api/workflows/2971/"
    assert card["preceding_tool_use_id"] == "tu-run"
    assert card["question"].startswith("Choose a Manager and a Device")
    assert [f["type"] for f in card["fields"]] == [
        "dynamic_list", "checkbox", "text"]


def test_button_only_gate_is_not_carded(ops):
    # No fields ⇒ a pure Ok/Cancel gate the model resumes itself, not a form.
    content = {"code": "awaiting_input", "run_pk": "9",
               "awaiting": {"manual_input_id": 1, "fields": [],
                            "options": [{"option": "Ok", "primary": True}]}}
    assert ops._manual_input_card_from_awaiting(content, "tu", 1) is None


def test_non_awaiting_result_is_not_carded(ops):
    assert ops._manual_input_card_from_awaiting(
        {"ok": True, "status": "finished"}, "tu", 1) is None


# --------------------------------------------------------------------------- #
# _wire_transcript splice + truncate
# --------------------------------------------------------------------------- #
def test_wire_transcript_splices_manual_input_card_and_truncates(ops):
    transcript = [
        ToolUseEvent(call_id="tu-run", name="run_playbook",
                     arguments={"playbook": "P"}, tier=2),
        ToolResultEvent(call_id="tu-run", result=_AWAITING),
        # A trailing text event that MUST be truncated away (card is terminal).
        ToolUseEvent(call_id="tu-x", name="get_record",
                     arguments={}, tier=0),
    ]
    wired = ops._wire_transcript(transcript)
    assert wired[-1]["type"] == "manual_input"
    assert wired[-1]["preceding_tool_use_id"] == "tu-run"
    # Truncated: the trailing get_record tool_use is gone.
    assert not any(w.get("name") == "get_record" for w in wired)
    # stop_reason bubbles to awaiting_manual_input.
    assert ops._envelope_stop_reason(wired, "end_turn") == "awaiting_manual_input"


def test_resume_playbook_awaiting_also_cards(ops):
    transcript = [
        ToolUseEvent(call_id="tu-res", name="resume_playbook",
                     arguments={"run": "2971"}, tier=1),
        ToolResultEvent(call_id="tu-res", result=_AWAITING),
    ]
    wired = ops._wire_transcript(transcript)
    assert wired[-1]["type"] == "manual_input"
    assert wired[-1]["id"] == "126"


def test_orphan_awaiting_tool_result_from_approval_resume_still_cards(ops):
    # THE tier-3 approval-resume case: run_playbook's `tool_use` was in the PRIOR
    # turn (before the approval gate), so this resume turn's transcript OPENS with
    # an ORPHAN tool_result -- its call_id has no matching tool_use here, so the
    # tool name resolves to None. The awaiting form must STILL become a card
    # (regression: the strict name gate skipped it, so the paused run only ever
    # produced a "what value?" narration and never the fillable form).
    transcript = [
        ToolResultEvent(call_id="tu-run-prior-turn", result=_AWAITING),
        # The model then narrated instead of the connector carding -- must truncate.
        TextEvent(text="What value would you like to enter?"),
    ]
    wired = ops._wire_transcript(transcript)
    assert wired[-1]["type"] == "manual_input"
    assert wired[-1]["id"] == "126"
    assert not any(w.get("type") == "text" for w in wired), "narration truncated"
    assert ops._envelope_stop_reason(wired, "end_turn") == "awaiting_manual_input"


def test_orphan_non_awaiting_tool_result_is_not_carded(ops):
    # The relaxed gate must not false-positive: an orphan tool_result that is NOT
    # an awaiting form (any other prior-turn tool) yields no card.
    transcript = [
        ToolResultEvent(call_id="tu-orphan", result={"ok": True, "status": "finished"}),
        TextEvent(text="Done."),
    ]
    wired = ops._wire_transcript(transcript)
    assert not any(w.get("type") == "manual_input" for w in wired)


# --------------------------------------------------------------------------- #
# submit: _forward_manual_input rides pyfsr answer() (not a hand-rolled resume)
# --------------------------------------------------------------------------- #
class _StubMI:
    """Mirrors the pyfsr ManualInputAPI surface _forward_manual_input uses:
    retrieve (no owners) → response_mapping/options + step ids, then resume."""
    def __init__(self, options=None):
        self.calls = []
        # Extra top-level keys the real record carries (e.g. `timeout`, the
        # gate's absolute deadline). Set per-test.
        self.payload_extra: dict = {}
        self._options = options if options is not None else [
            {"option": "Ok", "primary": True, "step_iri": "/api/3/workflow_steps/ok"},
            {"option": "Cancel", "primary": False, "step_iri": "/api/3/workflow_steps/cx"},
        ]

    def retrieve(self, input_id, *, typed=True, owners=None, **_kw):
        self.calls.append({"op": "retrieve", "input_id": input_id, "owners": owners})
        return {"workflow": 2971, "step_id": 555,
                "response_mapping": {"options": self._options},
                **self.payload_extra}

    def _resolve_user_iri(self):
        return "/api/3/people/admin"

    def resume(self, workflow_id, *, step_iri=None, step_id=None,
               manual_input_id=None, user=None, input=None, **_kw):
        self.calls.append({"op": "resume", "workflow_id": workflow_id,
                           "step_iri": step_iri, "step_id": step_id,
                           "manual_input_id": manual_input_id, "user": user,
                           "input": input})
        return {"task_id": "t-1", "message": "resumed"}


class _StubClient:
    def __init__(self, mi):
        self.manual_input = mi


def _patch_live_client(monkeypatch, client):
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: client)


def test_forward_manual_input_retrieves_then_resumes(ops, monkeypatch):
    mi = _StubMI()
    _patch_live_client(monkeypatch, _StubClient(mi))
    vals = {"managerAndDevice": ["FMG (v8.0.0)", "CORE (VM64)"],
            "populateInputParameters": False}
    out = ops._forward_manual_input("126", "approve", vals)
    assert out["forwarded"] is True and out["manual_input_id"] == 126
    assert out["option"] == "Ok"
    # retrieve WITHOUT owners (the on-box path that returns options), then resume
    # via the PRIMARY option's step_iri with the values under their field names.
    retrieve, resume = mi.calls
    assert retrieve == {"op": "retrieve", "input_id": 126, "owners": None}
    assert resume == {"op": "resume", "workflow_id": 2971,
                      "step_iri": "/api/3/workflow_steps/ok", "step_id": 555,
                      "manual_input_id": 126, "user": "/api/3/people/admin",
                      "input": vals}


def test_forward_manual_input_no_options_degrades(ops, monkeypatch):
    mi = _StubMI(options=[])  # retrieve returns no buttons
    _patch_live_client(monkeypatch, _StubClient(mi))
    out = ops._forward_manual_input("126", "approve", {"x": 1})
    assert out["forwarded"] is False and out["reason"] == "no_response_options"


def test_forward_manual_input_dev_degrades_when_no_client(ops, monkeypatch):
    _patch_live_client(monkeypatch, None)
    out = ops._forward_manual_input("126", "approve", {"x": 1})
    assert out["forwarded"] is False and out["reason"] == "crudhub_unavailable"


def test_forward_manual_input_bad_id_degrades(ops, monkeypatch):
    _patch_live_client(monkeypatch, _StubClient(_StubMI()))
    out = ops._forward_manual_input("not-an-int", "approve", {"x": 1})
    assert out["forwarded"] is False and out["reason"] == "bad_input_id"


# --------------------------------------------------------------------------- #
# Fix C -- the bridge message must never present the gate id as a run pk, and
# resume_playbook must refuse a manual_input_id passed as `run`.
# --------------------------------------------------------------------------- #
def test_bridge_message_labels_the_id_and_names_the_run(ops):
    msg = _joined(ops, 
        "508", "approve", {"x": 1},
        {"forwarded": False, "reason": "no_response_options", "run": "9046"})
    assert "manual_input_id=508" in msg
    assert "'508'" not in msg          # never bare, where it reads as a run pk
    assert "9046" in msg
    assert "Do NOT call resume_playbook" in msg


def test_bridge_message_without_a_known_run_still_labels_the_id(ops):
    msg = _joined(ops, 
        "508", "approve", {}, {"forwarded": True, "manual_input_id": 508})
    assert "manual_input_id=508" in msg


def test_forward_falls_back_to_the_worker_map_for_the_run(ops, monkeypatch):
    """When the record does not name its run, the per-worker id→run map is the
    fallback so the bridge message can still name a run pk."""
    class _NoWorkflow(_StubMI):
        def retrieve(self, input_id, *, typed=True, owners=None, **_kw):
            return {"step_id": 555, "response_mapping": {"options": []}}
    from fsr_playbooks.mcp_server import tools_playbook_run as tp
    tp._note_manual_input(126, "9046")
    _patch_live_client(monkeypatch, _StubClient(_NoWorkflow(options=[])))
    out = ops._forward_manual_input("126", "approve", {"x": 1})
    assert out["reason"] == "no_response_options" and out["run"] == "9046"


def test_resume_playbook_rejects_a_manual_input_id_as_run(monkeypatch):
    from fsr_playbooks.mcp_server import tools_playbook_run as tp
    from fsr_soc_triage import tools_playbook as policy
    monkeypatch.setattr(policy, "get_active_profile", lambda: {"name": "x"})
    tp._MANUAL_INPUT_RUNS.clear()
    tp._note_manual_input(508, "9046")
    out = tp.resume_playbook(run="508")
    assert out["ok"] is False and out["code"] == "bad_run_reference"
    assert out["run"] == "9046" and "not a workflow run" in out["message"]


def test_resume_playbook_lets_an_ambiguous_id_through(monkeypatch):
    """An id that is ALSO a known run pk must not be refused -- ids come from
    different tables and refusing a valid resume is worse than the old error."""
    from fsr_playbooks.mcp_server import tools_playbook_run as tp
    from fsr_soc_triage import tools_playbook as policy
    monkeypatch.setattr(policy, "get_active_profile", lambda: {"name": "x"})
    monkeypatch.setattr(tp._shared, "_live_client", lambda: None)
    tp._MANUAL_INPUT_RUNS.clear()
    tp._note_manual_input(508, "9046")
    tp._note_manual_input(777, "508")   # 508 is also a run pk we've seen
    out = tp.resume_playbook(run="508")
    assert out["code"] == "no_fsr_configured"


# --------------------------------------------------------------------------- #
# Fix B -- the gate's own timeout. Live on .206 the ZTPF metadata-source
# playbook's manual_input step carries a ONE-MINUTE timeout; the run resolves
# down its timeout branch and finishes, so a submit arriving later can never
# land. The old code called that `no_response_options` and then told the model
# to narrate as if the input had been accepted.
# --------------------------------------------------------------------------- #
_EXPIRING = {
    "ok": False, "code": "awaiting_input", "run_pk": "9051", "triggered": True,
    "awaiting": dict(_AWAITING["awaiting"],
                     expires_at="2026-08-03T10:25:07.793745Z",
                     expires_in_seconds=95),
}


def test_card_tells_the_analyst_the_form_expires(ops):
    card = ops._manual_input_card_from_awaiting(_EXPIRING, "tu_1", 0)
    assert "expires in about 1m 35s" in card["question"]
    assert card["expires_in_seconds"] == 95
    assert card["expires_at"] == "2026-08-03T10:25:07.793745Z"


def test_card_without_a_deadline_says_nothing_about_expiry(ops):
    card = ops._manual_input_card_from_awaiting(_AWAITING, "tu_1", 0)
    assert "expires" not in card["question"]
    assert card.get("expires_in_seconds") in (None, 0)


def test_expired_gate_is_named_not_reported_as_no_options(ops, monkeypatch):
    mi = _StubMI(options=[])
    mi.payload_extra = {"timeout": "2000-01-01T00:00:00Z"}
    _patch_live_client(monkeypatch, _StubClient(mi))
    out = ops._forward_manual_input("126", "approve", {"x": 1})
    assert out["forwarded"] is False and out["reason"] == "gate_expired"
    assert out["expired_seconds_ago"] > 0


def test_a_live_gate_with_no_buttons_is_still_no_response_options(ops, monkeypatch):
    mi = _StubMI(options=[])
    mi.payload_extra = {"timeout": "2999-01-01T00:00:00Z"}
    _patch_live_client(monkeypatch, _StubClient(mi))
    out = ops._forward_manual_input("126", "approve", {"x": 1})
    assert out["reason"] == "no_response_options"


def test_expired_bridge_message_refuses_to_claim_acceptance(ops):
    msg = _joined(ops, 
        "512", "approve", {"x": 1},
        {"forwarded": False, "reason": "gate_expired", "run": "9051",
         "expired_seconds_ago": 42})
    assert "NOT applied" in msg
    assert "as if the input was accepted" not in msg
    assert "expired" in msg and "9051" in msg
    assert "Do NOT call resume_playbook" in msg


def test_unreachable_bridge_no_longer_invites_a_false_narrative(ops):
    msg = _joined(ops, 
        "512", "approve", {}, {"forwarded": False, "reason": "forward_failed"})
    assert "as if the input was accepted" not in msg
    assert "could not be delivered" in msg


def test_empty_retrieve_is_read_as_an_expired_gate(ops, monkeypatch):
    """Live on .206: a lapsed manual input retrieves as `{}` while a live one
    returns the full record. Empty ⇒ the gate is gone, not "no buttons"."""
    class _Gone(_StubMI):
        def retrieve(self, input_id, *, typed=True, owners=None, **_kw):
            return {}
    _patch_live_client(monkeypatch, _StubClient(_Gone()))
    out = ops._forward_manual_input("513", "approve", {"x": 1})
    assert out["forwarded"] is False and out["reason"] == "gate_expired"


def test_forward_takes_the_run_pk_from_the_record_not_the_worker_map(ops, monkeypatch):
    """The id→run map is per-worker memory and the box runs a pool, so the
    record's own `workflow` must win (live: `run` came back null)."""
    from fsr_playbooks.mcp_server import tools_playbook_run as tp
    tp._MANUAL_INPUT_RUNS.clear()
    mi = _StubMI(options=[])
    _patch_live_client(monkeypatch, _StubClient(mi))
    out = ops._forward_manual_input("126", "approve", {"x": 1})
    assert out["run"] == "2971"          # _StubMI.retrieve's workflow


# --------------------------------------------------------------------------- #
# Buttons-only pauses and the analyst's choice of button. Live, the
# metadata-source run paused a second time on "Data Collected" (Ok / Save
# Example, the collected data in its text); nothing showed it, and the card
# could only ever send the primary button.
# --------------------------------------------------------------------------- #
_RESULTS = {"ok": False, "code": "awaiting_input", "run_pk": "80929",
            "awaiting": {"manual_input_id": 652, "title": "Data Collected",
                         "description": "**Metadata Results**\n```\n{\"available\": []}\n```",
                         "fields": [],
                         "options": [{"option": "Ok", "primary": True},
                                     {"option": "Save Example", "primary": False}]}}


def test_a_buttons_only_pause_with_a_choice_is_carded(ops):
    card = ops._manual_input_card_from_awaiting(_RESULTS, "tu-1", 0)
    assert card is not None and card["fields"] == []
    assert card["buttons"] == [{"label": "Ok", "primary": True},
                               {"label": "Save Example", "primary": False}]
    assert "Metadata Results" in card["question"]


def test_a_single_button_pause_is_not_carded(ops):
    one = {**_RESULTS, "awaiting": {**_RESULTS["awaiting"],
                                    "options": [{"option": "Ok", "primary": True}]}}
    assert ops._manual_input_card_from_awaiting(one, "tu-1", 0) is None


def test_a_form_card_carries_its_buttons_too(ops):
    card = ops._manual_input_card_from_awaiting(_AWAITING, "tu-1", 0)
    assert [b["label"] for b in card["buttons"]] == ["Ok", "Cancel"]


def test_forward_honours_the_chosen_button(ops, monkeypatch):
    mi = _StubMI(options=[
        {"option": "Ok", "primary": True, "step_iri": "/api/3/workflow_steps/ok"},
        {"option": "Save Example", "primary": False, "step_iri": "/api/3/workflow_steps/save"}])
    _patch_live_client(monkeypatch, _StubClient(mi))
    out = ops._forward_manual_input("652", "approve", {}, "save example")
    assert out["forwarded"] is True and out["option"] == "Save Example"
    assert mi.calls[-1]["step_iri"] == "/api/3/workflow_steps/save"


def test_an_unknown_button_is_refused_not_mapped_to_the_primary(ops, monkeypatch):
    mi = _StubMI()
    _patch_live_client(monkeypatch, _StubClient(mi))
    out = ops._forward_manual_input("652", "approve", {}, "Save as YAML")
    assert out["forwarded"] is False and out["reason"] == "bad_option"
    assert out["options"] == ["Ok", "Cancel"]
    assert all(c["op"] != "resume" for c in mi.calls)


def test_next_pause_text_reaches_the_model(ops):
    fact, directive = ops._next_pause_message(
        {"form": _RESULTS["awaiting"], "status": "awaiting"}, "80929")
    assert "Data Collected" in fact and "Ok / Save Example" in fact
    assert "Metadata Results" in fact
    assert "Do NOT call resume_playbook" in directive


def test_a_single_button_next_pause_may_be_acknowledged_by_the_agent(ops):
    form = {**_RESULTS["awaiting"], "options": [{"option": "Ok", "primary": True}]}
    _, directive = ops._next_pause_message({"form": form, "status": "awaiting"}, "80929")
    assert "resume_playbook(run='80929'" in directive


def test_a_run_that_ended_says_so(ops):
    fact, _ = ops._next_pause_message({"form": None, "status": "finished"}, "80929")
    assert "ended with status finished" in fact


def test_submit_appends_the_next_pause_as_a_card(ops, monkeypatch):
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_check_health", lambda c: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_health"], "_check_health", lambda c: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_manual"], "_check_health", lambda c: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_build_provider", lambda c, g: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_manual"], "_build_provider", lambda c, g: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_manual"], "_forward_manual_input",
                        lambda i, d, v, o=None: {"forwarded": True, "run": "80929",
                                                 "manual_input_id": 651, "option": "Ok"})
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_manual"], "_next_pause_after_forward",
                        lambda f: {"form": _RESULTS["awaiting"], "status": "awaiting"})
    seen = {}

    def fake_resume(config, params, decision, storage, provider, *, user_msg, resume_tag,
                    turn_directive):
        seen["user_msg"], seen["directive"] = user_msg, turn_directive
        return {"ok": True, "stop_reason": "end_turn",
                "transcript": [{"type": "text", "text": "It collected the firmware list."}]}

    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_manual"], "_resume_conversation", fake_resume)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_resume_conversation", fake_resume)
    out = ops.respond_manual_input({}, {"session_id": "s1", "input_id": 651,
                                        "values": {"managerAndDevice": ["FMG", "CORE"]}})
    assert "Data Collected" in seen["user_msg"]
    assert out["stop_reason"] == "awaiting_manual_input"
    assert out["transcript"][-1]["type"] == "manual_input"
    assert out["transcript"][-1]["buttons"][1]["label"] == "Save Example"
