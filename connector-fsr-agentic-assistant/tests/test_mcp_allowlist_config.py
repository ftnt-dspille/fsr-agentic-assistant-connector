"""The native-MCP config contract: one parser, loud failures, picker + JSON.

Before this, `mcp_allowlist` was parsed independently in three places, each
swallowing every error into a silent empty result -- so a typo'd allowlist was
indistinguishable from an unset one, and the operator got no signal from the UI,
the health check, or the logs. These pin the distinction that fixes it:
``{}`` means "native MCP not configured", ``AllowlistError`` means
"configured, but wrong".
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_HAVE_FW = importlib.util.find_spec("fsr_playbooks") is not None

pytestmark = pytest.mark.skipif(
    not _HAVE_FW, reason="fsr_playbooks framework not importable in this env")


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
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


def _llm_ok() -> dict:
    return {"llm_provider": "fortiai-proxy"}  # on-platform; needs no key


# --- server key normalization ------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("https://localhost/mcp/soc/", "soc"),
    ("https://localhost/mcp/modules/", "modules"),
    ("https://fortisoar.example.com/mcp/utility/", "utility"),
    ("soc", "soc"),                       # already bare -- hand-written JSON
    ("connector:fortigate", "connector:fortigate"),
    ("https://localhost/mcp/connector/aws/", "connector:aws"),
    ("", ""),
])
def test_server_key_from_mcp_configuration_url(ops, value, expected):
    assert ops._mcp_server_key(value) == expected


# --- "not configured" stays silent -------------------------------------------

@pytest.mark.parametrize("config", [
    {},
    {"mcp_allowlist": ""},
    {"mcp_servers": []},
    {"mcp_servers": [], "mcp_allowlist": ""},
])
def test_unset_is_empty_not_an_error(ops, config, no_platform):
    assert ops._parse_mcp_allowlist(config) == {}


# --- access mode: All servers (default) vs Selected ---------------------------

@pytest.fixture
def mcp_mod(ops):
    return sys.modules["_fsrpb_conn.ops_mcp"]


@pytest.fixture
def no_platform(mcp_mod, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_platform_mcp_servers", lambda: [])


@pytest.fixture
def platform(mcp_mod, monkeypatch):
    servers = ["modules", "playbooks", "soc", "utility", "connector:aws"]
    monkeypatch.setattr(mcp_mod, "_platform_mcp_servers", lambda: list(servers))
    return servers


def test_unset_access_means_every_platform_server(ops, platform):
    al = ops._parse_mcp_allowlist({"soar_api_key": "k"})
    assert sorted(al) == sorted(platform)
    assert all(r == {"tools": "*", "tier": "read_only"} for r in al.values())


def test_all_servers_under_hmac_skips_servers_that_would_401(ops, platform):
    """soc and playbooks replay the caller's identity downstream; HMAC is
    URI-bound, so every record read 401s. Don't advertise them."""
    al = ops._parse_mcp_allowlist({})
    assert sorted(al) == ["connector:aws", "modules", "utility"]
    svc = ops._parse_mcp_allowlist({"soar_username": "u", "soar_password": "p"})
    assert {"soc", "playbooks"} <= set(svc)


def test_a_named_server_is_kept_under_hmac(ops, platform):
    al = ops._parse_mcp_allowlist({"mcp_allowlist": json.dumps({"soc": {"tools": "*"}})})
    assert "soc" in al and "playbooks" not in al


def test_picked_servers_keep_meaning_just_those(ops, platform):
    al = ops._parse_mcp_allowlist({"mcp_servers": ["https://localhost/mcp/soc/"]})
    assert sorted(al) == ["soc"]


def test_advanced_allowlist_alone_adds_to_all_servers(ops, platform):
    """An external server (FortiSIEM's own MCP) rides on top of the platform's."""
    ext = {"fortisiem": {"url": "https://siem.example.com/phoenix/mcp",
                         "tools": "*", "tier": "read_only"},
           "soc": {"tools": ["get_alert"], "tier": "read_only"}}
    al = ops._parse_mcp_allowlist({"mcp_allowlist": json.dumps(ext), "soar_api_key": "k"})
    assert sorted(al) == sorted(platform + ["fortisiem"])
    assert al["soc"] == {"tools": ["get_alert"], "tier": "read_only"}  # narrowed


def test_selected_access_ignores_the_platform(ops, platform):
    al = ops._parse_mcp_allowlist({
        "mcp_access": "Selected servers",
        "mcp_allowlist": json.dumps({"soc": {"tools": "*"}})})
    assert sorted(al) == ["soc"]


def test_selected_access_with_nothing_named_is_dormant(ops, platform):
    assert ops._parse_mcp_allowlist({"mcp_access": "Selected servers"}) == {}


def test_all_servers_carries_the_picker_tier(ops, platform):
    al = ops._parse_mcp_allowlist({"mcp_tier": "mutating"})
    assert {r["tier"] for r in al.values()} == {"mutating"}


def test_bad_access_value_raises(ops, platform):
    with pytest.raises(ops.AllowlistError, match="Access"):
        ops._parse_mcp_allowlist({"mcp_access": "everything"})


def test_discovery_is_empty_off_box(ops, mcp_mod, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_platform_servers_cache", None)
    assert mcp_mod._platform_mcp_servers() == []


def test_discovery_skips_inactive_rows_and_keys_connectors(ops, mcp_mod, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_platform_servers_cache", None)
    monkeypatch.setattr(mcp_mod, "_read_mcp_configurations", lambda: [
        {"url": "https://localhost/mcp/soc/"},
        {"url": "https://localhost/mcp/utility/", "active": False},
        {"url": "https://localhost/mcp/connector/aws/"},
        {"url": "https://localhost/mcp/soc/"},
    ])
    assert mcp_mod._platform_mcp_servers() == ["soc", "connector:aws"]
    monkeypatch.setattr(mcp_mod, "_platform_servers_cache", None)


def test_access_field_defaults_to_all_servers():
    info = json.loads((ROOT / "info.json").read_text())
    f = next(x for x in info["configuration"]["fields"] if x["name"] == "mcp_access")
    assert f["value"] == "All servers"
    assert set(f["options"]) == {"All servers", "Selected servers"}


# --- the pickers --------------------------------------------------------------

def test_pickers_build_the_allowlist_from_urls(ops):
    al = ops._parse_mcp_allowlist({
        "mcp_servers": ["https://localhost/mcp/soc/",
                        "https://localhost/mcp/utility/"],
        "mcp_tier": "read_only",
    })
    assert al == {"soc": {"tools": "*", "tier": "read_only"},
                  "utility": {"tools": "*", "tier": "read_only"}}


def test_picker_tier_is_carried_through(ops):
    al = ops._parse_mcp_allowlist({
        "mcp_servers": ["https://localhost/mcp/soc/"], "mcp_tier": "mutating"})
    assert al["soc"]["tier"] == "mutating"


def test_picker_accepts_a_comma_string(ops):
    """A single-select or a serialized multiselect arrives as a string."""
    al = ops._parse_mcp_allowlist(
        {"mcp_servers": "https://localhost/mcp/soc/,https://localhost/mcp/utility/"})
    assert sorted(al) == ["soc", "utility"]


def test_bad_picker_tier_raises(ops):
    with pytest.raises(ops.AllowlistError, match="Tier"):
        ops._parse_mcp_allowlist({"mcp_servers": ["soc"], "mcp_tier": "readonly"})


# --- the advanced JSON escape hatch ------------------------------------------

def test_advanced_json_wins_over_the_pickers(ops):
    """Back-compat: a config written before the pickers existed must still win."""
    al = ops._parse_mcp_allowlist({
        "mcp_servers": ["https://localhost/mcp/utility/"],
        "mcp_tier": "read_only",
        "mcp_allowlist": json.dumps({"soc": {"tools": ["get_alert"],
                                             "tier": "mutating"}}),
    })
    assert al == {"soc": {"tools": ["get_alert"], "tier": "mutating"}}


@pytest.mark.parametrize("raw,match", [
    ("{not json", "not valid JSON"),
    ('["soc"]', "must be a JSON object"),
    ('{"soc": "everything"}', "must be an object"),
    ('{"soc": {"tier": "sometimes"}}', "tier"),
    ('{"soc": {"tools": 3}}', "tools"),
])
def test_malformed_advanced_json_raises_instead_of_silently_disabling(
        ops, raw, match):
    with pytest.raises(ops.AllowlistError, match=match):
        ops._parse_mcp_allowlist({"mcp_allowlist": raw})


# --- the fail-open that mattered ---------------------------------------------

def test_a_rule_that_is_not_an_object_no_longer_silently_ungates(ops):
    """`{"soc": "everything"}` is the dangerous shape.

    It is a dict, so the old `_apply_mcp_allowlist` accepted it and materialized
    the server's tools; but the old `_mcp_mutating_tools_by_server` skipped the
    non-dict rule and returned {}, i.e. NOTHING declared mutating -- tools that
    should have required an approval card could run unattended. Both readers now
    reject the same config, and the built-in write tools stay gated anyway.
    """
    cfg = {"mcp_allowlist": '{"soc": "everything"}'}
    with pytest.raises(ops.AllowlistError):
        ops._parse_mcp_allowlist(cfg)
    assert ops._mcp_mutating_tools_by_server(cfg)["soc"] == {
        "block_indicator", "update_alert_ai_analysis"}


def test_mutating_tools_are_read_from_the_shared_parser(ops):
    cfg = {"mcp_allowlist": json.dumps(
        {"soc": {"tools": "*", "tier": "mutating",
                 "mutating_tools": ["enrich_indicator"]}})}
    assert "enrich_indicator" in ops._mcp_mutating_tools_by_server(cfg)["soc"]


def test_mutating_tools_keyed_by_bare_server_when_set_via_picker_url(ops):
    """The gate must key off the same normalized name the materializer used."""
    cfg = {"mcp_allowlist": json.dumps(
        {"https://localhost/mcp/soc/": {"tools": "*", "tier": "mutating",
                                        "mutating_tools": ["enrich_indicator"]}})}
    got = ops._mcp_mutating_tools_by_server(cfg)
    assert "enrich_indicator" in got["soc"]
    assert not any(k.startswith("http") for k in got)


# --- health check surfaces it -------------------------------------------------

def test_health_check_rejects_a_malformed_allowlist(ops):
    from connectors.core.connector import ConnectorError
    with pytest.raises(ConnectorError, match="not valid JSON"):
        ops._check_health({**_llm_ok(), "mcp_allowlist": "{oops"})


def test_health_check_rejects_api_key_identity_with_no_key(ops):
    from connectors.core.connector import ConnectorError
    with pytest.raises(ConnectorError, match="no API Key"):
        ops._check_health({**_llm_ok(), "mcp_servers": ["soc"],
                           "mcp_auth": "API key", "soar_api_key": ""})


def test_health_check_rejects_service_account_missing_password(ops):
    from connectors.core.connector import ConnectorError
    with pytest.raises(ConnectorError, match="username/password"):
        ops._check_health({**_llm_ok(), "mcp_servers": ["soc"],
                           "mcp_auth": "Service account",
                           "soar_username": "svc", "soar_password": ""})


def test_health_check_passes_when_native_mcp_is_simply_unconfigured(ops):
    assert ops._check_health(_llm_ok()) is True


# --- the schema the UI renders ------------------------------------------------

def test_every_config_field_declares_visible():
    """A field without an explicit `visible` is HIDDEN in the connector form.

    Five fields once omitted it, so `mcp_allowlist` and all three credential
    fields were unreachable in the UI -- native MCP could not be enabled by any
    user. A census of an 8.0 appliance found 238 config fields across 56
    connectors and ZERO omitting the key; it is a hard convention, not a style.
    """
    info = json.loads((ROOT / "info.json").read_text())
    missing = []
    for f in info["configuration"]["fields"]:
        if "visible" not in f:
            missing.append(f.get("name"))
        for subs in (f.get("onchange") or {}).values():
            missing += [s.get("name") for s in subs if "visible" not in s]
    assert missing == [], f"config fields missing an explicit 'visible': {missing}"


def test_credential_fields_are_revealed_by_the_identity_picker():
    """Each credential hangs off exactly one `mcp_auth` branch, not all at once."""
    info = json.loads((ROOT / "info.json").read_text())
    auth = next(f for f in info["configuration"]["fields"]
                if f["name"] == "mcp_auth")
    branches = {k: [s["name"] for s in v] for k, v in auth["onchange"].items()}
    assert branches == {
        "Built-in (HMAC)": [],
        "API key": ["soar_api_key"],
        "Service account": ["soar_username", "soar_password"],
    }
    top = {f["name"] for f in info["configuration"]["fields"]}
    assert not ({"soar_api_key", "soar_username", "soar_password"} & top), \
        "credentials must be onchange-revealed, not top-level"


def test_server_picker_is_populated_from_the_platform():
    """The options come from FortiSOAR's own MCP Configurations, not a hardcoded
    list -- so only servers that actually exist on the box are offered."""
    info = json.loads((ROOT / "info.json").read_text())
    f = next(x for x in info["configuration"]["fields"]
             if x["name"] == "mcp_servers")
    assert f["type"] == "multiselect" and f["renderer"] == "dynamic"
    rp = f["rendererParams"]
    assert rp["url"].startswith("/api/3/mcp_configurations")
    # The UI resolves `response.data[urlResponseKey]`; /api/3 returns hydra.
    assert rp["urlResponseKey"] == "hydra:member"
    # contentKey becomes the stored value -- must be what _mcp_server_key parses.
    assert rp["contentKey"] == "url" and rp["contentLabel"] == "name"


# --- the page ranking names only this config's MCP tools ----------------------
# The registry is worker-wide: a turn on a config with FortiSIEM materializes
# mcp_fortisiem__* for every later turn on that worker. The prompt's "tools
# that usually matter" sentence read that registry, so a config with no MCP
# was told to reach for tools it cannot call.

def _rank_sees(ops, monkeypatch, config, servers):
    import fsr_playbooks.llm.tools as T
    import fsr_soc_triage.triage_sources as ts
    ts_mod = sys.modules["_fsrpb_conn.ops_turn_state"]
    mcp_mod = sys.modules["_fsrpb_conn.ops_mcp"]
    monkeypatch.setattr(T, "anthropic_tools", lambda: [
        {"name": "get_record"}, {"name": "mcp_fortisiem__get_reputation_by_entity"},
        {"name": "mcp_soc__get_alert"}])
    monkeypatch.setattr(ts, "page_tool_ranking", lambda tools, module: tuple(t["name"] for t in tools))
    monkeypatch.setattr(mcp_mod, "_parse_mcp_allowlist", lambda cfg: {s: {} for s in servers})
    return set(ts_mod._page_tool_ranking("alerts", config))


def test_ranking_drops_mcp_tools_from_servers_the_config_lacks(ops, monkeypatch):
    seen = _rank_sees(ops, monkeypatch, {"mcp_access": "Selected servers"}, servers=["soc"])
    assert seen == {"get_record", "mcp_soc__get_alert"}


def test_ranking_with_no_mcp_servers_names_no_mcp_tool(ops, monkeypatch):
    seen = _rank_sees(ops, monkeypatch, {}, servers=[])
    assert seen == {"get_record"}
