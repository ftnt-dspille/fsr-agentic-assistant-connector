"""`enhancement_offer` ACCEPT applies a verified edit to the OPEN playbook.

The UPDATE counterpart of `playbook_offer` accept. Before this existed, enhance
mode had no write path at all: `verify_enhancement` returned
`ready_to_push: True` and the only way an edit could reach the analyst was a
regex in the widget scraping the last ```yaml fence out of the model's prose.
Live, that lost the edit — the model verified one document and re-typed a
different one, and the analyst's "add a manual input step" never landed.

Two invariants matter here and both are load-bearing:

  1. accept must route to `update_playbook`, never `push_playbook`. The create
     path's `overwrite` deletes by a deterministic uuid5-of-name, which a
     designer-authored playbook does not have — so a mis-route would not error,
     it would quietly save a DUPLICATE and leave the original untouched.
  2. the applied text is the card's `final_yaml` (copied from the passing
     verify), never anything re-derived from chat.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def operations():
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


_YAML = ("playbooks:\n  - name: BGP Flap Response\n    steps:\n"
         "      - {name: Ask, type: manual_input}\n")
_IRI = "/api/3/workflows/11111111-2222-3333-4444-555555555555"


def _card(**over):
    card = {"type": "enhancement_offer", "id": "e1",
            "summary": "Adds an approval gate",
            "verified_id": "abc123",
            "final_yaml": _YAML,
            "steps_added": ["Prompt Block IP"],
            "steps_modified": [],
            "ops_summary": [{"label": "Ask", "step_type": "manual_input"}]}
    card.update(over)
    return card


class _CardStorage:
    def __init__(self, card=None):
        self._card = card
        self.cards = []

    def get_card(self, session_id, card_id):
        return self._card

    def save_card(self, session_id, card):
        self.cards.append(card)

    def save_conversation(self, session_id, messages):
        pass

    def append_transcript_assistant(self, session_id, transcript):
        pass


def _patch(operations, monkeypatch, calls, *, update_ok=True, compile_ok=True):
    def compile_yaml(config, params):
        calls.append(("compile", params["yaml"]))
        if not compile_ok:
            return {"ok": False, "errors": [{"message": "nope"}]}
        return {"ok": True, "workflow_json": {"wf": 1}}

    def update_playbook(config, params):
        calls.append(("update", params))
        if not update_ok:
            return {"ok": False, "code": "put_rejected"}
        if params.get("stage"):
            return {"ok": True, "staged": True, "workflow_iri": _IRI,
                    "version_uuid": "v-1", "workflow": {"steps": [], "routes": []}}
        return {"ok": True, "workflow_iri": _IRI, "version_uuid": "v-1",
                "method": "put"}

    def push_playbook(config, params):  # must never be reached
        calls.append(("push", params))
        return {"ok": True, "workflow_uuids": ["u-1"]}

    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "compile_yaml", compile_yaml)
    monkeypatch.setattr(operations, "compile_yaml", compile_yaml)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "compile_yaml", compile_yaml)
    monkeypatch.setattr(operations, "update_playbook", update_playbook)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "update_playbook", update_playbook)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_autonomy"], "push_playbook", push_playbook)
    monkeypatch.setattr(operations, "push_playbook", push_playbook)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_playbooks"], "push_playbook", push_playbook)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "push_playbook", push_playbook)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_scheduling"], "push_playbook", push_playbook)


def test_accept_updates_in_place_and_never_pushes(operations, monkeypatch):
    calls = []
    _patch(operations, monkeypatch, calls)
    res = operations._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "e1", "workflow_iri": _IRI},
        _CardStorage(card=_card()))

    assert ("compile", _YAML) in calls, "must apply the card's verified bytes"
    kinds = [c[0] for c in calls]
    assert "update" in kinds
    assert "push" not in kinds, (
        "an enhancement accept that pushes creates a DUPLICATE playbook and "
        "leaves the analyst's open one unchanged")
    assert res["tags"]["saved"] is True
    # The open playbook's identity is forwarded to the in-place write.
    update_params = next(c[1] for c in calls if c[0] == "update")
    assert update_params["workflow_iri"] == _IRI


def test_card_is_wired_end_to_end_through_the_dispatch_tables(operations):
    """Emitter → card type → gating stop_reason.

    A break anywhere along this chain is silent in exactly the way the original
    bug was: the tool runs, returns ok, and the turn simply ends without the
    card ever gating — so the analyst gets prose and no Apply button, which is
    indistinguishable from "the agent couldn't do it".
    """
    assert operations._CARD_EMITTER_TO_TYPE["emit_enhancement_offer"] == \
        "enhancement_offer"
    assert operations._CARD_STOP_REASON["enhancement_offer"] == \
        "awaiting_enhancement_offer"


def test_missing_open_playbook_id_changes_nothing(operations, monkeypatch):
    """chat_resume carries no entity block; without the id there is no safe
    target, and guessing one would edit the wrong playbook."""
    calls = []
    _patch(operations, monkeypatch, calls)
    res = operations._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "e1"}, _CardStorage(card=_card()))
    assert res["tags"]["saved"] is False
    assert res["tags"]["code"] == "missing_workflow_target"
    assert not calls, "must not compile or write without a target"


def test_expired_card_is_reported_not_guessed(operations, monkeypatch):
    """The verified bytes live on the card. Gone means gone — re-verify."""
    calls = []
    _patch(operations, monkeypatch, calls)
    res = operations._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "e1", "workflow_iri": _IRI},
        _CardStorage(card=None))
    assert res["tags"]["saved"] is False
    assert res["tags"]["code"] == "offer_expired"
    assert not calls


def test_failed_write_says_the_playbook_is_unchanged(operations, monkeypatch):
    calls = []
    _patch(operations, monkeypatch, calls, update_ok=False)
    res = operations._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "e1", "workflow_iri": _IRI},
        _CardStorage(card=_card()))
    assert res["tags"]["saved"] is False
    text = " ".join(ev.get("text", "") for ev in res["transcript"])
    assert "unchanged" in text.lower()


def test_compile_failure_does_not_write(operations, monkeypatch):
    calls = []
    _patch(operations, monkeypatch, calls, compile_ok=False)
    res = operations._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "e1", "workflow_iri": _IRI},
        _CardStorage(card=_card()))
    assert res["tags"]["saved"] is False
    assert [c[0] for c in calls] == ["compile"], "must not reach update"


def test_enhancement_offer_card_is_persisted(operations):
    """Accept reads `final_yaml` off the STORED card — the framework's
    verified-id registry is per-worker and per-turn, so if the card isn't
    persisted the accept has nothing to apply."""
    st = _CardStorage()
    transcript = [{"type": "enhancement_offer", "id": "e9",
                   "final_yaml": _YAML}]
    operations._persist_session_state(st, "s3", [{"role": "user"}], transcript)
    assert st.cards and st.cards[0]["id"] == "e9"


def test_accept_from_the_designer_stages_for_the_analysts_save(operations, monkeypatch):
    """apply_to=canvas: restore point + guards run (inside update_playbook's
    stage mode), nothing is written, and the card hands the widget the
    playbook to load into the canvas unsaved. Reported as not saved."""
    calls = []
    _patch(operations, monkeypatch, calls)
    res = operations._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "e1", "workflow_iri": _IRI,
             "apply_to": "canvas"},
        _CardStorage(card=_card()))
    update_params = next(c[1] for c in calls if c[0] == "update")
    assert update_params["stage"] is True
    assert res["tags"]["saved"] is False and res["tags"]["code"] == "staged"
    card = next(f for f in res["transcript"] if f.get("type") == "info_card")
    assert card["variant"] == "playbook_staged"
    assert card["workflow"] == {"steps": [], "routes": []}
    assert card["version_uuid"] == "v-1"
    # The canvas now holds this text; the widget grounds the next turn on it
    # (live: turn 2 edited the saved, empty playbook -- "no steps yet").
    assert card["yaml"] == _card()["final_yaml"]
    text = " ".join(f.get("text", "") for f in res["transcript"] if f.get("type") == "text")
    assert "Save" in text and "Versions" in text


def test_accept_without_apply_to_still_writes(operations, monkeypatch):
    calls = []
    _patch(operations, monkeypatch, calls)
    operations._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "e1", "workflow_iri": _IRI},
        _CardStorage(card=_card()))
    update_params = next(c[1] for c in calls if c[0] == "update")
    assert not update_params.get("stage")


def _decline_text(operations, card):
    res = operations._chat_resume_routes(
        {}, {"session_id": "s1", "offer_id": "e1"}, "decline",
        _CardStorage(card=card), None, None)
    return " ".join(f.get("text", "") for f in res.get("transcript") or []
                    if isinstance(f, dict))


def test_declining_an_edit_does_not_say_the_build_is_over(operations):
    # Analyst sim: declining one unwanted step read "I won't save this as a
    # playbook", and the analyst took the build as abandoned.
    text = _decline_text(operations, _card())
    assert "won't save this as a playbook" not in text
    assert "left the playbook as it is" in text


def test_declining_a_create_offer_keeps_its_wording(operations):
    text = _decline_text(operations, {"type": "playbook_offer", "id": "e1"})
    assert "won't save this as a playbook" in text


def test_apply_with_a_new_child_names_only_this_canvas_steps(operations, monkeypatch):
    # Analyst sim: "I added Start, Block on FortiGate" -- the CHILD's steps --
    # read as if the child were on this canvas; the analyst kept looking for it.
    calls = []
    _patch(operations, monkeypatch, calls)
    real = operations.update_playbook

    def with_child(config, params):
        out = real(config, params)
        out["created_playbooks"] = [{"name": "Block IP", "uuid": "c-1"}]
        return out
    monkeypatch.setattr(operations, "update_playbook", with_child)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "update_playbook", with_child)
    card = _card(playbooks_added=["Block IP"],
                 steps_added=["Call Block", "Start", "Block on FortiGate"],
                 changes=[{"playbook": "P", "step": "Call Block", "kind": "added"},
                          {"playbook": "Block IP", "step": "Start", "kind": "added"},
                          {"playbook": "Block IP", "step": "Block on FortiGate", "kind": "added"}])
    res = operations._resume_enhancement_offer_accept(
        {}, {"session_id": "s1", "offer_id": "e1", "workflow_iri": _IRI,
             "apply_to": "canvas"}, _CardStorage(card=card))
    text = " ".join(f.get("text", "") for f in res["transcript"] if f.get("type") == "text")
    assert "added “Call Block”" in text and "Block on FortiGate" not in text
    assert "I also saved “Block IP” as its own playbook" in text
    assert "not on this canvas" in text
