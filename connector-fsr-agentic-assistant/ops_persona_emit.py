"""Emit a `persona_offer` card: the connector drafts a module-scoped persona and
offers to save it (the EMIT half of the persona builder, docs/PERSONA_BUILDER_PLAN.md).

Deterministic by design: the connector gathers the module's real shape and calls
`persona_author.draft_persona` itself, rather than relying on the model to emit the
card, so the draft quality is the one proven on a box (not in-loop model variance).
The card this produces is exactly what `ops_resume._resume_persona_offer_accept`
consumes on accept -- the accept is the human approval of the tools/write scope it
grants, so nothing is written here.

The chat_turn short-circuit (the `author_persona` quick-action) is a thin caller of
`build_persona_offer`; this module holds the whole logic so it can be tested without
driving a full turn.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from .ops_common import CONTRACT_VERSION, _make_request, _text, _validate_model
from .pydantic_models import ChatTurnResponse


class _MakeRequestClient:
    """A pyfsr-`.get`-shaped shim over the on-box crudhub transport, so
    `persona_author.gather_module_knowledge` (written for a pyfsr client) can read
    the module on a box, where there is no pyfsr client -- only `_make_request`."""

    def __init__(self, make_request: Callable):
        self._mr = make_request

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        url = path
        if params:
            from urllib.parse import urlencode
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        return self._mr(url, "GET")


def _turn(session_id: str, turn_idx: int, frames: list[dict], *,
          saved: bool = False, code: Optional[str] = None) -> dict:
    envelope = {
        "ok": True,
        "session_id": session_id,
        "turn": turn_idx,
        "turn_id": uuid.uuid4().hex,
        "stop_reason": "end_turn",
        "contract_version": CONTRACT_VERSION,
        "transcript": frames,
        "last_assistant_yaml": None,
        "tags": {"session_id": session_id, "intent": "author_persona",
                 "quick_action": "author_persona", "offer": "persona",
                 "saved": bool(saved), "code": code},
        "error": None,
    }
    _validate_model(ChatTurnResponse, envelope)
    return envelope


def _latest_user_text(params: dict) -> str:
    msgs = params.get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
    return ""


def build_persona_offer(config: dict, params: dict, storage, provider, *,
                        turn_idx: Optional[int] = None,
                        fetch: Optional[Callable] = None) -> dict:
    """Draft a persona for the mounted module and return a turn that OFFERS it.

    Stores the drafted card so the accept resume can read it, and returns a
    `ChatTurnResponse` whose transcript carries a `persona_offer` frame. Never
    dead-ends: no mounted module, an unreadable module, or a model that returns
    nothing usable each return an explanatory turn with no offer."""
    from fsr_soc_triage import persona_author

    session_id = params.get("session_id") or "session"
    if turn_idx is None:
        turn_idx = storage.reserve_next_turn(session_id)

    entity = params.get("entity") if isinstance(params.get("entity"), dict) else {}
    module = str(entity.get("module") or params.get("module") or "").strip()
    goal = str(params.get("goal") or _latest_user_text(params) or "").strip()

    if not module:
        return _turn(session_id, turn_idx, [_text(
            "Open a record in the module you want an assistant for, then ask me "
            "to build one -- I shape the persona around that module.")],
            code="no_module")
    if not goal:
        return _turn(session_id, turn_idx, [_text(
            f"Tell me what the assistant for {module} should help with, and I'll "
            f"draft a persona for it.")], code="no_goal")

    mr = fetch or _make_request()
    knowledge = (persona_author.gather_module_knowledge(_MakeRequestClient(mr), module)
                 if mr is not None else {"module": module, "fields": [], "relations": {}})

    rec, issues = persona_author.draft_persona(provider, module, goal, knowledge=knowledge)
    errors = [i for i in (issues or []) if getattr(i, "level", None) == "error"]
    if rec is None or errors:
        return _turn(session_id, turn_idx, [_text(
            "I couldn't put together a solid persona for that just now. Try "
            "describing the task a little more specifically.")], code="draft_failed")

    jv = rec.get("jSONValue") if isinstance(rec.get("jSONValue"), dict) else {}
    label = jv.get("label") or module
    offer_id = f"persona-{uuid.uuid4().hex[:8]}"
    summary = persona_author.summarize(rec)

    card = {"type": "persona_offer", "id": offer_id, "module": module,
            "value": rec["value"], "jSONValue": jv, "label": label,
            "summary": summary}
    try:
        storage.save_card(session_id, card)
    except Exception:  # noqa: BLE001 - a save-card failure must not fail the turn
        pass

    lead = (f'I drafted an assistant persona for {module}. Here is what it would '
            f'do and what it can use -- save it and it applies whenever the '
            f'assistant opens a {module} record.')
    return _turn(session_id, turn_idx,
                 [_text(f"{lead}\n\n{summary}"), card],
                 code="persona_offered")
