"""tools domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

from typing import Any
from .pydantic_models import (
    ListAgentToolsParams, ListAgentToolsResult, AgentToolSurface, AgentToolRow,
)

from .ops_cards import _CARD_STOP_REASON
from .ops_common import _log, _validate_model
from .ops_mcp import (
    AllowlistError, _allowed_mcp_servers_for, _apply_mcp_allowlist, _config_allowlist_keys,
    _parse_mcp_allowlist, _scope_rows_to_config, _scope_tools_to_config,
)
from .ops_personas import ResolvedPersona
from .ops_prompts import (
    _BUILD_ONLY_TOOLS, _normalize_intent, _read_only_quick_action,
    _retired_to_union,
)
from .ops_turn_state import _resolve_profile



# ── Phase 2: state-derived tool frontier (state-derived-intent plan) ─────────
# The page-derived intent is a PRIOR, not a one-way latch. A triage-prior
# session reaches the authoring/enhance surface -- without a page flip -- once its
# state affords it. These two sub-frontiers of _BUILD_ONLY_TOOLS partition the
# build surface by the state signal that unlocks each.

def _enhance_frontier_tools() -> frozenset:
    """Build tools that only make sense against an OPEN playbook: the enhance
    verify+write pair (``ENHANCE_ONLY_TOOLS``). Unlocked into a triage-prior
    session when ``has_open_playbook``."""
    from fsr_playbooks.llm.intents import ENHANCE_ONLY_TOOLS
    return frozenset(ENHANCE_ONLY_TOOLS)


def _authoring_core_tools() -> frozenset:
    """Pure AUTHORING build tools (compile/validate/verify/push/dry-run/…): the
    connector's ``_BUILD_ONLY_TOOLS`` minus the enhance frontier (needs an open
    playbook), minus ``build_playbook_from_trace`` (needs a trace), minus the
    persona-allowlist-only ztpf renderer ``test_template``, and minus anything
    also in ``TRIAGE_ONLY_TOOLS``. That last cut matters: ``run_playbook`` /
    ``resume_playbook`` sit in BOTH build-only and (via
    ``register_triage_tools``) triage-only, i.e. they are deliberately
    persona-allowlist-only (dropped from every base slice). Unlocking authoring
    must not resurrect a tool the build prior itself refuses to advertise --
    otherwise the triage-authoring frontier would over-reach past build.
    Unlocked into a triage-prior session when authoring is active."""
    from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    return (frozenset(_BUILD_ONLY_TOOLS) - _enhance_frontier_tools()
            - {"build_playbook_from_trace", "test_template"}
            - set(TRIAGE_ONLY_TOOLS))


def _authoring_active(has_trace: bool, has_open_playbook: bool) -> bool:
    """Phase 2 predicate: a triage-PRIOR session is in an authoring posture once
    it has material to author from -- a recorded trace to replay OR an open
    playbook to edit. These are the signals available at dispatch WITHOUT a
    per-turn prior; Phase 1 adds the widget's overridable authoring ask for
    precision, and Phase 3's disposition prompt keeps focus while the tool
    surface stays broad. Fail-open by construction (its inputs default True)."""
    return bool(has_trace or has_open_playbook)


def _run_verb_tools() -> set[str]:
    """The "run an ALREADY-DEPLOYED playbook" verb, exempt from BOTH per-intent
    subtractions (#82). Framework-owned so the two repos cannot disagree about
    which names it covers.

    THIS is the function that decides the deployed surface -- `_afforded_drop_set`,
    not the framework's `tools_for_intent`. The card measured `tools_for_intent`
    and reported that triage keeps `run_playbook`; on the live path it does not.
    The connector-local `_BUILD_ONLY_TOOLS` lists `run_playbook`,
    `resume_playbook` AND `search_playbooks`, so a triage turn with no persona
    allowlist had NO way to run a deployed playbook at all. That is why the
    surface only ever worked from a persona-gated module: an allowlist wins over
    the intent slice and quietly re-added what this set had removed.

    The existing parity test cannot catch this. It asserts the connector's set is
    a SUPERSET of the framework's -- deliberately, so the connector may know
    about tools the framework doesn't -- which means an EXTRA entry here (a tool
    the framework deliberately keeps IN triage) is invisible to it.

    Fail-open: an older pinned wheel without the symbol subtracts nothing and
    behaves exactly as before, matching the defensive-import convention used for
    TRIAGE_ONLY_TOOLS.
    """
    try:
        from fsr_playbooks.llm.intents import RUN_VERB_TOOLS
    except ImportError:  # pragma: no cover - older pinned framework wheel
        return set()
    return set(RUN_VERB_TOOLS)


def _afforded_drop_set(prior: str, *, has_trace: bool = True,
                       has_open_playbook: bool = True,
                       authoring_active: bool = False) -> set[str]:
    """State-derived tool frontier, expressed as the DROP set over the full
    registry (drop = registry − afforded frontiers). The Phase 2 seam the plan
    calls ``afforded_tools(state)``; ``_tools_for_persona`` is the one caller.

    PHASE 3 -- the full-surface switch. The Track C5 build/triage wall is DOWN:
    the disposition prompt (turn_plan's context-prior block, appended to every
    live prompt via ``_plan_constraints_suffix``) now carries the focus that tool
    absence used to enforce, so BOTH priors advertise the whole registry and
    the only subtractions left are:

    - **deliberately persona-allowlist-only tools** -- the names BOTH sides
      scoped out (``_BUILD_ONLY_TOOLS`` ∩ ``TRIAGE_ONLY_TOOLS``: run/resume --
      re-added below via the run verb -- and the ztpf renderer
      ``test_template``). Computed as the intersection so neither list needs a
      third hand-maintained copy of the concept.
    - **state gates**: ``build_playbook_from_trace`` without a trace would only
      dead-end with ``empty_trace``; the enhance verify+write pair without an
      open playbook has nothing to write to (this keeps the advertised list
      honest).

    ``prior`` and ``authoring_active`` no longer change the surface -- they are
    kept in the signature for the callers/tests that pass them, and the prior
    still drives the PROMPT via plan_turn. Safety is unchanged and never came
    from tool absence: broadened reach still hits the tier-3 /
    ``confirm_mode="approve"`` gate and the ``_write_gate``.

    PHASE 3.1 -- **both state drops are gone.** They came out one at a time,
    for the same reason but on different evidence.

    The shared reason: the advertised tool list is a **prompt-cache prefix**.
    Anthropic caches on a prefix match over (tools, system), so any change to
    the list discards the whole cached prefix -- system prompt included -- and
    re-bills it at write price. Both flags flip *within* a session, on the two
    commonest arcs we have, so each silently cost a cache rebuild
    mid-conversation, every time.

    ``has_trace`` went first: ``build_playbook_from_trace`` on an empty trace
    already answers ``empty_trace`` with "fall back to the hand-author build
    path" -- a refusal that teaches, from inside the tool, always armed. The
    drop bought nothing the tool did not already do.

    ``has_open_playbook`` had to wait, and the reason is worth keeping: its
    refusal was NOT always armed. ``turn_plan.gate_refusal`` covers the
    frontier only when a TurnPlan is installed, and every derivation site is
    wrapped so a turn is never broken by the gate -- which meant any
    derivation error produced no plan and therefore *no gate*.
    ``EnhanceDeliveryGuard`` was never a second mechanism either: its
    ``outstanding()`` short-circuits when the offer tool is absent, so the
    drop was what kept it inert on a create. Removing the drop then would have
    left ONE fail-open mechanism between a from-scratch create and a model
    terminating at ``awaiting_enhancement_offer`` -- a drift we have observed.

    What changed: the install now fails **closed**. A derivation failure
    installs ``TurnPlan.failsafe(...)`` instead of nothing (see
    ``_install_failsafe_turn_plan``), so the frontier is refused with
    ``state_unresolved`` rather than silently opened. With the gate armed on
    every live and resume turn, the drop is redundant -- and
    ``EnhanceDeliveryGuard`` stays inert on a create for a better reason than
    tool absence: ``verify_enhancement`` is itself gated, so a create never
    reaches a passing verify to leave outstanding.

    The advertised list is now a per-config constant: nothing in this function
    varies within a session. Measured by ``fsr_playbooks.llm.cache_prefix``
    and gated by ``tests/test_cache_prefix_stability.py``.
    """
    del prior, authoring_active, has_trace, has_open_playbook  # see above
    from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    drop = set(_BUILD_ONLY_TOOLS) & set(TRIAGE_ONLY_TOOLS)
    return drop - _run_verb_tools()


#: Read-only tools granted with any persona allowlist (see _tools_for_persona).
_PERSONA_IMPLIED_TOOLS: frozenset[str] = frozenset({"test_template"})


def _tools_for_persona(persona: ResolvedPersona,
                       has_trace: bool = True,
                       has_open_playbook: bool = True,
                       authoring_active: bool = False,
                       quick_action: Any = None,
                       read_only: bool = False) -> list[dict[str, Any]]:
    """Tool slice for a resolved persona. A persona allowlist WINS over the
    built-in intent slice (an authoring persona may legitimately want a tool the
    intent slice would exclude); otherwise the intent's state-derived drop-set
    applies. An empty/absent allowlist leaves the intent slice unchanged.

    ``authoring_active`` (Phase 2) lets a triage-prior turn reach the authoring/
    enhance surface when session state affords it; default False preserves the
    old per-intent slice for bare/introspection callers."""
    from fsr_playbooks.llm.tools import anthropic_tools
    # The connector's own triage/NOC tools (get_record, siem_*, faz_*, fmg_*)
    # register into the library REGISTRY as a side effect of importing
    # `fsr_soc_triage`, and nothing imported it before the FIRST slice was
    # computed. `_advertised_tools` reaches it only later, via the M2 surfacing
    # import -- so the first call in a fresh worker advertised 20 tools and every
    # call after it 40. Live on .159, a cold worker's turn had NO record lookup
    # and NO SIEM/FAZ/FMG at all, and which worker answered decided it.
    # Idempotent (`register_triage_tools` is), so this is a no-op once warm.
    try:
        import fsr_soc_triage  # noqa: F401 - imported for its registration side effect
    except Exception:  # noqa: BLE001 - older layout: pre-registration surface
        pass
    # The agentic toolkit's workflows (debug_run, build_playbook, provision_module,
    # verify_effect, orient, repair_playbook, investigate_alert) register into the
    # SAME library REGISTRY as a one-time, idempotent side effect, so the
    # connector's dispatch, tiers and approval cards cover them without a copy.
    # `offer_to_model=False`: dispatchable by name but withheld from the advertised
    # list -- the connector's own per-turn slice still decides what the model sees,
    # and the workflows showed no model-outcome uplift to headline. Degrades to a
    # no-op where the toolkit is not installed or the framework surface is older.
    try:
        from fsr_agentic_toolkit.workflow_tools import register_into_framework
        register_into_framework(offer_to_model=False)
    except Exception:  # noqa: BLE001 - toolkit absent or pre-registration framework
        pass
    # A read-only chip is withheld LAST, over every branch below including a
    # persona allowlist. The allowlist wins over the built-in intent slice
    # because a persona author knows their workflow -- but "explain" is the
    # analyst's own statement about THIS turn, made after the persona was
    # configured, and nothing downstream re-asks them.
    def _finish(sliced: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Enforce read-only mode if either: (1) quick_action is a read-only chip,
        # or (2) the read_only parameter is set (scheduled tasks with allow_mutating=False)
        if not (_read_only_quick_action(quick_action) or read_only):
            return sliced
        # The framework's set: the one dispatch refuses on, so the list and
        # the refusal cannot disagree (this connector used to keep a copy).
        from fsr_playbooks.llm.tools import READ_ONLY_WITHHELD_TOOLS
        return [t for t in sliced if t["name"] not in READ_ONLY_WITHHELD_TOOLS]

    allow = persona.tools_allow
    if allow:
        allow = set(allow)
        # Phase 1/2 consolidation: field personas predate the union tools and
        # name the retired members (`emit_action_card`, `find_operation`, ...).
        # Those names are no longer advertised, so an untranslated allowlist
        # silently DROPS the capability -- the shipped-but-inert class. Grant
        # the union the retired name is subsumed by.
        retired = _retired_to_union()
        allow |= {retired[n] for n in allow & set(retired)}
        # Read-only tools every module persona carries, whether its author
        # listed them or not. Live, the Automation Actions persona left out
        # the Jinja renderer and the analyst asked for a render four times;
        # the model could only offer to run a playbook.
        allow |= _PERSONA_IMPLIED_TOOLS
        # M2 -- COMPOSE (not replace): a persona's allowlist ∪ the afforded MCP
        # surface, so a persona needn't re-list every materialized MCP tool by
        # name. The per-page server-surfacing + capability-dedup filters in
        # `_advertised_tools` then narrow the MCP set to what this page affords.
        # Safety unchanged: writes stay tier-3/approve; this only widens the
        # afforded READ/MCP surface.
        return _finish([t for t in anthropic_tools()
                        if t["name"] in allow or str(t["name"]).startswith("mcp_")])
    drop = _afforded_drop_set(persona.intent, has_trace=has_trace,
                              has_open_playbook=has_open_playbook,
                              authoring_active=authoring_active)
    return _finish([t for t in anthropic_tools() if t["name"] not in drop])


def _resolve_turn_persona(intent_hint: Any, module: Any,
                          session_id: str) -> ResolvedPersona:
    """THE single turn-resolution point: normalize the built-in base intent and
    layer the module's Key Store persona (if any). Fail-open -- no persona ⇒ the
    built-in base behaves exactly as before."""
    return ResolvedPersona(intent=_normalize_intent(intent_hint),
                           profile=_resolve_profile(module, session_id))


# Native-MCP tools that change something, by server. The platform gateway
# publishes no read-only/destructive annotations, so this is declared, never
# guessed from a name. These always route through an approval card, whatever
# the config says; an operator's `mutating_tools` add to the list.
_BUILTIN_MUTATING_MCP_TOOLS: dict[str, frozenset[str]] = {
    "soc": frozenset({"block_indicator", "update_alert_ai_analysis"}),
    "playbooks": frozenset({"execute_playbook"}),
}

# The same, for external MCP servers that are known products. An operator picks
# the allowlist key for these, so the rule names its product (`"product":
# "fortisiem"`); a server keyed by the product name counts as that product.
# FortiSIEM's MCP server (checked on v1.24) publishes no MCP tool annotations.
_PRODUCT_MUTATING_MCP_TOOLS: dict[str, frozenset[str]] = {
    "fortisiem": frozenset({
        "append_incident_comment_by_id", "clear_incident_by_id",
        "update_incident_resolution_by_id", "update_incident_severity_by_id"}),
}


# External-product MCP tools that are threat-intel lookups, by product, for the
# autonomy policy's evidence rule. Declared, never guessed: the policy can act
# on what one of these says. An operator adds more with a rule's
# `"intel_tools": [...]`.
_PRODUCT_INTEL_MCP_TOOLS: dict[str, frozenset[str]] = {
    "fortisiem": frozenset({"get_reputation_by_entity"}),
}


def _mcp_intel_tools(config: dict) -> dict[str, str]:
    """`{mcp_<server>__<tool>: product}` for the MCP tools this config declares
    to be threat-intel lookups. A rule's product is its `product`, else its
    key (the wiring script keys FortiSIEM `fortisiem`)."""
    try:
        al = _parse_mcp_allowlist(config)
    except AllowlistError:
        return {}
    out: dict[str, str] = {}
    for server, rule in al.items():
        if not isinstance(rule, dict):
            continue
        product = str(rule.get("product") or server)
        tools = set(_PRODUCT_INTEL_MCP_TOOLS.get(product, ()))
        extra = rule.get("intel_tools")
        if isinstance(extra, list):
            tools.update(str(t) for t in extra if isinstance(t, str) and t)
        for t in tools:
            out[f"mcp_{server}__{t}"] = product
    return out


def _mcp_mutating_tools_by_server(config: dict) -> dict[str, set[str]]:
    """{server -> {bare tool name, ...}} of the MCP tools an operator has
    DECLARED mutating: ``_BUILTIN_MUTATING_MCP_TOOLS`` plus the ``mcp_allowlist``
    config. Each server rule
    may carry ``"mutating_tools": ["block_indicator", ...]`` (bare names) beside
    the existing ``tools``/``tier`` keys. Deterministic and explicit -- we never
    guess from the tool name (the FortiSOAR MCP gateway exposes no
    read-only/destructive annotations to key off, and a name heuristic is
    locale-bound and wrong for equivalents like `hunt_ioc_siem`). Absent ⇒ {}."""
    # Share the ONE parser: this used to re-parse `mcp_allowlist` independently,
    # so the two readers could disagree about the same config -- and this one
    # fails open (no tool declared mutating ⇒ no approval gate), which is the
    # dangerous direction. Now a config `_apply_mcp_allowlist` refuses to
    # materialize is also one this refuses to derive gates from.
    out: dict[str, set[str]] = {k: set(v) for k, v in _BUILTIN_MUTATING_MCP_TOOLS.items()}
    try:
        al = _parse_mcp_allowlist(config)
    except AllowlistError:
        return out
    for server, rule in al.items():
        if not isinstance(rule, dict):
            continue
        product = rule.get("product") or server
        if product in _PRODUCT_MUTATING_MCP_TOOLS:
            out.setdefault(str(server), set()).update(_PRODUCT_MUTATING_MCP_TOOLS[product])
        names = rule.get("mutating_tools")
        if isinstance(names, list):
            out.setdefault(str(server), set()).update(
                str(n) for n in names if isinstance(n, str) and n)
    return out


def _gate_mutating_mcp_tools(tools: list[dict[str, Any]], config: dict) -> None:
    """Elevate operator-declared mutating MCP tools in ``tools`` to tier-3 in the
    framework's ``TOOL_TIERS`` so ``_resolve_tier``/``dispatch`` route them
    through the approval card instead of auto-running.

    The materializer tiers MCP tools per SERVER, which is too coarse for a
    gateway like ``soc`` that mixes reads (``get_alert``) and mutations
    (``block_indicator``) -- the server is read_only, so ``block_indicator``
    lands at tier-1 auto-run and containment EXECUTES with no approval card,
    defeating the tier-3 gate. Classification is EXPLICIT (config
    ``mcp_allowlist[server].mutating_tools``), never guessed from the name.

    Idempotent, fail-open, in-place on the process-global tier map (dispatch
    reads it, not the per-tool spec). Only ever RAISES a tier, never lowers one."""
    declared = _mcp_mutating_tools_by_server(config)
    if not declared:
        return
    try:
        from fsr_playbooks.llm.tools import TOOL_TIERS
    except Exception:  # noqa: BLE001 - older wheel / no framework tier map
        return
    for t in tools:
        name = t.get("name") if isinstance(t, dict) else None
        if not isinstance(name, str) or not name.startswith("mcp_") or "__" not in name:
            continue
        server, bare = name[len("mcp_"):].split("__", 1)
        if bare in declared.get(server, ()) and TOOL_TIERS.get(name, 1) < 3:
            TOOL_TIERS[name] = 3
            if isinstance(t, dict):  # keep the advertised spec consistent
                t["tier"] = 3
                t["confirm_mode"] = "approve"


def _advertised_tools(config: dict, persona: ResolvedPersona, *,
                      has_trace: bool = True, has_open_playbook: bool = True,
                      authoring_active: bool = False,
                      include_mcp: bool = True,
                      module: Any = None,
                      quick_action: Any = None,
                      reach_servers: Any = None,
                      read_only: bool = False,
                      create_strategy: Any = None) -> list[dict[str, Any]]:
    """THE single source of the advertised tool surface for EVERY turn-like
    dispatch -- chat turn, resume, and ``list_agent_tools`` introspection. All
    three must route through here so the surface can't drift between paths (the
    drift that let a real turn materialize ``mcp_*`` tools a resume/introspection
    silently missed).

    Two ordered steps:
      1. apply the connector's ``mcp_allowlist`` -- materialize
         ``mcp_<server>__<tool>`` identically everywhere -- UNLESS ``include_mcp``
         is False. A caller that already applied it earlier this same dispatch
         (the chat turn and resume paths do, before their pre-model card
         execution) passes ``include_mcp=False`` to skip a redundant
         re-materialize, which would otherwise re-hit external MCP servers over
         the network.
      2. compute the persona/intent slice via ``_tools_for_persona``.

    Never raises over MCP -- a materializer failure leaves the curated slice
    intact (fail-open, as every other MCP call site is)."""
    if include_mcp:
        try:
            _apply_mcp_allowlist(config or {})
        except Exception:  # noqa: BLE001 - never fail a turn over the allowlist
            pass
    tools = _tools_for_persona(persona, has_trace=has_trace,
                               has_open_playbook=has_open_playbook,
                               authoring_active=authoring_active,
                               quick_action=quick_action,
                               read_only=read_only)
    # Scope the materialized `mcp_*` set to what THIS config authorizes
    # (tracker #63). `_tools_for_persona` reads the process-global materialized
    # surface -- `materializer.SERVER_MAP` / `llm.tools.REGISTRY` -- which every
    # config on the worker shares, so a worker that had served another config
    # carried that config's servers into this one's turn. Live on .159 an
    # empty-allowlist config advertised a byte-identical 66-tool surface to a
    # 3-server one (20 cold). #62 scoped the two introspection ops; this is the
    # turn surface itself, where it is an authorization question, not a
    # cosmetic one -- `mcp_soc__block_indicator` was in that set.
    #
    # BEFORE surfacing/tier-gating deliberately: a server this config never
    # named must not be reachable on-ask via `reach_servers` either. Fail-open
    # on error, like every other MCP step here.
    try:
        tools = _scope_tools_to_config(tools, _allowed_mcp_servers_for(config or {}))
    except Exception:  # noqa: BLE001 - never fail a turn over scoping
        pass
    # M2: deterministic per-page MCP surfacing + curated↔MCP capability dedup.
    # Pure state→set; fail-open -- an unrecognized module leaves the slice intact,
    # and any error keeps the pre-M2 surface (never fail a turn over surfacing).
    try:
        from fsr_soc_triage.triage_sources import surface_page_tools
        tools = surface_page_tools(
            tools, module,
            persona_servers=persona.mcp_servers,
            persona_dedup=persona.mcp_capability_dedup,
            reach_servers=reach_servers)  # M2.1: on-ask off-surface reach
    except Exception:  # noqa: BLE001 - older layout / import issue: pre-M2 surface
        pass
    # B3a: a log-source helper family (siem_*/faz_*/fmg_*) is advertised only
    # when its connector is installed, configured and healthy. Live on .159
    # (no FortiSIEM REST connector, no FortiAnalyzer) siem_search failed 14/14
    # calls and faz_search 11/11 while still on offer. Unknown ⇒ kept; the
    # turn is told what's missing via `_source_availability_note`.
    try:
        from fsr_soc_triage.source_availability import (
            gate_source_tools, source_availability)
        _avail = source_availability()
        tools = gate_source_tools(tools, _avail)
        # I3: tell the evidence cap which ready sources this turn can query,
        # so a capped thin verdict names the ones it never checked.
        try:
            from fsr_playbooks.mcp_server._citation_validator import set_turn_ready_sources
            from fsr_soc_triage.source_availability import FAMILY_PREFIXES
            _names = [t.get("name") or "" for t in tools if isinstance(t, dict)]
            set_turn_ready_sources({
                prefix: _avail[key].label
                for prefix, key in FAMILY_PREFIXES.items()
                if key in _avail and _avail[key].ready
                and any(n.startswith(prefix) for n in _names)})
        except Exception:  # noqa: BLE001 - older framework: no source naming
            pass
        # I5: configured ticket and threat-intel connectors this turn has not
        # queried belong in the same ledger (reached through run_op, so they
        # are matched by connector, not tool prefix).
        try:
            from fsr_playbooks.mcp_server._citation_validator import (
                set_turn_ready_connectors)
            from fsr_soc_triage.source_availability import ready_connector_sources
            _run_op_ok = any(n in ("run_op", "run_connector_operation")
                             for n in _names)
            set_turn_ready_connectors(ready_connector_sources() if _run_op_ok else None)
        except Exception:  # noqa: BLE001 - older framework: no connector ledger
            pass
        # U6: the change card names connectors; hand the framework the catalog
        # label reader, called only when an enhancement offer is emitted.
        try:
            from fsr_playbooks.mcp_server._citation_validator import (
                set_turn_connector_labels)
            from fsr_soc_triage.source_availability import connector_labels
            set_turn_connector_labels(connector_labels)
        except Exception:  # noqa: BLE001 - older framework: no label map
            pass
    except Exception:  # noqa: BLE001 - never fail a turn over availability
        pass
    # Gate mutating MCP tools (e.g. mcp_soc__block_indicator) to tier-3 so
    # containment surfaces an approval card instead of auto-running. Runs AFTER
    # materialization (the mcp_* names are in `tools`), so it overrides the
    # coarse per-server tier. Fail-open.
    try:
        _gate_mutating_mcp_tools(tools, config or {})
    except Exception:  # noqa: BLE001 - never fail a turn over tier gating
        pass
    # Last: withhold every tool THIS turn's dispatch would refuse whatever its
    # arguments (the bound read-only flag and turn plan; framework
    # `advertised_for_turn`). A build turn used to be shown the triage tools
    # its plan refuses, and each call cost a round.
    try:
        from fsr_playbooks.llm.tools import advertised_for_turn
        tools = advertised_for_turn(tools)
    except Exception:  # noqa: BLE001 - older framework: the list as built
        pass
    # Tell the discovery tools which MCP servers this turn reaches. They list
    # connectors only, and live the model read "FortiSIEM is not set up" off
    # them while mcp_fortisiem__* sat in its tool list.
    try:
        from fsr_playbooks.mcp_server.materializer import set_turn_mcp_tools
        set_turn_mcp_tools([t.get("name") for t in tools if isinstance(t, dict)])
    except Exception:  # noqa: BLE001 - older framework: no reach reporting
        pass
    # And which of them are declared threat-intel lookups, so a policy's
    # evidence rule can rest on one (roadmap S12).
    try:
        from fsr_playbooks.mcp_server.materializer import set_turn_intel_mcp_tools
        advertised = {t.get("name") for t in tools if isinstance(t, dict)}
        set_turn_intel_mcp_tools({n: p for n, p in _mcp_intel_tools(config or {}).items()
                                  if n in advertised})
    except Exception:  # noqa: BLE001 - older framework: MCP is never intel
        pass
    # Create-method strategy (the widget toggle). On a BUILD turn, a resolved
    # `guided` strategy removes the free-text YAML authoring surface so
    # compose_playbook (plan->compiler) is the only path to a deliverable -- the
    # slice IS the policy, so merely advertising compose does not stop a model
    # free-writing YAML. Applied LAST, over any persona allowlist, because the
    # analyst's live toggle (like the read-only chip) overrides the persona
    # config. `freeform`/unresolved is a no-op. Gated to build: triage never
    # authors YAML anyway. Fail-open on any error.
    try:
        from fsr_playbooks.llm import intents
        if _normalize_intent(getattr(persona, "intent", None)) == "build":
            tools = intents.apply_create_strategy(tools, create_strategy)
    except Exception:  # noqa: BLE001 - older framework / no create-strategy
        pass
    return tools


def _source_availability_note(tools: list[dict[str, Any]]) -> str:
    """The "log sources you cannot reach" prompt block for this turn, or "".

    Pairs with the B3a gate in `_advertised_tools`: withholding a source's
    tools must not make it silently vanish -- the analyst can install,
    configure or repair it, so the turn names what is missing and the fix."""
    try:
        from fsr_soc_triage.source_availability import (
            availability_note, source_availability)
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        note = availability_note(source_availability(),
                                 {n for n in names if isinstance(n, str)})
    except Exception:  # noqa: BLE001 - never fail a turn over the note
        return ""
    return f"\n\n{note}" if note else ""


def _append_record_source_gaps(wire_transcript: list[dict], config: dict, *,
                               storage: Any, session_id: str) -> list[dict] | None:
    """B3a backstop: a capability_gap card for each source that raised a record
    this turn read but that can't be reached. Live, the model sent that card in
    one run of two and in the other offered a pivot it had no tool for. Once
    per session per connector: a card already stored for this session is not
    repeated. None = leave the turn as is."""
    try:
        from fsr_soc_triage.source_availability import (
            append_gap_cards, gap_card_id, record_gaps_in_turn, source_availability)
        gaps = record_gaps_in_turn(wire_transcript, source_availability())
        gaps = [v for v in gaps if not storage.get_card(session_id, gap_card_id(v))]
        if not gaps:
            return None
        return append_gap_cards(
            wire_transcript, gaps,
            mcp_servers=_allowed_mcp_servers_for(config or {}),
            blocking_types=set(_CARD_STOP_REASON) | {"approval_request", "error"})
    except Exception:  # noqa: BLE001 - never fail a turn over the backstop
        return None


def _entity_has_open_playbook(entity: Any) -> bool:
    """True when the mounted entity carries an OPEN playbook to enhance --
    i.e. ``entity.playbook_yaml`` is present and non-empty (the same signal the
    enhance prompt/terminal keys on). A from-scratch CREATE mounts no playbook,
    so this is False and the enhance verify+write pair is dropped from the slice.
    Precise, not fail-open: absence of a playbook_yaml *is* the create case."""
    return bool(
        isinstance(entity, dict)
        and str(entity.get("playbook_yaml") or "").strip()
    )


def _playbook_in_hand(storage, session_id: str | None) -> dict | None:
    """The playbook this conversation built, when one exists: the latest
    offered draft ({kind: 'draft', yaml}) or the playbook it saved ({kind:
    'saved', yaml, workflow_uuid}), whichever came last. A build turn with no
    playbook open grounds on it, so "now also add X" edits it with
    edit_playbook rather than re-typing it -- re-typed refinements lost an
    alert link and rewrote a condition nobody asked about, live on the build
    sweep, while saying "everything else is unchanged". Fail-soft: any storage
    problem means nothing in hand, which is the pre-existing behaviour."""
    if not session_id:
        return None
    try:
        saved = storage.get_session_saved_playbook(session_id)
        offer = storage.latest_card_of_type(session_id, "playbook_offer")
    except Exception:  # noqa: BLE001
        return None
    draft = None
    if offer:
        card, at = offer
        yaml_text = card.get("final_yaml")
        if isinstance(yaml_text, str) and yaml_text.strip():
            draft = {"kind": "draft", "yaml": yaml_text, "at": at}
    if saved and saved.get("yaml") and (
            draft is None or saved["saved_at"] >= draft["at"]):
        return {"kind": "saved", "yaml": saved["yaml"],
                "workflow_uuid": saved["workflow_uuid"]}
    return draft


def _with_playbook_in_hand(entity: Any, intent: str, storage,
                           session_id: str | None) -> Any:
    """The turn's entity, carrying the conversation's playbook in hand when
    this is a build turn and the widget sent no open playbook. The playbook
    the analyst has OPEN always wins. Build turns only: a triage turn later in
    the same session is about a record, not the draft."""
    if intent != "build" or _entity_has_open_playbook(entity):
        return entity
    in_hand = _playbook_in_hand(storage, session_id)
    if not in_hand:
        return entity
    out = dict(entity) if isinstance(entity, dict) else {}
    out["playbook_yaml"] = in_hand["yaml"]
    out["playbook_in_hand"] = in_hand["kind"]
    return out


def _session_has_trace(storage, session_id: str) -> bool:
    """True when the session's SkillTrace has recorded connector-op calls -- the
    raw material ``build_playbook_from_trace`` replays. A from-scratch build has
    none. Fail-open (return True, leave the tool advertised) on any error so an
    unexpected trace-read failure never silently hides an authoring tool."""
    try:
        raw = storage.get_session_trace(session_id)
        if not raw or not raw.strip():
            return False
        from fsr_playbooks.agent.skill_trace import SkillTrace
        return len(SkillTrace.from_json(raw).calls) > 0
    except Exception:
        return True


def _tool_source(name: str) -> str:
    """Provenance of a tool as it appears in the agent surface: a materialized
    native-MCP tool (``mcp:<server>``), a widget card emitter (``widget``), or a
    curated SAFE_TOOLS entry (``safe``). Fail-open to ``safe`` off-box."""
    try:
        from fsr_playbooks.mcp_server import materializer
        server_map = getattr(materializer, "SERVER_MAP", None) or {}
        origin = server_map.get(name)
        if origin:
            return f"mcp:{origin[0]}"
    except Exception:  # noqa: BLE001 - older wheel / no materializer
        pass
    if name.startswith("emit_"):
        return "widget"
    return "safe"


def _agent_tool_row(tool: dict[str, Any], verbose: bool) -> "AgentToolRow":
    """Map one anthropic-format tool dict to a typed AgentToolRow, enriching it
    with the tier dispatch will apply, its confirm_mode and its provenance.

    The tier is ``TOOL_TIERS``'s, which is what dispatch reads; the REGISTRY
    spec keeps the materializer's per-server tier, so reading the spec reported
    a gated `mcp_soc__block_indicator` as tier 1 auto-run while a turn would
    card it."""
    name = tool.get("name", "")
    tier = None
    confirm_mode = None
    try:
        from fsr_playbooks.llm.tools import REGISTRY, TOOL_TIERS
        spec = REGISTRY.get(name)
        if spec is not None:
            tier = getattr(spec, "tier", None)
            confirm_mode = getattr(spec, "confirm_mode", None)
        live = TOOL_TIERS.get(name)
        if isinstance(live, int) and live >= 0 and live != tier:
            tier = live
            confirm_mode = ("auto" if live <= 1 else
                            "approve" if live <= 3 else "step_up")
    except Exception:  # noqa: BLE001 - off-box / import issue
        pass
    return AgentToolRow(
        name=name,
        tier=tier,
        confirm_mode=confirm_mode,
        source=_tool_source(name),
        description=tool.get("description"),
        input_schema=tool.get("input_schema") if verbose else None,
    )


def list_agent_tools(config: dict, params: dict) -> dict:
    """List the agentic tool surface the LLM is handed, per intent (and persona).

    This is the runtime-truth counterpart to the framework's static ``SAFE_TOOLS``
    registry: it reflects what a *real turn on this box* would advertise --

      - the intent slice (``triage`` drops the YAML-authoring/mutation tools;
        ``build`` drops the containment-staging ``emit_action_card``/``run_op``),
      - the mounted ``module``'s Key Store persona allowlist (which WINS over the
        intent slice), resolved via the same ``_resolve_profile`` path a turn uses,
      - the box's materialized native-MCP-gateway tools (``mcp_<server>__<tool>``)
        when ``include_mcp`` is set (default), sourced from the connector's
        ``mcp_allowlist``.

    Each tool carries its approval ``tier`` (0-4, or -1 dynamic), ``confirm_mode``,
    ``source`` (``safe`` / ``mcp:<server>`` / ``widget``) and description; pass
    ``verbose`` to include each tool's ``input_schema``. Read-only introspection --
    NOT itself an LLM-callable tool. Fail-open: any resolution error degrades to
    the built-in intent slice rather than raising.

    Params: ``{intent?: "triage"|"build"|"both", module?, include_mcp?, verbose?}``.
    Returns ``{ok, module, persona_applied, surfaces: [{intent, count, tools[...]}]}``.
    """
    p = _validate_model(ListAgentToolsParams, params or {})

    # Live fidelity: apply the connector's mcp_allowlist ONCE up front (same
    # helper the turn/resume paths use) so the per-intent slices below -- routed
    # through _advertised_tools with include_mcp=False -- advertise exactly the
    # mcp_<server>__<tool> set a real turn would. Apply-once-then-slice mirrors
    # the chat-turn and resume dispatch pattern.
    if p.include_mcp:
        try:
            _apply_mcp_allowlist(config or {})
        except Exception:  # noqa: BLE001 - never fail the listing over MCP
            pass

    # Resolve the module's Key Store persona once (empty session_id -- this is a
    # stateless introspection call, not a turn). Fail-open to None.
    profile = None
    if p.module and isinstance(p.module, str):
        try:
            profile = _resolve_profile(p.module, "")
        except Exception:  # noqa: BLE001
            profile = None
    persona_applied = bool(profile is not None
                           and getattr(profile, "tools_allow", None))

    want = _normalize_intent(p.intent) if p.intent != "both" else "both"
    intents = ["triage", "build"] if want == "both" else [want]

    surfaces: list[AgentToolSurface] = []
    for intent in intents:
        try:
            tools = _advertised_tools(
                config, ResolvedPersona(intent=intent, profile=profile),
                include_mcp=False,  # applied once before the loop
                module=p.module,  # M2: per-page MCP surfacing
                reach_servers=p.reach_servers)  # M2.1: on-ask reach preview
        except Exception as e:  # noqa: BLE001 - report empty, don't crash
            _log(f"list_agent_tools: slice for {intent!r} failed: {e}", "warning")
            tools = []
        rows = [_agent_tool_row(t, p.verbose) for t in tools]
        # Report THIS config's native tools, not the worker's union. Live on a
        # 3-server config and an empty-allowlist one, the answer was identical
        # (66 tools against 20 curated cold), because the slice below reads the
        # same process-global surface. Scoped HERE, on the introspection
        # output, rather than in `_advertised_tools`: that is the turn surface
        # the chat loop advertises from, and narrowing it changes what an
        # investigation can reach (tracker #62).
        if p.include_mcp:
            rows = _scope_rows_to_config(rows, set(_config_allowlist_keys(config)))
        rows.sort(key=lambda r: r.name)
        surfaces.append(AgentToolSurface(
            intent=intent, count=len(rows), tools=rows))

    return ListAgentToolsResult(
        ok=True,
        module=p.module if isinstance(p.module, str) else None,
        persona_applied=persona_applied,
        surfaces=surfaces,
    ).model_dump()
