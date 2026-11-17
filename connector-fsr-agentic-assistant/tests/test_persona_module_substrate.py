"""P0 -- persona substrate: the `assistant_personas` module (de)serializer and the
module-first / Key-Store-fallback resolution in `resolve_profile_status`."""
from __future__ import annotations

import json

from fsr_soc_triage import profiles
from fsr_soc_triage.profiles import (
    PERSONA_MODULE,
    PF_BIND,
    PF_ENABLED,
    PF_SCOPE,
    PF_TOOLS_ALLOW,
    Profile,
    PersonaCapabilities,
    PersonaMcp,
    parse_persona_module_record,
    profile_to_module_record,
    resolve_profile_status,
)


# --- P0.1 round-trip -------------------------------------------------------

def _rich_profile() -> Profile:
    return Profile(
        module="ztpf_devices",
        prompt="You author ZTPF device automation.",
        tools_allow=["get_record", "search_module_records"],
        may_write=["ztpf_devices"],
        run_playbook_allow=["Validate Device"],
        run_playbook_auto=["Validate Device"],
        bind_modules=["ztpf_metadata_sources"],
        version=3,
        label="ZTPF Device Author",
        ui={"greeting": "hi"},
        capabilities=PersonaCapabilities(grounding=True),
        mcp=PersonaMcp(servers=["modules", "connector:fortimanager"]),
    )


def test_round_trip_identity():
    p = _rich_profile()
    rec = profile_to_module_record(p)
    # JSON columns are strings in the record (grid-editable text).
    assert isinstance(rec[PF_TOOLS_ALLOW], str)
    back = parse_persona_module_record(rec, p.module)
    assert back == p


def test_round_trip_minimal_prompt_only():
    p = Profile(module="alerts", prompt="Be terse.")
    back = parse_persona_module_record(profile_to_module_record(p), p.module)
    assert back == p


def test_malformed_json_column_degrades_that_field_only():
    p = _rich_profile()
    rec = profile_to_module_record(p)
    rec[PF_TOOLS_ALLOW] = "{not valid json"  # one bad column
    back = parse_persona_module_record(rec, p.module)
    assert back is not None  # did not crash
    assert back.tools_allow == []  # degraded to empty
    assert back.may_write == ["ztpf_devices"]  # other fields intact


def test_disabled_row_resolves_to_no_persona():
    rec = profile_to_module_record(_rich_profile())
    rec[PF_ENABLED] = False
    assert parse_persona_module_record(rec, "ztpf_devices") is None


def test_non_dict_record_is_none():
    assert parse_persona_module_record(None, "m") is None
    assert parse_persona_module_record("x", "m") is None


# --- P0.2 module-first / Key-Store fallback --------------------------------

class _Resp:
    def __init__(self, status, members):
        self.status_code = status
        self._members = members

    def json(self):
        return {"hydra:member": self._members}


class _Client:
    base_url = "https://box"


def _module_member(module, prompt="P", tools=None):
    return {
        PF_SCOPE: module,
        "prompt": prompt,
        PF_TOOLS_ALLOW: json.dumps(tools or []),
        PF_ENABLED: True,
    }


def _keys_member(module, prompt="KS", tools=None):
    return {
        "key": profiles.KEY_PREFIX + module,
        "value": prompt,
        "jSONValue": {"tools": {"allow": tools or []}, "version": 1},
    }


def test_module_hit_wins_over_key_store():
    def fake_get(client, url):
        if PERSONA_MODULE in url:
            return _Resp(200, [_module_member("alerts", prompt="FROM_MODULE")])
        return _Resp(200, [_keys_member("alerts", prompt="FROM_KEYS")])

    prof, definitive = resolve_profile_status(_Client(), "alerts", fake_get)
    assert definitive is True
    assert prof is not None and prof.prompt == "FROM_MODULE"


def test_key_store_only_persona_still_resolves():
    def fake_get(client, url):
        if PERSONA_MODULE in url:
            return _Resp(404, [])  # module not provisioned
        return _Resp(200, [_keys_member("alerts", prompt="FROM_KEYS")])

    prof, definitive = resolve_profile_status(_Client(), "alerts", fake_get)
    assert definitive is True
    assert prof is not None and prof.prompt == "FROM_KEYS"


def test_neither_present_is_definitive_miss():
    def fake_get(client, url):
        return _Resp(200, [])  # clean, empty, from both sources

    prof, definitive = resolve_profile_status(_Client(), "alerts", fake_get)
    assert prof is None and definitive is True


def test_transient_module_read_is_not_cached_when_keys_also_transient():
    def fake_get(client, url):
        return _Resp(500, [])  # both sources error

    prof, definitive = resolve_profile_status(_Client(), "alerts", fake_get)
    assert prof is None and definitive is False  # do not cache a transient miss


def test_module_bind_modules_scan():
    def fake_get(client, url):
        if PERSONA_MODULE in url and PF_SCOPE in url:
            return _Resp(200, [])  # no direct scopeModule match
        if PERSONA_MODULE in url:
            rec = _module_member("ztpf_metadata_sources", prompt="BOUND")
            rec[PF_BIND] = json.dumps(["ztpf_devices"])
            return _Resp(200, [rec])
        return _Resp(200, [])

    prof, definitive = resolve_profile_status(_Client(), "ztpf_devices", fake_get)
    assert prof is not None and prof.prompt == "BOUND"
    assert prof.module == "ztpf_devices"
