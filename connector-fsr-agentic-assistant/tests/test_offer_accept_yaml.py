"""§A — direct-build playbook_offer carries the final YAML.

A build turn that hand-authored YAML ends with
`emit_playbook_offer(yaml=…)`; the card's `final_yaml` is persisted
(`playbook_offer` joined `_GATE_TYPES`) and accept compiles + pushes THAT
text instead of replaying the (empty) session trace.
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


_YAML = "playbooks:\n  - name: Direct Build\n    steps: []\n"


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


def _patch_compile_push(operations, monkeypatch, calls):
    def compile_yaml(config, params):
        calls.append(("compile", params["yaml"]))
        return {"ok": True, "workflow_json": {"wf": 1}}

    def push_playbook(config, params):
        calls.append(("push", params["workflow_json"]))
        return {"ok": True, "workflow_uuids": ["u-1"], "collection_uuid": "c-1"}

    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "compile_yaml", compile_yaml)
    monkeypatch.setattr(operations, "compile_yaml", compile_yaml)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "compile_yaml", compile_yaml)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_autonomy"], "push_playbook", push_playbook)
    monkeypatch.setattr(operations, "push_playbook", push_playbook)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_playbooks"], "push_playbook", push_playbook)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "push_playbook", push_playbook)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_scheduling"], "push_playbook", push_playbook)


def test_accept_uses_card_final_yaml_not_trace(operations, monkeypatch):
    calls = []
    _patch_compile_push(operations, monkeypatch, calls)
    st = _CardStorage(card={"type": "playbook_offer", "id": "o1",
                            "final_yaml": _YAML,
                            "title_suggestion": "Direct Build",
                            "ops_summary": [{"label": "Start"}]})
    res = operations._resume_playbook_offer_accept(
        {}, {"session_id": "s1", "offer_id": "o1"}, st)
    assert ("compile", _YAML) in calls
    assert calls[-1][0] == "push"
    assert res["tags"]["saved"] is True
    # title fell back to the card's suggestion
    assert any("Direct Build" in (ev.get("text") or "")
               for ev in res["transcript"] if ev.get("type") == "text")


def test_accept_without_final_yaml_falls_back_to_trace(operations, monkeypatch):
    calls = []
    _patch_compile_push(operations, monkeypatch, calls)
    st = _CardStorage(card=None)

    import fsr_playbooks.mcp_server.tools_compile as tc
    monkeypatch.setattr(tc, "build_playbook_from_trace",
                        lambda trace_json, name: {"ok": False,
                                                  "code": "empty_trace",
                                                  "message": "no trace"})
    res = operations._resume_playbook_offer_accept(
        {}, {"session_id": "s2", "offer_id": "missing"}, st)
    assert res["tags"]["saved"] is False
    assert not calls  # never compiled/pushed


def test_playbook_offer_card_is_persisted(operations):
    st = _CardStorage()
    transcript = [{"type": "playbook_offer", "id": "o9",
                   "final_yaml": _YAML}]
    operations._persist_session_state(st, "s3", [{"role": "user"}], transcript)
    assert st.cards and st.cards[0]["id"] == "o9"
