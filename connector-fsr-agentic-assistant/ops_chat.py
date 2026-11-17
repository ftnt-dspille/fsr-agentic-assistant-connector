"""chat domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any
from connectors.core.connector import ConnectorError
from .pydantic_models import (
    ChatTurnParams, ChatTurnResponse, ChatPollParams, ChatPollResponse,
    ChatCancelParams, ChatCancelResponse,
    ChatResumeParams, ChatHistoryParams, ChatHistoryResponse,
    ListSessionsParams,
    GetSessionTraceParams, RateSessionParams, ListActiveSessionsParams,
)
from .pydantic_models import (
    ActivityEvent as ActivityFrame,
)
from .history_replay import text_of, transcript_blocks
from .turn_frames import write_stream_end, write_terminal_error
from .turn_sink import make_event_sink

from .ops_approvals import (
    _DECIDER, _ONCE_DECISIONS, _already_decided, _approver_label, _claim_decision, _decision_gate,
)
from .ops_common import (
    CONTRACT_VERSION, _check_params, _check_turn_result, _err, _frame,
    _import_fsr_playbooks, _log, _reference_db_path, _require, _safe_turn_field,
    _validate_model,
)
from .ops_config import _build_provider, _config_for_persona, _provider_name
from .ops_entity import (
    _CONNECTOR_INVARIANT_RULES, _inject_entity_context, _is_blind_followup,
    _maybe_inject_failure_diagnosis, _wire_messages,
)
from .ops_health import _check_health
from .ops_mcp import _apply_mcp_allowlist
from .ops_offers import _salvage_build_offer
from .ops_personas import ResolvedPersona
from .ops_playbooks import compile_yaml
from .ops_policy import _bind_autonomy_policy, _bind_autonomy_subject
from .ops_prompts import (
    _QUICK_ACTION_DIRECTIVE, _legacy_intent_text, _load_prompt, _render_prompt_for_tools,
    _resolve_intent, _session_trace_scope,
)
from .ops_redaction import redaction_mode, redaction_scope
from .ops_resume import _chat_resume_routes
from .ops_scope import (
    _bind_ask_scope, _close_loop, _reset_ask_scope, _resolve_ask_scope, _scope_directive,
)
from .ops_tools import (
    _advertised_tools, _append_record_source_gaps, _authoring_active,
    _entity_has_open_playbook, _resolve_turn_persona, _session_has_trace,
    _source_availability_note, _with_playbook_in_hand,
)
from .ops_turn_io import (
    _STREAM_STALE_SECS, _case_state_kwargs, _case_state_mod, _case_state_summary_tag,
    _envelope_stop_reason, _persist_session_state, _wire_transcript, _with_guard_fires,
)
from .ops_turn_state import (
    _bind_offered_tools, _bind_session_state, _load_session_state, _turn_session_state,
    _unbind_session_state,
    _load_resume_profile, _plan_constraints_suffix, _plan_live_turn,
    _profile_resolution_degraded, _record_playbook_scope, _store_turn_profile,
)
from .ops_warmup import (_claim_gated_warm, _release_gated_warm,
                         _warmup_instance_mismatch, _warmup_needed, warmup)



def _triage_lookup_key(entity: Any) -> str | None:
    """The record locator (iri or module:uuid) from the widget entity, for
    the triage pre-flight fetch. None when the entity can't be resolved."""
    if not isinstance(entity, dict):
        return None
    iri = entity.get("iri")
    if isinstance(iri, str) and iri.strip():
        return iri.strip()
    module = entity.get("module")
    uuid_ = entity.get("uuid") or entity.get("id")
    if module and uuid_:
        return f"{module}:{uuid_}"
    return None


def _resolve_system_prompt(intent: str, entity: Any, storage, session_id: str,
                           turn_idx: int, user_message: str | None = None,
                           quick_action: str | None = None,
                           profile: Any = None,
                           plan: Any = None) -> tuple[str, Any]:
    """Resolve the system prompt for a turn, returning (prompt, case_state).

    The grounding/preflight/CaseState spine gates on the resolved persona's
    ``capabilities.grounding`` (P3), NOT on the literal intent -- True for the
    built-in triage persona and any Key Store persona that opts in, False for
    build and prompt-only personas.

    When grounding is off, returns the ungrounded base prompt (a persona's own
    prompt, else the built-in intent prompt + optional build quick-action) and
    no CaseState. When grounding is on and the record resolves: load cached
    CaseState -- if grounding is present for this record, skip preflight and use
    the cached system_prompt + continuation directive; otherwise run the dynamic
    pre-flight (normalize → classify → scenario prompt) and store the bundle. A
    grounding-capable persona grounds on its OWN prompt (preflight's triage
    scenario prompt never overrides it) but still runs preflight for indicators.

    Each pre-flight phase is mirrored into the streaming feed as an `activity`
    frame so the widget can show the analyst what the backend did before the
    model spoke.

    Falls back to the base prompt when there's no entity or if pre-flight raises
    (grounding is best-effort -- a fetch failure must never break the turn; a
    grounding persona still establishes grounding on its own prompt so
    continuation carries). Returns the system prompt and the CaseState instance
    (possibly mutated with new grounding)."""
    import time

    cs = _case_state_mod()
    case_state = None

    # The grounding/preflight/CaseState path gates on a persona-declared
    # CAPABILITY, not on the literal intent string. `capabilities.grounding` is
    # True for the built-in triage persona and for any Key Store persona that
    # opts in; False for build and for a prompt-only persona.
    persona = ResolvedPersona(intent=intent, profile=profile)
    caps = persona.capabilities
    # `base_prompt` is the prompt used when the turn is ungrounded or grounding
    # falls back: a persona's own prompt wins, else the built-in intent prompt
    # (served from the turn's resolved plan when one was derived -- the
    # plan_turn choke point).
    base_prompt = persona.prompt_override or _load_prompt(intent, plan=plan)

    if not caps.grounding:
        # Ungrounded turn -- no preflight, no CaseState. Covers (a) a Key Store
        # persona whose prompt is authoritative (guides authoring, not incident
        # hunting), and (b) the built-in `build` intent.
        prompt = base_prompt
        # Route a build quick-action chip to its mode. Only the build
        # intent carries quick-action chips; a persona ignores them. The build
        # prompt's "Quick-action modes" section documents each chip's tools;
        # this directive names the active one. See _QUICK_ACTION_DIRECTIVE.
        if intent == "build" and quick_action and persona.prompt_override is None:
            prompt = prompt + _QUICK_ACTION_DIRECTIVE.format(key=quick_action)
        return prompt, case_state

    # Grounding-capable turn (built-in triage, or a persona that opted in).
    key = _triage_lookup_key(entity)
    if not key:
        return base_prompt, case_state

    # Load existing case state (fail-open; None when the installed
    # fsr_playbooks has no case-state support).
    if cs is not None:
        try:
            stored_state = storage.get_case_state(session_id)
            case_state = cs.CaseState.from_dict(stored_state)
        except Exception:
            case_state = cs.CaseState()

    # Check if we have valid cached grounding for this record.
    if (case_state is not None
            and case_state.grounding is not None
            and case_state.record_key == key):
        # Use cached grounding + continuation directive (skip preflight).
        investigation_progress = (
            f"{case_state.investigation.invest_attempts} investigation steps, "
            f"hunt floor {'met' if case_state.investigation.hunt_floor_met else 'not met'}"
        )
        # Capability facts are unknown until populated; saying "none" would imply
        # a check that never ran -- omit the clause when the map is empty.
        unavailable_note = (
            f", unavailable connectors: "
            f"{', '.join(case_state.capabilities.unavailable.keys())}"
            if case_state.capabilities.unavailable else ""
        )
        continuation = (
            f"\n\nThis is an ongoing case you have already triaged -- continue it; "
            f"do not restart the hunt or re-run tools you have already run. "
            f"Prior progress: {investigation_progress}{unavailable_note}."
        )
        prompt = case_state.grounding.system_prompt + continuation if case_state.grounding.system_prompt else base_prompt
        return prompt, case_state

    # Grounding absent or record_key changed → reset case state and run preflight.
    if case_state is not None:
        case_state = case_state.for_record(key)

    def _emit(ev: dict) -> None:
        try:
            storage.append_turn_progress(session_id, turn_idx, _frame(
                ActivityFrame, type="activity", phase=ev.get("phase"),
                message=ev.get("message")))
        except Exception:
            pass

    # A grounding-capable persona grounds on its OWN prompt -- preflight's
    # triage scenario prompt is content-specific to alert hunting and must not
    # override an authoring/runbook persona. `grounded_system` is None for the
    # built-in triage turn (it adopts preflight's scenario prompt unchanged);
    # non-None for a persona (its prompt is authoritative,
    # preflight still runs to populate CaseState indicators).
    grounded_system = persona.prompt_override
    try:
        from fsr_soc_triage.triage_preflight import triage_preflight
        bundle = triage_preflight(target=key, user_message=user_message,
                                  emit=_emit)
        if grounded_system is None:
            grounded_system = bundle["system"]
        # Store the grounded prompt + preflight indicators into grounding.
        if cs is not None and case_state is not None:
            case_state.grounding = cs.Grounding(
                scenario_id=bundle.get("scenario") or bundle.get("scenario_id"),
                system_prompt=grounded_system,
                indicators=bundle.get("indicators"),
                computed_at=time.time(),
            )
        return grounded_system, case_state
    except Exception:
        # Preflight failed. A persona grounds on its own prompt, so still
        # establish grounding (continuation must carry across turns even when
        # the record fetch/classify fails). A built-in triage turn has no
        # persona prompt to fall back on → static prompt, no grounding.
        if grounded_system is not None and cs is not None and case_state is not None:
            case_state.grounding = cs.Grounding(
                system_prompt=grounded_system, computed_at=time.time())
            return grounded_system, case_state
        return base_prompt, case_state


def _with_stored_outcomes(prior: list, session_id: str | None) -> list:
    """`prior` with the stored resume outcomes (a playbook saved, an approved
    action run) that the widget's copy of the conversation lacks. See
    history_replay.with_resume_outcomes. Fail-open: no stored conversation, or a
    storage error, leaves the widget's history as sent."""
    if not session_id or not prior:
        return prior
    try:
        from .storage import default_storage
        from .history_replay import with_resume_outcomes
        return with_resume_outcomes(prior, default_storage().load_conversation(session_id))
    except Exception:  # noqa: BLE001 -- history is a nicety, the turn is not
        return prior


def _canonical_messages(prior: list | None, legacy_text: str | None,
                        wire_transcript: list[dict]) -> list[dict]:
    """The conversation persisted after a turn: prior messages (+ this turn's
    user text when carried in the legacy `intent` field) + the assistant's
    response as a block list (history_replay.transcript_blocks), so a resume
    replays its calls natively.

    Under the new contract the user text is the last entry in `prior`, so
    `legacy_text` is None and nothing extra is appended."""
    msgs: list[dict] = []
    for m in prior or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and content is not None:
            if role == "user" and not isinstance(content, str):
                content = text_of(content)
            entry = {"role": role, "content": content}
            if m.get("outcome"):
                # A resume outcome merged in from storage keeps its marker, so
                # the next turn still finds it there.
                entry["outcome"] = True
            msgs.append(entry)
    if legacy_text is not None:
        msgs.append({"role": "user", "content": legacy_text})
    msgs.append({"role": "assistant",
                 "content": transcript_blocks(wire_transcript)})
    return msgs


def chat_turn(config: dict, params: dict) -> dict:
    """Run one LLM-driven authoring turn. Returns the full transcript
    on completion. The widget should treat any ApprovalRequestEvent in
    the transcript as a HITL gate and call chat_resume with the
    approval_id + the user's decision to continue."""
    _validate_model(ChatTurnParams, params or {})
    _check_health(config)
    session_id = _require(params, "session_id")
    # `intent` is a task discriminator (triage|build);
    # a non-discriminator string is the legacy user-message shape.
    intent = _resolve_intent(params)
    legacy_text = _legacy_intent_text(params)
    # The build quick-action chip key (None on free-typed turns and
    # triage chips). Threaded onto the build system prompt + the turn tags.
    quick_action = params.get("quick_action")
    # Scheduled tasks with allow_mutating=False pass read_only=True to enforce
    # that headless runs cannot auto-approve writes (tier 3+ tools).
    read_only = bool(params.get("read_only", False))
    prior = params.get("messages") or []
    prior = _with_stored_outcomes(prior, session_id)

    wire_msgs = _wire_messages(prior, legacy_text)
    if not wire_msgs:
        raise ConnectorError(
            "no user message: pass the conversation in messages[] (or the "
            "user's text in intent for the legacy shape)")

    # Render-fidelity history: record the analyst's turn once, here -- BEFORE
    # _inject_entity_context folds the record block into the first user message
    # (so turn 1's stored text stays clean) and regardless of which return path
    # (blocking / detached) produces the assistant transcript. The assistant
    # side is appended in _persist_session_state. Empty (opener) text is a
    # no-op. Best-effort -- a history-write failure must never fail the turn.
    from .history_replay import is_analyst_turn
    _clean_latest_user = next((m.content for m in reversed(wire_msgs)
                               if is_analyst_turn(m)), "")
    # The acting SOAR user (widget-supplied; the connector runs under the
    # service account so it cannot derive who is chatting). Persisted per user
    # turn as the message author, and once per session as the initiator, so a
    # session opened from history attributes to its real author/owner rather
    # than the current viewer. Best-effort: a bad/missing actor never fails.
    _actor = params.get("actor") if isinstance(params.get("actor"), dict) else None
    # A system actor (auto-triage, a scheduled hunt): nobody is there to answer.
    _unattended_turn = bool(_actor and _actor.get("system"))
    try:
        from .storage import default_storage as _default_storage
        _default_storage().append_transcript_user(
            session_id, _clean_latest_user, author=_actor)
        if _actor:
            _default_storage().set_session_initiator(session_id, _actor)
    except Exception:
        pass
    # Ground the turn in the widget-supplied record. The
    # entity's cleaned top-level `fields` are authoritative and always
    # injected, so the model answers IOC/timeline/blast-radius quick
    # actions directly instead of asking the analyst to paste fields.
    #
    # We fold the record-context block into the FIRST user message rather
    # than the system prompt on purpose: the provider caches the
    # (tools + system) prefix with cache_control, and that prefix is large
    # and static. Keeping the per-record block out of `system` lets that
    # cached prefix be reused across records; the small entity block rides
    # in messages[] (never cache-marked) instead of busting the cache.
    try:
        _created = _default_storage().get_session_records(session_id) if session_id else []
    except Exception:  # noqa: BLE001
        _created = []
    # The playbook this conversation built rides in as the open one on a build
    # turn with nothing open. Everything below reads params["entity"], so
    # substituting it here grounds the turn, the plan and the tool slice alike.
    _ent_in = params.get("entity")
    _ent_eff = _with_playbook_in_hand(_ent_in, intent, _default_storage(),
                                      session_id)
    if _ent_eff is not _ent_in:
        params = {**params, "entity": _ent_eff}
    _inject_entity_context(wire_msgs, params.get("entity"), _created)
    # NOTE: the deterministic diagnose-before-fix lever runs LATER -- after
    # turn_idx is reserved -- so it can stream a visible `activity` frame onto
    # this turn's feed (transparency). See _maybe_inject_failure_diagnosis below.
    # The module of the triaged record (alerts/incidents/…), if the widget
    # supplied an entity. Stamped onto the session trace below so a
    # trace-built playbook binds its start trigger to this module (manual
    # Execute-menu run) instead of a designer-only Referenced trigger.
    _entity = params.get("entity")
    entity_module = _entity.get("module") if isinstance(_entity, dict) else None
    # On-ask reach: server keys the widget asked to union into this page's
    # default MCP surface for this turn (a normal list of strings; anything else
    # is ignored -- never fail a turn over it).
    _rs = params.get("reach_servers")
    _reach_servers = [s for s in _rs if isinstance(s, str)] if isinstance(_rs, list) else None
    # Resolve the turn to a single ResolvedPersona: the built-in base
    # intent + the module's Key Store persona (if any), resolved once and threaded
    # into both seams below (prompt + tool slice) so we don't double-query. No
    # persona record ⇒ profile is None ⇒ unchanged built-in triage/build behavior.
    persona = _resolve_turn_persona(intent, entity_module, session_id)
    profile = persona.profile
    # Fail CLOSED on load-bearing state: when the module's
    # persona could not be READ this turn (Key Store hiccup, cold worker), the
    # turn must not impersonate the built-in default silently. The model is
    # told to disclose the degradation, and asks that depend on the persona's
    # capabilities become a named gap instead of a silent write refusal.
    _persona_degraded = (profile is None and
                         _profile_resolution_degraded(entity_module, session_id))
    # Lazy warm backstop: the lifecycle hooks warm the catalog at install /
    # config-save; a turn only warms it when the gate still says it is cold
    # (connectors/ops/op_safety empty, module fields unwarmed, or the instance
    # changed). Same single-flight + backoff as the hooks, so a warm already
    # running in the background is not repeated and a failing one is not retried
    # on every turn. crudhub-absent (dev) is non-fatal -- the turn proceeds
    # against what the catalog has.
    try:
        _db = _reference_db_path()
        if _warmup_needed(_db) or _warmup_instance_mismatch(_db):
            _blocked = _claim_gated_warm()
            if _blocked is not None:
                _log(f"warmup skipped (chat turn): {_blocked}")
            else:
                try:
                    warm_res = warmup(config, {})
                finally:
                    _release_gated_warm()
                if not warm_res.get("ok"):
                    # crudhub_unavailable / fetch errors land here; log but
                    # don't fail the turn.
                    pass
    except Exception:
        pass

    _apply_mcp_allowlist(config)

    from fsr_playbooks.llm.run_turn import run_agent_turn
    from .storage import default_storage, PersistedApprovalGateway

    storage = default_storage()
    gateway = PersistedApprovalGateway(storage)
    _persona_config = _config_for_persona(config, profile)
    provider = _build_provider(_persona_config, gateway)
    _audit_provider = _provider_name(provider, _persona_config)

    # Create-method strategy (the widget toggle): resolve `auto` by the model
    # this turn actually runs, now that the persona's model is swapped in. An
    # explicit guided/freeform passes through unchanged; an unknown/on-box
    # (fortiai-proxy) model resolves to guided -- valid-by-construction is the
    # floor. Only consulted on the build intent, inside _advertised_tools.
    try:
        from fsr_playbooks.llm import intents as _intents
        from .ops_config import _MODEL_FIELD, _selected_provider
        _prov_name = _selected_provider(_persona_config)
        _model_id = (_persona_config or {}).get(
            _MODEL_FIELD.get(_prov_name, "model"))
        _create_strategy = _intents.resolve_create_strategy(
            params.get("create_strategy"), _model_id, _prov_name)
    except Exception:  # noqa: BLE001 - older framework / no create-strategy
        _create_strategy = None

    # Each chat_turn call counts as one turn within the session. Derive the
    # next turn number from BOTH chat_turns and the turn_progress feed (see
    # Storage.reserve_next_turn): chat_turns is only written on a UsageEvent, so
    # a turn that fails before the provider emits usage (LLM gateway down, etc.)
    # would otherwise leave the counter unchanged and the NEXT turn would reuse
    # the same turn_idx -- which the widget's monotonic `since_turn` poll fence
    # then excludes forever (turn=None, no live messages). turn_progress always
    # gets a turn_start row, so this increments unconditionally.
    #
    # A blocking chat_turn is also a streaming producer: the
    # `turn_start` frame is written up front so a concurrent chat_poll latches
    # this turn immediately, then every emitted event is appended as a raw wire
    # frame for the poll feed. reserve_next_turn derives the turn AND writes
    # that turn_start in ONE atomic write transaction, so two concurrent turns
    # (including separate worker processes) can't mint a duplicate turn_idx.
    turn_idx = storage.reserve_next_turn(session_id)
    # Status from turn start: one activity frame right behind turn_start, so the
    # analyst sees the turn is working before preflight (or the first token)
    # writes anything. Progress feed only; it never enters the saved transcript.
    try:
        storage.append_turn_progress(session_id, turn_idx, _frame(
            ActivityFrame, type="activity", phase="thinking", message="Thinking"))
    except Exception:
        pass

    # Blind-follow-up detector. The model's context is `messages[]` and
    # nothing else -- the durable session is written for the history browser
    # and the poll feed, and is never read back as LLM context. So a caller
    # that ships only the new user message on turn N>1 runs the agent with no
    # memory of what it was asked, and the visible symptom is the agent
    # re-asking for an indicator it was already given. That is indistinguish-
    # able from a steering bug unless the assembly gap is named, so name it:
    # this is a caller contract violation, logged and tagged, never fatal (a
    # deliberately fresh conversation on a reused session id is legal).
    _blind_followup = _is_blind_followup(turn_idx, wire_msgs)
    if _blind_followup:
        _log(f"session={session_id} turn={turn_idx}: messages[] carries no "
             f"assistant history on a follow-up turn -- the agent will run "
             f"with no memory of prior turns. The caller must replay the "
             f"conversation (the widget's _serializeMessagesForServer does).",
             "warn")

    # Remember the session's intent so a later chat_resume (which carries
    # no intent) re-enters with the same prompt + tool slice.
    try:
        storage.set_session_intent(session_id, intent)
    except Exception:
        pass

    # Remember the mounted module too, so a later chat_resume (no entity) can
    # re-resolve the same persona and keep its prompt + tool slice. Only when a
    # persona is actually active -- otherwise resume stays on built-in behavior.
    if profile is not None and entity_module:
        try:
            storage.set_session_module(session_id, entity_module)
        except Exception:
            pass
    # Persist the resolved persona itself so an approved tier-3 write executed on
    # resume re-binds the SAME may_write scope deterministically -- without a Key
    # Store re-read that a cold resume worker can fail (→ persona None → the
    # approved write refused with write_not_permitted).
    _store_turn_profile(storage, session_id, profile)

    # Live Sessions panel: remember this session's mounted
    # record + latest prompt so the dashboard can show what's running right
    # now. Best-effort, like the sibling session writes above; the live status
    # (active/waiting_approval/idle) is derived at read time, not stored here.
    try:
        _rec_uuid = None
        if isinstance(_entity, dict):
            _rec_uuid = _entity.get("uuid") or _entity.get("id")
        storage.upsert_chat_session(session_id, record_uuid=_rec_uuid,
                                    record_module=entity_module,
                                    current_prompt=_clean_latest_user or None)
    except Exception:
        pass

    # C5: the prompt is resolved in _prepare(), which a detached turn runs on its
    # own thread AFTER the accepted-ack returns. A triage record's preflight
    # classify (a record read and an indicator lookup) therefore no longer
    # holds up the ack. A blocking turn runs it inline, before the turn starts.
    system_prompt = None
    case_state = None
    _turn_plan = None
    _ht = _session_has_trace(storage, session_id)
    _hop = _entity_has_open_playbook(_entity)

    def _prepare():
        nonlocal system_prompt, case_state, _turn_plan
        # Deterministic diagnose-before-fix (build): if the analyst reports a runtime
        # failure on the OPEN playbook, the connector diagnoses the failed run itself
        # and folds the cause into the turn -- so even a model that won't call
        # why_did_playbook_fail fixes the real defect instead of guessing, and we
        # don't spend context making the model reach the tool. Runs
        # here (after turn_start) so its `activity` frames latch onto this turn's feed
        # and render before the model's first token -- the analyst SEES the connector
        # consult the failed run; the grounding is transparent, not hidden.
        def _diag_activity(ev: dict) -> None:
            try:
                storage.append_turn_progress(session_id, turn_idx, _frame(
                    ActivityFrame, type="activity", phase=ev.get("phase"),
                    message=ev.get("message")))
            except Exception:
                pass

        # analyst_text is the un-folded message captured above (before the OPEN
        # PLAYBOOK was folded in), so the lever reads only what the analyst wrote.
        _diagnosed_step = _maybe_inject_failure_diagnosis(
            wire_msgs, params.get("entity"), intent, emit=_diag_activity,
            analyst_text=_clean_latest_user)
        if _diagnosed_step:
            _log(f"[fsrpb] failure-diagnosis lever injected (failing step "
                 f"{_diagnosed_step!r}) for session {session_id}")

        # Resolve this turn's TurnPlan ONCE, before prompt resolution -- the same
        # object supplies the base prompt (below), the prior/constraints suffix,
        # and the dispatch gates (installed inside _run_turn, which may
        # run on a raw thread -- see _plan_live_turn). _ht/_hop are computed here
        # and reused by _run_turn so the plan's context and the advertised slice
        # key on the SAME state reads.
        _turn_plan = _plan_live_turn(intent, has_trace=_ht, has_open_playbook=_hop,
                                     module=entity_module, config=config)

        # Triage pre-flight: ground the system prompt in the record (normalize →
        # classify → scenario) and stream `activity` frames for each phase. Done
        # after turn_start so the activity frames latch onto this turn's feed and
        # render before the model's first token. Also returns the CaseState instance
        # (possibly with new grounding) for passing to the agent and persisting.
        system_prompt, case_state = _resolve_system_prompt(
            intent, params.get("entity"), storage, session_id, turn_idx,
            user_message=_clean_latest_user, quick_action=quick_action,
            profile=profile, plan=_turn_plan)

        # Connector-wide INVARIANT house rules. Appended to the final resolved
        # prompt for EVERY turn -- built-in triage/build AND every Key Store persona
        # (which fully override the base prompt and would otherwise miss this). This
        # is the shared-layer fix for the confabulation defect: the model must not
        # present example/sample data or unstated assumptions as real results, and
        # must tool-verify any factual claim about live state. Kept short so it
        # can't drown a persona's own voice; deliberately NOT gated on profile.
        system_prompt = system_prompt + _CONNECTOR_INVARIANT_RULES

        # The degraded-persona disclosure (see _persona_degraded
        # above). Appended after the invariants so no persona override can drop it.
        if _persona_degraded:
            system_prompt = system_prompt + (
                "\n\n## Degraded turn -- persona unavailable\n"
                "This module's configured assistant persona could not be loaded "
                "for this turn (a transient Key Store read failure), so you are "
                "running with reduced, default capabilities. Tell the analyst "
                "this in your first sentence. If their request depends on the "
                "persona's capabilities (allowed tools, house rules, write "
                "permissions), do NOT guess or silently refuse: name what is "
                "unavailable and suggest retrying in a minute, using "
                "emit_card(card_type='capability_gap', ...) when the request "
                "cannot proceed at all."
            )
            _diag_activity({"phase": "persona",
                            "message": "Persona unavailable this turn (Key Store "
                                       "read failed) -- running with default "
                                       "capabilities."})

        # Turn-MODE directive (P2b, T2 fix): for a built-in triage turn, re-weight
        # the hunt-vs-contain emphasis from the analyst's ask. Appended to the final
        # resolved prompt so it covers BOTH the grounded (preflight) and ungrounded
        # paths uniformly. A persona turn owns its own prompt -- skip it. An ambiguous
        # ask classifies to None and appends nothing (unchanged behavior).
        if intent == "triage" and profile is None:
            try:
                from fsr_soc_triage import prompt_assembly as _pa
                _mode = _pa.classify_turn_mode(_clean_latest_user)
                if _mode:
                    system_prompt = system_prompt + _pa.mode_directive(_mode)
            except Exception:
                pass

        # Off a SOC record, suppress the UNPROMPTED "save this as a playbook" offer --
        # on a device / template / custom-module page it is noise (the analyst asked
        # to list steps, not to author automation). An explicit "build me a playbook"
        # still works everywhere; see prompt_assembly.proactive_offer_directive.
        # Applied for BOTH built-in triage and persona turns: a persona overrides the
        # base prompt but inherits the same offer tools, so gating only the built-in
        # path would leave exactly the module pages this is meant to quiet.
        if intent == "triage":
            try:
                from fsr_soc_triage import prompt_assembly as _pa
                system_prompt = system_prompt + _pa.proactive_offer_directive(
                    entity_module)
            except Exception:
                pass

    # Hook UsageEvents so each LLM round-trip writes a chat_turns row
    # keyed on the connector-side turn_idx (NOT ev.turn, which is the
    # provider's per-stream counter and resets every chat_turn call).
    # The sink for this turn is turn_sink.make_event_sink: one body shared with
    # the resume paths, so the audit row cannot drift between them.
    _on_event = make_event_sink(
        storage, config, session_id, turn_idx,
        provider_name=_audit_provider, audit_intent=intent,
        audit_prompt=_clean_latest_user or "",
        actor_resolver=lambda: _actor)

    # A stable turn id shared between the (detached) accepted-ack and the
    # terminal stream_end frame, so an approval-gated turn's resume HMAC
    # binding references the same id whether the widget reads the blocking
    # return or the poll feed.
    turn_id = uuid.uuid4().hex

    def _run_turn():
        # Redaction covers the scope declaration and the turn alike; off binds nothing.
        with redaction_scope(storage, config, session_id, _entity):
            return _run_turn_unredacted()

    def _run_turn_unredacted():
        with _session_trace_scope(storage, session_id):
            if entity_module:
                try:
                    from fsr_playbooks.agent import skill_trace
                    skill_trace.set_active_trace_module(entity_module)
                    # Stamp the triaged record's fields too, so the trace-build
                    # path can parameterize a one-off IOC to
                    # vars.input.records[0].<field> instead of baking the literal.
                    if isinstance(_entity, dict):
                        skill_trace.set_active_trace_record_fields(
                            _entity.get("fields"))
                except Exception:
                    pass
            _turn_tags = {"session_id": session_id, "intent": intent,
                          "intent_text": (_clean_latest_user or "")[:80]}
            if quick_action:  # Track C1: observability -- which chip fired.
                _turn_tags["quick_action"] = quick_action
            if redaction_mode(config) != "off":
                _turn_tags["redaction"] = redaction_mode(config)
            if not persona.concludes_with_verdict(entity_module):
                _turn_tags["verdicts"] = False
            if _blind_followup:  # A follow-up turn with no history.
                _turn_tags["blind_followup"] = True
            if _unattended_turn:
                # Nobody is watching (auto-triage): a true_positive that stages
                # nothing is asked once, in-turn, for its containment. Only
                # asks -- what runs is still decided by approval or policy.
                _turn_tags["unattended"] = True
            # Bind the turn's state -- persona, mounted record, open playbook
            # and its source, the modify-or-create answer, the read-only
            # flag, the turn plan -- HERE, inside
            # _run_turn: the detached return path runs this in a raw thread,
            # which starts with fresh contextvars, so a bind in the chat_turn
            # body never reaches the turn's tool dispatch. The same call
            # stores what a resume reads back (_load_session_state).
            #
            # The open playbook is the appliance's copy: a `yaml_text` tool
            # argument is the model RE-EMITTING it, and a ~20 kB verbatim copy
            # comes back corrupted often enough that `load_yaml_text` falls
            # back to this one.
            _state_token = _bind_session_state(_turn_session_state(
                storage, session_id, entity=_entity, quick_action=quick_action,
                has_open_playbook=_hop, read_only=read_only, profile=profile,
                entity_module=entity_module, plan=_turn_plan))
            _bind_autonomy_policy(session_id)
            _bind_autonomy_subject()
            # ONE event loop for the scope declaration and the turn: the async
            # client binds to the loop that first used it.
            _turn_loop = asyncio.new_event_loop()
            # Ask scope: the effort of the turn matches the ask. A narrow
            # question narrows the turn (lookup slice, no verdict, ends on its
            # answer); everything else keeps today's flow. Bound BEFORE the
            # slice is computed so the slice and dispatch agree.
            _scope, _scope_how, _scope_why = _resolve_ask_scope(
                provider, _clean_latest_user, intent=intent, loop=_turn_loop,
                quick_action=quick_action, entity=_entity,
                skip=bool(read_only or (_actor and _actor.get("system"))))
            _scope_token = _bind_ask_scope(_scope)
            if _scope:
                _turn_tags["ask_scope"] = _scope
                _turn_tags["ask_scope_how"] = _scope_how
                if _scope_why:
                    _turn_tags["ask_scope_reason"] = _scope_why
            _turn_tools = _advertised_tools(
                config, persona,
                has_trace=_ht,
                has_open_playbook=_hop,
                # A triage-prior turn reaches authoring/enhance once state
                # affords it (trace or open playbook).
                authoring_active=_authoring_active(_ht, _hop),
                include_mcp=False,  # applied above (line ~3295)
                module=entity_module,  # M2: per-page MCP surfacing
                # A read-only chip ("explain"/"find_issues") withholds the
                # write/offer frontier for this turn -- see
                # _READ_ONLY_QUICK_ACTIONS.
                quick_action=quick_action,
                reach_servers=_reach_servers,  # M2.1: on-ask reach
                read_only=read_only,  # scheduled tasks with allow_mutating=False
                create_strategy=_create_strategy)  # widget create-method toggle
            _offered_token = _bind_offered_tools(_turn_tools)
            # No run-vs-author classifier narrows this slice: it fails open to a
            # normal build, so BuildProgressGuard._RUN_INTENT_TOOLS covers the run
            # case instead.
            # The system prompt in named pieces, sizes only: the framework
            # records them first in the transcript (`turn_inputs`) so a turn can
            # be diagnosed against what the model was given. The named layers
            # are the ones this connector appends; `prompt` is the intent or
            # persona prompt with its grounding and directives.
            _rendered_prompt = _render_prompt_for_tools(system_prompt, _turn_tools)
            _rendered_prompt += _scope_directive(_scope)
            _source_note = _source_availability_note(_turn_tools)
            _plan_suffix = _plan_constraints_suffix()
            try:
                from fsr_playbooks.llm.provider import TurnInputsEvent  # noqa: F401
                _turn_tags["prompt_slices"] = {
                    "prompt": len(_rendered_prompt),
                    "connector_rules": len(_CONNECTOR_INVARIANT_RULES),
                    "source_availability": len(_source_note),
                    "plan_constraints": len(_plan_suffix),
                }
            except ImportError:
                # A framework without the record would leave the tag on every
                # usage row, where nothing reads it.
                pass
            try:
                return _turn_loop.run_until_complete(run_agent_turn(
                    provider=provider,
                    system=_rendered_prompt + _source_note + _plan_suffix,
                    messages=wire_msgs,
                    tools=_turn_tools,
                    tags=_turn_tags,
                    on_event=_on_event,
                    history_sink=storage,
                    turn_for_history=turn_idx,
                    session_id=session_id,
                    **_stop_kwargs(storage, session_id, turn_idx),
                    **_case_state_kwargs(case_state),
                ))
            finally:
                _close_loop(_turn_loop)
                _reset_ask_scope(_scope_token)
                _unbind_session_state(_offered_token)
                _unbind_session_state(_state_token)

    def _finalize(result) -> dict:
        # Attribute framework drift to the framework, here, rather than let it
        # surface as a failed envelope that costs the analyst the transcript.
        _check_turn_result(result)
        _last_yaml = _safe_turn_field(result, "last_assistant_yaml")
        # The turn's binds are reset by now, but this conversion shapes the
        # record-write cards and reads the mounted record to tell the analyst
        # whether a write targets the record they have open. Unbound, every
        # such card said "unknown".
        from fsr_soc_triage.profiles import (
            record_iri_from_entity as _rec_iri_of,
            reset_active_record_iri as _reset_rec_iri,
            set_active_record_iri as _set_rec_iri,
        )
        _wire_rec = _set_rec_iri(_rec_iri_of(_entity))
        try:
            wire_transcript = _wire_transcript(result.transcript,
                                               unattended=_unattended_turn)
        finally:
            _reset_rec_iri(_wire_rec)
        # A build turn that narrated its final YAML instead of
        # calling emit_playbook_offer dead-ends with no Deploy button. Route it
        # through the same offer card deterministically so every build turn lands.
        _salvaged = _salvage_build_offer(
            wire_transcript,
            intent=intent,
            entity=_entity,
            last_assistant_yaml=_last_yaml,
            session_id=session_id,
            turn_idx=turn_idx,
            read_only=read_only,
        )
        _connector_fires: list[str] = []
        if _salvaged is not None:
            wire_transcript = _salvaged
            # The card the loop never produced: record which backstop made it.
            _connector_fires.append(getattr(_salvaged, "mechanism", ""))
        _gapped = _append_record_source_gaps(wire_transcript, config,
                                             storage=storage,
                                             session_id=session_id)
        if _gapped is not None:
            wire_transcript = _gapped
        envelope_stop = _envelope_stop_reason(wire_transcript, result.stop_reason,
                                              unattended=_unattended_turn)
        # C6: a turn the analyst stopped ends as stopped_by_analyst, whichever
        # loop exit it took (a stop can land on a guard's wrap-up path too).
        if _turn_stop_requested(storage, session_id, turn_idx):
            envelope_stop = STOPPED_BY_ANALYST
        # Envelope tags carry a compact case_state summary so the widget can
        # render a session status strip without a second round-trip.
        tags = dict(result.tags or {})
        cs_tag = _case_state_summary_tag(storage, session_id, wire_transcript,
                                         case_state)
        if cs_tag is not None:
            tags["case_state"] = cs_tag
        tags = _with_guard_fires(tags, result, _connector_fires)
        envelope = {
            "ok": result.error is None,
            "session_id": session_id,
            "turn": turn_idx,
            "turn_id": turn_id,
            "stop_reason": envelope_stop,
            "contract_version": CONTRACT_VERSION,
            "transcript": wire_transcript,
            "last_assistant_yaml": _last_yaml,
            "tags": tags,
            "error": result.error,
        }
        _validate_model(ChatTurnResponse, envelope)
        # Close the streaming feed: a terminal frame carrying the envelope
        # stop_reason tells a concurrent chat_poll the turn finished. It also
        # carries the FULL authoritative envelope (transcript + turn_id +
        # contract_version + last_assistant_yaml + tags). In detached mode
        # this frame is the ONLY delivery of the transcript,
        # so it must mirror everything the blocking return would carry.
        # Blocking callers get the transcript from this fn's return, so a failed
        # feed write is harmless for them. For a DETACHED producer the terminal
        # frame is the ONLY delivery + the only `done` signal; write_stream_end
        # logs that loudly and the detached backstop verifies + recovers.
        write_stream_end(
            storage, session_id, turn_idx, source="finalize", turn_id=turn_id,
            ok=result.error is None, stop_reason=envelope_stop,
            transcript=wire_transcript, error=result.error,
            last_assistant_yaml=_last_yaml, tags=tags,
            # A blocking caller holds the transcript from the return below, so
            # a successful blocking turn closes the feed signal-only. Detached
            # turns and error turns keep it (delivery / fallback).
            carry_transcript=bool(params.get("detached"))
            or result.error is not None)

        # Persist the canonical conversation + any emitted card so a later
        # chat_resume (which carries no messages[]) can re-enter the loop.
        # Also persist the case state (grounding, investigation progress, etc.).
        _persist_session_state(
            storage, session_id,
            _canonical_messages(prior, legacy_text, wire_transcript),
            wire_transcript,
            **_case_state_kwargs(case_state))
        return envelope

    # Detached turn (opt-in via `detached`): decouple the
    # producer from the HTTP request so a long build can outlive the uwsgi
    # `harakiri` per-request ceiling (which SIGKILLs the worker mid-request --
    # the terminal frame would never be written and chat_poll would hang).
    # uwsgi runs with `enable-threads = true` and harakiri times only the
    # *request*, so a daemon thread that outlives the request is never reaped.
    # The caller reads the authoritative transcript from the terminal
    # stream_end frame via chat_poll. A worker recycle / touch-reload mid-build
    # still loses the thread -- the accepted (rare) failure mode.
    #
    # Opt-in (not unconditional) on purpose: the interactive widget sets
    # `detached: true` because a real build can run minutes; short programmatic
    # callers (the live self-test harness, golden re-capture, CLI) omit it and
    # keep the simple blocking transcript -- they aren't at harakiri risk and
    # would otherwise have to poll. Mock turns never reach here (the
    # _mock_aware wrapper short-circuits to mock_replay before dispatch).
    if bool(params.get("detached")):
        def _detached():
            import traceback
            try:
                _prepare()   # C5: the prompt (preflight) is resolved off the ack path
                _finalize(_run_turn())
            except Exception as exc:
                # Never leave chat_poll hanging: log the full traceback
                # and write a terminal error frame so the caller tears down with
                # a surfaced root cause instead of a generic "lost contact".
                _log("detached chat_turn CRASHED session=%s turn=%s\n%s"
                     % (session_id, turn_idx, traceback.format_exc()), "error")
                write_terminal_error(storage, session_id, turn_idx,
                                     turn_id=turn_id, exc=exc)
            # Backstop: _finalize's terminal-frame append swallows its
            # own write errors (harmless for blocking callers). If it silently
            # failed here, `done` would never flip and chat_poll would hang
            # until the lost-producer watchdog. Verify the terminal frame landed
            # and, if not, write one so the poller settles promptly.
            try:
                if not storage.turn_is_terminal(session_id, turn_idx):
                    _log("detached chat_turn session=%s turn=%s finished "
                         "WITHOUT a terminal frame -- writing backstop terminal "
                         "frame" % (session_id, turn_idx), "error")
                    write_terminal_error(storage, session_id, turn_idx,
                                         turn_id=turn_id, exc=RuntimeError(
                                             "producer finished without writing a terminal frame"))
            except Exception:
                _log("detached: backstop terminal check FAILED session=%s "
                     "turn=%s" % (session_id, turn_idx), "error")

        # A new thread starts with an EMPTY context: without the copy the
        # detached turn lost the bound caller, so config scoping by the
        # analyst's teams (and the reviewer check) saw no one.
        import contextvars
        threading.Thread(
            target=contextvars.copy_context().run, args=(_detached,),
            name="chat_turn:%s:%s" % (session_id, turn_idx),
            daemon=True).start()
        accepted_resp = {
            "ok": True,
            "accepted": True,
            "session_id": session_id,
            "turn": turn_idx,
            "turn_id": turn_id,
            "stop_reason": "accepted",
            "contract_version": CONTRACT_VERSION,
        }
        _validate_model(ChatTurnResponse, accepted_resp)
        return accepted_resp

    # Blocking turn (default): run inline and return the full envelope. Used by
    # the live self-test harness / golden capture and any caller that omits
    # `detached`.
    _prepare()
    return _finalize(_run_turn())


#: Ceiling on one long-poll wait. Well under the uwsgi harakiri ceiling, and
#: short enough that a worker is never parked for long on an idle turn.
_LONG_POLL_MAX_MS = 8000
#: How often a waiting poll re-reads the feed while it holds the worker.
_LONG_POLL_TICK_S = 0.25


def _long_poll_wait_ms(long_poll_ms) -> int:
    """The held-poll window in ms, capped; 0 when none was asked for."""
    try:
        wait_ms = int(long_poll_ms or 0)
    except (TypeError, ValueError):
        wait_ms = 0
    return max(0, min(wait_ms, _LONG_POLL_MAX_MS))


def _await_turn_progress(storage, session_id: str, after: int, since_turn: int,
                         long_poll_ms) -> dict:
    """Read the feed; when `long_poll_ms` is set and nothing new is there yet,
    hold the request up to that long (capped) and return the moment a frame or
    the terminal lands. Absent or 0 keeps the old immediate read, so a caller
    that does not ask for a wait is unchanged. Only a frame (or `done`) ends the
    wait early, so a poll never returns an empty answer before its deadline."""
    import time

    wait_ms = _long_poll_wait_ms(long_poll_ms)
    progress = storage.read_turn_progress(session_id, after, since_turn)
    if not wait_ms:
        return progress
    deadline = time.monotonic() + wait_ms / 1000.0
    while not progress["done"] and not progress["frames"]:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(_LONG_POLL_TICK_S, left))
        progress = storage.read_turn_progress(session_id, after, since_turn)
    return progress


STOPPED_BY_ANALYST = "stopped_by_analyst"   # C6: the framework's stop reason


def _stop_kwargs(storage, session_id: str, turn_idx: int) -> dict:
    """cancel_check for run_agent_turn (C6), passed only when the installed
    framework takes it. A connector shipped ahead of its framework must not
    fail every chat turn with a TypeError."""
    import inspect
    from fsr_playbooks.llm.run_turn import run_agent_turn as _rat
    if "cancel_check" not in inspect.signature(_rat).parameters:
        return {}
    return {"cancel_check": lambda: _turn_stop_requested(
        storage, session_id, turn_idx)}


def _turn_stop_requested(storage, session_id: str, turn_idx: int) -> bool:
    """The analyst's Stop for this turn (C6). A failing read never stops a turn."""
    try:
        return bool(storage.turn_cancel_requested(session_id, turn_idx))
    except Exception:  # noqa: BLE001
        return False


def chat_cancel(config: dict, params: dict) -> dict:
    """Stop the session's running turn (C6). Marks it cancelled; the agent loop
    runs no further model round and dispatches no tool once it sees the mark,
    and the turn ends with stop_reason `stopped_by_analyst`.

    Params:
      session_id (required) -- the session whose current turn to stop.

    Returns: {ok, session_id, turn, cancelled}. `cancelled` is False when no
    turn is running (nothing to stop).
    """
    from .storage import default_storage

    _validate_model(ChatCancelParams, params or {})
    session_id = _require(params, "session_id")
    storage = default_storage()
    progress = storage.read_turn_progress(session_id, 0, 0)
    turn = progress.get("turn")
    if turn is None or progress.get("done"):
        resp = {"ok": True, "session_id": session_id, "turn": turn,
                "cancelled": False}
    else:
        storage.request_turn_cancel(session_id, turn)
        resp = {"ok": True, "session_id": session_id, "turn": turn,
                "cancelled": True}
    _validate_model(ChatCancelResponse, resp)
    return resp


def chat_poll(config: dict, params: dict) -> dict:
    """Poll the streaming progress feed for an in-flight chat_turn (contract
    2.5.0 / action-based streaming).

    The widget fires chat_turn (a long, blocking call) and, in parallel,
    polls this op on an interval. chat_turn writes one frame per emitted
    event to a SQLite feed; chat_poll returns frames with cursor > the
    caller's last-seen `cursor`, plus a new cursor watermark and a `done`
    flag. This is a pure, cheap read -- no health check, warmup, provider
    build, or LLM round-trip -- so it is safe to call frequently.

    Frames are RAW per-event wire shapes (text chunks, tool_use,
    tool_result, usage). Injected cards and coalesced text are NOT in the
    feed; the authoritative transcript rides in the terminal `stream_end`
    frame (and chat_turn's own return). On `done`, render that transcript.

    Params:
      session_id (required) -- the session whose current turn to poll.
      cursor (optional, default 0) -- last cursor the caller has seen; 0
        starts from the beginning of the current turn.

    Returns: {ok, session_id, turn, cursor, frames, done, stop_reason}.
    """
    _validate_model(ChatPollParams, params or {})
    from .storage import default_storage

    session_id = _require(params, "session_id")
    try:
        after = int(params.get("cursor") or 0)
    except (TypeError, ValueError):
        after = 0
    # Turn fence (contract 2.7.0): the highest turn the caller has already
    # committed. The poll returns only a turn STRICTLY GREATER than this, so a
    # concurrent first poll that races chat_turn's prologue can't be served the
    # PREVIOUS, already-terminal turn (the stale-replay race; see
    # read_turn_progress). Absent/0 preserves the legacy unfenced behaviour.
    try:
        since_turn = int(params.get("since_turn") or 0)
    except (TypeError, ValueError):
        since_turn = 0

    storage = default_storage()
    progress = _await_turn_progress(storage, session_id, after, since_turn,
                                    params.get("long_poll_ms"))

    # Liveness (contract 2.7.0 + the turn-ownership rows). A turn with no
    # terminal frame is failed when its owner worker is gone, its heartbeat has
    # stopped, or it has gone silent past the producer ceiling -- so a polling
    # widget converges on `done` instead of spinning. Terminal frames are
    # written once per turn, so this cannot double-finish a turn that is
    # completing right now: the second writer is simply refused and we re-read.
    # A detached run whose worker was recycled never writes its own terminal
    # frame; this is what closes that turn.
    error = None
    if not progress["done"] and progress["turn"] is not None:
        reason = storage.turn_orphan_reason(
            session_id, progress["turn"],
            last_activity=progress.get("last_activity"),
            silent_after_s=_STREAM_STALE_SECS)
        if reason is not None:
            error = storage.fail_turn(session_id, progress["turn"], reason)
            # Re-read so the response carries the terminal frame (or, if a real
            # terminal beat us to it, that one) and `done`.
            progress = storage.read_turn_progress(session_id, after, since_turn)

    poll_resp = {
        "ok": True,
        "session_id": session_id,
        "contract_version": CONTRACT_VERSION,
        "turn": progress["turn"],
        "cursor": progress["cursor"],
        "frames": progress["frames"],
        "done": progress["done"],
        "stop_reason": progress["stop_reason"],
    }
    if error is not None:
        poll_resp["error"] = error
    # Tells the widget this poll was held open, so it may re-poll at once. A
    # poll without a wait (or an older connector) leaves it out, and the widget
    # keeps its fixed cadence.
    if _long_poll_wait_ms(params.get("long_poll_ms")):
        poll_resp["long_poll"] = True
    _validate_model(ChatPollResponse, poll_resp)
    return poll_resp


def chat_resume(config: dict, params: dict, *,
                _policy_decider: str | None = None) -> dict:
    """Resume a paused turn. Two mechanisms:

    1. **Tier-3 run_op gate** -- a `SuspendedSession` was stashed by the
       AnthropicProvider mid-tool. Resume by `approval_id`: pop it and
       re-enter the provider loop with approve/deny.
    2. **Card / choice resume (Model A)** -- `emit_action_card` /
       `emit_choice_card` are tier-0 and stash nothing; the turn simply
       ended with the card as its last event. The widget resumes by
       `card_id` (or `choice_id`) carrying the user's decision and edited
       args but NO messages[] (contract §4). We reload the connector's
       canonical conversation for the session, append the decision as a
       new user message, and run a fresh agent turn so the model executes
       the approved operation (per emit_action_card's contract)."""
    _validate_model(ChatResumeParams, params or {})
    _check_health(config)
    decision = _require(params, "decision")
    if decision not in ("approve", "reject", "choice", "accept", "decline"):
        raise ConnectorError(
            "decision must be approve|reject|choice|accept|decline, got "
            f"{decision!r}")

    # The materializer's allow-list is module-level state, per uwsgi worker.
    # `chat_turn` applies it on every entry; `chat_resume` did not -- so a
    # resume landing on a worker that had not served a matching chat_turn had
    # an EMPTY allow-list, materialized no native-MCP tools, and dispatch()
    # resolved the approved `mcp_soc__*` call to "unknown tool". Approve then
    # silently never executed. Apply it here for the same reason chat_turn does.
    _apply_mcp_allowlist(config or {})

    from .storage import default_storage, PersistedApprovalGateway

    storage = default_storage()
    gateway = PersistedApprovalGateway(storage)
    # The turn's persona chose its model; the resume continues on the same one.
    provider = _build_provider(
        _config_for_persona(config, _load_resume_profile(storage, params.get("session_id"))),
        gateway)

    # Re-bind the turn's state for EVERY resume route, from what the turn
    # stored: the entity and quick_action that ground chat_turn are absent here,
    # and a tool that runs on resume -- the tier-3 gate, an
    # approved card's execution, the conversational re-entry -- would otherwise
    # run with each gate at its permissive default. Bound once at the single
    # entry so a route added later cannot miss it. The scope pick is stored
    # first because the pick itself arrives as a resume.
    _record_playbook_scope(storage, params)
    _state_token = _bind_session_state(
        _load_session_state(storage, params.get("session_id")))
    claim = None
    _decider_token = None
    try:
        # D2: who may decide. Checked before the claim, so a refused caller
        # leaves the card open for a reviewer.
        verified = None
        if _policy_decider is not None:
            # The enforcing policy decides. A keyword argument, not a
            # param: nothing the widget sends can reach it.
            verified = _policy_decider
        elif decision in _ONCE_DECISIONS:
            refusal, verified = _decision_gate(config, storage,
                                               params.get("session_id"),
                                               initiator_may_decide=True)
            if refusal is not None:
                return refusal
        _decider_token = _DECIDER.set(_approver_label(verified, params))
        claim = _claim_decision(storage, params, decision, verified)
        if claim and claim.get("prior"):
            return _already_decided(claim["card_id"], claim["prior"])
        out = _chat_resume_routes(config, params, decision, storage,
                                  gateway, provider)
        if claim and isinstance(out, dict) and out.get("ok") is False:
            storage.release_card_decision(claim["session_id"], claim["card_id"])
        return out
    except Exception:
        if claim:
            storage.release_card_decision(claim["session_id"], claim["card_id"])
        raise
    finally:
        if _decider_token is not None:
            _DECIDER.reset(_decider_token)
        _unbind_session_state(_state_token)


def chat_history(config: dict, params: dict) -> dict:
    """Return the session's prior turns for refresh rehydration. Each entry
    is `{user: str}` or `{transcript: [...full wire events...]}` in
    chronological order -- the widget replays them through its render pipeline
    to restore the exact timeline (cards, tool calls, activity included).
    No `ok` envelope -- this is a pure read."""
    _validate_model(ChatHistoryParams, params or {})
    session_id = _require(params, "session_id")
    limit = int(params.get("limit") or 200)
    from .storage import default_storage
    storage = default_storage()
    decisions = storage.list_card_decisions(session_id)
    if (params or {}).get("decisions_only"):
        return {"turns": [], "card_decisions": decisions}
    hist_resp = {"turns": storage.list_transcript(session_id, limit=limit),
                 # Who started the session, so the widget frames the reopened
                 # conversation by its real initiator (None on pre-actor sessions).
                 "initiator": storage.get_session_initiator(session_id),
                 "card_decisions": decisions}
    _validate_model(ChatHistoryResponse, hist_resp)
    return hist_resp


def list_sessions(config: dict, params: dict) -> dict:
    """List stored conversations newest-first for the history browser. Pure
    read; each entry is {session_id, title, entries, last_activity,
    started_at, initiator, rating}. `title` is the session's first user message;
    `rating` is the analyst's {rating:'up'|'down', note} or None.

    With a non-empty `query`, searches WITHIN sessions instead: returns only
    sessions whose transcript CONTENT contains the keyword (case-insensitive),
    each carrying a `snippet` excerpt of the match -- so a search like 'FIXME'
    finds conversations that mention it, not just those whose title does."""
    _check_params(ListSessionsParams, params)
    limit = int(params.get("limit") or 50)
    query = (params.get("query") or "").strip()
    from .storage import default_storage
    storage = default_storage()
    if query:
        return {"sessions": storage.search_sessions(query, limit=limit),
                "query": query}
    return {"sessions": storage.list_sessions(limit=limit)}


def rate_session(config: dict, params: dict) -> dict:
    """Record an analyst's thumbs up/down on a session (for triaging which
    sessions to investigate). params: {session_id, rating:'up'|'down'|''|null,
    note?}. An empty/null rating CLEARS it. Returns the stored {rating, note}."""
    _check_params(RateSessionParams, params)
    session_id = _require(params, "session_id")
    rating = params.get("rating")
    note = params.get("note")
    from .storage import default_storage
    storage = default_storage()
    try:
        storage.set_session_rating(session_id, rating, note)
    except ValueError as e:
        return _err("bad_rating", str(e))
    return {"ok": True, "session_id": session_id,
            "rating": storage.get_session_rating(session_id)}


def get_session_trace(config: dict, params: dict) -> dict:
    """DEBUG / smoke: return the recorded skill trace for a session and,
    when it is non-empty, the deterministically-compiled playbook built from
    it (the SAME build the offer-accept path runs). Lets an off-platform
    harness prove the full triage→playbook pipeline end to end:

      - `calls`        -- per recorded action: connector, operation, step_name,
                          ref_prefix (the `.data`-nesting flag that decides
                          whether runtime refs resolve), skill_id.
      - `workflow_json`-- compiled export wrap, ready to feed straight into
                          `dry_run_playbook` to prove the playbook RUNS in FSR.

    Read-only: never mutates the trace (the scope writes back the identical
    JSON it read). Returns {ok, session_id, count, calls, compiled_ok,
    workflow_json?, yaml?, steps?}."""
    _check_params(GetSessionTraceParams, params)
    session_id = _require(params, "session_id")
    # Bootstrap THIS version's vendored fsr_playbooks onto sys.path before any
    # absolute `fsr_playbooks` import -- on-platform the connector dir isn't on
    # sys.path, so a worker that hasn't yet run another fsr_playbooks-backed op
    # would otherwise fail with "No module named 'fsr_playbooks'". Every other
    # fsr_playbooks op does this first; this one must too.
    _import_fsr_playbooks()
    from .storage import default_storage
    storage = default_storage()
    raw = storage.get_session_trace(session_id)

    from fsr_playbooks.agent.skill_trace import SkillTrace
    trace = SkillTrace.from_json(raw or "")
    calls = [{
        "connector": c.resolved_inputs.get("connector"),
        "operation": c.resolved_inputs.get("operation"),
        "step_name": c.step_name,
        "ref_prefix": c.ref_prefix,
        "skill_id": c.skill_id,
    } for c in trace.calls]

    out: dict[str, Any] = {
        "ok": True, "session_id": session_id,
        "count": len(calls), "calls": calls, "compiled_ok": False,
    }
    if not calls:
        return out

    from fsr_playbooks.mcp_server.tools_compile import build_playbook_from_trace
    name = (params.get("name") or "").strip() or "Triage Playbook"
    with _session_trace_scope(storage, session_id):
        built = build_playbook_from_trace(trace_json="", name=name)
    if not built.get("ok"):
        out["build_error"] = built.get("code") or built.get("message")
        return out
    out["yaml"] = built["yaml"]
    out["steps"] = (built.get("compile_summary") or {}).get("steps")
    compiled = compile_yaml(config, {"yaml": built["yaml"]})
    out["compiled_ok"] = bool(compiled.get("ok"))
    if compiled.get("ok"):
        out["workflow_json"] = compiled["workflow_json"]
    else:
        out["compile_errors"] = compiled.get("errors") or []
    return out


def list_active_sessions(config: dict, params: dict) -> dict:
    """Live agentic chat sessions for the monitor's Live Sessions panel
    (agentic-assistant-monitor §10.2). Each row carries a DERIVED status --
    active (a turn is streaming), waiting_approval (parked at a HITL gate),
    or idle -- plus the acting user, the mounted record (uuid + module), the
    latest prompt, the turn count, and last-activity. Derived from the
    connector's own session stores (chat_sessions + turn_progress +
    suspended_sessions); the platform /api/3/llm_activity_logs module is not
    involved. Params: limit (optional, default 50), status (optional --
    active|waiting_approval|idle), user_iri (optional)."""
    _check_params(ListActiveSessionsParams, params)
    from .storage import default_storage
    limit = int(params.get("limit") or 50)
    return {"sessions": default_storage().list_active_sessions(
        limit=limit,
        status=params.get("status") or None,
        user_iri=params.get("user_iri") or None,
    )}
