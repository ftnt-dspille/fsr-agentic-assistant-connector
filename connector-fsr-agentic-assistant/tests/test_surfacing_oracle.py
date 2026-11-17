"""M2 surfacing oracle — deterministic per-page MCP surface + capability dedup.

The advertised tool surface must be a pure function of the page (module): each
page shows only the MCP servers its affordance class surfaces, and an MCP tool
that duplicates a curated first-class helper is dropped in favor of the curated
one. These tests pin that oracle against a synthetic slice modeled on the LIVE
materialized inventory (three servers: ``deepwiki``, ``fortisiem``, ``soc``), so
a change to the maps that widened or narrowed a page's surface fails loudly.

Pure filters — no framework, no box — so this suite always runs.
"""
from fsr_soc_triage.triage_sources import (
    AFFORDANCE_SERVERS,
    CAPABILITY_DEDUP,
    MODULE_AFFORDANCE,
    apply_capability_dedup,
    effective_surfaced_servers,
    mcp_inventory,
    apply_persona_server_scope,
    mcp_server_of,
    page_tool_ranking,
    surface_page_tools,
    surfaced_servers_for_module,
)

# Materialized MCP tool inventory, mirrored from a live `list_agent_tools`
# (include_mcp=True) on the FortiSIEM-wired box. If the box surfaces new tools,
# extend this — the oracle only reasons about names, not schemas.
_MCP = {
    "deepwiki": [
        "mcp_deepwiki__ask_question",
        "mcp_deepwiki__read_wiki_contents",
        "mcp_deepwiki__read_wiki_structure",
    ],
    "fortisiem": [
        "mcp_fortisiem__append_incident_comment_by_id",
        "mcp_fortisiem__clear_incident_by_id",
        "mcp_fortisiem__get_context_by_entity",
        "mcp_fortisiem__get_incident_by_id",
        "mcp_fortisiem__get_incidents_by_entity",
        "mcp_fortisiem__get_iocs_by_incident_ids",
        "mcp_fortisiem__get_related_incidents_by_id",
        "mcp_fortisiem__get_reputation_by_entity",
        "mcp_fortisiem__get_top_10_risky_devices_incidents",
        "mcp_fortisiem__get_top_10_risky_users_incidents",
        "mcp_fortisiem__get_trigger_events_by_incident_id",  # dup of curated
        "mcp_fortisiem__query_fsm_clickhouse",               # dup of curated
        "mcp_fortisiem__query_fsm_clickhouse_prompts",
        "mcp_fortisiem__query_fsm_postgres",                 # dup of curated
        "mcp_fortisiem__query_fsm_postgres_prompts",
        "mcp_fortisiem__update_incident_resolution_by_id",
        "mcp_fortisiem__update_incident_severity_by_id",
    ],
    "soc": [
        "mcp_soc__block_indicator",
        "mcp_soc__enrich_indicator",
        "mcp_soc__get_alert",
        "mcp_soc__get_asset",
        "mcp_soc__get_indicators",
        "mcp_soc__hunt_ioc_siem",
    ],
}

# Curated first-class helpers that overlap MCP capabilities (the winners).
# B3a: the incident/raw helpers are axes of siem_search now.
_CURATED = ["siem_search"]


def _full_slice():
    """A tool slice as `_tools_for_persona` would hand it to the surfacer:
    curated helpers + every materialized MCP tool."""
    names = list(_CURATED) + [n for s in _MCP.values() for n in s]
    return [{"name": n} for n in names]


def _servers(tools):
    return {mcp_server_of(t["name"]) for t in tools} - {None}


def _names(tools):
    return {t["name"] for t in tools}


# --- pure helpers ----------------------------------------------------------

def test_mcp_server_of():
    assert mcp_server_of("mcp_fortisiem__get_incident_by_id") == "fortisiem"
    assert mcp_server_of("mcp_soc__get_alert") == "soc"
    assert mcp_server_of("siem_raw_query") is None      # curated, not MCP
    assert mcp_server_of("mcp_broken") is None           # no __ separator
    assert mcp_server_of(None) is None


def test_surfaced_servers_for_module():
    assert surfaced_servers_for_module("alerts") == {"fortisiem", "soc"}
    assert surfaced_servers_for_module("workflows") == {"deepwiki"}
    assert surfaced_servers_for_module("unknown_module") is None  # unrestricted
    assert surfaced_servers_for_module(None) is None
    assert surfaced_servers_for_module("") is None


# --- the oracle: per-module advertised surface -----------------------------

def test_soc_triage_page_ranks_rather_than_narrows():
    """A1.3 flipped this page's contract. What an alert page used to REMOVE
    (deepwiki) it now merely declines to RANK -- the tools stay advertised and
    the preference moves into the prompt prior. Dedup still subtracts."""
    out = surface_page_tools(_full_slice(), "alerts")
    assert _servers(out) == {"deepwiki", "fortisiem", "soc"}, (
        "the per-page server subtraction is gone -- every server the config "
        "allows stays advertised; the page states a preference instead")
    ranked = page_tool_ranking(_full_slice(), "alerts")
    assert {mcp_server_of(n) for n in ranked} == {"fortisiem", "soc"}
    assert all(mcp_server_of(n) != "deepwiki" for n in ranked)
    names = _names(out)
    # dedup: curated present ⇒ its MCP duplicates dropped
    assert "mcp_fortisiem__get_trigger_events_by_incident_id" not in names
    assert "mcp_fortisiem__query_fsm_clickhouse" not in names
    assert "mcp_fortisiem__query_fsm_postgres" not in names
    # curated winners kept
    assert "siem_search" in names
    # rich MCP-only fortisiem capabilities survive
    assert "mcp_fortisiem__get_reputation_by_entity" in names
    assert "mcp_fortisiem__get_iocs_by_incident_ids" in names


def test_authoring_page_ranks_the_reference_server():
    """A build page PREFERS the reference server. It no longer hides the
    others -- an analyst who asks to hunt from the editor gets the tools."""
    out = surface_page_tools(_full_slice(), "workflows")
    assert _servers(out) == {"deepwiki", "fortisiem", "soc"}
    ranked = page_tool_ranking(_full_slice(), "workflows")
    assert ranked, "a mapped page must state a preference"
    assert {mcp_server_of(n) for n in ranked} == {"deepwiki"}
    # curated helpers are not MCP tools — they pass regardless
    assert "siem_search" in _names(out)


def test_unrestricted_page_states_no_preference():
    """Silence, not "everything": an unmapped page has nothing to rank, and a
    recital of every tool would steer nothing."""
    assert page_tool_ranking(_full_slice(), "some_other_module") == ()
    assert page_tool_ranking(_full_slice(), None) == ()


def test_ranking_is_capped_so_the_sentence_stays_readable():
    ranked = page_tool_ranking(_full_slice(), "alerts", limit=3)
    assert len(ranked) == 3


def test_unrestricted_module_keeps_all_servers():
    """An unmapped page is unrestricted (pre-M2 surface) — every server stays,
    though capability dedup still applies (curated wins everywhere)."""
    out = surface_page_tools(_full_slice(), "some_other_module")
    assert _servers(out) == {"deepwiki", "fortisiem", "soc"}
    assert "mcp_fortisiem__query_fsm_clickhouse" not in _names(out)  # deduped


def test_capability_never_double_advertised():
    """The core invariant: on NO page does a capability appear twice (a curated
    helper AND its MCP duplicate both advertised)."""
    for module in [*MODULE_AFFORDANCE, "unmapped_page", None]:
        names = _names(surface_page_tools(_full_slice(), module))
        for cap, (curated, mcp_dupes) in CAPABILITY_DEDUP.items():
            if curated in names:
                overlap = names & set(mcp_dupes)
                assert not overlap, (
                    f"{module}: capability {cap} double-advertised "
                    f"(curated {curated} + {overlap})")


def test_mcp_only_dupe_survives_when_curated_absent():
    """When the curated winner isn't in the slice, its MCP counterpart is kept
    as the fallback for that capability (MCP-only kept)."""
    slice_no_curated = [{"name": n} for s in _MCP.values() for n in s]
    out = apply_capability_dedup(slice_no_curated)
    assert "mcp_fortisiem__query_fsm_clickhouse" in _names(out)
    assert "mcp_fortisiem__get_trigger_events_by_incident_id" in _names(out)


def test_affordance_servers_are_known_server_keys():
    """Every server named in an affordance class must be a real materialized
    server key (guards a typo that would silently surface nothing)."""
    known = set(_MCP)
    for cls, servers in AFFORDANCE_SERVERS.items():
        unknown = servers - known
        assert not unknown, f"affordance class {cls} names unknown server(s) {unknown}"


def test_every_module_maps_to_a_defined_class():
    for module, cls in MODULE_AFFORDANCE.items():
        assert cls in AFFORDANCE_SERVERS, (
            f"module {module} → undefined affordance class {cls}")


# --- M2 step 4: persona overrides ------------------------------------------

def test_persona_servers_still_SUBTRACT():
    """The half that did NOT become a ranking, and must not.

    A persona server list is a scope an operator configured, not a guess about
    what a page needs, and it is stable for the whole session -- so keeping it
    as a subtraction costs no prompt-cache churn and preserves the M2 scope."""
    out = surface_page_tools(_full_slice(), "alerts", persona_servers=["deepwiki"])
    assert _servers(out) == {"deepwiki"}
    # An unmapped page + persona override still restricts (override wins over None)
    out2 = surface_page_tools(_full_slice(), "unmapped", persona_servers=["soc"])
    assert _servers(out2) == {"soc"}
    # And the ranking follows the persona, not the page default.
    ranked = page_tool_ranking(_full_slice(), "alerts", persona_servers=["deepwiki"])
    assert {mcp_server_of(n) for n in ranked} == {"deepwiki"}


def test_persona_empty_servers_surfaces_no_mcp():
    """`[]` is a real override (surface nothing), distinct from None (no
    override). Still true after A1.3 -- it is a persona scope, not a page
    guess, so it still subtracts."""
    out = surface_page_tools(_full_slice(), "alerts", persona_servers=[])
    assert _servers(out) == set()
    assert "siem_search" in _names(out)  # curated still passes
    assert apply_persona_server_scope(_full_slice(), persona_servers=[]) == [
        {"name": n} for n in _CURATED]


def test_persona_dedup_override_extends_base():
    """A persona can add a curated↔MCP dedup the base map doesn't have."""
    from fsr_soc_triage.profiles import PersonaCapabilityDedup
    extra = {"siem.context": PersonaCapabilityDedup(
        curated="siem_search",
        mcp=["mcp_fortisiem__get_context_by_entity"])}
    out = surface_page_tools(_full_slice(), "some_other_module",
                             persona_dedup=extra)
    names = _names(out)
    # base dedup still applies AND the persona's extra one fires (curated present)
    assert "mcp_fortisiem__get_context_by_entity" not in names
    assert "mcp_fortisiem__query_fsm_clickhouse" not in names


# --- M2.1: on-ask reach for off-surface servers ----------------------------

def test_reach_unions_offsurface_server_onto_page():
    """`reach_servers` widens a page's default surface — an off-surface server
    named on-ask is kept, without disturbing the page defaults."""
    # deepwiki is OFF the alerts surface by default; reach it in.
    out = surface_page_tools(_full_slice(), "alerts", reach_servers=["deepwiki"])
    assert _servers(out) == {"fortisiem", "soc", "deepwiki"}


def test_reach_only_adds_never_removes():
    """Reaching a server already on-surface is a no-op; reaching an unknown
    server doesn't drop anything (union semantics)."""
    base = _servers(surface_page_tools(_full_slice(), "alerts"))
    same = _servers(surface_page_tools(_full_slice(), "alerts",
                                       reach_servers=["fortisiem"]))
    assert same == base
    bogus = _servers(surface_page_tools(_full_slice(), "alerts",
                                        reach_servers=["nope"]))
    assert bogus == base


def test_reach_is_moot_on_unrestricted_page():
    """An unmapped page already surfaces everything; reach can't narrow it."""
    out = surface_page_tools(_full_slice(), "unmapped_page",
                             reach_servers=["deepwiki"])
    assert _servers(out) == {"deepwiki", "fortisiem", "soc"}


def test_reach_composes_with_persona_override():
    """A persona narrows the surface; reach still widens on top of the persona
    set (not the affordance default)."""
    out = surface_page_tools(_full_slice(), "alerts",
                             persona_servers=["soc"],
                             reach_servers=["deepwiki"])
    assert _servers(out) == {"soc", "deepwiki"}


def test_effective_surfaced_servers_precedence():
    assert effective_surfaced_servers("alerts") == {"fortisiem", "soc"}
    assert effective_surfaced_servers("alerts", reach_servers=["deepwiki"]) == \
        {"fortisiem", "soc", "deepwiki"}
    assert effective_surfaced_servers("alerts", persona_servers=["soc"]) == {"soc"}
    # unrestricted stays None even with reach (reach is moot)
    assert effective_surfaced_servers("unmapped", reach_servers=["soc"]) is None


# --- M2.1: list_mcp_servers discovery inventory ----------------------------

def test_mcp_inventory_groups_and_annotates():
    """`mcp_inventory` groups materialized tools by server and marks each on/off
    the module's default surface."""
    rows = mcp_inventory(_full_slice(), "alerts")
    by = {r["server"]: r for r in rows}
    assert set(by) == {"deepwiki", "fortisiem", "soc"}  # curated tools excluded
    assert by["fortisiem"]["on_surface"] is True
    assert by["soc"]["on_surface"] is True
    assert by["deepwiki"]["on_surface"] is False        # off-surface for alerts
    # tools listed + sorted, server prefix intact
    assert by["soc"]["tools"] == sorted(_MCP["soc"])


def test_mcp_inventory_reach_flips_on_surface():
    """Reaching an off-surface server flips its `on_surface` annotation."""
    rows = mcp_inventory(_full_slice(), "alerts", reach_servers=["deepwiki"])
    by = {r["server"]: r for r in rows}
    assert by["deepwiki"]["on_surface"] is True


def test_mcp_inventory_unrestricted_all_on_surface():
    rows = mcp_inventory(_full_slice(), None)
    assert all(r["on_surface"] for r in rows)
