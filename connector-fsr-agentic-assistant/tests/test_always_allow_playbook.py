""""Always allow" on a run-playbook approval card.

A persona's `run_playbook.allow` / `auto` lists decide which playbooks run
without asking. Before this, widening them meant editing the persona record by
hand. Now the run card offers "Always allow" when the persona would have asked,
and approving with it adds the playbook to both lists on the record the
resolver reads: the `assistant_personas` row when there is one, else the Key
Store entry. The approved run goes ahead whether or not the grant lands.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = "Render Script - Template"


@pytest.fixture(scope="module")
def ops():
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
    spec = importlib.util.spec_from_file_location("_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class _Box:
    """`/api/3/assistant_personas` and `/api/3/keys`, with PUTs recorded."""
    base_url = ""

    def __init__(self, persona_row=None, key_row=None):
        self.persona_row, self.key_row, self.puts = persona_row, key_row, []

    def request(self, method, client, url, json=None, **_):
        path = url.split("?", 1)[0]
        if method == "put":
            self.puts.append((path, json))
            return _Resp(200, json)
        if path == "/api/3/assistant_personas":
            return _Resp(200, {"hydra:member": [self.persona_row] if self.persona_row else []})
        if path == "/api/3/keys":
            return _Resp(200, {"hydra:member": [self.key_row] if self.key_row else []})
        return _Resp(404)


@pytest.fixture
def box(monkeypatch):
    from fsr_soc_triage import _common

    def make(**rows):
        b = _Box(**rows)
        monkeypatch.setattr(_common, "live_request", b.request)
        return b
    return make


_KEY = {"@id": "/api/3/keys/k1", "key": "fsr_assistant_profile:ztpf_templates",
        "jSONValue": {"module": "ztpf_templates", "prompt": "p", "tools": {"allow": ["run_playbook"]},
                      "run_playbook": {"allow": ["Other"], "auto": []}}}


def test_grant_extends_the_key_store_persona_and_keeps_the_rest(box):
    from fsr_soc_triage.profiles import grant_playbook
    b = box(key_row=_KEY)
    out = grant_playbook(b, "ztpf_templates", PB)
    assert out == {"ok": True, "source": "keys", "module": "ztpf_templates", "playbook": PB}
    (path, body), = b.puts
    assert path == "/api/3/keys/k1"
    jv = body["jSONValue"]
    assert jv["run_playbook"] == {"allow": ["Other", PB], "auto": [PB]}
    assert jv["prompt"] == "p" and jv["tools"] == {"allow": ["run_playbook"]}


def test_grant_prefers_the_personas_module_row(box):
    from fsr_soc_triage.profiles import grant_playbook
    row = {"@id": "/api/3/assistant_personas/p1", "scopeModule": "ztpf_templates",
           "runPlaybookAllow": json.dumps([PB]), "runPlaybookAuto": "[]"}
    b = box(persona_row=row, key_row=_KEY)
    out = grant_playbook(b, "ztpf_templates", PB)
    assert out["ok"] and out["source"] == "assistant_personas"
    (path, body), = b.puts
    assert path == "/api/3/assistant_personas/p1"
    assert json.loads(body["runPlaybookAllow"]) == [PB]      # not duplicated
    assert json.loads(body["runPlaybookAuto"]) == [PB]


def test_no_persona_is_a_refusal_not_a_write(box):
    from fsr_soc_triage.profiles import grant_playbook
    b = box()
    out = grant_playbook(b, "ztpf_templates", PB)
    assert out["ok"] is False and out["code"] == "no_persona" and b.puts == []


def _profile(**kw):
    from fsr_soc_triage.profiles import Profile
    return Profile(module="ztpf_templates", prompt="p", **kw)


def test_the_card_offers_it_only_when_the_persona_would_ask(ops):
    from fsr_soc_triage import profiles
    tok = profiles.set_active_profile(_profile(run_playbook_allow=[PB]))
    try:
        assert ops._always_allow_offer(PB) == {"persona": "ztpf_templates", "playbook": PB}
    finally:
        profiles.reset_active_profile(tok)
    tok = profiles.set_active_profile(_profile(run_playbook_allow=[PB], run_playbook_auto=[PB]))
    try:
        assert ops._always_allow_offer(PB) is None
    finally:
        profiles.reset_active_profile(tok)
    assert ops._always_allow_offer(PB) is None      # no persona this turn


class _Storage:
    def __init__(self, cards):
        self.cards = cards

    def get_card(self, session_id, card_id):
        return self.cards.get((session_id, card_id))


class _Gateway:
    def __init__(self, suspended):
        self.suspended = suspended

    def pop(self, approval_id):
        return self.suspended.pop(approval_id, None)


def _route(ops, monkeypatch, *, remember, decision="approve", card_extra=None):
    order = []
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_resume_suspended",
                        lambda p, s, d, st, session_id=None, config=None:
                        order.append("run") or {"ok": True})
    from fsr_soc_triage import profiles
    monkeypatch.setattr(profiles, "grant_playbook",
                        lambda c, m, p: order.append(("grant", m, p)) or
                        {"ok": True, "source": "keys", "module": m, "playbook": p})
    card = {"type": "approval_request", "approval_id": "a1", **(card_extra or {})}
    ops._PROFILE_CACHE[("s1", "ztpf_templates")] = (0.0, "stale")
    out = ops._chat_resume_routes({}, {"session_id": "s1", "approval_id": "a1",
                                       "remember": remember}, decision,
                                  _Storage({("s1", "a1"): card}), _Gateway({"a1": object()}), None)
    return out, order


_OFFER = {"always_allow": {"persona": "ztpf_templates", "playbook": PB}}


def test_approve_with_remember_grants_before_the_run(ops, monkeypatch):
    out, order = _route(ops, monkeypatch, remember=True, card_extra=_OFFER)
    assert order == [("grant", "ztpf_templates", PB), "run"]
    assert out["always_allow"]["ok"] is True
    assert ("s1", "ztpf_templates") not in ops._PROFILE_CACHE


def test_plain_approve_and_reject_grant_nothing(ops, monkeypatch):
    _, order = _route(ops, monkeypatch, remember=False, card_extra=_OFFER)
    assert order == ["run"]
    _, order = _route(ops, monkeypatch, remember=True, decision="reject", card_extra=_OFFER)
    assert order == ["run"]


def test_remember_on_a_card_that_offered_nothing_grants_nothing(ops, monkeypatch):
    out, order = _route(ops, monkeypatch, remember=True)
    assert order == ["run"] and "always_allow" not in out


def test_the_offer_survives_the_final_transcript_conversion(ops, monkeypatch):
    """Live: the run card never offered "Always allow". The event is converted
    once inside the turn (persona bound) and again for the final transcript
    after the persona is reset; the second conversion found no persona, and that
    copy is the one returned and stored."""
    from fsr_playbooks.llm.provider import ApprovalRequestEvent
    from fsr_soc_triage import profiles
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_turn_io"], "_playbook_run_context", lambda name, uuid: None)
    ev = ApprovalRequestEvent(approval_id="ap-1", tool_use_id="tu-1", tool="run_playbook",
                              tier=3, preview={"args": {"playbook": PB}}, args_hash="h")
    tok = profiles.set_active_profile(_profile())
    try:
        live = ops._event_to_wire(ev)
    finally:
        profiles.reset_active_profile(tok)
    final = ops._event_to_wire(ev)
    assert live["always_allow"] == final["always_allow"] == {"persona": "ztpf_templates",
                                                             "playbook": PB}
