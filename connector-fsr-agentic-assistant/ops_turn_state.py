"""turn_state domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

from typing import Any

from .ops_prompts import _read_only_quick_action




# Per-(session, module) persona cache. A Key Store persona is resolved once per
# session (not per turn) -- a `None` entry means "no persona, use built-in
# triage/build" and is cached too, so triage sessions don't re-query the Key
# Store every turn. Process-lifetime only; a persona edited mid-session is picked
# up on the next worker/process (acceptable for v1 -- personas change rarely).
# ONLY *definitive* resolutions are cached: a transient Key Store read failure
# yields None but is NOT cached, so a worker caught mid-recycle recovers the
# persona on a later turn instead of dropping it for its whole lifetime
# (plan §7 open item).
_PROFILE_CACHE: dict[tuple[str, str], Any] = {}

# Negative entries (`None` -- "no persona") carry a short TTL so a miss that
# slipped through as *definitive* (e.g. a cold worker whose Key Store read
# returned a stale/empty 200 right after a recycle) self-heals instead of
# pinning "no persona" for the worker's whole life. Positive entries (a resolved
# Profile) are trusted for the process lifetime as before. This is the belt to
# `resolve_profile_status`'s suspenders (which stops transient reads from being
# classified definitive in the first place).
_PROFILE_NEG_TTL_S = 60.0
_PROFILE_NEG_TS: dict[tuple[str, str], float] = {}
# (session, module) -> ts of the last TRANSIENT resolution failure (Key Store
# unreadable). Read straight after resolution by the same turn; the freshness
# window only guards against a stale marker from a crashed turn.
_PROFILE_DEGRADED_TS: dict[tuple[str, str], float] = {}
_PROFILE_DEGRADED_FRESH_S = 10.0


def _profile_resolution_degraded(module: Any, session_id: str) -> bool:
    """True when THIS turn's persona resolution failed transiently -- the
    module may have a persona we could not read. Phase 2 item 4: the caller
    must disclose this, not silently run the built-in default."""
    if not module or not isinstance(module, str):
        return False
    ts = _PROFILE_DEGRADED_TS.get((session_id or "", module))
    return ts is not None and (_profile_clock() - ts) < _PROFILE_DEGRADED_FRESH_S


def _profile_clock() -> float:
    import time
    return time.monotonic()


def _resolve_profile(module: Any, session_id: str):
    """Resolve the Key Store persona for the mounted ``module``, or ``None``.

    Fail-open (mirrors the case-state spine): no live client (dev/CLI), no
    matching Key Store record, or any read/parse error ⇒ ``None`` ⇒ the turn
    falls back to built-in triage/build behavior. A *definitive* result (persona
    found, or reads completed with no match) is cached per (session, module); a
    *transient* read failure is returned but NOT cached, so the next turn retries.
    See `fsr_soc_triage/profiles.py` (`resolve_profile_status`) and the
    module-scoped-personas plan."""
    if not module or not isinstance(module, str):
        return None
    ck = (session_id or "", module)
    if ck in _PROFILE_CACHE:
        cached = _PROFILE_CACHE[ck]
        if cached is not None:
            return cached  # positive resolution -- trusted for the process life
        # Negative entry: honor it only within the TTL, else drop it and
        # re-resolve below (self-heals a stale/spurious miss).
        if (_profile_clock() - _PROFILE_NEG_TS.get(ck, 0.0)) < _PROFILE_NEG_TTL_S:
            return None
        del _PROFILE_CACHE[ck]
        _PROFILE_NEG_TS.pop(ck, None)
    profile = None
    definitive = True
    try:
        from fsr_playbooks.mcp_server import _shared
        from fsr_soc_triage.profiles import resolve_profile_status
        client = _shared._live_client()
        if client is not None:
            profile, definitive = resolve_profile_status(client, module)
        else:
            # No live client (e.g. a cold resume worker) -- we simply couldn't
            # read the Key Store. Treat as transient, NOT a definitive "no
            # persona", so we don't poison the cache and refuse a later write.
            definitive = False
    except Exception:
        # An unexpected error in setup (not the Key Store read itself) -- treat as
        # transient and don't poison the cache.
        profile, definitive = None, False
    # Phase 2 item 4 (fail closed on load-bearing state): a TRANSIENT miss --
    # Key Store unreadable, no live client -- used to strip the persona
    # silently for the turn ("sometimes it just acts dumb"). Record it so the
    # turn can disclose the degradation instead of impersonating the built-in
    # default; a definitive resolution (found, or a real no-match) clears it.
    if not definitive and profile is None:
        _PROFILE_DEGRADED_TS[ck] = _profile_clock()
    else:
        _PROFILE_DEGRADED_TS.pop(ck, None)
    if profile is not None:
        # Fold the module's learned house rules ("skills") into the persona
        # prompt so every downstream path -- ungrounded, grounded, resume --
        # inherits them without a second lookup. Fail-open: no assistant_skills
        # module (404) or any read error leaves the prompt unchanged. Skills
        # shape HOW the generic tools are used; the tools themselves stay generic.
        profile = _apply_skills(profile, module)
    if definitive:
        _PROFILE_CACHE[ck] = profile
        if profile is None:
            _PROFILE_NEG_TS[ck] = _profile_clock()  # start the negative TTL
    return profile


def _apply_skills(profile: Any, module: str):
    """Return ``profile`` with the ``assistant_skills`` house rules for ``module``
    appended to its prompt. Unchanged on any failure or when there are none."""
    try:
        from fsr_playbooks.mcp_server import _shared
        from fsr_soc_triage.skills import skills_for_prompt
        client = _shared._live_client()
        block = skills_for_prompt(client, module)
        if block:
            return profile.model_copy(update={"prompt": profile.prompt + block})
    except Exception:  # noqa: BLE001 -- skills are best-effort; never break a turn.
        pass
    return profile


def _host_record_write_profile(module: Any):
    """A synthetic built-in profile permitting writes to ``module`` ONLY.

    `_write_gate` refuses every record write when no persona is active, so a
    plain SOC-triage session could not touch the record it was mounted on --
    "change the type of the alert" had no tool that could act (LIVE REPRO:
    session sess-v6uv6x15). Rather than open writes globally, scope them to the
    one module the analyst is actually looking at: the widget mounts over a
    record, and that record's module is the whole legitimate write surface for
    a triage turn.

    Defence in depth, unchanged by this: create_record/update_record are tier 3
    with confirm_mode="approve", so the write still returns a pending_approval
    card the analyst must accept before anything is sent. This grants *reach*,
    not autonomy.

    Returns None when there is no mounted module (a dashboard/off-record
    session stays read-only) or the profiles module isn't importable.
    """
    if not isinstance(module, str) or not module.strip():
        return None
    try:
        from fsr_soc_triage.profiles import Profile
    except Exception:  # noqa: BLE001 -- older layout: stay read-only
        return None
    try:
        # `module` and `prompt` are REQUIRED on Profile (extra="forbid"), so
        # both must be supplied even though neither is read on this path: the
        # built-in system prompt is chosen by intent, not from here, and this
        # object is bound at dispatch only -- never folded into ResolvedPersona
        # (which would swap BUILTIN_CAPABILITIES for the profile's own).
        # No `tools.allow`: the built-in intent slice still decides which tools
        # are advertised. This profile exists purely to carry may_write.
        return Profile(module=module.strip(), prompt="",
                       may_write=[module.strip()])
    except Exception:  # noqa: BLE001
        return None


def _set_active_profile(profile: Any):
    """Bind the active persona for this turn (contextvar in fsr_soc_triage.profiles),
    so the record-write tools see its may_write scope. Fail-open: returns None if
    the profiles module isn't importable (older layout) -- writes then refuse."""
    try:
        from fsr_soc_triage.profiles import set_active_profile
        return set_active_profile(profile)
    except Exception:
        return None


def _reset_active_profile(token: Any) -> None:
    """Undo _set_active_profile. No-op when binding failed (token is None)."""
    if token is None:
        return
    try:
        from fsr_soc_triage.profiles import reset_active_profile
        reset_active_profile(token)
    except Exception:
        pass


def _record_playbook_scope(storage: Any, params: dict) -> None:
    """Store the analyst's pick on the "Modify this playbook / Create new
    playbook" card. It is what authorizes removing steps they never named, or
    a second playbook with one open -- an answer, never a parse of wording.
    Best-effort: without it the gates stay closed, which is the safe side."""
    try:
        from fsr_playbooks.mcp_server._shared import (
            SCOPE_CARD_ID,
            SCOPE_CREATE_NEW,
            SCOPE_MODIFY,
        )
        if (params.get("decision") == "choice"
                and params.get("choice_id") == SCOPE_CARD_ID
                and params.get("session_id")):
            value = params.get("value")
            value = value[0] if isinstance(value, list) and value else value
            if value in (SCOPE_MODIFY, SCOPE_CREATE_NEW):
                storage.set_session_playbook_scope(params["session_id"], value)
    except Exception:  # noqa: BLE001
        pass


# --- the session's state for a turn and for every resume -------------------
#
# A turn binds what the host knows -- persona, mounted record, open playbook
# and where it came from, the modify-or-create answer,
# the read-only flag, the turn plan -- into the framework's SessionState.
# `chat_resume` carries no entity and no quick_action (contract 4), so it can
# re-derive none of it; it reads back what the turn stored. Each of those used
# to be its own ContextVar with its own `_set_resume_*` helper, and each was
# missed on some resume route at least once (#117, #118, #132, #148): the
# unbound default was the permissive value, so a resume reopened what the turn
# had closed.
#
# Now there is one pair. `_turn_session_state` derives the state from the
# turn's payload AND stores everything a resume reads; `_load_session_state`
# reads it back. Both return the same fields, and a test holds them equal.
# Bind either with `_bind_session_state`, which returns a token for
# `_unbind_session_state`.

#: The SessionState fields a resume restores. Everything else in SessionState
#: is re-derived by the resume itself (see the adjudication test).
RESUME_FIELDS = ("profile", "record_iri", "grounded_yaml", "grounded_source",
                 "playbook_scope", "read_only", "turn_plan", "created_records")


def _bind_session_state(fields: dict[str, Any]):
    """Bind `fields` into the framework's SessionState. Returns a token for
    `_unbind_session_state`.

    Fails CLOSED. These fields are the gates (read-only, the turn plan, the
    write persona), and an unbound gate reads as its
    permissive default -- so a bind that cannot happen must stop the turn,
    not run it ungated. A field this framework's SessionState does not have is
    dropped and logged rather than allowed to sink the others. Only a
    framework with no SessionState at all returns None: its gates do not read
    SessionState, so there is nothing to bind."""
    try:
        import dataclasses

        from fsr_playbooks.llm import session_state
    except ImportError:
        return None
    known = {f.name for f in dataclasses.fields(session_state.SessionState)}
    unknown = sorted(set(fields) - known)
    if unknown:
        from .ops_common import _log
        _log(f"session state: this framework has no field(s) {unknown}; "
             "not bound", "warning")
    return session_state.update(**{k: v for k, v in fields.items() if k in known})


def _bind_offered_tools(tools: Any):
    """Bind the names of the tools this turn offers the model
    (SessionState.offered_tools), so a framework tool that points the model at
    another tool names one it has. Returns a token for
    `_unbind_session_state`."""
    names = frozenset(str(t.get("name")) for t in tools or []
                      if isinstance(t, dict) and t.get("name"))
    return _bind_session_state({"offered_tools": names})


def _unbind_session_state(token: Any) -> None:
    if token is None:
        return
    try:
        from fsr_playbooks.llm import session_state
        session_state.reset(token)
    except Exception:  # noqa: BLE001
        pass


def _created_records_source(storage: Any, session_id: str | None):
    """SessionState.created_records for this session: a callable that reads
    storage on every call, so a record an approved create writes later in the
    same turn is seen by the run_playbook tier hook (#179). None without a
    session."""
    if not session_id:
        return None

    def _source() -> list:
        return [r["iri"] for r in storage.get_session_records(session_id)]
    return _source


def _resume_turn_plan(storage: Any, session_id: str | None):
    """The turn plan for a resume, derived from the same stored facts as the
    grounding (an open playbook is a stored session YAML) under the session's
    intent -- without the intent, a choice-card click mid-build ran run_op
    probes. Fails CLOSED: a resume is where approved tier-3 tools run, so an
    underivable plan becomes the failsafe plan, never no plan."""
    try:
        from fsr_playbooks.llm.turn_plan import TurnContext, plan_turn
        try:
            yaml_text = storage.get_session_playbook_yaml(session_id)
        except Exception:  # noqa: BLE001 -- absence of YAML is not an error
            yaml_text = None
        try:
            intent = storage.get_session_intent(session_id)
        except Exception:  # noqa: BLE001
            intent = None
        return plan_turn(intent, context=TurnContext(has_open_playbook=bool(yaml_text)))
    except Exception as e:  # noqa: BLE001
        return _failsafe_plan(f"resume: {type(e).__name__}")


def _failsafe_plan(reason: str):
    try:
        from fsr_playbooks.llm.turn_plan import TurnPlan
        return TurnPlan.failsafe(reason)
    except Exception:  # noqa: BLE001 -- no turn_plan module: no gate to arm
        return None


def _resume_profile(storage: Any, session_id: str | None):
    """The persona a resume runs under: the one the turn stored; else
    re-resolved from the mounted module; else the host-record write profile a
    plain triage mount gets (#148) -- scope identical to the turn that staged
    the card. A cold worker's Key Store read can fail, which is why the turn
    stores it."""
    profile = _load_resume_profile(storage, session_id) if session_id else None
    if profile is not None:
        return profile
    try:
        module = storage.get_session_module(session_id) if session_id else None
    except Exception:  # noqa: BLE001
        module = None
    if module:
        persona = _resolve_profile(module, session_id or "")
        if persona is not None:
            return persona
    # The session module is stored only when a persona was active, so a plain
    # triage mount has none: fall back to the mounted record's module, which
    # is what the turn scoped its host-record write profile to. Without this
    # the resume bound no profile and refused the write the analyst approved.
    try:
        row = storage.get_chat_session(session_id) if session_id else None
    except Exception:  # noqa: BLE001
        row = None
    return _host_record_write_profile(module or (row or {}).get("record_module"))


def _stored_record_iri(storage: Any, session_id: str | None) -> str | None:
    """The mounted record the turn stored. An approved run_playbook that
    omitted `record` refused `record_required` without it and spent the
    analyst's approval (matrix row SKL-MIC)."""
    if not session_id:
        return None
    try:
        from fsr_soc_triage.profiles import record_iri_from_entity
        row = storage.get_chat_session(session_id) or {}
        return record_iri_from_entity({"module": row.get("record_module"),
                                       "uuid": row.get("record_uuid")}) or None
    except Exception:  # noqa: BLE001
        return None


def _load_session_state(storage: Any, session_id: str | None) -> dict[str, Any]:
    """The state a resume binds, read back from what the turn stored. Absent
    rows read as the turn's own defaults (no playbook, not read-only), so a session that predates a column behaves as
    it did. Never raises."""
    def read(getter: str, default: Any = None) -> Any:
        if not session_id:
            return default
        try:
            value = getattr(storage, getter)(session_id)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    return {
        "profile": _resume_profile(storage, session_id),
        "record_iri": _stored_record_iri(storage, session_id),
        "grounded_yaml": read("get_session_playbook_yaml") or None,
        "grounded_source": read("get_session_grounded_source") or None,
        "playbook_scope": read("get_session_playbook_scope") or None,
        "read_only": bool(read("get_session_read_only", False)),
        "turn_plan": _resume_turn_plan(storage, session_id) if session_id else None,
        "created_records": _created_records_source(storage, session_id),
    }


def _turn_session_state(storage: Any, session_id: str, *, entity: Any,
                        quick_action: Any, has_open_playbook: bool,
                        read_only: bool, profile: Any, entity_module: Any,
                        plan: Any) -> dict[str, Any]:
    """The turn's state, from its payload, and stored for the resume.

    `read_only` is the scheduled task's `allow_mutating=False`; a read-only
    chip sets it too. Both hide the write tools AND make dispatch refuse them
    (the chip alone used to reach dispatch; a scheduled task's flag only hid
    the tools). The open playbook is stored even when there is none, so a
    resume never grounds on an earlier turn's playbook."""
    from fsr_soc_triage.profiles import record_iri_from_entity
    entity = entity if isinstance(entity, dict) else {}
    open_yaml = entity.get("playbook_yaml") or None
    source = entity.get("playbook_in_hand") or None
    ro = bool(read_only) or _read_only_quick_action(quick_action)
    for setter, value in (("set_session_playbook_yaml", open_yaml),
                          ("set_session_grounded_source", source),
                          ("set_session_read_only", ro)):
        try:
            getattr(storage, setter)(session_id, value)
        except Exception:  # noqa: BLE001 -- never fail a turn on this
            pass
    try:
        scope = storage.get_session_playbook_scope(session_id) or None
    except Exception:  # noqa: BLE001
        scope = None
    return {
        # No Key Store persona: a host-record-scoped write profile, so a plain
        # triage session can act (approval-carded) on the record it is
        # mounted on. A real persona always wins.
        "profile": profile if profile is not None
        else _host_record_write_profile(entity_module),
        "record_iri": record_iri_from_entity(entity) or None,
        "grounded_yaml": open_yaml,
        "grounded_source": source,
        "playbook_scope": scope,
        "read_only": ro,
        # A plan that could not be derived arms the failsafe, never no gate.
        "turn_plan": plan if plan is not None else _failsafe_plan("derivation failed"),
        "created_records": _created_records_source(storage, session_id),
    }


def _page_tool_ranking(module: Any, config: dict | None = None) -> tuple[str, ...]:
    """The page's preferred tools for the prompt prior, or `()`.

    Reads the LIVE registry for the same reason `_plan_live_turn` refuses to
    snapshot it: a cold worker registers MCP tools late, and a ranking taken
    before that would name a surface the turn does not have.

    The registry is worker-wide, though: MCP tools another config materialized
    stay in it. With `config`, an `mcp_*` tool is ranked only when its server is
    in this config's effective allowlist -- the same one `_advertised_tools`
    scopes by.

    Fail-open to silence -- an unrankable page states no preference, which is
    exactly what an unrestricted page states too."""
    try:
        from fsr_soc_triage.triage_sources import page_tool_ranking

        from fsr_playbooks.llm.tools import anthropic_tools
        tools = anthropic_tools()
        if config is not None:
            from fsr_playbooks.mcp_server.materializer import SERVER_MAP, _split_mcp_name

            from .ops_mcp import _parse_mcp_allowlist
            servers = set(_parse_mcp_allowlist(config) or {})

            def _in_reach(name: str) -> bool:
                if not name.startswith("mcp_"):
                    return True
                server = (SERVER_MAP.get(name) or _split_mcp_name(name))[0]
                return server in servers
            tools = [t for t in tools if _in_reach(str(t.get("name") or ""))]
        return page_tool_ranking(tools, module)
    except Exception:  # noqa: BLE001 -- a prose nicety, never fail a turn
        return ()


def _plan_live_turn(intent: str, *, has_trace: bool,
                    has_open_playbook: bool, module: Any = None,
                    config: dict | None = None):
    """Derive (but do not install) this LIVE turn's TurnPlan.

    The plan_turn choke point (Phase 2 full swap): chat_turn derives the plan
    ONCE, up front, and the same object then (a) supplies the base prompt via
    ``_load_prompt(plan=...)``, (b) supplies the prior/constraints suffix via
    ``_plan_constraints_suffix``, and (c) arms dispatch's gates when
    ``_run_turn`` installs it -- so prompt, suffix, and gates can never come
    from different derivations. Derivation and installation are split because
    the detached return path runs the turn in a raw thread with fresh
    contextvars: installing here, in the chat_turn body, would not reach it.

    The TOOL surface deliberately stays on ``_advertised_tools``: plan.tools
    is a snapshot of the same shared registry ``anthropic_tools()`` reads, but
    the connector's layers (MCP materialization, per-config scoping, per-page
    surfacing, persona allowlists, state drops) must apply to the LIVE
    registry, and a snapshot taken before a cold worker's registration import
    would resurrect the 20-vs-40-tool cold-worker bug.

    Fail-open: any import/derivation problem => None, zero behavior change."""
    try:
        from fsr_playbooks.llm.turn_plan import TurnContext, plan_turn
        page = f"the {module} module" if module else None
        return plan_turn(intent, context=TurnContext(
            page=page,
            has_open_playbook=bool(has_open_playbook),
            has_trace=bool(has_trace),
            # A1.3: the page's preference, stated rather than enforced. The
            # advertised array no longer varies per page (see
            # `surface_page_tools`), so this sentence is what carries the
            # steer -- and it rides the prompt, which is where volatility is
            # affordable.
            preferred_tools=_page_tool_ranking(module, config)))
    except Exception:  # noqa: BLE001 -- never break a turn over the plan
        # Returns None only as "there is no plan to install"; the CALLER
        # (`_install_turn_plan`) turns that into a failsafe plan rather than
        # into an unguarded turn. Kept split so a caller that only wants the
        # derived prompt can still tell "derivation failed" from "gated".
        return None


def _install_failsafe_turn_plan(reason: str):
    """Arm the dispatch gate when the real plan could not be derived.

    "Never break a turn over the gate" is right; "silently run the turn with
    NO gate" is not, and until now they were the same code path. The failsafe
    plan states no page facts -- it only holds the patch/enhance frontier
    closed, with a refusal that names a way forward.

    Itself fail-open, and that is not a contradiction: if `turn_plan` cannot
    even be imported, the dispatch site that consults it cannot import it
    either, so there is no gate to arm and nothing to protect.
    """
    try:
        from fsr_playbooks.llm.turn_plan import TurnPlan, set_turn_plan
        return set_turn_plan(TurnPlan.failsafe(reason))
    except Exception:  # noqa: BLE001
        return None


def _install_turn_plan(plan: Any, *, required: bool = True):
    """Install an already-derived TurnPlan for the current context (arms the
    dispatch gates). Returns a reset token (None if unbound).
    Must run in the thread that runs the agent loop -- see _plan_live_turn.

    `plan is None` means derivation FAILED (see `_plan_live_turn`), so the
    default is to install the failsafe plan rather than leave the turn
    unguarded. Pass `required=False` on a path that legitimately has no plan
    to install -- that is the framework's own fail-open shape and must stay
    reachable."""
    if plan is None:
        return (_install_failsafe_turn_plan("derivation failed")
                if required else None)
    try:
        from fsr_playbooks.llm.turn_plan import set_turn_plan
        return set_turn_plan(plan)
    except Exception:  # noqa: BLE001 -- never break a turn over the gate
        return None


def _plan_constraints_suffix() -> str:
    """The ACTIVE TurnPlan's prompt suffix for the live prompt: the
    context-prior/disposition block + the stated-constraints block.

    Phase 2 goal 3 landed the constraints (budget, approval-tier contract,
    never-dead-end). Phase 3 adds the context prior: with the C5 build/triage
    wall down in ``_afforded_drop_set``, its "the full tool surface is
    available regardless" statement is now TRUE, and its disposition
    paragraph is what keeps the turn focused now that tool absence doesn't.
    The two changes must travel together -- stating the prior against a
    walled slice would lie, and dropping the wall without the disposition
    loses focus.

    Fail-open: no plan installed / old wheel => empty string; a wheel
    without _context_prior still states the constraints."""
    try:
        from fsr_playbooks.llm import turn_plan as _tp
        plan = _tp.active_turn_plan()
        if plan is None:
            return ""
        prior = getattr(_tp, "_context_prior", None)
        prefix = prior(plan.intent, plan.context) if callable(prior) else ""
        return prefix + _tp._constraints(plan.budget)
    except Exception:  # noqa: BLE001 -- prose nicety, never fail a turn
        return ""


def _store_turn_profile(storage: Any, session_id: str, profile: Any) -> None:
    """Persist the turn's resolved persona (as Profile JSON) for chat_resume.

    A tier-3 write is approved during the turn but EXECUTED on resume, in a
    separate -- often cold -- worker. Re-resolving the persona there via the Key
    Store can fail (no live client ⇒ persona None ⇒ the may_write gate wrongly
    refuses the already-approved write). Persisting the resolved persona lets
    resume re-bind the SAME scope deterministically. No-op when no persona is
    active (built-in triage/build) -- resume then keeps its existing fallback.
    Best-effort: a storage error never fails the turn."""
    if profile is None:
        return
    try:
        dump = getattr(profile, "model_dump_json", None)
        payload = dump() if callable(dump) else None
        if payload:
            storage.set_session_profile(session_id, payload)
    except Exception:
        pass


def _load_resume_profile(storage: Any, session_id: str):
    """Reload the persona persisted by :func:`_store_turn_profile`, or None.

    Never raises -- a missing row, malformed JSON, or an older layout without
    the profiles module all degrade to None, and the caller then falls back to
    re-resolving from the mounted module."""
    try:
        raw = storage.get_session_profile(session_id)
    except Exception:
        return None
    if not raw:
        return None
    try:
        from fsr_soc_triage.profiles import Profile
        return Profile.model_validate_json(raw)
    except Exception:
        return None
