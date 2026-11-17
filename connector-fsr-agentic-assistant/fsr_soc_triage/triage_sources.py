"""Source-aware tool routing -- the curation layer that matches *tools to data*.

An alert's evidence lives in whatever system raised it: a FortiSIEM alert's
driving events are in FortiSIEM, a FortiAnalyzer alert's are in FAZ device
logs. The first-class triage helpers come in matching pairs (``siem_search``
↔ ``faz_search``, each with a ``by=`` axis per pivot), so the right
pivot depends on the source. This module maps a record's originating
connector(s) to the correct toolset and renders entity-filled opening moves --
and, for a *case* that spans multiple sources, recommends pivoting the same
indicator across every source it spans.

Declarative + idempotent: add a source = add a dict to ``SOURCE_TOOLS``.
"""
from __future__ import annotations

from typing import Any

# Canonical source key → its first-class toolset. `templates` are format
# strings filled with the record's entities; `connector` is the FSR connector
# name to confirm via list_configured_connectors before recommending, and the
# one to install when none is. `connectors`, where present, lists every
# connector that can back the helpers, preferred first -- see
# `source_connectors`.
SOURCE_TOOLS: dict[str, dict[str, Any]] = {
    "fortisiem": {
        "label": "FortiSIEM",
        # v2 is the successor and carries every v1 operation under v1's name
        # (fortinet-fortisiemv2 6.1.0+), so the helpers run on either. Boxes
        # have one or the other; .159 has only v2, and the helpers used to be
        # withheld there as "FortiSIEM not installed".
        "connector": "fortinet-fortisiemv2",
        "connectors": ("fortinet-fortisiemv2", "fortinet-fortisiem"),
        # Only ADVERTISED names: the #69 merge took siem_search_ip & co. off the
        # registry and these templates kept recommending them -- every one an
        # `unknown tool` (B3a).
        "ip": 'siem_search(by="ip", value="{ip}", direction="{direction}")',
        "host": 'siem_search(by="host", value="{host}")',
        "user": 'siem_search(by="user", value="{user}")',
        "incident": 'siem_search(by="incident", value="{incident_id}"{source_ip})',
        "raw": 'siem_search(by="raw")',
    },
    "fortianalyzer": {
        "label": "FortiAnalyzer",
        "connector": "fortinet-fortianalyzer",
        "ip": 'faz_search(by="ip", value="{ip}", direction="{direction}")',
        "device": 'faz_search(by="device", value="{device}", window="6h")',
        "alerts": 'faz_search(by="alerts", adom="root")',
        "raw": 'faz_search(by="raw")',
    },
    # Endpoint source: a FortiEDR-raised detection. The host is the pivot --
    # its collector (known? connected? who is logged on?) and what else it
    # has raised -- and the collector name is what an isolate action takes.
    "fortiedr": {
        "label": "FortiEDR",
        "connector": "fortinet-fortiedr",
        "host": 'edr_host(host="{host}")'
                "  -- is it a known collector, and what else has it raised?",
    },
    # NOC device-posture source: a FortiManager-raised "device stopped
    # reporting / went down" record. Pivots are device-centric (the device name
    # normalizes into indicators.hosts via the `deviceName` field).
    "fortimanager": {
        "label": "FortiManager",
        "connector": "fortinet-fortimanager-json-rpc",
        "device": 'fmg_device(by="status", device="{device}")',
        "device_extra": [
            'fmg_device(by="ha", device="{device}")'
            "  -- HA failover or true outage? (peer up ⇒ traffic likely survived)",
            'fmg_device(by="policy", device="{device}")'
            "  -- rule a bad config push in or out",
            'faz_search(by="device", value="{device}", window="6h")'
            "  -- the last logs it sent before going silent (link-down? tunnel?)",
        ],
        "raw": 'faz_search(by="raw")',
    },
}

# Substrings (matched case-insensitively against a source label OR connector id)
# → canonical key. Covers display labels ("Fortinet FortiSIEM") and connector
# names ("fortinet-fortisiem") alike.
_SOURCE_ALIASES: tuple[tuple[str, str], ...] = (
    ("fortisiem", "fortisiem"),
    ("forti-siem", "fortisiem"),
    ("forti siem", "fortisiem"),
    ("fortianalyzer", "fortianalyzer"),
    ("forti-analyzer", "fortianalyzer"),
    ("forti analyzer", "fortianalyzer"),
    ("faz", "fortianalyzer"),
    ("fortimanager", "fortimanager"),
    ("forti-manager", "fortimanager"),
    ("forti manager", "fortimanager"),
    ("fmg", "fortimanager"),
    ("fortiedr", "fortiedr"),
    ("forti-edr", "fortiedr"),
    ("forti edr", "fortiedr"),
)


def source_connectors(key: str) -> tuple[str, ...]:
    """Every connector that can back `key`'s helpers, preferred first."""
    ts = SOURCE_TOOLS.get(key) or {}
    return tuple(ts.get("connectors") or ((ts["connector"],) if ts.get("connector") else ()))


def connectors_label(key: str) -> str:
    """`a` or `a` / `b` -- for prose that names the backing connector(s)."""
    return " or ".join(f"`{c}`" for c in source_connectors(key))


def canonical_source(label: Any) -> str | None:
    """Map a free-text source label / connector id to a canonical key, or None
    when the source has no first-class toolset (falls back to generic run_op)."""
    if not isinstance(label, str) or not label.strip():
        return None
    s = label.strip().lower()
    for alias, key in _SOURCE_ALIASES:
        if alias in s:
            return key
    return None


def toolset_for(label: Any) -> dict[str, Any] | None:
    """The toolset dict for a source label, or None if unmapped."""
    key = canonical_source(label)
    return SOURCE_TOOLS.get(key) if key else None


def _primary_ip(norm: dict[str, Any]) -> dict[str, Any] | None:
    """The most pivot-worthy IP: prefer an external one (likely adversary)."""
    ips = norm.get("indicators", {}).get("ips", [])
    ext = [i for i in ips if not i.get("internal")]
    return (ext or ips or [None])[0]


def _moves_for_source(ts: dict[str, Any], norm: dict[str, Any]) -> list[str]:
    """Entity-filled opening moves for one source's toolset, using whatever
    indicators the record actually has."""
    ind = norm.get("indicators", {})
    moves: list[str] = []
    ip = _primary_ip(norm)
    if "incident" in ts and norm.get("incident_id"):
        # Pass the incident's source IP too: it lets siem_search(by=incident)
        # coerce a stale/seeded id to a live one (or fall back to an IP event
        # search) instead of dead-ending on "Invalid Incident Id".
        src = norm.get("source_ip")
        if not src:
            internal = [i for i in ind.get("ips", []) if i.get("internal")]
            src = (internal[0].get("value") if internal else None)
        src_arg = f', source_ip="{src}"' if src else ""
        moves.append(ts["incident"].format(incident_id=norm["incident_id"],
                                           source_ip=src_arg)
                     + "  -- the events that drove this detection")
    if "ip" in ts and ip:
        direction = "dst" if not ip.get("internal") else "any"
        moves.append(ts["ip"].format(ip=ip["value"], direction=direction)
                     + ("  -- who else talked to this external IP"
                        if not ip.get("internal") else "  -- this host's activity"))
    host = (ind.get("hosts") or [None])[0]
    if "host" in ts and host:
        moves.append(ts["host"].format(host=host))
    # Device posture (NOC): the device name normalizes into `hosts` via the
    # record's `deviceName` field. Lead with reachability, then the corroborating
    # HA / policy-push / last-logs pivots.
    device = (ind.get("hosts") or [None])[0]
    if "device" in ts and device:
        moves.append(ts["device"].format(device=device)
                     + "  -- is the device reachable right now?")
        for tmpl in ts.get("device_extra", []):
            moves.append(tmpl.format(device=device))
    user = (ind.get("users") or [None])[0]
    if "user" in ts and user:
        moves.append(ts["user"].format(user=user))
    if not moves and "alerts" in ts:
        moves.append(ts["alerts"] + "  -- recent alerts from this source")
    return moves


def record_sources(norm: dict[str, Any]) -> list[str]:
    """The distinct source labels this record/case spans (primary first)."""
    out: list[str] = []
    for s in [norm.get("source_connector"), *(norm.get("related_sources") or [])]:
        if isinstance(s, str) and s.strip() and s not in out:
            out.append(s)
    return out


def build_source_routing_block(norm: dict[str, Any],
                               availability: dict[str, Any] | None = None) -> str:
    """A source-aware routing block: for each source the record/case spans that
    has a first-class toolset, the right pre-filled pivots; plus a nudge to fan
    out across configured log sources. Empty when no source is recognized.

    ``availability`` ({source key: SourceAvailability}) marks sources whose
    helpers were withheld this turn: recommending `siem_search_ip` for a
    FortiSIEM alert on a box without FortiSIEM sent the model straight at a
    tool it didn't have."""
    sources = record_sources(norm)
    if not sources:
        return ""

    mapped: list[tuple[dict[str, Any], list[str]]] = []
    unmapped: list[str] = []
    seen_keys: set[str] = set()
    for label in sources:
        ts = toolset_for(label)
        if ts is None:
            unmapped.append(label)
            continue
        key = canonical_source(label)
        if key in seen_keys:
            continue
        seen_keys.add(key)  # type: ignore[arg-type]
        mapped.append((ts, _moves_for_source(ts, norm)))

    if not mapped and not unmapped:
        return ""

    lines = ["## Search the right place -- source-aware pivots"]
    multi = len(mapped) + len(unmapped) > 1
    if multi:
        spanned = ", ".join([ts["label"] for ts, _ in mapped] + unmapped)
        lines.append(
            f"This record spans **multiple sources** ({spanned}). The evidence "
            "for each alert lives in the system that raised it -- pivot your key "
            "indicators in EACH source, don't assume one holds the whole story.")
    else:
        lines.append(
            f"Originating source: **{(mapped or [(None,)])[0][0]['label'] if mapped else unmapped[0]}**. "
            "Use that source's own tools for the driving events.")

    down = {k: v for k, v in (availability or {}).items()
            if not getattr(v, "ready", True)}
    for ts, moves in mapped:
        key = canonical_source(ts["connector"]) or ""
        lines.append(f"**{ts['label']}** ({connectors_label(key)}):")
        gone = down.get(key)
        if gone is not None:
            lines.append(f"- UNAVAILABLE this turn ({gone.state.replace('_', ' ')}): "
                         "its helpers are not in your tool list -- see "
                         "\"Log sources you cannot reach\" below.")
            continue
        if moves:
            lines.extend(f"- {m}" for m in moves)
        else:
            lines.append(f"- (no entity pre-filled yet) use its tools / "
                         f"`{ts['raw']}` once you have an indicator")
    for label in unmapped:
        lines.append(f"**{label}**: no first-class helper -- query it with "
                     f"`run_op` (resolve the op via find_operation first).")

    lines.append("Confirm each connector is configured & healthy "
                 "(`list_configured_connectors`) before relying on it; if one "
                 "is missing, surface it with `emit_card(card_type='capability_gap')` rather "
                 "than silently skipping that source.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# M2 -- deterministic per-page MCP surfacing + curated↔MCP capability dedup
# ---------------------------------------------------------------------------
# Every page today advertises the *same* materialized MCP surface (all servers
# in the connector's ``mcp_allowlist``), regardless of what the analyst is
# looking at -- an alert-triage page and a playbook-authoring page both see the
# full FortiSIEM + SOC + DeepWiki tool set. M2 makes the advertised surface a
# deterministic function of the page (module), so each page shows only the MCP
# servers that fit its job, and an MCP tool that merely duplicates a curated
# first-class helper is dropped in favor of the curated one.
#
# Two pure filters, both state→set (no I/O, no LLM):
#   1. server-surfacing -- keep only the MCP servers this page's affordance class
#      surfaces; a materialized-but-unsurfaced server is reached on-ask (M2.1),
#      never silently gone (its creds still live in ``mcp_allowlist``).
#   2. capability-dedup -- when a curated helper and an MCP tool cover the SAME
#      capability, the curated one wins and the MCP duplicate is dropped; an
#      MCP-only capability is always kept.
#
# Declarative + idempotent, same posture as ``SOURCE_TOOLS``: add a page = add a
# row to ``MODULE_AFFORDANCE``; add a server = add it to an affordance class.

# Affordance class → the set of MCP *server* keys (the ``<server>`` in a
# materialized ``mcp_<server>__<tool>`` name) surfaced by default on that class
# of page. A server materialized in ``mcp_allowlist`` but NOT in this set is
# still callable on-ask (M2.1) -- it is only dropped from the *default* advertised
# surface for this page.
AFFORDANCE_SERVERS: dict[str, set[str]] = {
    # Alert / incident triage: investigate against the SIEM and take SOC
    # actions. Docs/wiki Q&A (deepwiki) is off-surface -- reached on-ask.
    "soc_triage": {"fortisiem", "soc"},
    # NOC device posture: device incidents still live in the SIEM; the
    # SOC-action server (block/enrich indicators) doesn't fit a device-down page.
    "noc_device": {"fortisiem"},
    # Playbook / record authoring: reference lookups help; live log-source and
    # SOC-action servers don't belong on a build page.
    "authoring": {"deepwiki"},
}

# Module (the page the widget is mounted on) → affordance class. A module NOT
# listed resolves to ``None`` (unrestricted) so the surface is unchanged from
# pre-M2 behavior -- mapping a page in is an explicit, reviewable opt-in.
MODULE_AFFORDANCE: dict[str, str] = {
    "alerts": "soc_triage",
    "incidents": "soc_triage",
    "workflows": "authoring",   # the playbook designer module
}

# The affordance class is the ONE page taxonomy (state-derived-intent Phase 1):
# `classify_page` derives it from page *facts*, and the frontier prior, the MCP
# surface, and the default built-in persona all key off it -- no parallel notions
# of "what page am I on". This map adds the frontier-PRIOR dimension to the same
# class already used for MCP surfacing (AFFORDANCE_SERVERS): the page seeds a
# SOFT prior over the per-turn frontier (Phase 2), never a latch. A class with no
# entry -- or an unclassified page (generic record) -- has no explicit prior, so
# the caller's default ("triage", the read-y default) holds.
AFFORDANCE_PRIOR: dict[str, str] = {
    "soc_triage": "triage",
    "noc_device": "triage",
    "authoring": "build",
}

# The FortiSOAR router state of the playbook designer. It is an `authoring` page
# even when its `workflows` record doesn't arrive as an `entity`, so the state
# name is a second classification signal beyond the module.
PLAYBOOK_EDITOR_STATE = "main.playbookDetail"


def classify_page(module: Any = None, state_name: Any = None,
                  in_playbook_editor: Any = False) -> str | None:
    """The affordance class for the page the widget is mounted on, from page
    *facts* -- the single classifier the prior, the MCP surface, and the default
    persona all consult.

    Returns a class present in :data:`AFFORDANCE_SERVERS` / :data:`AFFORDANCE_PRIOR`,
    or ``None`` when the page is unrestricted (a generic record with no mapped
    affordance -- pre-M2 behavior). The playbook designer classifies as
    ``authoring`` even when its module is absent. Never raises."""
    try:
        if in_playbook_editor or (isinstance(state_name, str)
                                  and state_name.strip() == PLAYBOOK_EDITOR_STATE):
            return "authoring"
        if isinstance(module, str) and module.strip():
            return MODULE_AFFORDANCE.get(module.strip())
    except Exception:  # noqa: BLE001 -- classification is best-effort; fail unclassified.
        return None
    return None


def prior_for_page(module: Any = None, state_name: Any = None,
                   in_playbook_editor: Any = False, default: str = "triage") -> str:
    """The SOFT frontier prior for the page: ``build`` on an authoring page, else
    ``default`` (``triage``). This is also the default built-in persona selector
    -- a triage prior seeds the built-in triage persona, a build prior the build
    persona -- so persona/prior/surface stay one taxonomy. Never raises."""
    cls = classify_page(module, state_name, in_playbook_editor)
    return AFFORDANCE_PRIOR.get(cls, default) if cls else default

# The record pages where turning the session into a playbook is WANTED -- a real
# SOC investigation of an alert / case / war room. Everything else (a device, a
# template, a custom module, any bare record) is not a triage worth automating,
# and the "Ready to automate this?" handoff / a proactive `emit_playbook_offer`
# there is noise: the analyst asked to list steps, not to author automation.
#
# Note on naming: FortiSOAR 8.0 renames the *display* label of the `incidents`
# module to "Cases" -- same module `type`, verified live on 8.0 (Alerts/Cases) vs
# 7.x (Alerts/Incidents). So "alert/case/incident" is covered by these two keys;
# there is no separate `cases` module to add.
#
# Kept SEPARATE from MODULE_AFFORDANCE on purpose: that map drives M2 MCP
# surfacing, and adding `warrooms` there would silently narrow which MCP servers
# appear on a war-room page -- a different decision. The two are tied by a
# RELATIONSHIP test (every `soc_triage` module here) rather than by copying a
# list, so they cannot drift into disagreeing about what triage means.
TRIAGE_RECORD_MODULES: frozenset[str] = frozenset({
    "alerts", "incidents", "warrooms",
})


def is_triage_record_module(module: Any) -> bool:
    """True when ``module`` is a record page where playbook-authoring handoffs
    belong (see :data:`TRIAGE_RECORD_MODULES`). The single server-side answer --
    the widget reads it off ``resolve_persona`` rather than keeping its own copy."""
    return isinstance(module, str) and module.strip() in TRIAGE_RECORD_MODULES

# Curated↔MCP capability overlap. For each capability: the curated first-class
# tool that provides it, and the MCP tool(s) that DUPLICATE it. When the curated
# tool is present in the advertised slice it WINS and the MCP duplicates are
# dropped; when it is absent (e.g. an intent slice that dropped the curated
# helper) the MCP tool is kept as the fallback for that capability. Conservative
# by design -- only unambiguous 1:1 overlaps are listed, so rich MCP-only
# FortiSIEM tools (reputation, IOCs, risky-device rollups, incident CRUD, entity
# context) are never suppressed.
CAPABILITY_DEDUP: dict[str, tuple[str, frozenset[str]]] = {
    # Curated side = the ADVERTISED union (B3a folded the incident/raw helpers
    # into siem_search's axes; keyed on the retired names, dedup never fired).
    "siem.incident_events": (
        "siem_search",
        frozenset({"mcp_fortisiem__get_trigger_events_by_incident_id"}),
    ),
    "siem.raw_query": (
        "siem_search",
        frozenset({"mcp_fortisiem__query_fsm_clickhouse",
                   "mcp_fortisiem__query_fsm_postgres"}),
    ),
}


def mcp_server_of(tool_name: Any) -> str | None:
    """The ``<server>`` segment of a materialized ``mcp_<server>__<tool>`` name,
    or ``None`` for a non-MCP (curated) tool name."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp_"):
        return None
    rest = tool_name[len("mcp_"):]
    server, sep, _tool = rest.partition("__")
    return server if sep and server else None


def surfaced_servers_for_module(module: Any) -> set[str] | None:
    """The set of MCP server keys this page surfaces, or ``None`` when the page
    is unrestricted (unknown/absent module -- pre-M2 behavior, surface all)."""
    # Route through the single `classify_page` classifier (module-only call ==
    # the old `MODULE_AFFORDANCE.get(module)` lookup) so MCP surfacing and the
    # frontier prior can't disagree about a page's affordance class.
    cls = classify_page(module)
    if cls is None:
        return None
    return set(AFFORDANCE_SERVERS.get(cls, set()))


def _tool_name(t: Any) -> Any:
    return t.get("name") if isinstance(t, dict) else None


def effective_surfaced_servers(module: Any,
                               persona_servers: Any = None,
                               reach_servers: Any = None) -> set[str] | None:
    """The set of MCP server keys surfaced for this page, or ``None`` when the
    page is unrestricted (surface all -- pre-M2 behavior).

    Precedence: a persona override (``persona_servers``) WINS over the static
    affordance default; ``reach_servers`` (M2.1 on-ask reach) is then UNIONED in
    so an off-surface server named by an explicit request this turn is kept.
    Reach is moot on an unrestricted page (everything is already surfaced)."""
    if persona_servers is not None:
        surfaced: set[str] | None = set(persona_servers)
    else:
        surfaced = surfaced_servers_for_module(module)
    if surfaced is None:
        return None
    if reach_servers:
        surfaced = surfaced | set(reach_servers)
    return surfaced


def apply_server_surfacing(tools: list[dict[str, Any]],
                           module: Any,
                           persona_servers: Any = None,
                           reach_servers: Any = None) -> list[dict[str, Any]]:
    """Drop materialized MCP tools whose server isn't surfaced on ``module``.
    Curated (non-MCP) tools always pass.

    ``persona_servers`` (M2) is a persona's per-module override: a list/set of
    server keys that WINS over the affordance default (``[]`` ⇒ surface no MCP
    servers). ``None`` ⇒ no override; fall back to the static affordance default,
    and an unrestricted module then leaves the list untouched.
    ``reach_servers`` (M2.1) is an explicit per-turn union -- server keys reached
    on-ask that are kept even when off the page's default surface."""
    surfaced = effective_surfaced_servers(module, persona_servers, reach_servers)
    if surfaced is None:
        return list(tools)
    out: list[dict[str, Any]] = []
    for t in tools:
        server = mcp_server_of(_tool_name(t))
        if server is None or server in surfaced:
            out.append(t)
    return out


def _effective_dedup(
        extra: Any) -> dict[str, tuple[str, frozenset[str]]]:
    """The static ``CAPABILITY_DEDUP`` map merged with a persona override
    (``extra`` -- a dict keyed by capability, each value carrying ``curated`` +
    ``mcp``, either a ``PersonaCapabilityDedup`` or a ``(curated, mcp)`` tuple).
    Persona entries override the base per capability key."""
    eff: dict[str, tuple[str, frozenset[str]]] = dict(CAPABILITY_DEDUP)
    if not extra:
        return eff
    for cap, spec in dict(extra).items():
        if isinstance(spec, tuple):
            curated, mcp = spec[0], spec[1]
        else:
            curated = getattr(spec, "curated", None)
            mcp = getattr(spec, "mcp", None)
        if isinstance(curated, str) and curated.strip():
            eff[str(cap)] = (curated, frozenset(mcp or []))
    return eff


def apply_capability_dedup(
        tools: list[dict[str, Any]],
        extra_dedup: Any = None) -> list[dict[str, Any]]:
    """Drop MCP tools that duplicate a curated helper present in ``tools``
    (curated wins); keep an MCP tool whose curated counterpart is absent.
    ``extra_dedup`` (M2) merges a persona's overrides over the static base map."""
    present = {_tool_name(t) for t in tools}
    drop: set[str] = set()
    for _cap, (curated, mcp_dupes) in _effective_dedup(extra_dedup).items():
        if curated in present:
            drop |= set(mcp_dupes)
    if not drop:
        return list(tools)
    return [t for t in tools if _tool_name(t) not in drop]


def apply_persona_server_scope(tools: list[dict[str, Any]],
                               *,
                               persona_servers: Any = None,
                               reach_servers: Any = None) -> list[dict[str, Any]]:
    """Drop MCP tools outside a PERSONA's server scope. Curated tools pass.

    The half of the old server surfacing that stays a subtraction. A persona
    override is a scope an operator configured -- ``[]`` really does mean "no
    MCP for this persona" -- and it does not change as the analyst moves
    around the product, so keeping it costs no prompt-cache churn. The
    per-PAGE affordance default is the half that became a ranking; see
    ``page_tool_ranking``.

    ``None`` (no persona override) leaves the list untouched.
    """
    if persona_servers is None:
        return list(tools)
    scope = set(persona_servers)
    if reach_servers:
        scope |= set(reach_servers)
    out: list[dict[str, Any]] = []
    for t in tools:
        server = mcp_server_of(_tool_name(t))
        if server is None or server in scope:
            out.append(t)
    return out


def page_tool_ranking(tools: list[dict[str, Any]],
                      module: Any,
                      *,
                      persona_servers: Any = None,
                      reach_servers: Any = None,
                      limit: int = 12) -> tuple[str, ...]:
    """The page's preferred tools, best first -- A1.3's RANKING.

    Same inputs as ``apply_server_surfacing``, opposite output: instead of
    deleting the off-surface tools it names the on-surface ones, so the steer
    can live in the prompt prior while the advertised array stays constant.

    Why that swap matters is a cache fact, not a taste one. Tool definitions
    are part of Anthropic's cacheable prefix (``tools`` -> ``system`` ->
    ``messages``), so a per-page subtraction discarded the whole prefix --
    the system prompt behind it included -- on every page change. A ranking
    costs a sentence.

    Ordered: the page's own MCP servers first (alphabetically within a
    server, servers in the page's declared order where we have one), then
    nothing else -- the tail is deliberately unnamed. ``limit`` keeps the
    sentence readable; a 60-tool recital steers nothing.

    Pure (state -> tuple). An unrestricted page has no preference to state and
    returns ``()`` -- silence, not "everything".
    """
    surfaced = effective_surfaced_servers(module, persona_servers, reach_servers)
    if surfaced is None:
        return ()
    by_server: dict[str, list[str]] = {}
    for t in tools:
        name = _tool_name(t)
        server = mcp_server_of(name)
        if server is None or server not in surfaced:
            continue
        by_server.setdefault(server, []).append(str(name))
    # Interleave across servers rather than concatenating them. A flat
    # alphabetical concat let the biggest server eat the whole cap -- on an
    # alert page `fortisiem` (17 tools) crowded `soc` out entirely, so the
    # ranking silently stopped naming the server that answers "block this".
    ordered = [sorted(by_server[srv]) for srv in sorted(by_server)]
    ranked: list[str] = []
    for tier in range(max((len(g) for g in ordered), default=0)):
        for group in ordered:
            if tier < len(group):
                ranked.append(group[tier])
    return tuple(ranked[:limit])


def surface_page_tools(tools: list[dict[str, Any]],
                       module: Any,
                       *,
                       persona_servers: Any = None,
                       persona_dedup: Any = None,
                       reach_servers: Any = None) -> list[dict[str, Any]]:
    """THE deterministic per-page surface: server-surfacing then capability
    dedup, applied to an already-sliced tool list. Pure (state→set). Fail-open --
    an unrecognized module with no persona override leaves the list untouched.

    A persona (M2) may override the surfaced servers (``persona_servers``) and/or
    extend the curated↔MCP capability map (``persona_dedup``). ``reach_servers``
    (M2.1) unions in servers reached on-ask this turn.

    **A1.3: the per-page SERVER subtraction is gone.** It varied within a
    session (the analyst changes page mid-conversation), and the advertised
    array is a prompt-cache prefix, so every page change was re-billing the
    whole prefix at write price. The page's preference is now stated as a
    ranking in the prompt prior -- see ``page_tool_ranking``.

    What is NOT relaxed, and must not be confused with this: per-config
    scoping (``_scope_tools_to_config``) is an AUTHORIZATION filter and still
    subtracts, tier-3 gating still holds at dispatch, and the affordance gates
    still refuse the frontier. Discovery got broader; execution did not.

    Capability dedup stays a subtraction: it is keyed on config + persona, not
    on the page, so it does not vary within a session and costs no cache. So
    does a PERSONA server override -- a persona is a scope somebody configured,
    not a guess about what this page needs, and it is stable for the session."""
    return apply_capability_dedup(
        apply_persona_server_scope(tools, persona_servers=persona_servers,
                                   reach_servers=reach_servers),
        extra_dedup=persona_dedup)


def mcp_inventory(tools: list[dict[str, Any]],
                  module: Any = None,
                  *,
                  persona_servers: Any = None,
                  reach_servers: Any = None) -> list[dict[str, Any]]:
    """Enumerate the materialized MCP surface grouped by server, annotated with
    whether each server is on the page's default surface (M2.1 discovery).

    Pure (state→set) over an already-materialized tool list -- the same inputs the
    surfacing filters see, so ``on_surface`` here matches what a turn on ``module``
    would advertise. An off-surface server is still reachable on-ask (see
    ``reach_servers`` / ``apply_server_surfacing``); ``on_surface=False`` means
    only that it's dropped from the *default* advertised set for this page."""
    surfaced = effective_surfaced_servers(module, persona_servers, reach_servers)
    by_server: dict[str, list[str]] = {}
    for t in tools:
        name = _tool_name(t)
        server = mcp_server_of(name)
        if server is None:
            continue
        by_server.setdefault(server, []).append(str(name))
    out: list[dict[str, Any]] = []
    for server in sorted(by_server):
        out.append({
            "server": server,
            "on_surface": surfaced is None or server in surfaced,
            "tools": sorted(by_server[server]),
        })
    return out


__all__ = ["SOURCE_TOOLS", "canonical_source", "toolset_for",
           "record_sources", "build_source_routing_block",
           "AFFORDANCE_SERVERS", "MODULE_AFFORDANCE", "CAPABILITY_DEDUP",
           "mcp_server_of", "surfaced_servers_for_module",
           "effective_surfaced_servers",
           "apply_server_surfacing", "apply_capability_dedup",
           "apply_persona_server_scope",
           "surface_page_tools", "page_tool_ranking", "mcp_inventory"]
