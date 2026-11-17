"""The one event sink for a turn: wire feed, tool and text buffers, usage rows.

Phase 2 Step 2. chat_turn, _resume_conversation and _resume_suspended each
drove the agent loop's on_event hook through a closure of their own. The chat
and conversation closures wrote the same usage rows and differed only in the
per-path audit identity. The suspended closure wrote wire frames and nothing
else. This module is the one body. Each caller passes its own audit identity
explicitly, so the parity test asserts the factory rather than two copies.

Per-path parameters (the only fields that may legitimately differ in an audit
row, see tests/test_audit_row_provider_and_sinks.py):
  audit_intent    chat: the request intent; resume: the stored session intent
  audit_prompt    chat: the latest analyst message; resume: the resume text
  actor_resolver  chat: the request actor; resume: the session initiator
"""
from __future__ import annotations

from typing import Any, Callable

from .ops_common import _log
from .ops_turn_io import (
    _event_to_wire, _summarize_result, _tool_result_status, _turn_tool_calls,
)
from .ops_usage import (
    _log_usage_jsonl, _now_iso, _usage_event_cost_usd, _usage_log_path,
    log_llm_activity,
)


def make_event_sink(storage, config: dict, session_id: str, turn_idx: int, *,
                    provider_name: str, audit_intent: str, audit_prompt: str,
                    actor_resolver: Callable[[], Any],
                    record_usage: bool = True,
                    usage_error_policy: str = "catch_and_log",
                    feed: bool = True) -> Callable[[Any], None]:
    """Build the on_event hook for one agent turn.

    Every event is appended to the poll feed on ``turn_idx`` when ``feed`` is
    true (best-effort: the feed must never break a turn). A UsageEvent writes
    one chat_turns row, the JSONL firehose line, one llm-activity audit row and
    the per-tool-call rows, and only when ``record_usage`` is true.

    ``usage_error_policy`` decides what a raising ``record_chat_turn`` does.
    "catch_and_log" (the default) logs it at error level and carries on: an
    accounting write must never fail a live turn, and the other rows still
    land. "propagate" re-raises it into the agent loop instead.
    """
    if usage_error_policy not in ("catch_and_log", "propagate"):
        raise ValueError(f"unknown usage_error_policy {usage_error_policy!r}")
    from fsr_playbooks.llm.provider import (
        ApprovalRequestEvent, TextEvent,
        ToolResultEvent, ToolUseEvent, UsageEvent)
    try:
        from fsr_playbooks.llm.provider import ToolStartEvent
    except ImportError:   # framework older than the early tool-chip contract
        ToolStartEvent = ()   # type: ignore[assignment,misc]

    tool_buffer: dict = {}
    text_buffer: list[str] = []
    approval_count = 0
    # Call ids already shown as a chip at block start (C4): their dispatch-time
    # ToolUseEvent must not put a second chip on the feed.
    announced: set = set()

    def _actor() -> dict:
        try:
            return actor_resolver() or {}
        except Exception:
            return {}

    def _write_chat_turn(record: dict) -> None:
        if usage_error_policy == "propagate":
            storage.record_chat_turn(record)
            return
        try:
            storage.record_chat_turn(record)
        except Exception as exc:  # noqa: BLE001 -- loud, and the turn goes on
            _log("record_chat_turn FAILED session=%s turn=%s: %s: %s "
                 "(this round's chat_turns row is lost; the turn continues)"
                 % (session_id, turn_idx, type(exc).__name__, exc), "error")

    def _record_usage(ev) -> None:
        nonlocal approval_count
        _write_chat_turn({
            "session": session_id,
            "turn": turn_idx,
            "model": ev.model,
            "input_tokens": ev.input_tokens,
            "output_tokens": ev.output_tokens,
            "cache_read": ev.cache_read,
            "cache_write": ev.cache_write,
            "stop_reason": ev.stop_reason,
            "tags": dict(ev.tags or {}),
            "tool_calls": _turn_tool_calls(ev),
        })
        # The JSONL firehose line feeds the Studio token profiler (path derived
        # from the connector config; see _usage_log_path). Best-effort inside.
        _log_usage_jsonl(ev, _usage_log_path(config))
        # Monitor dual-write: one llm-activity audit row per LLM round-trip plus
        # the agent_usage row. Best-effort.
        try:
            actor = _actor()
            _usage_id = log_llm_activity(config, {
                "ts": _now_iso(),
                "session_id": session_id,
                "model": ev.model,
                "turn": turn_idx,
                "input_tokens": ev.input_tokens,
                "output_tokens": ev.output_tokens,
                "cache_read": ev.cache_read,
                "cache_write": ev.cache_write,
                "cost_usd": _usage_event_cost_usd(ev),
                "intent": audit_intent,
                "status": "success",
                "stop_reason": ev.stop_reason,
                "prompt": audit_prompt or "",
                "response": "".join(text_buffer),
                "agent_thinking": getattr(ev, "thinking", "") or "",
                "approvals_count": approval_count,
                "provider": provider_name,
                "user_iri": actor.get("iri"),
                "user_name": actor.get("name"),
                "tool_calls": [{"name": t.name} for t in ev.tool_calls],
                "tags": dict(ev.tags or {}),
            })
            # Per-tool-call detail rows linked to this round-trip's agent_usage
            # row; then clear the buffers so the next round starts clean.
            try:
                storage.record_agent_tool_calls(
                    _usage_id, turn_idx, list(tool_buffer.values()))
            except Exception:
                pass
            tool_buffer.clear()
            text_buffer.clear()
            approval_count = 0
        except Exception:
            pass

    def _on_event(ev):
        nonlocal approval_count
        shown_early = False
        if ToolStartEvent and isinstance(ev, ToolStartEvent):
            announced.add(ev.call_id)
        elif isinstance(ev, ToolUseEvent) and ev.call_id in announced:
            shown_early = True
        # Every renderable event is also a streaming progress frame.
        if feed and not shown_early:
            try:
                wire = _event_to_wire(ev)
                if wire is not None:
                    storage.append_turn_progress(session_id, turn_idx, wire)
            except Exception:
                pass
        # Tool params and results are buffered by call_id and flushed with the
        # round's usage row (ToolUseEvent / ToolResultEvent precede UsageEvent).
        try:
            if isinstance(ev, ToolUseEvent):
                tool_buffer[ev.call_id] = {
                    "tool_name": ev.name,
                    "params": ev.arguments,
                    "result_status": None,
                    "result_summary": None,
                    "latency_ms": None,
                }
            elif isinstance(ev, ToolResultEvent):
                entry = tool_buffer.get(ev.call_id)
                if entry is None:
                    entry = {"tool_name": "(unknown)", "params": None}
                    tool_buffer[ev.call_id] = entry
                entry["result_status"] = _tool_result_status(ev.result)
                entry["result_summary"] = _summarize_result(ev.result)
                entry["latency_ms"] = getattr(ev, "duration_ms", None)
        except Exception:
            pass
        try:
            if isinstance(ev, TextEvent):
                if ev.text:
                    text_buffer.append(ev.text)
            elif isinstance(ev, ApprovalRequestEvent):
                approval_count += 1
        except Exception:
            pass
        if record_usage and isinstance(ev, UsageEvent):
            _record_usage(ev)

    return _on_event
