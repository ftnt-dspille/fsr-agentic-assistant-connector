"""HARDEN-4: an invented record uuid is refused with REAL candidates.

Before, `update_record` / `run_playbook(record=...)` with a made-up uuid became
an approval card: the analyst approved, the body ran, and only then did it
404. And `get_record` on a bad uuid answered "no record at ..." with nothing
to act on, so the model's next move was another guess.

Driven at the real seam (`fsr_playbooks.llm.tools.dispatch`) with a fake
crudhub behind `_shared._live_client`. The contract has two halves and both
are pinned: a clean 404 refuses BEFORE a card, and anything short of a clean
404 (exists, 500, transport error) still cards -- a blinking box must never
read as "no such record".
"""
from __future__ import annotations

import sys

import pytest

from fsr_playbooks.llm import tools as T
from fsr_playbooks.mcp_server import _shared

MODULE = "ztpf_automation_actions"
GOOD = "11111111-1111-1111-1111-111111111111"
BAD = "99999999-9999-9999-9999-999999999999"
ACTIVE = f"/api/3/{MODULE}/{GOOD}"


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}
        self.text = ""

    def json(self):
        return self._data


_RECENT = {"hydra:member": [
    {"@id": f"/api/3/{MODULE}/r1", "uuid": "r1", "name": "Push config"},
    {"@id": ACTIVE, "uuid": GOOD, "name": "Reboot"},
]}


class _Session:
    def __init__(self, record_status=404, raise_on_get=False):
        self.record_status = record_status
        self.raise_on_get = raise_on_get
        self.calls = []

    def get(self, url, verify=None, **kw):
        self.calls.append(("get", url))
        if self.raise_on_get:
            raise ConnectionError("box blinked")
        path = url.split("?", 1)[0]
        if path.endswith(f"/{GOOD}"):
            return _Resp(200, {"@id": ACTIVE})
        if path.endswith(f"/{BAD}"):
            return _Resp(self.record_status)
        return _Resp(200, _RECENT)

    def post(self, url, verify=None, **kw):
        self.calls.append(("post", url))
        return _Resp(200, _RECENT)


class _Client:
    base_url = "https://fsr.example"
    verify_ssl = False

    def __init__(self, **kw):
        self.session = _Session(**kw)


def _connector():
    import connector_fsr_agentic_assistant.fsr_soc_triage  # noqa: F401 -- registers
    fn = _shared._RECORD_WRITE_GATE  # the connector's persona gate
    assert fn is not None
    pkg = fn.__module__.rsplit(".", 1)[0]
    return sys.modules[fn.__module__], sys.modules[pkg + ".profiles"], \
        sys.modules["fsr_playbooks.mcp_server.tools_playbook_run"]


@pytest.fixture
def box(monkeypatch):
    """A writable persona mounted on ACTIVE, and a fake box to answer reads."""
    _, P, _ = _connector()
    holder = {"client": _Client()}
    monkeypatch.setattr(_shared, "_live_client", lambda: holder["client"])
    tok = P.set_active_profile(P.Profile(
        module=MODULE, prompt="x", may_write=[MODULE]))
    rtok = P.set_active_record_iri(ACTIVE)
    try:
        yield holder
    finally:
        P.reset_active_record_iri(rtok)
        P.reset_active_profile(tok)


def _update(uuid):
    return T.dispatch("update_record", {"module": MODULE, "uuid": uuid,
                                        "fields": {"description": "x"}})


def test_invented_uuid_is_refused_before_a_card_with_candidates(box):
    out = _update(BAD)
    assert not out.get("pending_approval"), out
    assert out.get("code") == "unknown_record", out
    iris = [c["iri"] for c in out["candidates"]]
    # The mounted record leads; recent records follow, de-duplicated.
    assert iris[0] == ACTIVE
    assert f"/api/3/{MODULE}/r1" in iris
    assert iris.count(ACTIVE) == 1
    assert "search_module_records" in out["message"]


def test_a_real_uuid_still_cards(box):
    out = _update(GOOD)
    assert out.get("pending_approval"), out


@pytest.mark.parametrize("client_kw", [{"record_status": 500},
                                       {"raise_on_get": True}])
def test_anything_short_of_a_clean_404_still_cards(box, client_kw):
    box["client"] = _Client(**client_kw)
    out = _update(BAD)
    assert out.get("pending_approval"), out


def test_run_playbook_record_is_checked_after_the_playbook(box, monkeypatch):
    _, _, PB = _connector()
    monkeypatch.setattr(PB, "_precard_playbook", lambda args: None)
    out = PB.precard_validate({"playbook": "Reboot Device",
                                "record": f"/api/3/{MODULE}/{BAD}"})
    assert out and out["code"] == "unknown_record", out
    assert "nothing was run" in out["message"]
    # A real record passes through; a value that is not a plain record IRI is
    # refused (its parts would reach request URLs).
    assert PB.precard_validate({"playbook": "p", "record": ACTIVE}) is None
    assert PB.precard_validate({"playbook": "p", "record": BAD})["code"] == "bad_record"


def test_a_playbook_refusal_wins_over_the_record_check(box, monkeypatch):
    _, _, PB = _connector()
    refusal = {"ok": False, "code": "unknown_playbook"}
    monkeypatch.setattr(PB, "_precard_playbook", lambda args: refusal)
    out = PB.precard_validate({"playbook": "nope",
                                "record": f"/api/3/{MODULE}/{BAD}"})
    assert out is refusal


def test_get_record_on_an_invented_uuid_names_real_records(box):
    """The read side: a real module with an unknown uuid used to answer a bare
    "no record at ..." -- nothing to act on but another guess."""
    _connector()  # registers the connector's active-record hook
    from fsr_playbooks.mcp_server.tools_triage import get_record
    out = get_record(module=MODULE, uuid=BAD)
    assert out.get("ok") is False and out.get("code") == "not_found", out
    iris = [c["iri"] for c in out.get("candidates") or []]
    assert iris and iris[0] == ACTIVE, out
    assert "search_module_records" in out["message"]
