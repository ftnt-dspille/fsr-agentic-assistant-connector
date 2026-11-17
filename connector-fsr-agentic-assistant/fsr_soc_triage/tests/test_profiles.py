"""Key Store persona loader: parse, bind_modules fallback, fail-open.

These pin the §2 data contract of the module-scoped-personas plan against
literal `keys` records and a fake crudhub client (no box). The invariant the
whole design leans on: any malformed record ⇒ `None` ⇒ built-in behavior, never
an exception that breaks a turn.
"""
from __future__ import annotations

from fsr_soc_triage import profiles
from fsr_soc_triage.profiles import (
    KEY_PREFIX,
    Profile,
    load_profile,
    parse_profile_record,
    resolve_profile_status,
    validate_persona_record,
)


def _levels(issues):
    return {i.level for i in issues}


def _by_field(issues, field):
    return [i for i in issues if i.field == field]


def _rec(module="ztpf_templates", *, value="Author ZTPF templates.",
         tools=None, may_write=None, bind=None, version=1, **extra):
    jv = {"version": version}
    if tools is not None:
        jv["tools"] = {"allow": tools}
    if may_write is not None:
        jv["may_write"] = may_write
    if bind is not None:
        jv["bind_modules"] = bind
    rec = {"key": KEY_PREFIX + module, "value": value, "jSONValue": jv,
           "notes": "desc"}
    rec.update(extra)
    return rec


# --------------------------------------------------------------------------- #
# parse_profile_record
# --------------------------------------------------------------------------- #

def test_parse_full_record():
    p = parse_profile_record(
        _rec(tools=["get_record", "search_module_records"],
             may_write=["ztpf_templates"], bind=["ztpf_automation_actions"]),
        "ztpf_templates")
    assert isinstance(p, Profile)
    assert p.module == "ztpf_templates"
    assert p.prompt == "Author ZTPF templates."
    assert p.tools_allow == ["get_record", "search_module_records"]
    assert p.may_write == ["ztpf_templates"]
    assert p.bind_modules == ["ztpf_automation_actions"]
    assert p.version == 1


def test_module_arg_overrides_record_key():
    # When matched via bind_modules the resolved module differs from the record.
    p = parse_profile_record(_rec(module="ztpf_templates", bind=["ztpf_x"]),
                             "ztpf_x")
    assert p.module == "ztpf_x"


def test_prompt_only_record_is_readonly_no_tools():
    p = parse_profile_record({"key": KEY_PREFIX + "m", "value": "hi"}, "m")
    assert p.prompt == "hi"
    assert p.tools_allow == []
    assert p.may_write == []
    assert p.version == 1


# --------------------------------------------------------------------------- #
# capabilities (P3): opt-in grounding declaration
# --------------------------------------------------------------------------- #

def _cap_rec(capabilities, module="m"):
    """A `keys` record carrying jSONValue.capabilities (built inline since the
    shared `_rec` routes extra kwargs to the record top-level, not jSONValue)."""
    return {"key": KEY_PREFIX + module, "value": "author things",
            "jSONValue": {"version": 1, "capabilities": capabilities}}


def test_capabilities_absent_is_none():
    # No jSONValue.capabilities ⇒ None (no declaration; prompt-only behavior).
    p = parse_profile_record(_rec(), "ztpf_templates")
    assert p.capabilities is None


def test_capabilities_grounding_parsed():
    p = parse_profile_record(_cap_rec({"grounding": True}), "m")
    assert p.capabilities is not None
    assert p.capabilities.grounding is True


def test_capabilities_grounding_false_parsed():
    p = parse_profile_record(_cap_rec({"grounding": False}), "m")
    assert p.capabilities is not None
    assert p.capabilities.grounding is False


def test_garbled_capabilities_degrades_not_raises():
    # capabilities not a dict ⇒ treated as no declaration (None).
    p = parse_profile_record(_cap_rec("not-a-dict"), "m")
    assert p.capabilities is None


def test_empty_prompt_is_malformed():
    assert parse_profile_record(_rec(value="   "), "m") is None
    assert parse_profile_record(_rec(value=None), "m") is None
    assert parse_profile_record({"key": "x"}, "m") is None


def test_non_dict_record_is_none():
    for bad in (None, "str", 42, [1, 2]):
        assert parse_profile_record(bad, "m") is None


def test_garbled_jsonvalue_degrades_not_raises():
    # jSONValue not a dict, and list fields with junk members.
    p = parse_profile_record(
        {"key": "k", "value": "p", "jSONValue": "not-a-dict"}, "m")
    assert p is not None and p.tools_allow == [] and p.may_write == []

    p2 = parse_profile_record(
        _rec(tools=["ok", "", 5, None], may_write=[1, "mod"]), "m")
    assert p2.tools_allow == ["ok"]
    assert p2.may_write == ["mod"]


# --------------------------------------------------------------------------- #
# validate_persona_record (P5): authoring-time lint
# --------------------------------------------------------------------------- #

def test_validate_clean_record_no_issues():
    rec = _rec(tools=["get_record", "update_record"], may_write=["m"])
    rec["jSONValue"]["capabilities"] = {"grounding": True}
    issues = validate_persona_record(
        rec, known_tools={"get_record", "update_record"})
    assert issues == []


def test_validate_never_raises_on_junk():
    for bad in (None, "str", 42, [1, 2], {}):
        # Must return a list, never raise.
        assert isinstance(validate_persona_record(bad), list)


def test_validate_missing_prompt_is_error():
    issues = validate_persona_record({"key": "k", "jSONValue": {}})
    assert _by_field(issues, "value")
    assert "error" in _levels(issues)


def test_validate_unknown_tool_is_error():
    rec = _rec(tools=["get_record", "get_recrod"])  # typo
    issues = validate_persona_record(rec, known_tools={"get_record"})
    tool_issues = _by_field(issues, "tools.allow")
    assert tool_issues and tool_issues[0].level == "error"
    assert "get_recrod" in tool_issues[0].message


def test_validate_skips_tool_check_without_known_tools():
    rec = _rec(tools=["anything_goes"])
    assert validate_persona_record(rec) == []  # no known_tools ⇒ no tool check


def test_validate_unknown_capability_is_warning():
    rec = _rec()
    rec["jSONValue"]["capabilities"] = {"groundign": True}  # typo
    issues = validate_persona_record(rec)
    cap = _by_field(issues, "capabilities.groundign")
    assert cap and cap[0].level == "warning"
    assert "unknown capability" in cap[0].message


def test_validate_non_bool_capability_is_warning():
    rec = _rec()
    rec["jSONValue"]["capabilities"] = {"grounding": "yes"}
    issues = validate_persona_record(rec)
    cap = _by_field(issues, "capabilities.grounding")
    assert cap and cap[0].level == "warning"


def test_validate_malformed_lists_and_version_are_warnings():
    rec = _rec()
    rec["jSONValue"].update({"may_write": "m", "bind_modules": 3, "version": "1"})
    issues = validate_persona_record(rec)
    fields = {i.field for i in issues}
    assert {"may_write", "bind_modules", "version"} <= fields
    assert _levels(issues) == {"warning"}


def test_validate_prompt_only_record_is_clean():
    # No jSONValue at all -- a valid prompt-only, read-only persona.
    issues = validate_persona_record({"key": "k", "value": "hello"})
    assert issues == []


def test_parse_extracts_ui_and_label():
    # Phase 4: the optional jSONValue.ui block + label ride out for the widget.
    rec = {"key": KEY_PREFIX + "ztpf_templates", "value": "Author templates.",
           "jSONValue": {"version": 1, "label": "ZTPF Template Author",
                         "ui": {"greeting": "Authoring {module}: {name}",
                                "placeholder": "Describe the template…",
                                "quickActions": [{"key": "explain", "label": "Explain"}]}}}
    p = parse_profile_record(rec, "ztpf_templates")
    assert p.label == "ZTPF Template Author"
    assert p.ui["greeting"] == "Authoring {module}: {name}"
    assert p.ui["quickActions"][0]["key"] == "explain"


def test_missing_or_malformed_ui_label_default_to_none():
    # No ui/label ⇒ None (widget then applies neutral best-effort framing).
    p = parse_profile_record(_rec(), "m")
    assert p.label is None and p.ui is None
    # A non-dict ui / blank label are dropped, not raised.
    bad = {"key": "k", "value": "p",
           "jSONValue": {"ui": "not-a-dict", "label": "   "}}
    p2 = parse_profile_record(bad, "m")
    assert p2 is not None and p2.ui is None and p2.label is None


def test_may_write_module_gate():
    p = parse_profile_record(_rec(may_write=["ztpf_templates"]), "m")
    assert p.may_write_module("ztpf_templates") is True
    assert p.may_write_module("alerts") is False
    assert p.may_write_module("") is False
    ro = parse_profile_record(_rec(), "m")  # no may_write ⇒ read-only
    assert ro.may_write_module("ztpf_templates") is False


def test_non_int_version_defaults():
    p = parse_profile_record(_rec(version="2"), "m")
    assert p.version == 1


# --------------------------------------------------------------------------- #
# load_profile -- fake crudhub client
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    base_url = ""


def _hydra(members):
    return {"hydra:member": members}


def test_load_direct_hit():
    rec = _rec(tools=["get_record"], may_write=["ztpf_templates"])

    def fake_get(client, url):
        assert "key=fsr_assistant_profile%3Aztpf_templates" in url
        return _Resp(200, _hydra([rec]))

    p = load_profile(_Client(), "ztpf_templates", get_with_reauth=fake_get)
    assert p is not None and p.tools_allow == ["get_record"]


def test_load_bind_modules_fallback():
    """The Key Store scan answers when its direct lookup misses -- on a box with
    no `assistant_personas` module.

    Asserts the SEQUENCE of reads, not a count. This test used to assert
    `calls == 2` with a fake that answered every non-keys URL as a scan page; P0
    put the persona MODULE first, so the module's two reads arrived, hit the
    fake's `$limit` assertion, and were swallowed by the resolver's fail-open
    except -- leaving a red test whose only symptom was a stale number. Bumping
    the number to 4 would have hidden that the module reads were being served
    garbage. A sequence says which reads happened, in order, and why.
    """
    bound = _rec(module="ztpf_templates", bind=["ztpf_automation_actions"])
    seen: list[str] = []

    def fake_get(client, url):
        seen.append(url)
        if f"/api/3/{profiles.PERSONA_MODULE}" in url:
            # The module is not provisioned on this box: a 404, which the
            # resolver treats as a DEFINITIVE "no module persona" so the Key
            # Store's own miss stays cacheable.
            return _Resp(404, {})
        if "key=fsr_assistant_profile" in url:
            return _Resp(200, _hydra([]))  # direct miss
        assert f"$limit={profiles._SCAN_LIMIT}" in url  # the keys scan page
        return _Resp(200, _hydra([{"key": "unrelated", "value": "x"}, bound]))

    p = load_profile(_Client(), "ztpf_automation_actions", get_with_reauth=fake_get)
    assert p is not None
    assert p.module == "ztpf_automation_actions"
    assert p.prompt == "Author ZTPF templates."

    assert len(seen) == 3, seen
    assert profiles.PERSONA_MODULE in seen[0], "the module must be tried FIRST"
    assert "key=fsr_assistant_profile" in seen[1], "then the Key Store direct"
    assert f"$limit={profiles._SCAN_LIMIT}" in seen[2], "then the Key Store scan"


def test_load_no_match_returns_none():
    def fake_get(client, url):
        return _Resp(200, _hydra([]))

    assert load_profile(_Client(), "alerts", get_with_reauth=fake_get) is None


def test_load_transport_error_is_none():
    def boom(client, url):
        raise RuntimeError("crudhub down")

    assert load_profile(_Client(), "m", get_with_reauth=boom) is None


def test_load_http_error_is_none():
    def fake_get(client, url):
        return _Resp(500, {})

    assert load_profile(_Client(), "m", get_with_reauth=fake_get) is None


def test_load_guards_bad_inputs():
    assert load_profile(None, "m", get_with_reauth=lambda *a: None) is None
    assert load_profile(_Client(), "", get_with_reauth=lambda *a: None) is None


# --------------------------------------------------------------------------- #
# resolve_profile_status -- definitive vs transient (transient-None cache fix)
# --------------------------------------------------------------------------- #

def test_status_hit_is_definitive():
    def fake_get(client, url):
        return _Resp(200, _hydra([_rec(tools=["get_record"])]))
    p, definitive = resolve_profile_status(_Client(), "ztpf_templates",
                                            get_with_reauth=fake_get)
    assert p is not None and definitive is True


def test_status_clean_miss_is_definitive():
    # Both reads complete (200) with no match → a trustworthy "no persona".
    def fake_get(client, url):
        return _Resp(200, _hydra([]))
    p, definitive = resolve_profile_status(_Client(), "alerts",
                                           get_with_reauth=fake_get)
    assert p is None and definitive is True


def test_status_direct_non200_is_transient():
    def fake_get(client, url):
        return _Resp(503, {})
    p, definitive = resolve_profile_status(_Client(), "m", get_with_reauth=fake_get)
    assert p is None and definitive is False  # do NOT cache this None


def test_status_scan_non200_is_transient():
    def fake_get(client, url):
        # direct lookup 200-but-empty, then the bind scan fails transiently.
        if "key=fsr_assistant_profile" in url:
            return _Resp(200, _hydra([]))
        return _Resp(500, {})
    p, definitive = resolve_profile_status(_Client(), "m", get_with_reauth=fake_get)
    assert p is None and definitive is False


def test_status_transport_error_is_transient():
    def boom(client, url):
        raise RuntimeError("crudhub down")
    p, definitive = resolve_profile_status(_Client(), "m", get_with_reauth=boom)
    assert p is None and definitive is False


def test_status_direct_raises_but_scan_clean_is_transient():
    # THE plateau bug: a cold/recycling worker's DIRECT lookup raises, then the
    # bind scan completes cleanly (200) with no match for this direct-key module.
    # That absence is NOT trustworthy (the direct read never succeeded), so it
    # must be transient -- else operations._resolve_profile caches a permanent
    # "no persona" and the worker drops the persona for its whole life.
    def fake_get(client, url):
        if "key=fsr_assistant_profile" in url:
            raise RuntimeError("cold-worker reauth blip")
        return _Resp(200, _hydra([{"key": "unrelated", "value": "x"}]))
    p, definitive = resolve_profile_status(
        _Client(), "ztpf_templates", get_with_reauth=fake_get)
    assert p is None and definitive is False


def test_status_bad_input_is_definitive():
    p, definitive = resolve_profile_status(None, "m", get_with_reauth=lambda *a: None)
    assert p is None and definitive is True


# ── record_iri_from_entity: single source both turn-drive entries use ────────
def test_record_iri_from_entity_prefers_explicit_iri():
    assert profiles.record_iri_from_entity(
        {"iri": "/api/3/ztpf_metadata_sources/abc", "module": "x", "uuid": "y"}
    ) == "/api/3/ztpf_metadata_sources/abc"


def test_record_iri_from_entity_composes_from_module_and_id():
    assert profiles.record_iri_from_entity(
        {"module": "ztpf_metadata_sources", "uuid": "abc"}
    ) == "/api/3/ztpf_metadata_sources/abc"
    # `type` aliases module; `id` aliases uuid.
    assert profiles.record_iri_from_entity(
        {"type": "alerts", "id": 42}
    ) == "/api/3/alerts/42"


def test_record_iri_from_entity_none_when_unresolvable():
    assert profiles.record_iri_from_entity(None) is None
    assert profiles.record_iri_from_entity({}) is None
    assert profiles.record_iri_from_entity({"module": "alerts"}) is None  # no id


# --------------------------------------------------------------------------- #
# M2 -- persona MCP surfacing block (jSONValue.mcp)
# --------------------------------------------------------------------------- #

def _rec_mcp(mcp, module="alerts"):
    return {"key": KEY_PREFIX + module, "value": "SOC triage.",
            "jSONValue": {"version": 1, "mcp": mcp}}


def test_parse_persona_mcp_servers_and_dedup():
    p = parse_profile_record(_rec_mcp({
        "servers": ["fortisiem", "soc"],
        "capability_dedup": {
            "siem.context": {"curated": "siem_search_ip",
                             "mcp": ["mcp_fortisiem__get_context_by_entity"]},
        },
    }), "alerts")
    assert p.mcp is not None
    assert p.mcp.servers == ["fortisiem", "soc"]
    dd = p.mcp.capability_dedup["siem.context"]
    assert dd.curated == "siem_search_ip"
    assert dd.mcp == ["mcp_fortisiem__get_context_by_entity"]


def test_parse_persona_mcp_empty_servers_is_a_real_override():
    p = parse_profile_record(_rec_mcp({"servers": []}), "alerts")
    assert p.mcp is not None
    assert p.mcp.servers == []  # surface NO MCP servers -- distinct from None


def test_parse_persona_mcp_absent_is_none():
    p = parse_profile_record(_rec(), "ztpf_templates")
    assert p.mcp is None


def test_parse_persona_mcp_garbled_degrades_to_none():
    # non-list servers ⇒ no override (None), not an empty surface; a bad dedup
    # entry is dropped. Neither raises.
    p = parse_profile_record(_rec_mcp({"servers": "fortisiem",
                                       "capability_dedup": {"x": {"no": "curated"}}}),
                             "alerts")
    assert p is not None
    assert p.mcp is None  # nothing usable declared


def test_validate_flags_bad_mcp_block():
    issues = validate_persona_record(_rec_mcp("nope"))
    assert any(i.field == "mcp" for i in issues)
    issues2 = validate_persona_record(_rec_mcp({"servers": "fortisiem"}))
    assert any(i.field == "mcp.servers" for i in issues2)
