"""The persona write policy behind the framework's create_record / update_record.

The writes (field checks, picklists, transport) are tested in the framework
(`fsr_playbooks/tests/test_tools_records.py`). What is pinned here is the
connector's half: its persona gate is the one the framework asks.
  - No active persona => `write_not_permitted` (no client call made).
  - Persona active but module not in `may_write` => refused, scope named.
  - Persona permits => the write goes out.
  - The pre-card validator refuses an out-of-scope write before any card.
"""
from __future__ import annotations

import pytest

from fsr_playbooks.mcp_server import _shared
from fsr_playbooks.mcp_server import tools_records as fw_records

from fsr_soc_triage import tools_records
from fsr_soc_triage.profiles import Profile, set_active_profile, reset_active_profile


@pytest.fixture
def persona():
    """Bind a persona that may write ztpf_templates; unbind after."""
    p = Profile(module="ztpf_templates", prompt="author",
                may_write=["ztpf_templates"])
    tok = set_active_profile(p)
    yield p
    reset_active_profile(tok)


@pytest.fixture
def no_persona():
    tok = set_active_profile(None)
    yield
    reset_active_profile(tok)


class _Resp:
    def __init__(self, status=201, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self):
        return self._payload


class _Client:
    base_url = ""


@pytest.fixture(autouse=True)
def _fresh_schema_cache():
    fw_records._ATTR_CACHE.clear()
    yield
    fw_records._ATTR_CACHE.clear()


def _patch_client(monkeypatch, capture):
    monkeypatch.setattr(_shared, "_live_client", lambda: _Client())

    def fake_request(method, client, url, **kwargs):
        capture.append((method, url, kwargs))
        return _Resp(201, {"@id": f"{url}/uuid-1", "uuid": "uuid-1", "id": 7})

    monkeypatch.setattr(_shared, "live_request_with_reauth", fake_request)


def test_the_persona_gate_is_the_registered_write_policy():
    import fsr_soc_triage  # noqa: F401 -- registration side effect
    assert _shared._RECORD_WRITE_GATE is tools_records._write_gate


def test_create_no_persona_refused(no_persona, monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap)
    out = fw_records.create_record("ztpf_templates", {"name": "x"})
    assert out["ok"] is False and out["code"] == "write_not_permitted"
    assert "persona" in out["message"]
    assert cap == []  # never reached the client


def test_create_module_not_in_may_write_refused(persona, monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap)
    out = fw_records.create_record("alerts", {"name": "x"})
    assert out["ok"] is False and out["code"] == "write_not_permitted"
    assert "alerts" in out["message"] and "ztpf_templates" in out["message"]
    assert cap == []


def test_create_permitted(persona, monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap)
    out = fw_records.create_record("ztpf_templates", {"name": "cfg-gen"})
    assert out["ok"] is True and out["uuid"] == "uuid-1"
    assert [c[0] for c in cap if c[0] != "get"] == ["post"]


def test_update_out_of_scope_refused(persona, monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap)
    out = fw_records.update_record("ztpf_devices", "u", {"name": "x"})
    assert out["code"] == "write_not_permitted" and cap == []


def test_precard_refuses_a_module_outside_the_personas_scope(persona):
    out = fw_records.precard_validate(
        {"module": "ztpf_devices", "fields": {"name": "x"}})
    assert out is not None and out["code"] == "write_not_permitted"
    assert "ztpf_templates" in out["message"], \
        "the refusal must NAME the scope, or the analyst cannot act on it"


def test_precard_is_the_same_gate_not_a_second_copy(persona):
    """Two spellings of "who may write where" would drift, and the drift would
    only show at the exact moment a write is refused after approval."""
    for module in ("ztpf_devices", "alerts"):
        assert (fw_records.precard_validate({"module": module})
                == tools_records._write_gate(module))


def test_dispatch_refuses_out_of_scope_before_building_a_card(persona):
    """End-to-end through the real dispatch: the analyst must never see a card
    for a write the persona's scope forbids."""
    import fsr_soc_triage  # noqa: F401 -- registration side effect
    from fsr_playbooks.llm import tools as fw

    out = fw.dispatch("create_record",
                      {"module": "ztpf_devices", "fields": {"name": "x"}},
                      session_id="s-precard")
    assert not out.get("pending_approval"), \
        "carded a write the persona's may_write scope forbids"
    assert out.get("code") == "write_not_permitted"

    # The inverse, so the test above cannot pass by refusing everything.
    ok = fw.dispatch("create_record",
                     {"module": "ztpf_templates", "fields": {"name": "x"}},
                     session_id="s-precard")
    assert ok.get("pending_approval"), \
        "an in-scope write must still reach the analyst as a card"
