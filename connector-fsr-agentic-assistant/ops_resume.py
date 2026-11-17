"""resume domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any
from .pydantic_models import (
    ChatResumeResponse,
)
from .pydantic_models import (
    InfoCardEvent as InfoCardFrame,
    TextEvent as TextFrame,
    ToolResultEvent as ToolResultFrame,
    ToolUseEvent as ToolUseFrame,
)
from .history_replay import transcript_blocks

from .ops_approvals import _decider, _grant_from_card
from .ops_cards import _derecord, _humanize_op
from .ops_config import _provider_name
from .ops_common import (
    CONTRACT_VERSION, _as_frame, _check_turn_result, _err, _frame, _log, _require,
    _safe_turn_field, _text, _validate_model,
)
from .ops_entity import _remember_created
from .ops_mcp import _apply_mcp_allowlist
from .ops_personas import ResolvedPersona
from .ops_playbooks import compile_yaml
from .ops_playbooks_persist import _open_playbook_yaml, push_playbook, update_playbook
from .ops_policy import _bind_autonomy_policy, _bind_autonomy_subject
from .ops_prompts import (
    _load_prompt, _normalize_intent, _render_prompt_for_tools, _session_trace_scope,
)
from .ops_redaction import redaction_scope
from .ops_tools import (
    _advertised_tools, _authoring_active, _session_has_trace, _source_availability_note,
)
from .ops_turn_io import (
    _case_state_kwargs, _case_state_mod, _case_state_summary_tag, _envelope_stop_reason,
    _persist_session_state, _save_cards, _wire_transcript, _with_guard_fires,
)
from .ops_turn_state import (
    _bind_session_state, _host_record_write_profile, _load_resume_profile,
    _bind_offered_tools, _created_records_source, _resolve_profile, _resume_profile,
    _stored_record_iri,
    _unbind_session_state,
)
from .turn_frames import write_stream_end
from .turn_sink import make_event_sink



def _chat_resume_routes(config: dict, params: dict, decision: str, storage,
                        gateway, provider) -> dict:
    """Route a validated resume to its handler. Split out of `chat_resume` only
    so the grounding re-bind there can wrap every route in one try/finally."""
    approval_id = params.get("approval_id")
    card_id = params.get("card_id")
    choice_id = params.get("choice_id")

    # (1) Tier-3 gate: resume an actual SuspendedSession when one exists.
    if approval_id:
        # "Always allow" rides an approve: grant first, so the persona the
        # approved run sees already lists the playbook. A failed grant is
        # reported but never blocks the run the analyst just approved.
        granted = (_grant_from_card(storage, params.get("session_id"), approval_id)
                   if decision == "approve" and params.get("remember") else None)
        suspended = gateway.pop(approval_id)
        if suspended is not None:
            out = _resume_suspended(provider, suspended, decision, storage,
                                    session_id=params.get("session_id"),
                                    config=config)
            if granted is not None and isinstance(out, dict):
                out["always_allow"] = granted
            return out
        # approval_id supplied but nothing suspended, and no card/choice
        # context to fall back on → the gate expired or already resumed.
        if not (card_id or choice_id):
            return _err(
                "approval_not_found",
                f"no suspended session for approval_id {approval_id!r}; "
                "may have expired (TTL 10m) or already been resumed.")

    # (2) action_card APPROVE → connector executes the op directly and
    # splices the real tool_result (contract §6 / T6). The triage prompt
    # forbids the model from running mutating ops, so this can't be deferred
    # to a conversational turn without dead-ending the approve→execute loop.
    if card_id and decision == "approve":
        return _resume_action_card_execute(config, params, decision,
                                           storage, provider)

    # (2.5) playbook_offer ACCEPT → the connector (not the model) compiles the
    # recorded session trace into FSRPB YAML and pushes it (contract §5,
    # 2.6.0). DECLINE ends the session with no extra content (contract §5) --
    # a direct end_turn, no agent round-trip, no conversation dependency.
    if decision == "accept":
        # Two offers ride this branch and they write very differently: a
        # playbook_offer CREATES (push_playbook), an enhancement_offer UPDATES
        # the open playbook in place (update_playbook). Route on the stored
        # card's type -- dispatching on decision alone would send an accepted
        # edit down the create path and save a duplicate, which is the exact
        # outcome the enhance flow exists to avoid.
        _sid = _require(params, "session_id")
        _oid = params.get("offer_id") or params.get("card_id") or "offer"
        _card = storage.get_card(_sid, _oid) or {}
        if _card.get("type") == "enhancement_offer":
            return _resume_enhancement_offer_accept(config, params, storage)
        return _resume_playbook_offer_accept(config, params, storage)
    if decision == "decline":
        sid = _require(params, "session_id")
        _oid = params.get("offer_id") or params.get("card_id") or "offer"
        if (storage.get_card(sid, _oid) or {}).get("type") == "enhancement_offer":
            # Declining an EDIT is "not this change", not "stop building".
            # Live (analyst sim): the analyst declined one unwanted step and
            # read "I won't save this as a playbook" as the build being over.
            text = ("Okay, I left the playbook as it is. Tell me what to "
                    "change and I'll revise the edit.")
        else:
            text = ("No problem -- I won't save this as a playbook. "
                    "Let me know if you need anything else.")
        return _offer_end_turn(
            sid, [_frame(TextFrame, type="text", text=text)],
            saved=False, code="declined")

    # (3) Card reject / decline / choice / generic conversational resume.
    return _resume_conversation(config, params, decision, storage, provider)


def _resume_suspended(provider, suspended, decision: str, storage,
                      session_id: str | None = None,
                      config: dict | None = None) -> dict:
    """Tier-3 path: re-enter the provider loop on a popped SuspendedSession.

    ``session_id`` is the real widget/connector session id (from chat_resume
    params) and is what every storage lookup below (persona, module, turn
    history, trace) uses.

    ``suspended.session_id`` is the fallback. It USED to be unusable: the
    provider's ``stream()`` mints a synthetic per-call trace id
    (``uuid4().hex[:8]``) and stamped THAT here, so it keyed nothing in the
    connector's session store. Providers now stash ``tags["session_id"]`` --
    the real chat session -- falling back to the trace id only when the caller
    passed no tags. Sessions parked by an older build still carry a trace id,
    which is the other reason the explicit param stays preferred."""
    from fsr_playbooks.llm.run_turn import resume_agent_turn

    # AnthropicProvider's resume() uses 'approve' or 'deny'; map our
    # reject → deny so the wire shape stays user-facing.
    provider_decision = "deny" if decision == "reject" else "approve"
    sid = session_id or suspended.session_id
    prior_turns = storage.list_turns(sid, limit=10_000)
    turn_idx = max((t["turn"] for t in prior_turns), default=0)

    # The approved tool EXECUTES inside this resume, so it needs the turn's
    # persona (the may_write gate) and mounted record (an approved run_playbook
    # that omitted `record`). chat_resume binds both at entry; bound again here
    # because this route is also reached without it.
    _state_token = _bind_session_state({
        "profile": _resume_profile(storage, sid),
        "record_iri": _stored_record_iri(storage, sid),
        "created_records": _created_records_source(storage, sid)})
    _bind_autonomy_policy(sid)
    _bind_autonomy_subject()
    # Streaming progress feed for the tier-3 gate's resume. Every OTHER resume
    # path (_execute_approved_card, _resume_conversation) already appends one
    # frame per event so the analyst watches the approved action run; this one
    # passed no `on_event` at all, so the whole post-approval turn -- every tool
    # call, every model round -- ran invisibly inside one blocking HTTP request
    # and the widget's approval card sat on "Approving…" for its full duration.
    # The card that gates state-changing actions was the one card with no
    # progress. Best-effort throughout: the poll feed must never break a turn.
    #
    # The feed needs its OWN turn index. `turn_idx` above is the ALREADY
    # COMMITTED turn (max of prior), and chat_poll fences on `since_turn`,
    # serving only a turn strictly greater than what the caller committed -- so
    # frames written onto `turn_idx` would be filtered out and the spinner would
    # not move. `reserve_next_turn` mints the next index and writes its
    # `turn_start` in one transaction (do NOT append a second turn_start).
    # History keying stays on `turn_idx`: `turn_for_history` is deliberately
    # unchanged here, since that is the assembly seam #114 lives in.
    try:
        feed_turn = storage.reserve_next_turn(sid)
    except Exception:
        feed_turn = None

    # The feed rides feed_turn; history and the terminal frame keep the old
    # turn (#114 seam). The round's usage rows are keyed on the same turn its
    # frames ride, so the monitor's per-turn cost includes approval rounds. The
    # audit identity mirrors _resume_conversation: the stored intent, no analyst
    # text (an approval carries none), and the session initiator.
    try:
        _audit_intent = storage.get_session_intent(sid) or "build"
    except Exception:
        _audit_intent = "build"
    _on_resume_event = make_event_sink(
        storage, config or {}, sid,
        feed_turn if feed_turn is not None else turn_idx,
        provider_name=_provider_name(provider, config or {}),
        audit_intent=_audit_intent, audit_prompt="",
        actor_resolver=lambda: storage.get_session_initiator(sid),
        feed=feed_turn is not None)

    try:
        with _session_trace_scope(storage, sid), \
                redaction_scope(storage, config, sid):
            result = asyncio.run(resume_agent_turn(
                provider=provider, suspended=suspended, decision=provider_decision,
                on_event=_on_resume_event,
                history_sink=storage, turn_for_history=turn_idx,
                session_id=sid,
            ))
    finally:
        _unbind_session_state(_state_token)
    _check_turn_result(result)  # same seam as chat_turn._finalize (#57)
    _wire_rec = _bind_session_state(  # see chat_turn._finalize
        {"record_iri": _stored_record_iri(storage, sid)})
    try:
        wire_transcript = _wire_transcript(result.transcript)
    finally:
        _unbind_session_state(_wire_rec)
    # The resumed turn can raise its own cards: an approved run, then the
    # update_record that fixes what the run left. This path skips the persist
    # chokepoint (the framework's history_sink keeps the conversation), so
    # without this those cards were never stored and approving one found
    # nothing to execute -- live, the model was told "approve card <id>" and
    # went looking for a record with that id.
    # Same gap for history: chat_history replays session_transcript, which the
    # chokepoint appends to and this path did not, so a reload showed the
    # conversation stopping at the approval card with everything after it gone.
    try:
        _save_cards(storage, sid, wire_transcript)
        storage.append_transcript_assistant(sid, wire_transcript)
    except Exception as exc:  # noqa: BLE001 -- the turn itself still succeeded
        _log("resume_suspended: could not store this turn's cards/history session=%s: %s: %s"
             % (sid, type(exc).__name__, exc), "error")
    # A resumed turn runs the agent loop, so guards can fire here too.
    _resume_tags = _with_guard_fires(result.tags, result)
    resume_resp = {
        "ok": result.error is None,
        "session_id": sid,
        "turn_id": uuid.uuid4().hex,
        "stop_reason": _envelope_stop_reason(wire_transcript,
                                             result.stop_reason),
        "contract_version": CONTRACT_VERSION,
        "transcript": wire_transcript,
        "tags": _resume_tags,
        "error": result.error,
    }
    # Name the turn the feed rides on. The transcript now reaches the widget
    # BOTH ways (poll terminal + this synchronous return), and the widget's
    # commit-once guard is keyed on `turn` -- absent it, `_turnNo` is null, the
    # idempotency check cannot fire, and a poll terminal that outraces this
    # return leaves the duplicate to be caught only by `_isStaleReplay`. That
    # heuristic is precisely what discarded a legitimate turn in #114, so it
    # should be the backstop, not the primary defense.
    if feed_turn is not None:
        resume_resp["turn"] = feed_turn
    _validate_model(ChatResumeResponse, resume_resp)
    # Close the feed, mirroring _resume_conversation's terminal. Without a
    # terminal frame `done` never flips, so a widget that keeps polling spins
    # until the lost-producer guard synthesizes an error frame -- turning a
    # turn that SUCCEEDED into a visible "lost contact with the server".
    if feed_turn is not None:
        write_stream_end(
            storage, sid, feed_turn, source="resume_suspended",
            turn_id=resume_resp["turn_id"], ok=result.error is None,
            stop_reason=resume_resp["stop_reason"], transcript=wire_transcript,
            error=result.error,
            last_assistant_yaml=_safe_turn_field(result, "last_assistant_yaml"),
            tags=_resume_tags)
    return resume_resp


# Plain names for the vendors whose operations an analyst approves most. Any
# other connector is shown with its name humanized (see _connector_label).
_CONNECTOR_LABELS = {
    "ninjaone": "NinjaOne",
    "virustotal": "VirusTotal",
    "alienvault-otx": "AlienVault OTX",
    "abuseipdb": "AbuseIPDB",
    "shodan": "Shodan",
    "fortigate-firewall": "FortiGate",
    "fortinet-fortiedr": "FortiEDR",
    "fortinet-fortisiemv2": "FortiSIEM",
    "fortinet-fortianalyzer": "FortiAnalyzer",
    "fortinet-fortimanager": "FortiManager",
}


def _connector_label(connector: str) -> str:
    """The analyst-facing name of a connector: a known vendor name, else the
    connector's id with its vendor prefix dropped and each word capitalised."""
    key = (connector or "").lower()
    if key in _CONNECTOR_LABELS:
        return _CONNECTOR_LABELS[key]
    for prefix in ("fortinet-", "cyops-"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    words = [w.capitalize() for w in key.replace("_", "-").split("-") if w]
    return " ".join(words) or "the connector"


def _exec_failure_reason(result: Any, label: str) -> str:
    """One plain sentence on why an approved action did not run.

    Read from the failure's code and HTTP status. The raw message and body stay
    in the tool_result the model reads; the analyst gets this sentence."""
    res = result if isinstance(result, dict) else {}
    code = str(res.get("code") or "")
    if code in ("exec_dispatch_failed", "transport_failed", "no_live_fsr"):
        return f"The request to {label} could not be sent."
    # The vendor's own code, when its body carries one ("[404]: Entity not
    # found"), says more than the transport's wrapper ("HTTP 400").
    message = str(res.get("message") or res.get("error") or "")
    inner = re.search(r"\[(\d{3})\]", message)
    wrapped = re.search(r"HTTP (\d{3})", message)
    status = ((inner.group(1) if inner else "")
              or str(res.get("status") or "")
              or (wrapped.group(1) if wrapped else ""))
    if status in ("401", "403"):
        return f"{label} refused the connection's credentials."
    if status == "404":
        return f"{label} could not find the item this action targets."
    if status == "429":
        return f"{label} is rate limiting requests. Try again shortly."
    if status.startswith("5"):
        return f"{label} returned a server error."
    if status.startswith("4") or code == "op_request_rejected":
        return f"{label} rejected the request."
    return f"{label} returned an error."


def _summarize_exec(connector: str, operation: str, result: Any) -> str:
    """Deterministic, analyst-facing one-liner for an executed action_card.

    We do NOT round-trip back to the LLM for this summary: the demo must
    survive an Anthropic outage, and the human already approved the action.
    Plain words only: no emoji, no connector.operation ids, no raw result
    body. The full result is the tool_result in the transcript."""
    title = _humanize_op(operation)
    label = _connector_label(connector)
    if isinstance(result, dict) and result.get("ok"):
        return f"{title} on {label} completed."
    reason = _exec_failure_reason(result, label)
    return (f"{title} on {label} did not complete. {reason} Check the "
            f"target and try again, or run it directly in {label}.")


# Post-approval execute-failure codes the AGENT can fix by itself (named the
# wrong op, malformed args) -- as opposed to genuine infrastructure failures
# (transport / preflight / dispatch) a human must resolve. ONLY these re-enter
# the provider loop after approval; everything else keeps the deterministic,
# outage-survivable "contain manually" path. Sourced from the validator-emitted
# codes in fsr_playbooks (_validate_op_exists / _validate_op_params / run_op).
_CORRECTABLE_EXEC_CODES = frozenset({
    "unknown_operation", "bad_params", "missing_required", "bad_select_value",
})
# Bound the self-heal so a model that keeps proposing bad ops can't spin: after
# this many self-heal passes in a session, fall through to the templated
# message. (Each pass is human-gated anyway -- it ends at a fresh action_card.)
_MAX_SELF_HEAL_PASSES = 2


def _self_heal_user_msg(connector: str, operation: str,
                        exec_result: Any) -> str:
    """The user turn that re-enters the agent loop after a *correctable*
    post-approval execute failure: report the failed action + the validator's
    suggestions, and instruct the agent to self-correct and re-card (not
    apologise)."""
    code = msg = ""
    suggestions: list = []
    if isinstance(exec_result, dict):
        code = exec_result.get("code") or ""
        msg = exec_result.get("message") or exec_result.get("error") or ""
        suggestions = exec_result.get("suggestions") or []
    hints = ("\nHints:\n" + "\n".join(f"- {s}" for s in suggestions) + "\n"
             if suggestions else "")
    return (
        f"The action I approved -- {connector}.{operation} -- could not be "
        f"executed:\n{code}: {msg}\n{hints}\n"
        "This is a correctable error (wrong operation name or malformed "
        "arguments), NOT an infrastructure failure. Use find(kind='operation') / "
        "get_op_schema to find the correct operation and its required "
        "arguments, then emit a CORRECTED action_card for me to approve. Do "
        "not apologise or tell me to contain manually -- self-correct first.")


# Messages written in the analyst's voice name an action by what it does, never
# by its card id: the model repeats what it is told, and live it answered a
# cancel with "I won't run action card 82e78376d443...". A record write has no
# connector or operation, which printed as a bare ".".
def _card_ref(card: dict | None) -> str:
    title = _derecord(str((card or {}).get("summary") or "")).strip()
    return f'your action ("{title}")' if title else "your action"


def _op_ref(connector: str, operation: str) -> str:
    if connector and operation:
        return f"{connector}.{operation}"
    return "it"


def _exec_followup_user_msg(card_id, connector: str, operation: str,
                            exec_result: Any) -> tuple[str, str]:
    """The post-execute follow-up as ``(durable, directive)``.

    **`durable` is history; `directive` is for THIS turn only** -- the same
    split as `_manual_input_user_message`, and for the same reason.

    This used to be one string, and it ended with "If a clear next containment
    action exists, propose it via an action_card for me to approve." Written in
    the analyst's voice and persisted to the canonical conversation, that is a
    standing request for another approval card -- so after a block executed
    cleanly the model went on to call `find_enrichment_actions` and stage a
    second card unprompted. The analyst asked for one action and was handed
    another to approve. The `contain` turn-mode directive says "once it's
    staged you are done", but it is appended on the chat_turn path only and
    never reached this turn.

    So: the fact that the op ran (and its result) is durable; everything that
    tells the model what to do about it now -- including the stop condition --
    rides the directive and is not written to history.
    """
    data = exec_result.get("data", exec_result) \
        if isinstance(exec_result, dict) else exec_result
    preview = json.dumps(data, default=str)
    if len(preview) > 600:
        preview = preview[:600] + "\u2026"
    durable = (
        f"I approved your action, and {_op_ref(connector, operation)} has "
        f"ALREADY been executed on my behalf -- it completed successfully. "
        f"Result:\n{preview}")
    directive = (
        "Do NOT run it again. Briefly confirm the outcome for the analyst in "
        "plain language, grounded in the result above. Then, if further work "
        "is warranted, describe the next steps in ONE short prose line -- do "
        "not self-assign them. This turn is complete once you have reported "
        "the outcome: do NOT stage another action_card, choice_card or "
        "approval, and do NOT start a fresh enrichment or containment sweep. "
        "The analyst asked for one action and it is done; wait for them to "
        "ask before proposing the next one.")
    return durable, directive


def _followup_after_exec(config: dict, params: dict, decision: str, storage,
                         provider, session_id: str, connector: str,
                         operation: str, exec_result: Any,
                         det_events: list[dict]) -> dict | None:
    """After a SUCCESSFUL human-approved execute, re-enter the agent loop so
    the model narrates the outcome and proposes next steps -- never dead-end.

    The real `run_op` tool_use + tool_result (`det_events`, contract T6) are
    PREPENDED to the model's follow-up transcript so the executed result still
    renders and the conversation continues in one envelope. Returns None -- so
    the caller falls back to the deterministic, outage-survivable summary --
    when no provider is available or the follow-up round-trip fails (e.g. an
    Anthropic outage)."""
    if provider is None:
        return None
    _durable, _directive = _exec_followup_user_msg(
        params.get("card_id"), connector, operation, exec_result)
    try:
        followup = _resume_conversation(
            config, params, decision, storage, provider,
            user_msg=_durable, turn_directive=_directive,
            resume_tag="action_card_exec_followup")
    except Exception:  # noqa: BLE001 -- provider/transport error → fall through
        return None
    if followup is None or followup.get("error"):
        return None
    # Splice the real executed op ahead of the model's narration. The last
    # event (and thus the envelope stop_reason) stays the model's, so a
    # proposed next action_card still surfaces as awaiting_action_card.
    followup["transcript"] = det_events + (followup.get("transcript") or [])
    tags = followup.get("tags")
    if isinstance(tags, dict):
        tags["resume"] = "action_card_exec_followup"
        tags["executed"] = True
    return followup


def _try_self_heal_resume(config: dict, params: dict, decision: str, storage,
                          provider, session_id: str, connector: str,
                          operation: str, exec_result: Any) -> dict | None:
    """Attempt one self-heal pass for a correctable post-approval failure.

    Returns a resume envelope (agent re-entered the loop and, ideally, emitted
    a corrected card) on success, or None to signal the caller to fall through
    to the deterministic summary -- when: the failure isn't agent-fixable, the
    self-heal budget is spent, no provider is available, or the provider
    itself failed (e.g. an Anthropic outage -- we must NOT lose the
    outage-survivable deterministic path)."""
    code = exec_result.get("code") if isinstance(exec_result, dict) else None
    if code not in _CORRECTABLE_EXEC_CODES or provider is None:
        return None
    try:
        prior = sum(
            1 for t in storage.list_turns(session_id, limit=10_000)
            if (t.get("tags") or {}).get("resume") == "action_card_self_heal")
    except Exception:
        prior = 0
    if prior >= _MAX_SELF_HEAL_PASSES:
        return None
    healed = None
    try:
        healed = _resume_conversation(
            config, params, decision, storage, provider,
            user_msg=_self_heal_user_msg(connector, operation, exec_result),
            resume_tag="action_card_self_heal")
    except Exception:  # noqa: BLE001 -- provider/transport error → fall through
        return None
    # Only accept the heal if the provider actually ran (no error). On a
    # provider failure return None so the caller emits the deterministic
    # message instead of an error envelope.
    if healed is not None and not healed.get("error"):
        return healed
    return None


def _approved_card_config(card: dict, chosen: Any = None) -> str:
    """The config id an approved action card runs on, or "" for the default.

    The analyst may switch it on the card (``chat_resume`` sends ``config``),
    but only to one of the configs the card itself offered -- an id the card
    never listed is ignored, so a resume can't aim an approved action at an
    arbitrary configuration. Without a choice, the card's own config."""
    card = card if isinstance(card, dict) else {}
    own = card.get("config") if isinstance(card.get("config"), dict) else {}
    offered = {str(o.get("id")) for o in (card.get("config_options") or [])
               if isinstance(o, dict) and o.get("id")}
    if own.get("id"):
        offered.add(str(own["id"]))
    if isinstance(chosen, dict):
        chosen = chosen.get("id")
    if chosen and str(chosen) in offered:
        return str(chosen)
    return str(own.get("id") or "")


def _editable_arg_names(card: dict) -> set[str]:
    """The argument names the analyst may change on this card.

    The card's own ``editable_fields`` is the contract. Without it, the
    ``param_schema`` rows marked ``editable``. A card that names neither is
    not editable at all."""
    names = card.get("editable_fields")
    if isinstance(names, list):
        return {str(n) for n in names if n}
    return {str(p.get("name")) for p in card.get("param_schema") or []
            if isinstance(p, dict) and p.get("editable") and p.get("name")}


def _approved_args(card: dict, widget_args: Any) -> dict:
    """The args an approved action runs with.

    The stored card owns them. The widget's ``args`` may change only the
    fields the card marked editable; any other key it sends is dropped
    (logged), so a resume can't retarget a fixed argument such as an
    organization id. Editable fields are the analyst's to change, and the
    widget's value for them is taken as the analyst's decision."""
    stored = card.get("args")
    if not isinstance(stored, dict):
        # A persisted approval_request keeps its call under `preview.args`.
        preview = card.get("preview") if isinstance(card.get("preview"), dict) else {}
        stored = preview.get("args") if isinstance(preview.get("args"), dict) else {}
    merged = dict(stored)
    if not isinstance(widget_args, dict):
        return merged
    editable = _editable_arg_names(card)
    dropped = []
    for key, value in widget_args.items():
        if key in editable:
            merged[key] = value
        elif stored.get(key) != value:
            dropped.append(str(key))
    if dropped:
        _log(f"approve card={card.get('id')}: ignored widget args for "
             f"non-editable field(s) {sorted(dropped)}", "warning")
    return merged


def _resume_action_card_execute(config: dict, params: dict, decision: str,
                                storage, provider) -> dict:
    """Contract §6 / T6: on **approve** of an `action_card`, the connector
    executes the operation directly and splices the real `tool_result` into
    the transcript.

    Why the connector (not the model) executes: the triage system prompt
    forbids the agent from running mutating ops itself (`run_op` is read-only;
    containment MUST go through `emit_action_card`). So handing an "execute
    it now" instruction back to the model dead-ends -- it has no tool that can
    mutate, so it either re-emits another card (the loop never closes) or just
    talks (no `tool_result`, failing T6). The human approval IS the gate; the
    connector fulfils it deterministically here.

    Falls back to a conversational resume when the card is missing or isn't an
    executable action_card (e.g. a stale id)."""
    session_id = _require(params, "session_id")
    card_id = params["card_id"]
    card = storage.get_card(session_id, card_id)
    connector = (card or {}).get("connector")
    operation = (card or {}).get("operation")
    if not card or card.get("type") != "action_card" or not (connector and operation):
        # Nothing executable to run -- degrade to the conversational path so
        # the chat never dead-ends (model acknowledges + asks how to proceed).
        return _resume_conversation(config, params, decision, storage, provider)

    final_args = _approved_args(card, params.get("args"))

    # If this card was auto-generated from a suspended mutating run_op
    # (card_id == approval_id), clean up the orphaned SuspendedSession --
    # we execute deterministically below rather than via provider.resume.
    try:
        from .storage import PersistedApprovalGateway
        PersistedApprovalGateway(storage).pop(card.get("approval_id") or card_id)
    except Exception:
        pass

    call_id = "exec-" + uuid.uuid4().hex[:8]
    run_args = {"connector": connector, "op": operation,
                "params": final_args, "confirm": True, "_approved": True}
    run_config = _approved_card_config(card, params.get("config"))
    if run_config:
        run_args["config"] = run_config
    try:
        from fsr_playbooks.llm.tools import dispatch, grant_tool_approval

        # If a grant mode was requested (e.g., "once" or "always"), record it
        # so future matching calls to the same tool auto-run without approval.
        grant_mode = params.get("grant")
        if grant_mode and grant_mode in ("once", "always"):
            op_key = f"{connector}:{operation}"
            grant_tool_approval(session_id, "run_op", op_key=op_key, mode=grant_mode)

        # _approved bypasses the HITL tier gate (human already approved);
        # confirm=True clears run_op's own destructive-op guard.
        # Record into the session trace: this approved containment op is
        # exactly the mutating step the trace-compiler turns into a
        # confirmed/decision step (PLAN §1, §3). The scope installs the
        # active trace so run_op's recorder hook fires for this dispatch.
        with _session_trace_scope(storage, session_id):
            exec_result = dispatch("run_op", run_args,
                                   _internal=True, session_id=session_id,
                                   approved_by=_decider())
    except Exception as e:  # noqa: BLE001 -- execution must never raise to caller
        exec_result = {"ok": False, "code": "exec_dispatch_failed",
                       "error": f"{type(e).__name__}: {e}"}

    exec_ok = isinstance(exec_result, dict) and exec_result.get("ok")
    # What actually ran, for a later one-click undo (undo_action). This is the
    # route a live action-card approve takes; recording only in
    # _execute_approved_card left every live approval un-undoable.
    _record_execution(storage, session_id, card_id, connector, operation,
                      run_args.get("config"), final_args, bool(exec_ok))

    # The real run_op tool_use + tool_result (contract T6) -- prepended to a
    # continuation transcript on success, or carried in the deterministic
    # summary path below.
    det_events = [
        _frame(ToolUseFrame, type="tool_use", id=call_id, name="run_op",
               tier=3, input={"connector": connector, "op": operation,
                              "params": final_args,
                              **({"config": run_args["config"]}
                                 if run_args.get("config") else {})}),
        _frame(ToolResultFrame, type="tool_result", tool_use_id=call_id,
               content=exec_result),
    ]

    if not exec_ok:
        # Self-heal (gap B, sess-uq31go5p): if the approved op failed with an
        # AGENT-FIXABLE code (wrong op name / bad args), re-enter the provider
        # loop so the agent corrects itself and emits a fresh, re-gated
        # action_card -- instead of dead-ending on the templated "contain
        # manually" message. A genuine infra failure (or a provider outage
        # during the heal) returns None here and falls through to the
        # deterministic path below, preserving the Anthropic-outage-survivable
        # property the human-approved execute relies on.
        healed = _try_self_heal_resume(
            config, params, decision, storage, provider, session_id,
            connector, operation, exec_result)
        if healed is not None:
            return healed
    else:
        # Success continuation: don't dead-end after a containment action.
        # Re-enter the loop so the model confirms the outcome + recommends
        # next steps (and may propose the next action_card). Falls through to
        # the deterministic summary when no provider / the round-trip fails.
        followup = _followup_after_exec(
            config, params, decision, storage, provider, session_id,
            connector, operation, exec_result, det_events)
        if followup is not None:
            return followup

    summary = _summarize_exec(connector, operation, exec_result)

    # Deterministic, outage-survivable fallback: the real tool_use +
    # tool_result followed by an analyst-facing summary text.
    wire_transcript = det_events + [
        _frame(TextFrame, type="text", text=summary)]

    # Extend the canonical conversation so any later turn recalls the exec.
    try:
        convo = storage.load_conversation(session_id) or []
        # Both entries are outcomes: the widget never carries them, so the next
        # chat_turn re-inserts them (history_replay.with_resume_outcomes).
        convo.append({"role": "user", "outcome": True,
                      "content": "I approve your action. "
                                 f"Execute {_op_ref(connector, operation)}."})
        convo.append({"role": "assistant", "outcome": True,
                      "content": transcript_blocks(wire_transcript)})
        _persist_session_state(storage, session_id, convo, wire_transcript)
    except Exception:
        pass

    action_resp = {
        "ok": True,
        "session_id": session_id,
        "turn_id": uuid.uuid4().hex,
        "stop_reason": "end_turn",
        "contract_version": CONTRACT_VERSION,
        "transcript": wire_transcript,
        "tags": {"session_id": session_id, "resume": "action_card_exec",
                 "executed": bool(exec_ok)},
        "error": None,
    }
    _validate_model(ChatResumeResponse, action_resp)
    return action_resp


def _failure(result: Any, default_code: str = "apply_failed") -> tuple[str, str]:
    """`(code, message)` out of either failure envelope this connector produces.

    THE shape trap on every write path, and the one that made #137 a dead end.
    `_err` nests its class: ``{"ok": False, "error": {"code", "message", ...}}``.
    A handler reading top-level ``result["code"]`` therefore NEVER sees a code --
    not for `would_drop_fields`, not for `live_read_failed`, not for a snapshot
    refusal -- and reports the `default_code` for all of them while discarding
    the message that says what to do about it. Live probe A2 hit exactly that:
    a real pre-write refusal reached the analyst as a bare "apply_failed", and
    the diagnosis cost a live run to recover something the connector already
    knew and threw away.

    The other shape is the hand-rolled ``{"code": ..., "error": "<str>"}`` some
    compile/except branches build inline. Both are normalised here so no caller
    has to remember which one it is holding -- three call sites each guessed,
    and two guessed wrong.
    """
    res = result if isinstance(result, dict) else {}
    err = res.get("error")
    if isinstance(err, dict):
        return (str(err.get("code") or default_code),
                str(err.get("message") or ""))
    return (str(res.get("code") or default_code),
            str(err or res.get("message") or ""))


def _resume_enhancement_offer_accept(config: dict, params: dict,
                                     storage) -> dict:
    """`enhancement_offer` ACCEPT: apply a verified edit to the OPEN playbook.

    The UPDATE counterpart of ``_resume_playbook_offer_accept``. That one
    CREATES via ``push_playbook``; this one edits in place via
    ``update_playbook`` -- the designer's own snapshot-then-PUT -- so the analyst
    keeps a pre-edit restore point in the Versions tab.

    Routing through ``push_playbook`` here would be silently wrong rather than
    loudly broken: its ``overwrite`` deletes by a DETERMINISTIC uuid5-of-name,
    and a playbook the analyst authored in the designer does not have one, so
    the "overwrite" would miss and save a duplicate alongside the untouched
    original.

    The applied text is the card's ``final_yaml``, which the framework's
    ``emit_enhancement_offer`` copied straight out of the passing
    ``verify_enhancement`` -- not anything the model typed afterwards. That
    binding is the whole point of the flow; do not add a path that recompiles
    from chat text.

    Never dead-ends: a missing card, a compile error, or a failed PUT all
    return an end_turn explaining what happened and stating that the open
    playbook is unchanged.
    """
    session_id = _require(params, "session_id")
    offer_id = params.get("offer_id") or params.get("card_id") or "offer"
    card = storage.get_card(session_id, offer_id) or {}
    final_yaml = card.get("final_yaml")

    if not isinstance(final_yaml, str) or not final_yaml.strip():
        return _offer_end_turn(
            session_id,
            [_text("I couldn't find the verified edit for this card any "
                   "more, so I didn't touch your playbook. Ask me to make "
                   "the change again and I'll re-verify it.")],
            saved=False, code="offer_expired")

    # The widget passes the open playbook's identity on the accept resume;
    # chat_resume carries no entity block, so there is nothing else to fall
    # back to. Say so plainly rather than guessing a target.
    workflow_iri = params.get("workflow_iri") or ""
    workflow_uuid = params.get("workflow_uuid") or ""
    # Nothing open in the designer: the edit can only be to the playbook this
    # conversation saved (that is what grounded it). Use the id recorded at
    # save time, and only for the same playbook.
    saved_target = False
    if not (workflow_iri or workflow_uuid):
        saved = None
        try:
            saved = storage.get_session_saved_playbook(session_id)
        except Exception:  # noqa: BLE001
            saved = None
        if saved and (_playbook_name_in_yaml(saved.get("yaml"))
                      == _playbook_name_in_yaml(final_yaml)):
            workflow_uuid = saved["workflow_uuid"]
            saved_target = True
    if not (workflow_iri or workflow_uuid):
        return _offer_end_turn(
            session_id,
            [_text("I couldn't tell which playbook to update -- the accept "
                   "didn't carry the open playbook's id -- so nothing was "
                   "changed.")],
            saved=False, code="missing_workflow_target")

    try:
        compiled = compile_yaml(config, {"yaml": final_yaml})
    except Exception as e:  # noqa: BLE001
        compiled = {"ok": False, "errors": [{"message": f"{type(e).__name__}: {e}"}]}
    if not (isinstance(compiled, dict) and compiled.get("ok")
            and compiled.get("workflow_json")):
        return _offer_end_turn(
            session_id,
            [_text("The edit passed verification but failed to compile, so "
                   "I left your playbook alone."),
             _as_frame(InfoCardFrame, {
                 "type": "info_card", "id": f"enh-err-{offer_id}",
                 "variant": "compile_errors", "title": "Could not apply edit",
                 "errors": (compiled or {}).get("errors") or []})],
            saved=False, code="compile_failed")

    up_params: dict = {"workflow_json": compiled["workflow_json"],
                       "session_id": session_id}
    if workflow_iri:
        up_params["workflow_iri"] = workflow_iri
    if workflow_uuid:
        up_params["workflow_uuid"] = workflow_uuid
    # Deletions the VERIFY gate judged intentional, recorded on the card at emit
    # time. Without them the pre-write guard refuses any accepted edit that
    # removes a step -- live A3: "would_drop_fields ... steps[Dead End]" on a
    # deletion the analyst had asked for in exactly those words. Read from the
    # STORED card, never from the resume payload, so the widget cannot widen
    # what gets acknowledged: the analyst approves the consequences the gate
    # verified, not whatever the caller asserts at apply time.
    acked = card.get("acknowledged_drops")
    if isinstance(acked, list) and acked:
        up_params["acknowledged_drops"] = [str(p) for p in acked]
    # Playbooks the verified edit ADDS (a reusable child). Read from the STORED
    # card, like the drops: the analyst approved creating exactly these.
    added_pbs = card.get("playbooks_added")
    if isinstance(added_pbs, list) and added_pbs:
        up_params["create_playbooks"] = [str(n) for n in added_pbs]
    # From the open designer the widget applies the edit the way the analyst
    # would: restore point first, then the steps land in the canvas unsaved
    # and the analyst's own Save writes them (and undo still works).
    staged = params.get("apply_to") == "canvas"
    if staged:
        up_params["stage"] = True
    try:
        res = update_playbook(config, up_params)
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "code": "apply_failed",
               "error": f"{type(e).__name__}: {e}"}

    if not (isinstance(res, dict) and res.get("ok")):
        # Read the code out of the `_err` envelope it actually lives in. Reading
        # the top level reported EVERY failure here as "apply_failed" and swallowed
        # the message -- including `would_drop_fields`, whose message is the only
        # thing that tells the analyst which field it thinks is disappearing.
        code, msg = _failure(res)
        detail = f" {msg}" if msg else ""
        return _offer_end_turn(
            session_id,
            [_text(f"I compiled the edit but saving it to FortiSOAR failed "
                   f"({code}).{detail} Your playbook is unchanged.")],
            saved=False, code=code)

    # With a created child, the open playbook is the parent ALONE, calling the
    # child by the id it now has -- what the designer shows and what a fresh
    # mount decompiles to. The two-playbook text would ground the next turn on
    # a document the canvas does not hold.
    open_yaml = _open_playbook_yaml(final_yaml, res.get("created_playbooks"))
    if saved_target and not staged:
        # The saved playbook now IS this text; the next edit starts from it.
        try:
            storage.set_session_saved_playbook(session_id, workflow_uuid,
                                               open_yaml)
        except Exception:  # noqa: BLE001 -- the write itself succeeded
            pass

    title = _playbook_name_in_yaml(final_yaml) or "the open playbook"
    new_pbs = [str(c.get("name")) for c in (res.get("created_playbooks") or [])
               if isinstance(c, dict) and c.get("name")]
    added = card.get("steps_added") or []
    modified = card.get("steps_modified") or []
    if new_pbs and card.get("changes"):
        # The new playbook's own steps are not on this canvas: listing them as
        # "added" read like the child was built here (analyst sim: "I only
        # see the alert side").
        mine = [c for c in card["changes"] if isinstance(c, dict)
                and c.get("playbook") not in new_pbs]
        added = [c.get("step") for c in mine if c.get("kind") == "added"]
        modified = [c.get("step") for c in mine if c.get("kind") == "modified"]
    bits = []
    if added:
        bits.append("added " + ", ".join(f"“{s}”" for s in added))
    if modified:
        bits.append("updated " + ", ".join(f"“{s}”" for s in modified))
    what = "; ".join(bits) or "applied the change"
    # A created playbook is SAVED, unlike the staged canvas, and lives in the
    # playbook list rather than on this canvas: say both, or the analyst looks
    # for it here, or discards the canvas thinking it goes too.
    also = (" I also saved " + ", ".join(f"“{n}”" for n in new_pbs)
            + (" as its own playbook" if len(new_pbs) == 1 else " as their own playbooks")
            + f" in the same collection; “{title}” calls it. Open it from the "
            "playbook list -- it is not on this canvas." if new_pbs else "")
    if staged:
        what = "; ".join(bits) or "made the change"
        summary = (f"The edit to “{title}” is in the designer -- {what}. "
                   f"It is not saved yet.{also}")
        info = _as_frame(InfoCardFrame, {
            "type": "info_card", "id": f"staged-{offer_id}",
            "variant": "playbook_staged", "title": f"Review “{title}”",
            "summary": summary,
            "blocks": [{"kind": "text", "text": summary},
                       {"kind": "text",
                        "text": "Review it and click Save in the designer. The "
                                "playbook as it was is saved in the Versions "
                                "tab."}],
            "workflow_iri": res.get("workflow_iri"),
            "version_uuid": res.get("version_uuid"),
            "workflow": res.get("workflow"),
            # The canvas now holds this text: the widget makes it the open
            # playbook, so a follow-up edit starts from the draft.
            "yaml": open_yaml})
        text = _text(f"I {what} in the designer. Review the change and click "
                     f"Save to keep it -- nothing on the canvas is saved until "
                     f"you do. The previous version is in the Versions tab.{also}")
        return _offer_end_turn(session_id, [text, info], saved=False,
                               code="staged", compiled=True)
    summary = f"Applied the edit to “{title}” -- {what}.{also}"

    info = _as_frame(InfoCardFrame, {
        "type": "info_card", "id": f"applied-{offer_id}",
        "variant": "playbook_updated", "title": f"Updated “{title}”",
        "summary": summary,
        "blocks": [{"kind": "text", "text": summary},
                   {"kind": "text",
                    "text": "A pre-edit restore point was saved in the "
                            "playbook's Versions tab."}],
        "workflow_iri": res.get("workflow_iri"),
        "version_uuid": res.get("version_uuid")})
    text = _text(f"Done -- I {what} in “{title}” and saved it. Reload the "
                 f"designer to see the change; the previous version is in "
                 f"the Versions tab if you want it back.{also}")
    return _offer_end_turn(session_id, [text, info], saved=True, code=None)


def _playbook_name_in_yaml(yaml_text: Any) -> str:
    """The first playbook's `name:` from an FSRPB document, or "".

    Read-only and total: a non-string, unparseable, or oddly-shaped document
    yields "" so the caller falls through to its own default. Naming a saved
    playbook must never be able to fail the save.
    """
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return ""
    try:
        import yaml as _yaml   # local: matches the module's lazy-yaml posture
        doc = _yaml.safe_load(yaml_text)
        pbs = doc.get("playbooks") if isinstance(doc, dict) else None
        first = pbs[0] if isinstance(pbs, list) and pbs else None
        name = first.get("name") if isinstance(first, dict) else None
        return name.strip() if isinstance(name, str) else ""
    except Exception:  # noqa: BLE001 -- cosmetic; never block the push
        return ""


def _offer_end_turn(session_id: str, transcript: list[dict], *,
                    saved: bool, code: str | None = None,
                    compiled: bool = False, persisted: bool = False) -> dict:
    """End-turn envelope for a playbook_offer accept (contract §5). Always
    `ok: True` / `end_turn` -- a failed save is surfaced as analyst-facing text
    + an info_card (no error banner), matching the action_card-exec pattern."""
    # Persist the outcome to the render-fidelity history. The offer accept
    # paths return directly from chat_resume and never pass the turn-persist
    # chokepoint, so without this a failed (or successful) apply left NO trace
    # in chat_history -- live: an apply that 500'd twice showed the analyst
    # nothing and the session read as if Apply was never clicked. Best-effort:
    # a history write failure must not fail the resume itself.
    # `persisted`: the caller already wrote this transcript through
    # _persist_session_state (the successful save does) -- writing it again
    # rendered "Saved ..." twice after every refresh.
    if not persisted:
        try:
            from .storage import default_storage
            default_storage().append_transcript_assistant(session_id, transcript)
        except Exception:  # noqa: BLE001
            pass
    offer_resp = {
        "ok": True,
        "session_id": session_id,
        "turn_id": uuid.uuid4().hex,
        "stop_reason": "end_turn",
        "contract_version": CONTRACT_VERSION,
        "transcript": transcript,
        "tags": {"session_id": session_id, "resume": "playbook_offer_accept",
                 "saved": bool(saved), "code": code,
                 # How far the accept got. A push can only fail on a box, so
                 # the offline tier grades THIS: the trace compiled to a
                 # playbook that validates (N2 graded PASS on "Could not save").
                 "compiled": bool(compiled or saved)},
        "error": None,
    }
    _validate_model(ChatResumeResponse, offer_resp)
    return offer_resp


def _apply_offer_edits(yaml_text: str, edits: dict) -> str:
    """Apply the contract 2.6.0 safe inline `edits` to compiled YAML -- text
    and labels ONLY (manual_input prompt text, decision branch labels). Never
    touches wiring or the DAG shape. Best-effort: any parse/shape surprise
    returns the YAML unchanged so a cosmetic edit can't block the save."""
    prompts = (edits or {}).get("manual_input_prompts") or {}
    branch_labels = (edits or {}).get("branch_labels") or {}
    if not prompts and not branch_labels:
        return yaml_text
    try:
        import yaml as _yaml
        doc = _yaml.safe_load(yaml_text)
        workflows = (doc or {}).get("workflows") or []
        for wf in workflows:
            for step in wf.get("steps") or []:
                name = step.get("name")
                if name in prompts and isinstance(step.get("arguments"), dict):
                    # manual_input prompt text lives under arguments.message/label
                    for key in ("message", "label", "prompt"):
                        if key in step["arguments"]:
                            step["arguments"][key] = prompts[name]
                if name in branch_labels and isinstance(
                        step.get("arguments"), dict):
                    conds = step["arguments"].get("conditions")
                    if isinstance(conds, list):
                        labels = branch_labels[name]
                        for cond in conds:
                            cur = cond.get("label") or cond.get("name")
                            if cur in labels:
                                cond["label"] = labels[cur]
        return _yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    except Exception:  # noqa: BLE001 -- a cosmetic edit must never block the save
        return yaml_text


def _resume_playbook_offer_accept(config: dict, params: dict, storage) -> dict:
    """Contract §5 / 2.6.0: on **accept** of a `playbook_offer`, the CONNECTOR
    (not the model) compiles the recorded session trace into FSRPB YAML and
    pushes it. Deterministic -- the same trace that built the offer card's
    reviewable draft builds the pushed playbook, so the wiring the analyst
    reviewed is exactly what ships. Re-enters on the SAME session_id, so the
    trace recorded during triage is in scope (this is why the offer flow rides
    chat_resume rather than a fresh session).

    Never dead-ends: an empty trace, a compile error, or a push failure all
    return an end_turn with an explanatory message instead of an error."""
    session_id = _require(params, "session_id")
    offer_id = params.get("offer_id") or params.get("card_id") or "offer"
    edits = params.get("edits") if isinstance(params.get("edits"), dict) else {}

    # Direct-build offer (§A): the card carries the final verified YAML the
    # model authored (emit_playbook_offer(yaml=…)); compile + push THAT text.
    # Only a trace-built offer (no final_yaml) replays the session trace.
    stored_card = storage.get_card(session_id, offer_id) or {}
    final_yaml = stored_card.get("final_yaml")
    # The name the MODEL authored inside the YAML ranks above the generic
    # fallback. A direct-build offer carries the finished document, and the
    # model names it deliberately -- announcing that name to the analyst in the
    # same breath -- so falling straight through to "Triage Playbook" saved the
    # playbook under a name nobody chose and the chat had just contradicted.
    # LIVE REPRO (session sess-v6uv6x15): the model said 'The playbook will be
    # named "DNS Tunneling Investigation and Response"'; the save card then read
    # 'Saved "Triage Playbook"'. Still ranks BELOW an explicit params["title"]
    # (an analyst rename wins) and below title_suggestion (the offer author sets
    # that for trace-built offers, where there is no authored YAML to read).
    title = ((params.get("title") or "").strip()
             or (stored_card.get("title_suggestion") or "").strip()
             or _playbook_name_in_yaml(final_yaml)
             or "Triage Playbook")
    if isinstance(final_yaml, str) and final_yaml.strip():
        n_steps = len(stored_card.get("ops_summary") or [])
        built = {"ok": True, "yaml": final_yaml,
                 "compile_summary": {"steps": n_steps}}
    else:
        from fsr_playbooks.mcp_server.tools_compile import (
            build_playbook_from_trace,
        )

        # Install the session's recorded trace so the compiler reads it (same
        # scope used by chat_turn / the action_card execute path).
        with _session_trace_scope(storage, session_id):
            built = build_playbook_from_trace(trace_json="", name=title)

    if not built.get("ok"):
        msg = (built.get("message") or built.get("code")
               or "no recorded actions")
        return _offer_end_turn(
            session_id,
            [_text(f"I couldn't assemble a playbook from this session -- "
                   f"{msg}.")],
            saved=False, code=built.get("code") or "empty_trace")

    yaml_text = _apply_offer_edits(built["yaml"], edits)

    compiled = compile_yaml(config, {"yaml": yaml_text})
    if not compiled.get("ok"):
        return _offer_end_turn(
            session_id,
            [_text("The playbook compiled from this session had validation "
                   "errors, so I didn't save it."),
             _as_frame(InfoCardFrame, {
                 "type": "info_card", "id": f"offer-err-{offer_id}",
                 "variant": "compile_errors", "title": "Could not save playbook",
                 "errors": compiled.get("errors") or []})],
            saved=False, code="compile_failed")

    try:
        push = push_playbook(config, {"workflow_json": compiled["workflow_json"],
                                      "overwrite": True})
    except Exception as e:  # noqa: BLE001 -- surface, don't crash the resume
        push = {"ok": False, "code": "push_failed", "error": str(e)}

    if not push.get("ok"):
        code, msg = _failure(push, "push_failed")
        detail = f" {msg}" if msg else ""
        return _offer_end_turn(
            session_id,
            [_text("I compiled the playbook but saving it to FortiSOAR "
                   f"failed ({code}).{detail}")],
            saved=False, code=code, compiled=True)

    steps = (built.get("compile_summary") or {}).get("steps", 0)
    # Remember what was saved, and where, so "now also add X" in this
    # conversation edits THIS playbook and its Apply writes to it -- the widget
    # only knows a playbook's id when it is open in the designer.
    _saved_wf = (push.get("workflow_uuids") or [None])[0]
    if _saved_wf:
        try:
            storage.set_session_saved_playbook(session_id, str(_saved_wf),
                                               yaml_text)
        except Exception:  # noqa: BLE001 -- the save itself succeeded
            pass
    # Root-relative deep link into the playbook designer. A workflow uuid opens
    # that playbook directly (/playbooks/<wf_uuid>); fall back to the collection
    # view (/playbooks/collections/<coll_uuid>). Root-relative resolves both
    # same-origin in the SOAR SPA and through the harness proxy.
    wf_uuids = push.get("workflow_uuids") or []
    coll_uuid = push.get("collection_uuid")
    if wf_uuids:
        pb_url = "/playbooks/" + str(wf_uuids[0])
    elif coll_uuid:
        pb_url = "/playbooks/collections/" + str(coll_uuid)
    else:
        pb_url = None
    summary = (f"Saved this investigation as a re-runnable playbook "
               f"({steps} steps).")
    blocks = [{"kind": "text", "text": summary}]
    if pb_url:
        blocks.append({"kind": "link", "label": "Open in Playbook Designer",
                       "url": pb_url})
    info = _as_frame(InfoCardFrame, {
        "type": "info_card", "id": f"pushed-{offer_id}",
        "variant": "playbook_pushed", "title": f"Saved “{title}”",
        "summary": summary,
        "blocks": blocks,
        "url": pb_url,
        "collection_uuid": coll_uuid,
        "workflow_uuids": wf_uuids})
    link_md = f" Open it here: [“{title}”]({pb_url})." if pb_url else ""
    text = _text(f"Done -- I saved this investigation as the playbook "
                 f"“{title}”.{link_md} You can also run it from the playbook "
                 f"library the next time this pattern shows up.")

    # Extend the canonical conversation so later turns recall the save.
    persisted = False
    try:
        convo = storage.load_conversation(session_id) or []
        convo.append({"role": "user", "outcome": True,
                      "content": f"Save this triage as a playbook "
                                 f"named '{title}'."})
        convo.append({"role": "assistant", "outcome": True,
                      "content": text["text"]})
        _persist_session_state(storage, session_id, convo, [info, text])
        persisted = True
    except Exception:
        pass

    return _offer_end_turn(session_id, [info, text], saved=True,
                           persisted=persisted)


def _choice_answer_label(card: dict, value: Any) -> str | None:
    """The option label(s) for a choice answer, or None for typed text (the
    value IS what the analyst wrote, so it needs no label)."""
    by_value = {str(o.get("value")): str(o.get("label") or o.get("value"))
                for o in (card.get("options") or []) if isinstance(o, dict)}
    picks = value if isinstance(value, list) else [value]
    labels = [by_value[str(v)] for v in picks if str(v) in by_value]
    return " · ".join(labels) if labels and len(labels) == len(picks) else None


def _record_choice_answer(storage, session_id: str, choice_id: Any, value: Any,
                          actor: Any) -> None:
    try:
        card = storage.get_card(session_id, choice_id) or {}
        storage.append_transcript_resume(
            session_id, str(choice_id), value,
            label=_choice_answer_label(card, value),
            author=actor if isinstance(actor, dict) else None)
    except Exception:  # noqa: BLE001 -- history is a nicety, the resume is not
        pass


def _resume_user_message(storage, session_id: str, card_id, choice_id,
                         decision: str, params: dict, note: str) -> str:
    """Render the user message that encodes the resume decision so the
    agent has everything it needs to act on the next turn."""
    if card_id:
        card = storage.get_card(session_id, card_id) or {}
        connector = card.get("connector") or ""
        operation = card.get("operation") or ""
        final_args = _approved_args(card, params.get("args"))
        if decision == "approve":
            return (
                f"I approve {_card_ref(card)}. Execute "
                f"{_op_ref(connector, operation)} now using exactly these "
                f"arguments:\n{json.dumps(final_args, indent=2, default=str)}\n"
                "Then report the result.")
        extra = f" Note: {note}" if note else ""
        n_batch = len([b for b in (card.get("batch") or []) if isinstance(b, dict)])
        also = (f" That card also listed {n_batch} more action(s) you "
                "proposed with it; none of them ran either, so do not "
                "re-issue them one at a time." if n_batch else "")
        return (
            f"I cancelled {_card_ref(card)}, so it did not run.{extra}{also} "
            "Do not execute it. Briefly acknowledge and ask what I'd like to "
            "change or do instead.")
    if choice_id:
        value = params.get("value")
        if choice_id == "_budget_ask":
            if str(value) == "continue":
                return ("Continue researching with more tool rounds. Pick up "
                        "exactly where you left off -- do not restart.")
            if str(value) == "deliver":
                return ("Stop researching and deliver now. Summarize what you "
                        "found and, if this is a build session, emit the best "
                        "draft playbook you can from what you already know.")
        return (f"For '{choice_id}', I chose: "
                f"{json.dumps(value, default=str)}. Please continue.")
    # Generic conversational resume (turn_id + optional note).
    if decision == "reject":
        extra = f" Note: {note}" if note else ""
        return f"I declined.{extra} Acknowledge and ask how to proceed."
    return note or "Please continue."



def _async_task_id(result: Any) -> "str | None":
    """See fsr_soc_triage.async_outcome.async_task_id (single definition --
    the native-MCP funnel enriches with the same logic)."""
    try:
        from fsr_soc_triage.async_outcome import async_task_id
        return async_task_id(result)
    except Exception:  # noqa: BLE001 -- older layout: treat as sync
        return None


def _poll_task_outcome(task_id: str, timeout_s: float = 12.0) -> "dict | None":
    """See fsr_soc_triage.async_outcome.poll_task_outcome."""
    try:
        from fsr_soc_triage.async_outcome import poll_task_outcome
        return poll_task_outcome(task_id, timeout_s)
    except Exception:  # noqa: BLE001 -- off-box: unpollable
        return None


def _record_execution(storage, session_id: str, card_id: str, connector: str,
                      operation: str, config: Any, args: dict, ok: bool) -> None:
    """Remember what an approved run_op card ran, for undo_action. Both
    approve routes call this; it never fails the approval."""
    try:
        storage.record_card_execution(
            session_id, card_id, connector=connector, operation=operation,
            config=config, args=dict(args or {}), ok=ok)
    except Exception as exc:  # noqa: BLE001
        _log(f"resume: could not record execution of {card_id}: {exc}", "warning")


def _execute_approved_card(storage, session_id: str, turn_idx: int,
                           card_id: str,
                           params: dict) -> tuple[str, list[dict]]:
    """Execute an approved action card and return (user_msg, wire_frames).

    The approval already happened -- a human clicked approve on a card that
    named a specific connector, operation and argument set. So we dispatch it
    directly with the `_approved` sentinel (the same internal-only bypass the
    framework's tier-3 resume uses) instead of asking the model to re-issue
    the call.

    Two things come back:

    * `user_msg` -- states the action was ALREADY executed and carries the real
      result, so the model narrates evidence instead of an intention. This is
      the half that stops the confabulation: there is no longer any reading of
      the input under which "execute this" is pending.
    * `wire_frames` -- a real tool_use/tool_result pair so the execution is
      visible in the live poll feed AND in the persisted transcript. An
      approved containment action must leave a trace; "the assistant said so"
      is not an audit record.
    """
    import time  # operations.py has no module-level `import time`

    from fsr_playbooks.llm.tools import dispatch

    card = storage.get_card(session_id, card_id) or {}
    connector = card.get("connector") or ""
    operation = card.get("operation") or ""
    tool = card.get("tool") or ""
    # Only the card's editable fields may come from the widget (see
    # _approved_args); an approval_request keeps its call under `preview.args`.
    final_args = _approved_args(card, params.get("args"))
    # Internal sentinels are plumbing and must never be re-sent as call args.
    final_args = {k: v for k, v in final_args.items()
                  if not str(k).startswith("_")}

    if connector and operation and tool in ("", "run_op"):
        tool_name = "run_op"
        tool_args = {"connector": connector, "op": operation,
                     "params": dict(final_args), "confirm": True}
        run_config = _approved_card_config(card, params.get("config"))
        if run_config:
            tool_args["config"] = run_config
        label = f"{connector}.{operation}"
    elif tool in ("update_record", "create_record", "delete_record"):
        # A record-write card's `args` deliberately carries the FIELDS alone --
        # that is the editable surface the analyst reviews -- while the module
        # and uuid ride on `target` (see _shape_record_write_card). Executing
        # `args` verbatim therefore called update_record(description=...) with
        # no module/uuid/fields nesting: the approve "succeeded" and the row
        # never changed (probe T2). Rebuild the real call shape here: target
        # supplies the coordinates, the (possibly analyst-edited) args supply
        # the field values.
        target = card.get("target") if isinstance(card.get("target"), dict) else {}
        _mod = target.get("module") or final_args.get("module") or ""
        _uid = target.get("uuid") or final_args.get("uuid") or ""
        if "fields" in final_args and isinstance(final_args.get("fields"), dict):
            _fields = dict(final_args["fields"])  # already call-shaped
        else:
            _fields = {k: v for k, v in final_args.items()
                       if k not in ("module", "uuid")}
        tool_name = tool
        if tool == "create_record":
            tool_args = {"module": _mod, "fields": _fields}
        elif tool == "update_record":
            tool_args = {"module": _mod, "uuid": _uid, "fields": _fields}
        else:
            tool_args = {"module": _mod, "uuid": _uid}
        label = f"{tool}:{_mod}"
    elif tool:
        # Any other tier-3 tool (run_playbook, resume_playbook, mcp_*). Its arg
        # dict is the payload; the `_approved` sentinel below is tool-agnostic
        # (dispatch pops it before tier resolution), so this bypasses the gate
        # the same way run_op does.
        tool_name = tool
        tool_args = dict(final_args)
        label = tool
    elif card.get("type") in ("action_card", "approval_request"):
        # An APPROVAL with nothing dispatchable on it. This must never be a
        # quiet fallback: the analyst clicked approve, so something is owed
        # them either way. Returning (None, []) here is what let an approved
        # run_playbook end the turn with no run, no result and no error.
        _log("resume: approved card %s on session %s carries no dispatchable "
             "tool (type=%r keys=%r) -- approval could not be honoured"
             % (card_id, session_id, card.get("type"), sorted(card)), "error")
        result = {"ok": False, "code": "approval_not_dispatchable",
                  "message": ("the approved card carries no tool to execute, "
                              "so nothing ran")}
        call_id = "cardexec_" + uuid.uuid4().hex[:16]
        frames = [
            _frame(ToolUseFrame, type="tool_use", id=call_id,
                   name="approved_card", input={"card_id": card_id}, tier=4),
            _frame(ToolResultFrame, type="tool_result", tool_use_id=call_id,
                   content=result, duration_ms=0),
        ]
        for frame in frames:
            try:
                storage.append_turn_progress(session_id, turn_idx, frame)
            except Exception:
                pass
        return (
            "I approved your action, but it could NOT be executed: "
            "the stored approval carries no operation to run, so nothing "
            "happened. Tell me plainly that the approved action did not run "
            "and that this is a defect worth reporting -- do NOT claim it "
            "succeeded, and do not silently retry it as a different call.",
            frames,
        )
    else:
        # Not an approval at all (an offer / choice / manual_input card has its
        # own accept path) -- the conversational resume is correct here.
        return None, []

    def _run_one(tool_name: str, tool_args: dict) -> tuple[Any, list[dict]]:
        """Dispatch one approved call past the gate; persist its frames."""
        call_id = "cardexec_" + uuid.uuid4().hex[:16]
        frames: list[dict] = [_frame(
            ToolUseFrame, type="tool_use", id=call_id, name=tool_name,
            input=tool_args, tier=4)]

        t0 = time.time()
        try:
            # `approved_dispatch()` is the signal the run_playbook gate reads: a
            # human clicked approve on a card naming this playbook, so a missing
            # assistant persona must not veto them. Scoped to this call only, and
            # unreachable by the model (the `_approved` arg below cannot carry it --
            # FastMCP forbids underscore-prefixed tool parameters).
            # The flag lives in the framework (one module, whatever name the
            # connector package loaded under), and the framework hands it to the
            # registered run gate. Never import a connector copy here: a second
            # copy's __init__ re-registers every hook onto its own ContextVars,
            # which is how one approved card once broke every later record write
            # in the worker (#148: "no persona is active").
            from fsr_playbooks.mcp_server.tools_playbook_run import approved_dispatch
            # The card executes BEFORE the resume's model turn, so the profile bind
            # that wraps run_agent_turn (further down _resume_conversation) is not
            # active yet -- the record-write body's _write_gate saw no persona and
            # refused an already-approved write (probe T2, third failure shape).
            # Bind the same persona resolution the resume turn will use, with the
            # same host-record fallback chat_turn binds (#148), around just this
            # dispatch.
            _exec_prof_token = _bind_session_state(
                {"profile": _resume_profile(storage, session_id)})
            try:
                with approved_dispatch():
                    result = dispatch(tool_name, {**tool_args, "_approved": True},
                                      _internal=True, session_id=session_id,
                                      approved_by=_decider())
            finally:
                _unbind_session_state(_exec_prof_token)
        except Exception as exc:
            result = {"ok": False, "code": "card_execution_failed",
                      "message": f"{type(exc).__name__}: {exc}"}
            _log("resume: approved card %s dispatch RAISED session=%s: %s: %s"
                 % (card_id, session_id, type(exc).__name__, exc), "error")
        duration_ms = int((time.time() - t0) * 1000)

        # #149: a success that only QUEUED a playbook is not an outcome. Chase the
        # task_id before anything is narrated or persisted, and splice what we
        # learn into the result the model (and the transcript) sees. A result the
        # native-MCP funnel already enriched carries `async_playbook`: trust that
        # outcome and skip the second poll (up to 12 s + a duplicate listing).
        _already_enriched = isinstance(result, dict) and bool(result.get("async_playbook"))
        _async_tid = None if _already_enriched else _async_task_id(result)
        _async_outcome = _poll_task_outcome(_async_tid) if _async_tid else None
        if _async_tid:
            if not isinstance(result, dict):
                result = {"raw": result}
            result["async_playbook"] = dict(_async_outcome or {"status": "unknown"},
                                            task_id=_async_tid)

        frames.append(_frame(ToolResultFrame, type="tool_result",
                             tool_use_id=call_id, content=result,
                             duration_ms=duration_ms))
        _remember_created(storage, session_id, tool_name, result)
        for frame in frames:
            try:
                storage.append_turn_progress(session_id, turn_idx, frame)
            except Exception:
                pass

        return result, frames

    result, frames = _run_one(tool_name, tool_args)
    if tool_name == "run_op":
        _record_execution(storage, session_id, card_id, connector, operation,
                          tool_args.get("config"), final_args,
                          bool(isinstance(result, dict) and result.get("ok")))
    # Batched approval: the card also listed the gated calls the model emitted
    # right behind this one (framework `approvals.collect_batch`), and the
    # analyst's one decision covered them all. Run them in order, each through
    # the same gate bypass and persona binding. Analyst edits (`params.args`)
    # apply to the lead call only -- the card edits that one.
    batch_done: list[tuple[dict, Any]] = []
    for item in card.get("batch") or []:
        if not isinstance(item, dict) or not item.get("tool"):
            continue
        b_args = {k: v for k, v in (item.get("args") or {}).items()
                  if not str(k).startswith("_")}
        b_res, b_frames = _run_one(str(item["tool"]), b_args)
        frames.extend(b_frames)
        batch_done.append((item, b_res))
    ok = bool(isinstance(result, dict) and result.get("ok"))
    rendered = _resume_result_view(tool_name, tool_args, result)

    _async_status = ((result.get("async_playbook") or {}).get("status")
                     if isinstance(result, dict) else None)
    if ok and _async_status and _async_status not in ("finished",):
        # The tool call "succeeded" but only queued a playbook whose outcome
        # is failed / pending / unknown. Never let that narrate as a landed
        # action (#149): state exactly what is and is not known.
        _ao = result.get("async_playbook") or {}
        if _async_status in ("failed", "terminated", "finished_with_error",
                             "rejected"):
            user_msg = (
                f"I approved your action. {label} accepted the "
                f"request and queued a playbook (task {_ao.get('task_id')}), "
                f"but that playbook {_async_status.upper()}"
                + (f" at step {_ao['failing_step']!r}" if _ao.get("failing_step") else "")
                + (f": {_ao['error_message']}" if _ao.get("error_message") else ".")
                + "\n\nSo the action did NOT land. Tell me plainly that the "
                "approved action failed on the box, what the error means, and "
                "what I should do about it. Do NOT claim any part of it "
                "succeeded, and do not silently retry.")
        else:
            user_msg = (
                f"I approved your action. {label} accepted the "
                f"request and QUEUED it as task {_ao.get('task_id')} -- the "
                f"outcome is still {_async_status or 'unknown'}.\n\n"
                "Tell me it is queued with the outcome pending. Do NOT claim "
                "it completed, succeeded, or that anything is blocked/changed "
                "yet -- nothing has been confirmed on the box.")
    elif ok:
        # Facts first, then one short ask. The old closing lines ("Do NOT run
        # it again. Report this result to me and say what it means for the
        # case") came back verbatim: "Confirmed: I did not run it again" and a
        # "what this means for the case" section on a template edit.
        _said = (f" (fields written: {', '.join(sorted((tool_args.get('fields') or {})))})"
             if tool_name in _RECORD_WRITES else
             f" with these arguments:\n{json.dumps(final_args, indent=2, default=str)}")
        user_msg = (
            "I approved your action. "
            f"{label} HAS ALREADY BEEN EXECUTED on my approval{_said}.\n\n"
            f"Its actual result:\n{rendered}\n\n"
            "Tell me in one or two plain sentences what changed, and first "
            "anything that did not save. Claim only what this result shows. "
            "It is complete: do not propose it again, and do not mention "
            "re-running it. Then carry on with anything I asked that is still open.")
    else:
        user_msg = (
            "I approved your action, but executing "
            f"{label} FAILED. The error:\n{rendered}\n\n"
            "Do not claim the action succeeded. Tell me plainly that it "
            "failed, what the error means, and what I should do about it. "
            "Only retry if the error is clearly transient.")
    if batch_done:
        user_msg += "\n\n" + _batch_outcome_message(batch_done)
    return user_msg, frames


_RECORD_WRITES = ("create_record", "update_record")
# Characters of any one result the resume turn carries. A record write's
# result is the whole stored record: 117K characters on a ZTPF action, which
# made the turn after an approval cost 50-60K input tokens.
_RESUME_RESULT_MAX = 6000
_SAVED_VALUE_MAX = 200


def _landed(sent: Any, stored: Any) -> bool:
    """Whether a value sent in a write is what the record now holds. Links and
    picklists come back hydrated, so only a text that differs, or a field that
    is not on the record at all, counts as not saved."""
    if isinstance(sent, str) and isinstance(stored, str):
        return sent == stored
    return True


def _resume_result_view(tool: str, args: dict, result: Any) -> str:
    """The result as the resume turn's model sees it: a record write is read
    back field by field (what it saved, what did not land); anything else is
    its JSON, capped."""
    rec = result.get("record") if isinstance(result, dict) else None
    if tool in _RECORD_WRITES and result.get("ok") and isinstance(rec, dict):
        sent = (args or {}).get("fields") or {}
        saved: dict[str, Any] = {}
        not_saved: list[str] = []
        for k, v in sent.items():
            if k not in rec or not _landed(v, rec.get(k)):
                not_saved.append(k)
                continue
            shown = rec.get(k)
            if isinstance(shown, dict):
                shown = shown.get("itemValue") or shown.get("name") or shown.get("@id")
            if isinstance(shown, str) and len(shown) > _SAVED_VALUE_MAX:
                shown = f"{shown[:_SAVED_VALUE_MAX]}... ({len(shown)} characters saved)"
            saved[k] = shown
        view: dict[str, Any] = {k: result.get(k) for k in ("ok", "action", "module", "iri")
                                if result.get(k) is not None}
        view["record_name"] = rec.get("name")
        view["saved"] = saved
        if not_saved:
            view["not_saved"] = not_saved
            view["not_saved_note"] = ("the record does not hold what was sent for "
                                      "these fields: the write did not change them")
        result = view
    try:
        text = json.dumps(result, indent=2, default=str)
    except Exception:
        text = repr(result)
    if len(text) > _RESUME_RESULT_MAX:
        text = (text[:_RESUME_RESULT_MAX]
                + f"\n... [{len(text) - _RESUME_RESULT_MAX} more characters of the result not shown]")
    return text


def _batch_outcome_message(done: list[tuple[dict, Any]]) -> str:
    """What the rest of an approved batch did, for the resume's model turn.

    Every call is listed with its real outcome, because the model proposed
    them and will otherwise re-issue them (or narrate them as pending).
    """
    lines = [f"The same approval covered {len(done)} more action(s) you "
             "proposed in that turn. They HAVE ALREADY BEEN EXECUTED, in "
             "order:"]
    failed = 0
    for n, (item, res) in enumerate(done, start=1):
        what = item.get("summary") or item.get("tool")
        if isinstance(res, dict) and res.get("ok"):
            ref = res.get("iri") or res.get("uuid") or ""
            lines.append(f"{n}. {what}: done" + (f" ({ref})" if ref else ""))
        else:
            failed += 1
            err = (res.get("message") or res.get("error") or res.get("code")
                   if isinstance(res, dict) else res)
            lines.append(f"{n}. {what}: FAILED -- {err}")
    lines.append("Do NOT call any of them again."
                 + (" Tell me plainly which failed and why; do not claim "
                    "they succeeded." if failed else ""))
    return "\n".join(lines)


def _resume_conversation(config: dict, params: dict, decision: str,
                         storage, provider, user_msg: str | None = None,
                         resume_tag: str | None = None,
                         turn_directive: str | None = None) -> dict:
    """Model A resume: reload the canonical conversation, append the
    decision as a user turn, and run a fresh agent turn.

    When `user_msg` is supplied (e.g. by respond_manual_input) it is used
    verbatim as the appended user turn; otherwise the card/choice decision
    is rendered via _resume_user_message.

    `turn_directive` is guidance for THIS turn only -- it reaches the model
    appended to the resume message, and is deliberately NOT written to the
    canonical conversation. An instruction persisted as a user message is a
    standing order: it outlives the turn it was written for and competes with
    whatever the analyst asks next. That is what stranded a live session (see `_manual_input_user_message`), so keep imperatives here and
    facts in `user_msg`."""
    from fsr_playbooks.llm.provider import Message
    from fsr_playbooks.llm.run_turn import run_agent_turn

    cs = _case_state_mod()

    session_id = _require(params, "session_id")
    card_id = params.get("card_id")
    choice_id = params.get("choice_id")
    note = params.get("note") or ""

    convo = storage.load_conversation(session_id)
    # Anthropic requires the first message to be role=user; the widget may
    # have seeded a leading assistant summary (contract §2 / Phase 5).
    while convo and convo[0].get("role") != "user":
        convo.pop(0)
    if not convo:
        return _err("no_conversation",
                    f"no stored conversation for session {session_id!r}; "
                    "cannot resume -- was there a prior chat_turn?")

    # Materialize the connector's mcp_allowlist for the resume turn, exactly as
    # the chat-turn path does at dispatch. Must run BEFORE the approved-card
    # dispatch below (an approved mcp_<server>__<tool> card is executed
    # deterministically at _execute_approved_card, before the model turn) -- and
    # before the resume turn's own tool slice is computed. Previously absent,
    # so a resume relied on a prior turn having already initialized the
    # materializer in the same worker; a fresh/recycled worker resuming an
    # approval saw no MCP tools at all.
    _apply_mcp_allowlist(config)

    # Persist the answer so history replay shows the card as answered (the pick
    # otherwise lived only in the browser). Best-effort, like the user-turn write.
    if choice_id and decision == "choice":
        _record_choice_answer(storage, session_id, choice_id, params.get("value"),
                              params.get("actor"))

    # Capability-gap re-check, part 1 (§E / spine P3). When the analyst clicks
    # the card's resume button they are saying "I fixed it -- try again". Make
    # that deterministic instead of hoping the model re-probes: bust the
    # framework's availability caches (the 5-min unhealthy TTL / 2-min
    # configured-rows cache would re-serve the stale verdict) and tell the
    # agent explicitly to re-attempt the blocked call. A "skip" alternative
    # keeps the plain choice echo -- nothing to re-check. Part 2 (clearing the
    # persisted capability facts) runs after the CaseState load below.
    capgap_recheck = False
    if choice_id and decision == "choice":
        try:
            gap_card = storage.get_card(session_id, choice_id) or {}
            picked = str(params.get("value") or "")
            if (gap_card.get("type") == "capability_gap"
                    and picked and not picked.startswith("skip")):
                capgap_recheck = True
                # Card ids are minted as capgap_cfg_<connector> /
                # capgap_health_<connector> by the framework's preflight;
                # fall back to clearing all cached verdicts otherwise.
                gap_connector = None
                cid = str(gap_card.get("id") or "")
                for prefix in ("capgap_cfg_", "capgap_health_"):
                    if cid.startswith(prefix):
                        gap_connector = cid[len(prefix):]
                        break
                try:
                    from fsr_playbooks.mcp_server.tools_execution import (
                        forget_connector_availability,
                    )
                    forget_connector_availability(gap_connector)
                except Exception:
                    pass
                try:  # and the per-turn source gate's memo, or the re-check
                    # re-advertises nothing for up to a minute
                    from fsr_soc_triage.source_availability import forget
                    forget()
                except Exception:
                    pass
                if user_msg is None:
                    user_msg = (
                        f"For '{choice_id}', I chose: {picked!r}. I have "
                        f"fixed the missing capability "
                        f"({gap_card.get('missing') or 'it'}) -- re-check it "
                        "now by retrying the operation that was blocked, then "
                        "continue the case from where we left off. Do not "
                        "restart the hunt."
                    )
        except Exception:
            pass

    # A resume is a streaming producer exactly like chat_turn: reserve_next_turn
    # derives the turn AND writes the `turn_start` frame in one atomic write, so
    # a concurrent chat_poll latches this turn immediately. The old
    # `max(list_turns) + 1` wrote no turn_start, so the widget's monotonic
    # `since_turn` fence never advanced and the analyst watched a bare spinner
    # for the whole resume -- 7 minutes, in the case that surfaced this.
    # Reserved BEFORE the approved-card dispatch below so that execution's
    # tool_use/tool_result frames land on this turn's feed.
    turn_idx = storage.reserve_next_turn(session_id)

    # An APPROVED action card executes deterministically, here -- not by asking
    # the model to please call run_op. The old path rendered the approval as a
    # natural-language instruction ("Execute X now… then report the result")
    # and left execution to the model's discretion. Live on GA the model
    # ignored it, went hunting for unrelated search ops, and still narrated
    # "the host was successfully isolated" -- an approved containment action
    # that never ran, reported as done, with nothing in the transcript to
    # contradict it. Approval is a human decision about an operation; honouring
    # it must not be probabilistic. Mirrors the framework's legacy tier-3 gate
    # (openai_provider.resume), which has always dispatched directly.
    card_exec_frames: list[dict] = []
    if card_id and decision == "approve" and user_msg is None:
        user_msg, card_exec_frames = _execute_approved_card(
            storage, session_id, turn_idx, card_id, params)

    if user_msg is None:
        user_msg = _resume_user_message(storage, session_id, card_id,
                                        choice_id, decision, params, note)

    from .history_replay import replay_turns
    messages = [Message(role=role, content=content)
                for role, content in replay_turns(convo)]
    # The model sees facts + directive; only `user_msg` (the facts) is persisted
    # further down. Keep these two assembled in one place so they cannot drift.
    messages.append(Message(
        role="user",
        content=f"{user_msg}\n{turn_directive}" if turn_directive else user_msg))

    # Resume under the same intent the session was opened with (triage|build)
    # so the prompt + tool slice stay consistent across the HITL gap.
    try:
        intent = storage.get_session_intent(session_id) or "build"
    except Exception:
        intent = "build"

    # Recover the session's persona (resume carries no entity). When a persona is
    # active it stays authoritative across the HITL gap: its prompt + tool slice,
    # no case-state grounding directive. None ⇒ built-in resume below.
    #
    # Prefer the persona persisted at turn time (_store_turn_profile) -- a
    # deterministic re-bind that needs no live client. Re-resolving from the
    # mounted module via the Key Store is the fallback, and a cold resume worker
    # can fail it, yielding no persona: that is what silently broke every
    # approved tier-3 run_playbook (_run_playbook_gate refuses without an active
    # persona, so the approval was consumed and nothing ran). Same guard
    # _resume_suspended already carries.
    resume_module = None
    try:
        resume_module = storage.get_session_module(session_id)
    except Exception:
        resume_module = None
    profile = _load_resume_profile(storage, session_id)
    if profile is None:
        profile = _resolve_profile(resume_module, session_id)
    _write_profile = profile
    if _write_profile is None:
        # Same fallback chat_turn binds (#148): keep the approved record write
        # executable on a plain triage mount. Bound for dispatch only -- the
        # synthetic profile carries no prompt and must not shadow the
        # grounded/built-in system prompt chosen below (which keys off
        # `profile`, deliberately still None here in that case).
        _write_profile = _host_record_write_profile(resume_module)

    # Load case state for resume. If grounding exists, use it with resume directive
    # (no preflight re-run on resume, ever).
    case_state = None
    system_prompt = _load_prompt(intent)
    if profile is not None and getattr(profile, "prompt", None):
        # Persona prompt is authoritative -- skip the grounding-directive block.
        system_prompt = profile.prompt
    try:
        stored_state = storage.get_case_state(session_id) if cs is not None else None
        case_state = cs.CaseState.from_dict(stored_state) if cs is not None else None
        if (profile is None
                and case_state is not None and case_state.grounding is not None
                and case_state.grounding.system_prompt):
            # Use grounded prompt + resume directive (no preflight).
            resume_directive = (
                "\n\nYou are resuming an in-progress triage -- act on the "
                "analyst's decision; do NOT restart the hunt or re-run tools "
                "you have already run."
            )
            system_prompt = case_state.grounding.system_prompt + resume_directive
    except Exception:
        pass

    # Capability-gap re-check, part 2 (§E / spine P3): clear the learned
    # unavailable facts on the SAME CaseState instance this turn runs and
    # persists -- otherwise the capability guard re-blocks the retry the
    # analyst just asked for.
    if capgap_recheck and case_state is not None:
        try:
            case_state.capabilities.unavailable.clear()
        except Exception:
            pass

    # Capture resume-turn usage too (previously dropped: resume ran with no
    # event hook, so HITL-continuation turns never reached the token profile).
    # The sink is turn_sink.make_event_sink, shared with chat_turn. Its audit
    # identity: the stored intent, the resume text, and the session initiator.
    _audit_provider = _provider_name(provider, config)
    _on_resume_event = make_event_sink(
        storage, config, session_id, turn_idx,
        provider_name=_audit_provider, audit_intent=intent,
        audit_prompt=user_msg or "",
        actor_resolver=lambda: storage.get_session_initiator(session_id))

    # Budget-ask resume: the prior turn hit MAX_TOOL_TURNS and emitted a
    # choice card (_budget_ask) asking the analyst whether to continue with
    # more rounds or deliver now. On "continue" we grant extra tool rounds
    # via max_tool_turns; on "deliver" we strip the tool list so the model
    # is forced to summarize/deliver with no further research.
    _budget_max_turns = None
    _budget_tools_override = None
    if choice_id == "_budget_ask" and decision == "choice":
        _picked = str(params.get("value") or "")
        if _picked == "deliver":
            _budget_tools_override = []
        elif _picked == "continue":
            try:
                from fsr_playbooks.llm._loop_helpers import (
                    BUDGET_CONTINUE_ROUNDS as _bc,
                )
                _budget_max_turns = _bc
            except Exception:
                _budget_max_turns = 8

    # Bind the persona for the resume turn too (an approved create_record/
    # update_record re-dispatches here -- the may_write gate must still see the
    # persona). Reset after.
    _prof_token = _bind_session_state({"profile": _write_profile})
    _offered_token = None
    try:
        with _session_trace_scope(storage, session_id), \
                redaction_scope(storage, config, session_id):
            # For scheduled tasks parked at approval gates, re-apply read_only enforcement
            _resume_read_only = bool(storage.get_session_read_only(session_id)) if hasattr(storage, 'get_session_read_only') else False

            # include_mcp=False: applied at resume entry above.
            # has_open_playbook defaults True -- resume carries no entity, so
            # the signal isn't available here (Phase-2 gap; see the
            # state-derived-intent plan). authoring_active follows from the
            # available signals (has_trace ∨ the fail-open open-playbook
            # default), so a resumed triage-prior turn keeps authoring reach.
            _seam_tools = _advertised_tools(
                config,
                ResolvedPersona(intent=_normalize_intent(intent),
                                profile=profile),
                has_trace=(_rht := _session_has_trace(storage, session_id)),
                authoring_active=_authoring_active(_rht, True),
                include_mcp=False,
                module=resume_module,  # M2: per-page MCP surfacing
                read_only=_resume_read_only)  # re-apply for scheduled task enforcement
            _stripped = _budget_tools_override is not None
            _offered_token = _bind_offered_tools(
                _budget_tools_override if _stripped else _seam_tools)
            result = asyncio.run(run_agent_turn(
                provider=provider,
                system=(_render_prompt_for_tools(
                            system_prompt,
                            _budget_tools_override if _stripped else _seam_tools)
                        + ("" if _stripped
                           else _source_availability_note(_seam_tools))),
                messages=messages,
                tools=(_budget_tools_override if _stripped else _seam_tools),
                tags={"session_id": session_id, "intent": intent,
                      "resume": resume_tag or decision},
                on_event=_on_resume_event,
                history_sink=storage, turn_for_history=turn_idx, session_id=session_id,
                max_tool_turns=_budget_max_turns,
                **_case_state_kwargs(case_state),
            ))
    finally:
        _unbind_session_state(_offered_token)
        _unbind_session_state(_prof_token)
    _check_turn_result(result)  # same seam as chat_turn._finalize (#57)
    _wire_rec = _bind_session_state(  # see chat_turn._finalize
        {"record_iri": _stored_record_iri(storage, session_id)})
    try:
        wire_transcript = _wire_transcript(result.transcript)
    finally:
        _unbind_session_state(_wire_rec)

    # Front the turn's transcript with the approved card's real execution. The
    # dispatch happened before the model turn, so its frames are not in
    # result.transcript -- without this the persisted history (and any rehydrate
    # after a refresh) shows the assistant discussing an action with no record
    # that it ran.
    if card_exec_frames:
        wire_transcript = list(card_exec_frames) + list(wire_transcript)

    # Extend the canonical conversation for any subsequent resume.
    new_convo = [{"role": m["role"], "content": m["content"]} for m in convo]
    new_convo.append({"role": "user", "content": user_msg})
    new_convo.append({"role": "assistant",
                      "content": transcript_blocks(wire_transcript)})
    _persist_session_state(storage, session_id, new_convo, wire_transcript,
                          **_case_state_kwargs(case_state))

    resume_tags = dict(result.tags or {})
    cs_tag = _case_state_summary_tag(storage, session_id, wire_transcript,
                                     case_state)
    if cs_tag is not None:
        resume_tags["case_state"] = cs_tag
    conv_resp = {
        "ok": result.error is None,
        "session_id": session_id,
        "turn_id": uuid.uuid4().hex,
        "stop_reason": _envelope_stop_reason(wire_transcript,
                                             result.stop_reason),
        "contract_version": CONTRACT_VERSION,
        "transcript": wire_transcript,
        "tags": resume_tags,
        "error": result.error,
    }
    _validate_model(ChatResumeResponse, conv_resp)
    # Close the streaming feed for this resume turn, mirroring chat_turn's
    # finalize: without a terminal frame a concurrent chat_poll keeps polling a
    # turn that already returned. The blocking caller gets the transcript from
    # this return regardless. The frame comes from turn_frames, the one writer
    # chat_turn uses too: the terminal frame is the ONLY transcript delivery
    # for a polling consumer, so there is no second hand-written payload to drift.
    write_stream_end(
        storage, session_id, turn_idx, source="resume",
        turn_id=conv_resp["turn_id"], ok=result.error is None,
        stop_reason=conv_resp["stop_reason"], transcript=wire_transcript,
        error=result.error,
        last_assistant_yaml=_safe_turn_field(result, "last_assistant_yaml"),
        tags=resume_tags)
    return conv_resp
