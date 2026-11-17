"""turn_io domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
from typing import Any
from .pydantic_models import (
    ActivityEvent as ActivityFrame,
    ApprovalRequestEvent as ApprovalRequestFrame,
    ErrorEvent as ErrorFrame,
    TextEvent as TextFrame,
    ToolResultEvent as ToolResultFrame,
    ToolUseEvent as ToolUseFrame,
    UnknownEvent as UnknownFrame,
    UsageEvent as UsageFrame,
    TurnInputsFrame,
)
from .wire_enrichment import _ioc_cards

from .ops_cards import (
    _CARD_STOP_REASON, _UNATTENDED_INFORMATIONAL_CARDS, _always_allow_offer,
    _non_gating_cards,
    _approval_to_action_card, _emitted_card, _enrich_action_card,
    _manual_input_card_from_awaiting, _playbook_run_context, _push_card_from_tool_result,
    _run_card_from_tool_result, _splice_verdict_cards, _status_card_from_tool_result,
    _triage_summary_fallback_card,
)
from .ops_common import _as_frame, _frame, _frame_model_for, _log
from .ops_usage import _total_input_tokens, _usage_event_cost_usd
from .tool_labels import tool_label



# Cap for tool_result preview content written to the streaming feed (the
# authoritative transcript is uncapped). Keeps poll rows + the terminal frame
# from being bloated by large get_record/search payloads.
_FEED_RESULT_CAP = 2000

# Silence threshold (seconds) after which chat_poll declares a producerless
# turn lost and synthesizes a terminal error frame. MUST exceed the per-LLM
# round ceiling (fsr_playbooks STREAM_TIMEOUT_SECS = 300) so a slow-but-alive round
# that emits no frames is not mistaken for a dead detached producer.
_STREAM_STALE_SECS = 420


def _event_to_wire(ev: Any) -> dict | None:
    """Serialize an fsr_playbooks Event into the widget contract shape.

    Field names match
    `FSR_PLAYBOOK_BUILDER_CONNECTOR_CONTRACT.md`:
    - top-level discriminator is `type` (not `kind`)
    - tool_use: `id` + `input`
    - tool_result: `tool_use_id` + `content`
    - usage: `input_tokens` / `output_tokens` / `cost_usd`

    LadderEvent and DoneEvent return None -- they're harness/telemetry,
    not widget-renderable; the loop's stop_reason already carries the
    DoneEvent payload at the envelope level."""
    from fsr_playbooks.llm.provider import (
        ApprovalRequestEvent, DoneEvent, ErrorEvent,
        TextEvent, ToolResultEvent, ToolUseEvent, UsageEvent,
    )
    try:
        from fsr_playbooks.llm.provider import NoticeEvent
    except ImportError:   # framework older than the NoticeEvent contract
        NoticeEvent = ()   # type: ignore[assignment,misc]
    try:
        from fsr_playbooks.llm.provider import ToolStartEvent
    except ImportError:   # framework older than the early tool-chip contract
        ToolStartEvent = ()   # type: ignore[assignment,misc]
    try:
        from fsr_playbooks.llm.provider import TurnInputsEvent
    except ImportError:   # framework older than the turn-inputs record
        TurnInputsEvent = ()   # type: ignore[assignment,misc]
    if TurnInputsEvent and isinstance(ev, TurnInputsEvent):
        # What the model was given. A diagnosis record, not a display frame:
        # the widget ignores a type it has no branch for.
        return _frame(TurnInputsFrame, type="turn_inputs", model=ev.model,
                      tools=list(ev.tools), intent=ev.intent or None,
                      system_chars=ev.system_chars, slices=list(ev.slices))
    if isinstance(ev, TextEvent):
        return _frame(TextFrame, type="text", text=ev.text)
    if ToolStartEvent and isinstance(ev, ToolStartEvent):
        # The model has started the tool block (C4): the chip goes out now,
        # before its arguments are complete. The full call follows at dispatch
        # under the same id, and the sink does not show it a second time. emit_*
        # calls never show as chips (the widget skips them), so none here.
        if ev.name.startswith("emit_"):
            return None
        label = tool_label(ev.name)
        return _frame(ToolUseFrame, type="tool_use", id=ev.call_id,
                      name=ev.name, input={}, tier=0,
                      **({"label": label} if label else {}))
    if isinstance(ev, ToolUseEvent):
        label = tool_label(ev.name)
        return _frame(ToolUseFrame, type="tool_use", id=ev.call_id,
                      name=ev.name, input=ev.arguments,
                      tier=ev.tier, **({"label": label} if label else {}))
    if isinstance(ev, ToolResultEvent):
        # The feed is a preview, not the source of truth (the authoritative
        # card-injected transcript rides the stream_end frame). Large
        # get_record/search payloads would bloat every poll row and the
        # terminal frame, so cap the preview content here. The full result
        # still reaches the widget via the authoritative transcript.
        content = ev.result
        if isinstance(content, str) and len(content) > _FEED_RESULT_CAP:
            content = content[:_FEED_RESULT_CAP] + "…[truncated]"
        frame = _frame(ToolResultFrame, type="tool_result",
                       tool_use_id=ev.call_id, content=content)
        # Server-side wall-time (ms) of the dispatch, so the widget can freeze
        # a per-tool duration on the result chip and flag the slowest tool.
        # None on the approval-resume path; omit so the widget keeps any
        # client-side estimate it computed from the PENDING ticker.
        if getattr(ev, "duration_ms", None) is not None:
            frame["duration_ms"] = ev.duration_ms
        return frame
    if isinstance(ev, ApprovalRequestEvent):
        # Legacy event; widget still supports it via the `approval_required`
        # stop_reason. The contract's preferred path is `action_card`.
        summary = ev.summary
        context = None
        playbook_url = None
        always_allow = None
        if ev.tool == "run_playbook":
            # What is about to run, and whether it has been running already.
            # Appended to the summary (display-only text) so it surfaces on the
            # CURRENT widget, and carried structurally in `context` so a future
            # card layout can render it properly. Never replaces a summary.
            args = ((ev.preview or {}).get("args")
                    if isinstance(ev.preview, dict) else None) or {}
            context = _playbook_run_context(
                str(args.get("playbook") or ""),
                str(args.get("playbook_uuid") or ""))
            if context:
                bits = [f"Last run: {context['last_run']}"]
                if context.get("runs_24h") is not None:
                    n = context["runs_24h"]
                    bits.append(f"{n} run{'' if n == 1 else 's'} in the last 24h")
                summary = " · ".join([summary] + bits) if summary \
                    else " · ".join(bits)
                # Surfaced as its own field, not inside the summary: the summary
                # is plain-text interpolated by the widget, so a URL there would
                # render as unclickable text.
                playbook_url = context.get("playbook_url") or None
            # Computed once, on the event's first conversion: that one runs
            # inside the turn, with its persona bound. The final transcript
            # converts the same event again AFTER the persona is reset, and a
            # fresh computation there found no persona: live, the card (and the
            # stored copy the grant reads) never carried the offer.
            if not hasattr(ev, "_always_allow"):
                ev._always_allow = _always_allow_offer(str(args.get("playbook") or ""))
            always_allow = ev._always_allow
        return _frame(ApprovalRequestFrame, type="approval_request",
                      approval_id=ev.approval_id,
                      tool_use_id=ev.tool_use_id,
                      tool=ev.tool, tier=ev.tier,
                      preview=ev.preview, args_hash=ev.args_hash,
                      summary=summary, context=context,
                      playbook_url=playbook_url,
                      requires_step_up=ev.requires_step_up,
                      batch=list(getattr(ev, "batch", None) or []) or None,
                      always_allow=always_allow,
                      # getattr: a framework older than the field has none.
                      **({"policy": ev.policy}
                         if getattr(ev, "policy", None) else {}))
    if isinstance(ev, UsageEvent):
        # cache_read/cache_write ride the wire too: without them a consumer
        # (eval harness, usage UI) cannot tell a 99%-cached turn from a cold one,
        # and cost_usd alone hides why two same-token turns differ in price.
        # stop_reason + dropped_calls: a round cut off at the output cap was
        # invisible in chat_history -- only its token count survived.
        #
        # thinking: the model's reasoning summary for this round (Anthropic
        # summarized thinking). Carried only when the model emitted one, so
        # standard-model turns keep the lean usage row they had before. It
        # rides the usage frame (not a new event type) so it reaches both the
        # live poll feed and the persisted transcript, and chat_history
        # rehydrates the reasoning panel with no further change.
        thinking = (getattr(ev, "thinking", "") or "").strip()
        return _frame(UsageFrame, type="usage",
                      model=ev.model,
                      stop_reason=getattr(ev, "stop_reason", None),
                      dropped_calls=_dropped_calls(ev),
                      **({"thinking": thinking} if thinking else {}),
                      input_tokens=ev.input_tokens,
                      output_tokens=ev.output_tokens,
                      cache_read=getattr(ev, "cache_read", 0) or 0,
                      cache_write=getattr(ev, "cache_write", 0) or 0,
                      # Normalized so no consumer re-derives the provider
                      # shape: total_input counts every input token (cached
                      # ones too) and total_tokens = total_input + output.
                      total_input=_total_input_tokens(
                          ev.model, ev.input_tokens,
                          getattr(ev, "cache_read", 0) or 0,
                          getattr(ev, "cache_write", 0) or 0),
                      total_tokens=_total_input_tokens(
                          ev.model, ev.input_tokens,
                          getattr(ev, "cache_read", 0) or 0,
                          getattr(ev, "cache_write", 0) or 0)
                      + int(ev.output_tokens or 0),
                      cost_usd=_usage_event_cost_usd(ev))
    if isinstance(ev, ErrorEvent):
        return _frame(ErrorFrame, type="error", message=ev.message)
    if NoticeEvent and isinstance(ev, NoticeEvent):
        # A non-terminal status line (e.g. waiting out a provider rate limit).
        # Rendered as an activity frame -- the turn is still running.
        return _frame(ActivityFrame, type="activity", message=ev.message,
                      phase=getattr(ev, "reason", "") or None)
    if isinstance(ev, DoneEvent):
        return None
    return _frame(UnknownFrame, type="unknown", repr=repr(ev))


def _dropped_calls(ev: Any) -> list[dict]:
    """Tool calls a round started but never ran (cut off at the output cap).

    `getattr` because the framework only grew `UsageEvent.dropped_calls` in
    0.6.46; an older wheel simply reports none."""
    return [{"name": d.name, "arg_chars": d.arg_chars, "tail": d.tail}
            for d in (getattr(ev, "dropped_calls", None) or [])]


def _turn_tool_calls(ev: Any) -> list[dict]:
    """`chat_turns.tool_calls_json` for one UsageEvent -- ONE builder for both
    writers (live turn and resume). Dropped calls ride along marked
    `dropped: true` so the row shows what a truncated round was writing."""
    rows = [{"name": t.name, "args_chars": t.args_chars,
             "result_chars": t.result_chars} for t in ev.tool_calls]
    rows += [{"name": d["name"], "args_chars": d["arg_chars"], "result_chars": 0,
              "dropped": True, "tail": d["tail"]} for d in _dropped_calls(ev)]
    return rows


# ── Per-tool-call audit capture (agentic-assistant-monitor §10.2) ──────────────
# The UsageEvent.tool_calls list carries only char counts; the actual params
# + result live on the ToolUseEvent / ToolResultEvent frames that precede it
# in the stream. These helpers turn a buffered ToolResultEvent into the
# audit-row fields (status, summary, latency) so record_agent_tool_calls can
# persist the expandable tool chain.

_AUDIT_RESULT_CAP = 600  # audit-row result preview cap (full result rides the transcript)


def _tool_result_status(result) -> str:
    """Derive success/error from a ToolResultEvent.result. The framework's
    tool-dispatch wraps errors as {"error": "..."} (tools.py:1303) and approval-
    skips as {"ok": False, "code": "superseded_by_approval"} (run_turn.py:460);
    everything else is a successful result. Non-dict results (strings, lists)
    are successes."""
    if not isinstance(result, dict):
        return "success"
    if result.get("ok") is False:
        return "error"
    err = result.get("error")
    if isinstance(err, str) and err:
        return "error"
    return "success"


def _summarize_result(result, cap: int = _AUDIT_RESULT_CAP) -> str:
    """A short, human-readable preview of a tool result for the audit row. The
    full result already rides the session transcript; this is the expandable
    row's one-line summary. Keeps JSON compact and caps length."""
    try:
        if result is None:
            return ""
        if isinstance(result, str):
            s = result
        else:
            s = json.dumps(result, default=str)
        return s if len(s) <= cap else s[:cap] + "…"
    except Exception:
        return str(result)[:cap]


def _wire_transcript(transcript: list[Any], unattended: bool = False) -> list[dict]:
    """Apply _event_to_wire across the transcript and drop the Nones
    (DoneEvent doesn't have a widget rendering).

    Post-process (two card surfaces, both end at an `action_card`):

    1. **Explicit emitter** -- the agent called a widget card emitter
       (emit_choice_card / emit_action_card / emit_manual_input). Splice
       the card right after its tool_result.
    2. **Auto-carded mutation** -- the agent called `run_op` on a mutating
       op, which the tier-3 gate suspended into an `approval_request`. The
       analyst should NOT have to know the agent must use emit_action_card;
       a mutating `run_op` is rendered as an `action_card` automatically
       (id = approval_id, args = the run_op params). The agent just runs
       the natural tool; the framework handles approval.

    Either way the card is TRUNCATED to be the last event of the turn
    (contract invariant for `awaiting_*`). The LAST delivered card wins.

    `unattended`: nobody is there to answer a choice or a capability gap, so on
    such a turn those are informational, spliced like a verdict and never the
    truncation point; the turn goes on to its verdict. An approval still gates."""
    wired: list[dict] = []
    for ev in transcript:
        wire = _event_to_wire(ev)
        if wire is not None:
            wired.append(wire)

    # Inject non-gating connector-health status_cards (contract 2.1.0 §5)
    # right after the tool_result that carries the health info. These ride in
    # the same turn, never change stop_reason, and don't truncate -- so a
    # `connector_unhealthy` preflight failure (or an explicit healthcheck)
    # renders as a card instead of buried error text. We attribute each
    # tool_result to its tool_use name via tool_use_id.
    _tool_name_by_id = {w.get("id"): w.get("name") for w in wired
                        if w.get("type") == "tool_use"}
    # Per-tool_result single-card mappers (health / push / run-result).
    _single_mappers = (_status_card_from_tool_result,
                       _push_card_from_tool_result,
                       _run_card_from_tool_result)
    # IOC enrichment is aggregated across the whole turn (one card per
    # indicator), so resolve its insert positions up front.
    _ioc_by_after: dict[int, list[dict]] = {}
    for after_idx, card in _ioc_cards(wired):
        _ioc_by_after.setdefault(after_idx, []).append(card)

    augmented: list[dict] = []
    for src_idx, w in enumerate(wired):
        augmented.append(w)
        tname = _tool_name_by_id.get(w.get("tool_use_id"))
        for mapper in _single_mappers:
            card = mapper(w, tname, len(augmented))
            if card is not None:
                # These cards are built FROM a tool_result -- that result's own
                # tool_use_id is the visible (non-emit_) call the card reports
                # on, so the widget can nest the card under it.
                card["preceding_tool_use_id"] = w.get("tool_use_id")
                augmented.append(card)
                break
        for card in _ioc_by_after.get(src_idx, []):
            card["preceding_tool_use_id"] = w.get("tool_use_id")
            augmented.append(card)
    wired = augmented

    # The verdict card is NOT gating: it is the finding, and a turn that stages
    # a block card owes it too. Splice every delivered verdict in place first,
    # so "first card wins" below only ever picks among GATING cards -- before
    # this, a verdict emitted after an action card was cut off (ka_c2_beacon:
    # the analyst got "block this" with no finding), and one emitted before
    # it cut the action card off instead.
    non_gating = _non_gating_cards(unattended)
    wired, verdict_cards = _splice_verdict_cards(wired, non_gating)
    # Only a verdict ends a turn that has no gating card; an informational card
    # (unattended) must not.
    true_verdicts = {id(w) for w in wired
                     if id(w) in verdict_cards and w.get("type") == "verdict_card"}

    def _close_on(prefix: list[dict], card: dict) -> list[dict]:
        """Truncate at a gating card, which stays the turn's LAST event. A
        verdict delivered after it moves in front of it rather than vanish.

        Usage frames from the truncated tail are KEPT (before the card): the
        per-round DB rows are written from the event stream before this
        truncation, so dropping the tail's usage frames made the returned
        envelope's token/cost totals undercount a card turn by the rounds after
        the gating card, disagreeing with the authoritative DB. The card stays
        last; usage frames carry no visible chat content, so their position is
        immaterial to rendering."""
        tail = wired[len(prefix):]
        late = [w for w in tail if id(w) in verdict_cards]
        usage_tail = [w for w in tail if w.get("type") == "usage"
                      and id(w) not in verdict_cards]
        return prefix + late + usage_tail + [card]

    # A run_playbook/resume_playbook result that PAUSED on a fillable form gates
    # the turn deterministically: synthesize a manual_input card from its
    # `awaiting` block and truncate after that tool_result. This takes precedence
    # over the emit-based path below -- the run is genuinely waiting on the human,
    # so the form is the turn's terminal card. (No fields ⇒ a pure button gate the
    # model resumes itself; _manual_input_card_from_awaiting returns None.)
    for i, w in enumerate(wired):
        if w.get("type") != "tool_result":
            continue
        # Normally gate on the emitting tool's name. BUT on the tier-3 approval
        # resume path the run_playbook `tool_use` lives in the PRIOR turn (before
        # the approval gate), so this resume turn's transcript opens with an
        # ORPHAN tool_result whose tool_use_id isn't in `_tool_name_by_id` -- the
        # name resolves to None. Gating strictly on the name then skips it and the
        # manual-input form never becomes a card (the run pauses, the model just
        # narrates "what value?"). So: a KNOWN name must be a playbook tool, but an
        # UNKNOWN (orphan) tool_result is allowed through -- the content itself is
        # self-identifying, `_manual_input_card_from_awaiting` only builds a card
        # for a genuine `awaiting_input` result carrying fillable fields.
        _tname = _tool_name_by_id.get(w.get("tool_use_id"))
        if _tname is not None and _tname not in ("run_playbook", "resume_playbook"):
            continue
        card = _manual_input_card_from_awaiting(
            w.get("content"), w.get("tool_use_id"), len(wired))
        if card is not None:
            return _close_on(wired[: i + 1], card)

    # Find each card emitter's tool_use and its matching tool_result. The LAST
    # successfully delivered gating card wins: an earlier one in the same turn
    # was superseded (build_plain_request_no_record: a capability_gap card, then
    # a verified playbook_offer -- "first wins" handed the analyst the gap card
    # and cut the deployable offer). Superseded cards are never spliced; the
    # winner is truncated to be the turn's last event.
    winner: "tuple[int, dict] | None" = None
    pending_card: dict | None = None
    pending_tool_use_id: str | None = None
    pending_preceding_tool_use_id: str | None = None
    for i, w in enumerate(wired):
        emitted = _emitted_card(w)
        if emitted is not None and emitted[0] in non_gating:
            continue
        if emitted is not None:
            pending_tool_use_id = w.get("id")
            card_type, input_args = emitted
            # emit_* tool_use frames are never rendered by the widget -- walk
            # back to the last VISIBLE (non-emit_) tool call so the card can
            # be nested under the investigation step it actually reports on.
            pending_preceding_tool_use_id = None
            for j in range(i - 1, -1, -1):
                prev = wired[j]
                if (prev.get("type") == "tool_use"
                        and _emitted_card(prev) is None):
                    pending_preceding_tool_use_id = prev.get("id")
                    break
            # Built THROUGH its model like every other outbound frame. This was
            # the last raw `{"type": ...}` literal on a wire path, and it is the
            # one all seven emit_* card types take. `_as_frame` is
            # identity-preserving (exclude_unset), so the emitted dict is the
            # same one the literal produced. The model is derived from the
            # PollFrame union, not hand-listed -- see `_frame_model_for`.
            card_payload = {"type": card_type,
                            "preceding_tool_use_id": pending_preceding_tool_use_id,
                            **input_args}
            card_model = _frame_model_for(card_type)
            pending_card = (_as_frame(card_model, card_payload)
                            if card_model is not None else card_payload)
            continue
        if (pending_card is not None
                and w.get("type") == "tool_result"
                and w.get("tool_use_id") == pending_tool_use_id):
            content = w.get("content")
            # A refusal, or a guard's deferral (`ok: True` but NOT RUN -- e.g. a
            # second card after an approval card is staged), delivered nothing.
            if isinstance(content, dict) and (
                    content.get("ok") is False
                    or content.get("kind") in ("guard_defer", "guard_redirect")):
                pending_card = None
                pending_tool_use_id = None
                pending_preceding_tool_use_id = None
                continue
            if isinstance(content, dict) and isinstance(content.get("card"), dict):
                pending_card = dict(content["card"])
                pending_card.setdefault("preceding_tool_use_id",
                                        pending_preceding_tool_use_id)
            if pending_card.get("type") == "action_card":
                pending_card = _enrich_action_card(pending_card)
            winner = (i, pending_card)
            pending_card = None
            pending_tool_use_id = None
            pending_preceding_tool_use_id = None
    if winner is not None:
        # Keep events up to and including the winner's tool_result, then the card.
        i, card = winner
        return _close_on(wired[: i + 1], card)

    # Auto-card a suspended mutating run_op: convert its approval_request
    # into an action_card and truncate after it.
    call_inputs = {w.get("id"): w.get("input") for w in wired
                   if w.get("type") == "tool_use" and w.get("id")}
    for i, w in enumerate(wired):
        if w.get("type") == "approval_request":
            card = _approval_to_action_card(w, call_inputs.get(w.get("tool_use_id")))
            if card is not None:
                return _close_on(wired[:i], _enrich_action_card(card))

    # No gating card: the first verdict still ends the turn, as it always has.
    for i, w in enumerate(wired):
        if id(w) in true_verdicts:
            return wired[: i + 1]

    # Deliverable fallback: a triage/hunt turn that investigated via native MCP
    # tools but produced NO deliverable card still owes the analyst a summary
    # card, not bare prose. When the turn ran >=1 native MCP investigation tool,
    # carries no deliverable-family card, and closed with substantive verdict
    # text, wrap that text as an `info_card` so the deliverable always lands.
    # (native MCP results the ioc synthesizer can't structure -- e.g. incident
    # lookups -- would otherwise vanish into text the widget shows as prose.)
    summary = _triage_summary_fallback_card(wired)
    if summary is not None:
        wired.append(summary)
    return wired


def _with_guard_fires(tags: Any, result: Any, extra: Any = ()) -> dict:
    """Copy the turn's guard fires onto the envelope tags.

    A guard that forces a terminal tool produces a correct-LOOKING turn, so it
    HIDES the tool-description or prompt bug that made it necessary -- that is
    how a first-paragraph-only truncation in `build_registry` survived for
    years. The fires have to reach the wire or we stay blind to them.

    Absent = healthy: the model chose the right tool unaided. Present = that
    guard is still compensating for prose worth fixing. Deciding a guard's
    fate needs these rates, because the eval harness calls `dispatch()`
    directly and never exercises guards at all.

    Only set when non-empty -- healthy turns are the overwhelming majority and
    an always-present empty list is pure envelope weight. `getattr` because an
    older `fsr_playbooks` has no such field.
    """
    out = dict(tags or {})
    fired = list(getattr(result, "guards_fired", None) or [])
    # Connector-side mechanisms (offer salvage) happen after the framework took
    # its snapshot, so the caller hands them in; they ride the same list.
    fired += [m for m in (extra or ()) if isinstance(m, str) and m]
    if fired:
        out["guards_fired"] = fired
    return out


def _envelope_stop_reason(wire_transcript: list[dict],
                          result_stop_reason: str | None,
                          unattended: bool = False) -> str:
    """Higher-precision than _normalize_stop_reason: looks at the
    *wire* transcript so a pending choice/action/manual-input event
    bubbles to the envelope as the awaiting_* stop_reason expected by
    the widget."""
    if not wire_transcript:
        return result_stop_reason or "end_turn"
    last = wire_transcript[-1]
    t = last.get("type")
    # Phase 3.1: a resume that fails the HMAC binding check terminates with an
    # ErrorEvent (so the message renders) but carries the distinct loop reason
    # `approval_unverified`. Surface it ABOVE the generic `error` precedence so
    # the widget can clear the pending approval/action card and prompt a
    # re-issue, rather than showing it as an ordinary turn error.
    if result_stop_reason == "approval_unverified":
        return "approval_unverified"
    if t == "error":
        return "error"
    if t in _CARD_STOP_REASON and not (unattended
                                       and t in _UNATTENDED_INFORMATIONAL_CARDS):
        return _CARD_STOP_REASON[t]
    if t == "approval_request":
        return "approval_required"
    # A tool-level approval suspension (e.g. push_playbook, or any non-run_op
    # tier-gated tool that `_approval_to_action_card` leaves as a legacy
    # approval_request) emits a trailing `usage` frame AFTER the
    # approval_request, so `wire_transcript[-1]` is `usage`, not the
    # approval_request -- the check above misses it. Map the loop's
    # authoritative reason directly so the widget opens the approval modal
    # instead of going idle on a stuck PENDING card. (run_op auto-cards are
    # already caught by `_CARD_STOP_REASON` above and never reach here.)
    if result_stop_reason == "pending_approval":
        return "approval_required"
    if result_stop_reason == "max_turns":
        return "max_turns"
    return result_stop_reason or "end_turn"


def _case_state_mod():
    """The case-state spine needs an fsr_playbooks wheel that ships
    `agent.case_state` and a `run_agent_turn(case_state=…)` kwarg (>= 0.4.14).
    On an older pinned wheel the spine degrades to the pre-spine behavior
    (preflight every turn, static resume prompt) instead of breaking chat --
    fail-open is the design contract (CASE_STATE_SPINE_DESIGN.md)."""
    try:
        from fsr_playbooks.agent import case_state
        return case_state
    except Exception:
        return None


def _case_state_kwargs(case_state: Any) -> dict:
    """kwargs for run_agent_turn: only pass case_state when the feature is
    live -- an older run_agent_turn signature rejects the kwarg entirely."""
    return {"case_state": case_state} if case_state is not None else {}


def _advance_phase(case_state: Any, wire_transcript: list[dict],
                   intent: str | None = None) -> None:
    """Case-state spine P4 -- advance `case_state.phase` from this turn's wire
    events. Runs at the persist chokepoint so every path (blocking turn,
    detached thread, card-exec resume, offer-accept) transitions without new
    call sites. Priority: pushed > contained > offered > awaiting_approval;
    a first eventful turn otherwise moves new → building/investigating."""
    # Approved containment = a tier-3 run_op tool_use whose result came back ok
    # (only the card-exec resume splices tier into the wire event).
    tier3_ids = {ev.get("id") for ev in wire_transcript
                 if ev.get("type") == "tool_use" and ev.get("tier") == 3}
    contained = any(
        ev.get("type") == "tool_result"
        and ev.get("tool_use_id") in tier3_ids
        and isinstance(ev.get("content"), dict)
        and ev["content"].get("ok")
        for ev in wire_transcript)
    pushed = any(ev.get("type") == "info_card"
                 and ev.get("variant") == "playbook_pushed"
                 for ev in wire_transcript)
    offered = any(ev.get("type") == "playbook_offer" for ev in wire_transcript)
    awaiting = any(ev.get("type") == "action_card" for ev in wire_transcript)
    if pushed:
        case_state.phase = "pushed"
    elif contained:
        case_state.phase = "contained"
    elif offered:
        case_state.phase = "offered"
    elif awaiting:
        case_state.phase = "awaiting_approval"
    elif getattr(case_state, "phase", "new") in ("new", ""):
        case_state.phase = "building" if intent == "build" else "investigating"


def _case_state_summary_tag(storage, session_id: str,
                            wire_transcript: list[dict],
                            case_state: Any | None = None) -> dict | None:
    """Compact case-state summary for the envelope `tags` (widget status
    strip): phase, record, scenario, investigation counts, dead connectors.
    Mirrors the persist chokepoint's load+advance (idempotent -- the persist
    path re-advancing the same transcript lands on the same phase), so the
    tag always matches what gets persisted. Fail-open: None on any error."""
    try:
        cs = case_state
        if cs is None:
            cs_mod = _case_state_mod()
            if cs_mod is None:
                return None
            stored = storage.get_case_state(session_id)
            cs = (cs_mod.CaseState.from_dict(stored)
                  if stored is not None else cs_mod.CaseState())
        try:
            intent = storage.get_session_intent(session_id)
        except Exception:
            intent = None
        _advance_phase(cs, wire_transcript, intent=intent)
        inv = getattr(cs, "investigation", None)
        caps = getattr(cs, "capabilities", None)
        grounding = getattr(cs, "grounding", None)
        return {
            "phase": getattr(cs, "phase", "new"),
            "record_key": getattr(cs, "record_key", None),
            "scenario": (getattr(grounding, "scenario_id", None)
                         if grounding else None),
            "searched": len(getattr(inv, "searched", None) or []),
            "enriched": len(getattr(inv, "enriched", None) or []),
            "unavailable_connectors": sorted(
                (getattr(caps, "unavailable", None) or {}).keys()),
        }
    except Exception:  # noqa: BLE001 -- advisory tag, never fail the turn
        return None


# Cards a later chat_resume answers by id, so they must be stored when emitted.
_STORED_CARD_TYPES = ("action_card", "choice_card", "manual_input",
                      "capability_gap", "playbook_offer", "enhancement_offer",
                      # A tier-3 approval for a NON-run_op tool (run_playbook,
                      # resume_playbook, mcp_*) stays an approval_request -- it
                      # renders better than an action_card. Unstored, an approved
                      # run_playbook on .206 produced no run, no result and no
                      # error: the resume only TOLD the model it was approved.
                      "approval_request")


def _save_cards(storage, session_id: str, wire_transcript: list[dict]) -> None:
    """Store every answerable card in ``wire_transcript``. Raises on a storage
    error; callers decide whether that is fatal."""
    for ev in wire_transcript:
        if isinstance(ev, dict) and ev.get("type") in _STORED_CARD_TYPES:
            storage.save_card(session_id, ev)


def _persist_session_state(storage, session_id: str,
                           messages: list[dict],
                           wire_transcript: list[dict],
                           case_state: Any | None = None) -> None:
    """Save the canonical conversation + any emitted cards + case state.
    Best-effort; a persistence failure must never fail the turn the user can
    see. But a silent drop of the canonical conversation strands a later
    chat_resume (which carries no messages[]) with a blind history -- so we
    RETRY transient sqlite errors, WARN on loss, and ESCALATE to error when
    this turn is gating an approval/decision card (a lost gate makes the
    pending decision unresumable)."""
    # A turn that emitted a resume-gating card can't be recovered from a lost
    # conversation -- the widget will chat_resume against a session that has no
    # memory of the pending decision. Flag that so the failure is loud.
    # playbook_offer is persisted so accept can read a direct-build card's
    # final_yaml (§A); the trace-built offer stores harmlessly alongside.
    # enhancement_offer MUST be persisted: accept reads `final_yaml` off the
    # stored card, which is the only copy of the verified bytes once the turn
    # ends (the framework's verified-id registry is per-worker and per-turn).
    _GATE_TYPES = ("action_card", "choice_card", "manual_input",
                   "capability_gap", "playbook_offer", "enhancement_offer")
    import time
    gating = any(ev.get("type") in _GATE_TYPES for ev in wire_transcript)
    # P4 -- phase transition at the single persist chokepoint. Callers that
    # don't thread a CaseState (card-exec resume, offer-accept push) still
    # advance the phase: load the stored state, transition, persist below.
    # Fail-open: a phase we can't compute never fails the turn.
    try:
        cs_obj = case_state
        if cs_obj is None:
            cs_mod = _case_state_mod()
            if cs_mod is not None:
                stored = storage.get_case_state(session_id)
                # A session with no state yet still gets phase tracking
                # (build sessions never run preflight, so nothing else
                # would ever create their CaseState).
                cs_obj = (cs_mod.CaseState.from_dict(stored)
                          if stored is not None else cs_mod.CaseState())
        if cs_obj is not None:
            try:
                intent = storage.get_session_intent(session_id)
            except Exception:
                intent = None
            _advance_phase(cs_obj, wire_transcript, intent=intent)
            case_state = cs_obj
    except Exception:
        pass
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            storage.save_conversation(session_id, messages)
            _save_cards(storage, session_id, wire_transcript)
            # Render-fidelity history: append this turn's full visible transcript
            # so a page refresh can replay the exact timeline (every persist path
            # -- blocking turn, detached thread, card-exec resume, LLM resume --
            # flows through here, so this is the single chokepoint).
            storage.append_transcript_assistant(session_id, wire_transcript)
            # Persist case state (grounding, investigation, capabilities) if present.
            # Best-effort: a case state write failure never breaks the turn.
            if case_state is not None:
                try:
                    storage.save_case_state(session_id, case_state.to_dict())
                except Exception:
                    pass
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            # sqlite "database is locked"/"busy" is transient under concurrent
            # writers; a short retry usually clears it.
            if attempt < 2:
                try:
                    time.sleep(0.05 * (attempt + 1))
                except Exception:
                    pass
                continue
    level = "error" if gating else "warning"
    _log("persist_session_state: DROPPED canonical conversation for "
         "session=%s after 3 attempts%s: %s: %s"
         % (session_id, " (turn GATES an approval -- resume will be blind)"
            if gating else "", type(last_exc).__name__, last_exc), level)
