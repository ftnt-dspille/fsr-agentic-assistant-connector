"""`persona_offer` ACCEPT writes the drafted persona to the Key Store.

The save half of the persona builder (docs/PERSONA_BUILDER_PLAN.md P2). The
invariants that make it safe:
  * accepting the card IS the human approval, so the CONNECTOR writes the Key
    Store record here -- the model never writes it;
  * the draft is RE-LINTED at save time: an error-level issue (e.g. a tool name
    not in the catalog, or a missing prompt) refuses the save instead of writing
    a persona that would resolve to nothing;
  * an existing persona for the module is UPDATED in place (PUT), a new one is
    CREATED (POST) -- never a duplicate;
  * missing module, no transport, or a write error each end the turn with
    `saved: False` and analyst-facing text, never an error banner.
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


KNOWN = {"get_record", "search_module_records", "create_record"}


def _card(**over):
    card = {"type": "persona_offer", "id": "p1", "module": "ztpf_templates",
            "value": "You author ZTPF device config templates. Ground in the "
                     "template on screen, test before you write.",
            "jSONValue": {"label": "ZTPF Template Author",
                          "tools": {"allow": ["get_record", "create_record"]},
                          "may_write": ["ztpf_templates"],
                          "ui": {"label": "ZTPF Template Author"}}}
    card.update(over)
    return card


class _CardStorage:
    def __init__(self, card=None):
        self._card = card

    def get_card(self, session_id, card_id):
        return self._card

    def append_transcript_assistant(self, session_id, transcript):
        pass


def _patch_transport(operations, monkeypatch, calls, *, existing_members=None,
                     raise_on=None):
    def fake_mr(url, method, body=None):
        calls.append((method, url, body))
        if raise_on and method == raise_on:
            raise RuntimeError("box said no")
        if method == "GET":
            return {"hydra:member": list(existing_members or [])}
        return {"@id": "/api/3/keys/new", "key": body.get("key") if body else None}

    oc = sys.modules["_fsrpb_conn.ops_common"]
    monkeypatch.setattr(oc, "_make_request", lambda: fake_mr)
    # Deterministic catalog, so an invented tool is flagged regardless of whether
    # the live registry is importable in the test env.
    import fsr_soc_triage.persona_author as pa
    monkeypatch.setattr(pa, "default_known_tools", lambda: set(KNOWN))


def test_accept_creates_a_new_persona_record(operations, monkeypatch):
    calls = []
    _patch_transport(operations, monkeypatch, calls, existing_members=[])
    res = operations._resume_persona_offer_accept(
        {}, {"session_id": "s1", "offer_id": "p1"}, _CardStorage(_card()))
    assert res["tags"]["saved"] is True
    methods = [c[0] for c in calls]
    assert "GET" in methods and "POST" in methods and "PUT" not in methods
    post = next(c for c in calls if c[0] == "POST")
    assert post[1] == "/api/3/keys"
    assert post[2]["key"] == "fsr_assistant_profile:ztpf_templates"
    assert post[2]["jSONValue"]["may_write"] == ["ztpf_templates"]


def test_accept_updates_an_existing_persona_in_place(operations, monkeypatch):
    calls = []
    _patch_transport(operations, monkeypatch, calls,
                     existing_members=[{"@id": "/api/3/keys/abc"}])
    res = operations._resume_persona_offer_accept(
        {}, {"session_id": "s1", "offer_id": "p1"}, _CardStorage(_card()))
    assert res["tags"]["saved"] is True
    methods = [c[0] for c in calls]
    assert "PUT" in methods and "POST" not in methods, "update, never duplicate"
    put = next(c for c in calls if c[0] == "PUT")
    assert put[1] == "/api/3/keys/abc"


def test_accept_refuses_a_hallucinated_tool_and_writes_nothing(operations, monkeypatch):
    calls = []
    _patch_transport(operations, monkeypatch, calls, existing_members=[])
    bad = _card()
    bad["jSONValue"]["tools"]["allow"] = ["get_record", "make_coffee"]
    res = operations._resume_persona_offer_accept(
        {}, {"session_id": "s1", "offer_id": "p1"}, _CardStorage(bad))
    assert res["tags"]["saved"] is False
    assert [c for c in calls if c[0] in ("POST", "PUT")] == [], "no write on invalid"


def test_accept_refuses_when_module_is_missing(operations, monkeypatch):
    calls = []
    _patch_transport(operations, monkeypatch, calls, existing_members=[])
    res = operations._resume_persona_offer_accept(
        {}, {"session_id": "s1", "offer_id": "p1"}, _CardStorage(_card(module="")))
    assert res["tags"]["saved"] is False
    assert calls == [], "no transport touched when there is no module"


def test_accept_surfaces_a_write_failure_without_an_error_banner(operations, monkeypatch):
    calls = []
    _patch_transport(operations, monkeypatch, calls, existing_members=[], raise_on="POST")
    res = operations._resume_persona_offer_accept(
        {}, {"session_id": "s1", "offer_id": "p1"}, _CardStorage(_card()))
    assert res["ok"] is True and res["tags"]["saved"] is False
    assert res["tags"].get("code") == "write_failed"
