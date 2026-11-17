"""prompts domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import contextlib
from typing import Any




# ---------- intent-aware prompts + tool slicing (Phase 4) -----------------

# The two task intents the widget sends on every chat_turn (contract §2).
# `build` is the default and the legacy behavior; `triage` biases the model
# toward incident-response containment via emit_action_card.
_TASK_INTENTS = ("triage", "build")

# Fallback prompts used when the vendored markdown can't be read (keeps the
# connector functional even if packaging drops the .md files). The real
# prompts live in fsr_playbooks/agent/system_prompt_{triage,build}.md and are
# vendored wholesale by scripts/build.sh.
_FALLBACK_BUILD_PROMPT = (
    "You are a FortiSOAR playbook author. Help the user compose, "
    "validate, and refine YAML playbooks using the tools available. "
    "Be concise. Quote tool errors verbatim and explain the fix. The "
    "conversation may open with a prior triage transcript plus a directive "
    "to design a re-runnable playbook around the operations used during "
    "triage -- reproduce those operations as parameterized steps."
)
_FALLBACK_TRIAGE_PROMPT = (
    "You are a FortiSOAR incident-response assistant triaging the record in "
    "front of you. Use find(kind='connector') -> find(kind='operation') "
    "-> get_op_schema to "
    "locate capabilities. Call run_op directly only for read-only "
    "intelligence; for ANY mutating/containment action (block, isolate, "
    "quarantine, disable, add-to-group, etc.) build the call and emit it via "
    "emit_card(card_type='action') for analyst approval -- never run it "
    "silently. Do not "
    "author YAML here. Be concise; quote tool errors verbatim."
)

# Track C1: appended to the build system prompt when the analyst fired a
# quick-action chip. Points the model at the matching mode documented in
# system_prompt_build.md's "Quick-action modes" section. Build-only -- triage
# ignores the tag (the build slice carries the playbook-analysis tools the modes
# reference). NOTE: a build turn that suspends (manual_input / a tier-3
# approval) and resumes via chat_resume re-loads the static build prompt
# without this directive; the behavioral mode still carries forward via the
# conversation history, which is sufficient -- the directive is a routing hint,
# not load-bearing turn state.
_QUICK_ACTION_DIRECTIVE = (
    "\n\n# Active quick-action\n\n"
    "The analyst opened this turn with the \"{key}\" quick-action chip. "
    "Apply the matching mode in \"Quick-action modes\" above -- reach for the "
    "tools it lists and follow that mode's guidance for this turn."
)

# Quick-action chips that ask a QUESTION about the open playbook. They are
# read-only by definition: the analyst pressed "explain" or "find issues", not
# "change this".
#
# The build prompt has always SAID so ("do not propose edits unless asked"), and
# that was not enough. Live in sess-vtb857ns: the analyst pressed `explain`, the
# model produced the full step-by-step explanation, and then -- because
# analyze_playbook surfaced a real defect while it explained -- kept going for
# five more model round-trips (find_operation, get_step_type x3,
# verify_enhancement) and ended the turn on emit_enhancement_offer. Two separate
# symptoms, one cause:
#   * the analyst was proposed a playbook CHANGE they never asked for, and
#   * the composer stayed locked with the typing indicator up long after the
#     prose finished -- correctly, because the turn really was still running.
#     The tool calls cost 88ms; the five model round-trips were the dead time.
#
# So the turn's write frontier is removed at DISPATCH, not requested in a prompt. Finding a defect while
# explaining is good -- the model should still SAY so in prose, and the analyst
# can then ask for the fix. It just cannot author and offer one unbidden.
# The list-page chips (what needs attention, summarize, find) are questions
# about records: a drafted playbook is not what was asked for. The widget's
# `_READ_ONLY_CHIPS` must name the same set (test_read_only_chip_parity).
_READ_ONLY_QUICK_ACTIONS = frozenset({"explain", "find_issues", "list_attention",
                                      "list_find", "list_summary"})

def _read_only_quick_action(quick_action: Any) -> bool:
    """True when this turn is a read-only quick-action chip."""
    return bool(quick_action) and str(quick_action).strip().lower() \
        in _READ_ONLY_QUICK_ACTIONS

# Build-only tools dropped from the triage slice (PLAN Phase 4 step 3): the
# YAML-authoring + playbook-mutation surface. Triage keeps discovery,
# picklists, run_op (read-only intel), the HITL emit_* cards, and run-history
# diagnostics.
_BUILD_ONLY_TOOLS = frozenset({
    "validate_yaml", "compile_yaml", "build_playbook_from_trace",
    "compose_playbook",
    "plan_playbook",
    "analyze_playbook",
    "verify_playbook", "verify_enhancement", "edit_playbook",
    "emit_decision_step",
    "search_playbooks", "get_step_type",
    "find_jinja_filter", "find_jinja_pattern", "get_filter_examples",
    "step_through_playbook", "dry_run_playbook",
    "diagnose_yaml_against_pb_execution",
    "push_playbook", "run_playbook", "resume_playbook",
    # Enhance mode's write path -- meaningless in triage, which has no open
    # playbook to update. Caught live: the framework's own BUILD_ONLY_TOOLS
    # already listed it, but this is a SECOND, connector-local copy of the same
    # concept (note `_afforded_drop_set` imports TRIAGE_ONLY_TOOLS from the
    # framework while reading build-only from here), so the framework-side
    # addition did not reach the deployed triage slice. See the parity test in
    # tests/test_intent_tool_scoping.py.
    "emit_enhancement_offer",
    # NOTE: create_record / update_record deliberately are NOT listed here.
    # They used to be, which -- combined with registry.register_record_tools()
    # adding them to TRIAGE_ONLY_TOOLS -- put them in BOTH drop sets, so they
    # were unreachable in every non-persona session. Net effect on a live box:
    # the Agentic assistant could not touch the record it was triaging. LIVE REPRO
    # (session sess-v6uv6x15): "change the type of the alert" over an alert made
    # ZERO tool calls, because no tool existed that could do it. They stay out
    # of the BUILD slice (authoring a playbook is not the place to mutate live
    # records) and are tier 3 / confirm_mode="approve", so every write still
    # returns a pending_approval card the analyst must accept -- the HITL gate,
    # not tool absence, is what makes this safe.
    # ZTPF authoring tool (render + lint) -- persona-allowlist-only posture:
    # excluded here from triage and via TRIAGE_ONLY_TOOLS from build
    # (registry.register_ztpf_tools). See §7c.4.
    "test_template",
})

# Triage-only tools dropped from the build slice (ROADMAP §4, three-pillar plan
# Track C5). The single source is the framework's mutable TRIAGE_ONLY_TOOLS set
# (`fsr_playbooks.llm.intents.TRIAGE_ONLY_TOOLS`), which
# fsr_soc_triage.registry.register_triage_tools() extends at import with the
# connector-owned alert/incident hunt tools (get_record, search_module_records,
# siem_*, faz_*, fmg_*). Build mode authors playbooks -- it must NOT stage live
# containment action-cards (emit_action_card), run connector ops directly
# (run_op), or pivot records/SIEM/FAZ/FMG. find_containment_actions /
# find_enrichment_actions are connector-AWARENESS (authoring) and stay in build.

_PROMPT_CACHE: dict[str, str] = {}


@contextlib.contextmanager
def _session_trace_scope(storage, session_id: str):
    """Install the session's accumulating SkillTrace as the active recorder
    for the duration of an agent turn, then persist it (SKILL_BASED_PLAYBOOK
    PLAN §2). run_op records each executed connector op + its full output
    into this trace; the build path later replays it via
    build_playbook_from_trace instead of guessing jinja wiring.

    Best-effort throughout: a recorder failure must never break a turn. The
    active trace is process-local module state, so it is cleared in finally
    to avoid leaking across concurrent sessions in the same worker.
    """
    trace = None
    try:
        from fsr_playbooks.agent import skill_trace
        prior = storage.get_session_trace(session_id)
        trace = skill_trace.SkillTrace.from_json(prior or "")
        skill_trace.set_active_trace(trace)
    except Exception:
        trace = None
    try:
        yield trace
    finally:
        try:
            from fsr_playbooks.agent import skill_trace
            if trace is not None:
                storage.set_session_trace(session_id, trace.to_json())
            skill_trace.clear_active_trace()
        except Exception:
            pass


def _normalize_intent(value: Any) -> str:
    """Normalize an intent hint to a built-in intent (triage|build).

    Contract §2: `intent` is a discriminator. A value that isn't exactly a
    known task intent is the legacy free-text shape (the pre-Phase-4 contract
    carried the user message here) and resolves to `build`. This is the ONE
    place that back-compat default lives -- the tool-slice / prompt paths dispatch
    on an already-normalized intent, never re-derive `!= "triage"` themselves."""
    return value if value in _TASK_INTENTS else "build"


def _resolve_intent(params: dict) -> str:
    """The chat_turn intent for this turn (Phase 1: a SOFT page prior).

    Precedence:
      1. An explicit task ``intent`` (``triage``/``build``) WINS -- the soft
         override that lets a widget/power-user pin the slice.
      2. A legacy free-text ``intent`` (the pre-Phase-4 shape carried the user
         message here) resolves to ``build``, unchanged.
      3. Absent ``intent`` -- the Phase-1 case once the widget stops pinning --
         derives the prior from PAGE FACTS (the mounted module + the router
         ``page`` state + ``in_playbook_editor``) via the single affordance
         taxonomy: ``build`` on an authoring page, ``triage`` everywhere else.

    Still returns a built-in intent (``triage``/``build``); the per-turn frontier
    (Phase 2) decides reachability from there, so the page is a prior, not a latch.
    """
    raw = params.get("intent")
    if raw in _TASK_INTENTS:
        return raw
    if isinstance(raw, str) and raw.strip():
        return "build"  # legacy free-text shape -> build (unchanged)
    from fsr_soc_triage.triage_sources import prior_for_page
    entity = params.get("entity")
    module = entity.get("module") if isinstance(entity, dict) else None
    return prior_for_page(module=module, state_name=params.get("page"),
                          in_playbook_editor=params.get("in_playbook_editor", False))


def _legacy_intent_text(params: dict):
    """Return the user message carried in the legacy `intent` field, or
    None when `intent` is a task discriminator (new contract shape, where
    the user text lives in messages[])."""
    raw = params.get("intent")
    if isinstance(raw, str) and raw and raw not in _TASK_INTENTS:
        return raw
    return None


def _render_prompt_for_tools(prompt: str, tools: Any) -> str:
    """Resolve the prompt's `<!--if tool-->A<!--else-->B<!--endif-->` blocks
    against the tools this turn actually advertises, so the prompt never tells
    the model to call a tool its slice removed (a Guided create drops
    get_step_type / edit_playbook; the unrendered prompt still named them and
    the model's calls were refused). Fail-open: an older framework without
    `render_prompt_for_slice` returns the prompt unchanged."""
    try:
        from fsr_playbooks.llm.intents import render_prompt_for_slice
    except Exception:  # noqa: BLE001
        return prompt
    names = [str(t.get("name")) for t in tools or []
             if isinstance(t, dict) and t.get("name")]
    try:
        return render_prompt_for_slice(prompt, names)
    except Exception:  # noqa: BLE001 -- never break a turn over the prompt
        return prompt


def _load_prompt(intent: str, plan: Any = None) -> str:
    """Load the intent's system prompt from the vendored fsr_playbooks/agent/
    markdown, caching the read. Falls back to an inline string if the file
    is missing.

    When the turn resolved a TurnPlan (chat_turn derives one up front and
    threads it through prompt resolution), its ``base_prompt`` wins -- the
    plan is the single source of the base text, so the connector cannot drift
    from what plan_turn states. Passed explicitly rather than read from the
    ContextVar because the detached return path runs the turn in a raw thread
    with fresh contextvars (see the profile bind in ``_run_turn``). Bare and
    introspection callers pass no plan and keep the local read."""
    if plan is not None and getattr(plan, "base_prompt", ""):
        # Serve the plan's text UNRENDERED for the tool slice: the plan renders
        # it for the full surface, but the slice the model gets is known only
        # once `_advertised_tools` runs, where `_render_prompt_for_tools`
        # resolves the `<!--if tool-->` blocks. Same source, so it is the
        # plan's text whenever the raw template renders back to it.
        try:
            from fsr_playbooks.llm import intents as _intents
            raw = _intents._load_intent_prompt_raw(intent)
            if _intents.render_prompt_for_slice(raw, None) == plan.base_prompt:
                return raw
        except Exception:  # noqa: BLE001 -- older framework: no slice blocks
            pass
        return plan.base_prompt
    if intent in _PROMPT_CACHE:
        return _PROMPT_CACHE[intent]
    fallback = _FALLBACK_TRIAGE_PROMPT if intent == "triage" else _FALLBACK_BUILD_PROMPT
    text = fallback
    try:
        from pathlib import Path
        if intent == "triage":
            # Triage prompt is now assembled from composable fragments (P2). The
            # canonical fragment set reproduces the legacy monolith byte-for-byte
            # (locked by test_prompt_fragments.py), so this is behavior-identical.
            # Fall back to the monolith .md if the fragment dir is absent (older
            # tarball). The prompt lives in the connector-owned fsr_soc_triage
            # package (carved out of fsr_playbooks; RECONCILIATION_PLAN Phase 1).
            from fsr_soc_triage import prompt_assembly
            if prompt_assembly.fragments_available("triage"):
                loaded = prompt_assembly.assemble_triage_prompt().strip()
                if loaded:
                    _PROMPT_CACHE[intent] = loaded
                    return loaded
            import fsr_soc_triage
            p = (Path(fsr_soc_triage.__file__).resolve().parent
                 / "system_prompt_triage.md")
        else:
            import fsr_playbooks
            p = (Path(fsr_playbooks.__file__).resolve().parent
                 / "agent" / "system_prompt_build.md")
        if p.is_file():
            loaded = p.read_text(encoding="utf-8").strip()
            if loaded:
                text = loaded
    except Exception:
        pass
    _PROMPT_CACHE[intent] = text
    return text


# ---------- one resolution spine: built-in intents as personas (P1) --------
#
# `triage` and `build` are the two BUILT-IN personas. A Key Store persona
# (Profile) layers OVER the chosen built-in: it overrides the tool slice
# (tools.allow) and the system prompt. `ResolvedPersona` is the single object a
# turn resolves to; `_resolve_turn_persona` is the single resolution point
# (intent hint + module + session → ResolvedPersona). This collapses the old
# scattered split -- `_resolve_intent`, the retired `_tools_for_intent` `!= "triage"`
# catch-all, and the retired `_tools_for_turn` persona check -- into one path, and removes
# the implicit default-to-build antipattern from tool slicing (the built-in is
# dispatched explicitly per intent; the only back-compat default lives in
# `_normalize_intent`). See docs/plans/PERSONA_SPINE_UNIFICATION_PLAN.md (P1).


# Retired tool name -> the advertised union that subsumes it. Used to translate
# persona allowlists authored against an older surface. This used to be a
# hand-kept copy "in lockstep with CONSOLIDATED_AWAY"; the framework now owns
# the one map (`retired_to_union`, which also carries the connector's own
# retired hunt helpers). The literal below is the fallback for an older wheel.
_RETIRED_TO_UNION_FALLBACK = {
    "find_connector": "find", "find_operation": "find",
    "find_jinja_filter": "find", "search_playbooks": "find",
    "find_containment_actions": "find", "find_enrichment_actions": "find",
    "find_record_actions": "find",
    "list_picklists": "picklist", "picklist_for_field": "picklist",
    "resolve_picklist_value": "picklist",
    "healthcheck_connector": "connector_health",
    "emit_choice_card": "emit_card", "emit_action_card": "emit_card",
    "emit_manual_input": "emit_card", "emit_capability_gap_card": "emit_card",
    "emit_playbook_offer": "emit_card", "emit_enhancement_offer": "emit_card",
}


def _retired_to_union() -> dict[str, str]:
    try:
        from fsr_playbooks.llm.tools import retired_to_union
    except ImportError:  # fsr-playbooks < the B3a release
        return dict(_RETIRED_TO_UNION_FALLBACK)
    return retired_to_union()
