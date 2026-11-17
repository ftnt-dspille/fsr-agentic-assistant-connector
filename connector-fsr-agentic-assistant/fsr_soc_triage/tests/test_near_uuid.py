"""A near-miss uuid gets a "did you mean".

Live on the box the model copied run group ...214b as ...214f, got a bare
not-found, and told the analyst the run group did not exist. The real IRI was
in the device on screen (`ztpfArtifacts.ztfGroup_fqid`). A not-found now names
the real record when one is within two characters of the guess.
"""
from __future__ import annotations

import json

from fsr_soc_triage import profiles
from fsr_playbooks.mcp_server import _record_lookup
from fsr_playbooks.mcp_server import tools_triage as tt
from fsr_playbooks.mcp_server import _shared

RG = "ztpf_run_groups"
REAL = "6f355a36-566d-432c-8a78-c470eba4214b"
TYPO = "6f355a36-566d-432c-8a78-c470eba4214f"
FAR = "00000000-0000-4000-8000-000000000000"
DEVICE = "/api/3/ztpf_devices/a0a36846-82c3-42ec-919b-fb391a645ba0"


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def _box(path: str) -> _Resp:
    if path == DEVICE:
        return _Resp(200, {"@id": DEVICE, "ztpfArtifacts": {
            "ztfGroup": "ztpf-1790110223", "ztfGroup_fqid": f"/api/3/{RG}/{REAL}"}})
    if path == f"/api/3/{RG}/{REAL}":
        return _Resp(200, {"@id": path, "name": "ztpf-1790110223"})
    if path.startswith(f"/api/3/{RG}/"):
        return _Resp(404)
    return _Resp(200, {"hydra:member": []})


class _Client:
    base_url = ""


def _wire(monkeypatch):
    client = _Client()
    monkeypatch.setattr(_shared, "_live_client", lambda: client)
    monkeypatch.setattr(tt, "_get_with_reauth", lambda c, url: _box(url.split("?", 1)[0]))
    monkeypatch.setattr(_record_lookup, "live_request", lambda m, c, url, **k: _box(url.split("?", 1)[0]))
    monkeypatch.setattr(_record_lookup, "recent_records", lambda c, m: [])
    tok = profiles.set_active_record_iri(DEVICE)
    return client, tok


def test_get_record_names_the_record_one_character_off(monkeypatch):
    _, tok = _wire(monkeypatch)
    try:
        out = tt.get_record(module=RG, uuid=TYPO)
    finally:
        profiles.reset_active_record_iri(tok)
    assert out["ok"] is False and out["code"] == "not_found"
    assert out["did_you_mean"] == {"iri": f"/api/3/{RG}/{REAL}", "uuid": REAL, "differs_by": 1}
    assert f"Did you mean /api/3/{RG}/{REAL}" in out["message"]


def test_a_uuid_nothing_like_a_real_one_gets_no_guess(monkeypatch):
    _, tok = _wire(monkeypatch)
    try:
        out = tt.get_record(module=RG, uuid=FAR)
    finally:
        profiles.reset_active_record_iri(tok)
    assert out["code"] == "not_found" and "did_you_mean" not in out


def test_unknown_record_refusal_carries_it_too(monkeypatch):
    client, tok = _wire(monkeypatch)
    try:
        out = _record_lookup.unknown_record(client, RG, TYPO, active_iri=DEVICE)
    finally:
        profiles.reset_active_record_iri(tok)
    assert out["code"] == "unknown_record"
    assert out["did_you_mean"]["uuid"] == REAL


def test_only_iris_of_the_asked_module_count():
    near = _record_lookup.near_uuid(None, "ztpf_devices", TYPO, None,
                             [{"iri": f"/api/3/{RG}/{REAL}"}])
    assert near is None


def test_a_bare_id_in_iri_is_refused_not_fetched(monkeypatch):
    # Live: the model passed an approval card's id as `iri`; it was fetched as
    # the path "/<id>", and the reply crashed the tool ("'str' object has no
    # attribute 'get'"). It is no record route, so say so and fetch nothing.
    monkeypatch.setattr(_shared, "_live_client", lambda: (_ for _ in ()).throw(
        AssertionError("nothing should be fetched")))
    out = tt.get_record(iri="f4e9037ed94941928dad35b9e705f652")
    assert out["ok"] is False and out["code"] == "bad_iri"
    assert "/api/3/<module>/<uuid>" in out["message"]
