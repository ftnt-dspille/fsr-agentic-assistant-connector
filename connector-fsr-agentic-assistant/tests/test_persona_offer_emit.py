"""`persona_offer` EMIT: the connector drafts a persona and offers to save it.

The emit half's logic, tested without driving a full turn. The invariants:
  * the emitted turn validates as a ChatTurnResponse and carries a first-class
    `persona_offer` frame (not the UnknownEvent catch-all);
  * the drafted card is STORED, so the accept resume can read it -- proven by an
    end-to-end emit -> accept round-trip that writes the Key Store;
  * no mounted module / no goal / an unusable draft each return an explanatory
    turn with NO offer and NO stored card.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KNOWN = {"get_record", "search_module_records", "create_record"}


@pytest.fixture(scope="module")
def mods():
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
    emit = importlib.import_module("_fsrpb_conn.ops_persona_emit")
    return types.SimpleNamespace(operations=mod, emit=emit,
                                 models=sys.modules["_fsrpb_conn.pydantic_models"])


class _FakeProvider:
    def __init__(self, reply):
        self._reply = reply

    async def stream(self, *, system, messages, tools, tags, max_tool_turns):
        from fsr_playbooks.llm.provider import TextEvent
        yield TextEvent(text=self._reply)


class _Storage:
    def __init__(self):
        self.cards = {}
        self._turn = 0

    def reserve_next_turn(self, session_id):
        self._turn += 1
        return self._turn

    def save_card(self, session_id, card):
        self.cards[(session_id, card["id"])] = card

    def get_card(self, session_id, card_id):
        return self.cards.get((session_id, card_id))

    def append_transcript_assistant(self, session_id, transcript):
        pass


_DRAFT = json.dumps({
    "value": "You triage alerts on the alerts module. Investigate, cite, verdict.",
    "jSONValue": {"label": "Alert Triage",
                  "tools": {"allow": ["get_record", "search_module_records"]},
                  "may_write": [], "ui": {"label": "Alert Triage"}}})


def _fetch_ok(url, method, body=None):
    return {"hydra:member": [{"@id": "/api/3/alerts/u1", "name": "A", "severity": "High"}]}


@pytest.fixture(autouse=True)
def _deterministic_catalog(mods, monkeypatch):
    import fsr_soc_triage.persona_author as pa
    monkeypatch.setattr(pa, "default_known_tools", lambda: set(KNOWN))


def test_persona_offer_frame_is_first_class_not_unknown(mods):
    m = mods.models
    # The union resolves a persona_offer frame to PersonaOfferEvent, left-to-right.
    import pydantic
    adapter = pydantic.TypeAdapter(m.PollFrame)
    got = adapter.validate_python({"type": "persona_offer", "module": "alerts",
                                   "value": "p", "jSONValue": {}})
    assert type(got).__name__ == "PersonaOfferEvent"


def test_emit_returns_a_valid_turn_with_an_offer_and_stores_the_card(mods):
    storage = _Storage()
    res = mods.emit.build_persona_offer(
        {}, {"session_id": "s1", "entity": {"module": "alerts"},
             "goal": "triage alerts and recommend a verdict"},
        storage, _FakeProvider(_DRAFT), fetch=_fetch_ok)
    assert res["ok"] is True and res["stop_reason"] == "end_turn"
    cards = [f for f in res["transcript"] if f.get("type") == "persona_offer"]
    assert len(cards) == 1
    assert cards[0]["module"] == "alerts"
    assert cards[0]["jSONValue"]["tools"]["allow"] == ["get_record", "search_module_records"]
    # stored so the accept can read it
    assert storage.get_card("s1", cards[0]["id"])["type"] == "persona_offer"


def test_emit_then_accept_writes_the_key_store(mods):
    storage = _Storage()
    emitted = mods.emit.build_persona_offer(
        {}, {"session_id": "s1", "entity": {"module": "alerts"}, "goal": "triage alerts"},
        storage, _FakeProvider(_DRAFT), fetch=_fetch_ok)
    offer_id = next(f["id"] for f in emitted["transcript"] if f.get("type") == "persona_offer")

    writes = []

    def fake_mr(url, method, body=None):
        writes.append((method, url, body))
        return {"hydra:member": []} if method == "GET" else {"@id": "/api/3/keys/new"}

    import _fsrpb_conn.ops_common as oc  # noqa: F401
    import importlib
    oc = importlib.import_module("_fsrpb_conn.ops_common")
    oc._make_request = lambda: fake_mr  # the accept handler resolves transport here

    res = mods.operations._resume_persona_offer_accept(
        {}, {"session_id": "s1", "offer_id": offer_id}, storage)
    assert res["tags"]["saved"] is True
    post = next(c for c in writes if c[0] == "POST")
    assert post[2]["key"] == "fsr_assistant_profile:alerts"


def test_emit_without_a_module_offers_nothing(mods):
    storage = _Storage()
    res = mods.emit.build_persona_offer(
        {}, {"session_id": "s1", "goal": "do stuff"}, storage, _FakeProvider(_DRAFT),
        fetch=_fetch_ok)
    assert res["tags"]["code"] == "no_module"
    assert [f for f in res["transcript"] if f.get("type") == "persona_offer"] == []
    assert storage.cards == {}


def test_emit_with_an_unusable_draft_offers_nothing(mods):
    storage = _Storage()
    res = mods.emit.build_persona_offer(
        {}, {"session_id": "s1", "entity": {"module": "alerts"}, "goal": "x"},
        storage, _FakeProvider("sorry no"), fetch=_fetch_ok)
    assert res["tags"]["code"] == "draft_failed"
    assert storage.cards == {}
