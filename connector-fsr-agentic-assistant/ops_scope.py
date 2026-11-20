"""Ask scope for a chat turn: the effort matches the ask.

The decision and its enforcement live in the framework (`llm.ask_scope`); this
module is the connector's seam: resolve the scope of the analyst's message,
bind it for the turn, and say it in the prompt. Every failure leaves the turn
unscoped, which is the flow the connector has always run.
"""
from __future__ import annotations

import asyncio
from typing import Any


def _escalation() -> str:
    """The escalation offer, worded by the framework from the card's own
    options. Empty on a framework that predates it."""
    try:
        from fsr_playbooks.llm import ask_scope
        return ask_scope.escalation_directive()
    except (ImportError, AttributeError):
        return ""


_BUDGET = (
    "This turn allows {cap} tool calls in all. Every call counts, one that "
    "fails or finds nothing too, and calls sent together in one round count "
    "one each. When a source comes back empty, try a different kind of "
    "source or stop and say what is missing; do not guess the same lookup "
    "again under another name. An honest \"not found\" is a complete answer.\n")

#: Appended to the turn's system prompt on a narrow turn (with the budget and
#: the escalation offer). It restates what dispatch enforces, so the model does
#: not spend a refused call finding out.
_SCOPE_TEXT = {
    "answer": (
        "\n\n## Scope of this ask: answer\n"
        "The analyst asked a narrow question. Read what answers it (the mounted "
        "record, or one lookup) and reply in a sentence or two with the answer "
        "itself, quoting exact values. This turn has no hunt, no prior-case "
        "sweep, no coverage check and no verdict or action card.\n"),
    "lookup": (
        "\n\n## Scope of this ask: lookup\n"
        "The analyst asked a focused question about one entity. Make the one or "
        "two lookups that answer it (the intel source or log search that fits), "
        "then reply in a few sentences that name the evidence. This turn has no "
        "prior-case sweep, no ticket pivot, no coverage check and no verdict or "
        "action card.\n"),
    "act": (
        "\n\n## Scope of this ask: act\n"
        "The analyst asked you to take an action (block, isolate, update). "
        "Stage it and stop. Find the action with find(kind='action') for the "
        "target, read its schema if you need it, and stage it with "
        "emit_card(card_type='action'); the analyst approves it on the card. "
        "Take the target from the message or the record in hand. Do not "
        "investigate, enrich, search tickets or prior cases, and do not write "
        "a verdict. A value only the analyst can choose (a duration, a mode, "
        "an option) goes on the card: list it in editable_fields and leave it "
        "out of args, and the analyst picks it there. Do not ask for it in "
        "the reply. If no target is named and the record holds none, ask the "
        "analyst for it in one sentence.\n"),
}


def _directive_text(scope: str) -> str:
    # The escalation offer is for a narrow question; an act turn has nothing
    # to escalate.
    return _SCOPE_TEXT[scope] + _BUDGET + (_escalation() if scope != "act" else "")


def _scope_directive(scope: str | None) -> str:
    if scope not in _SCOPE_TEXT:
        return ""
    text = _directive_text(scope)
    from fsr_playbooks.llm import ask_scope
    return text.replace("{cap}", str(ask_scope.max_tool_calls(scope)))


def _record_label(entity: Any) -> str:
    """The record in hand for the declaration: module, name and the NAMES of
    its filled fields (never values), so the model can tell a fact the record
    holds from one that needs a lookup."""
    if not isinstance(entity, dict):
        return ""
    fields = entity.get("fields") if isinstance(entity.get("fields"), dict) else {}
    name = fields.get("name")
    head = f"{entity.get('module') or 'record'}: {name}" if name else \
        str(entity.get("module") or "")
    keys = sorted(k for k, v in fields.items()
                  if v not in (None, "", [], {}) and not str(k).startswith("@"))
    return f"{head}; fields: {', '.join(keys[:40])}" if keys else head


def _resolve_ask_scope(provider: Any, text: Any, *, intent: str,
                       quick_action: Any, skip: bool = False,
                       entity: Any = None, loop: Any = None) -> tuple[str | None, str, str]:
    """(scope, how, reason). `skip` leaves the turn unscoped: a read-only chip
    already narrows itself, and an unattended turn is an investigation by
    design."""
    return _resolve_ask_scope_and_language(
        provider, text, intent=intent, quick_action=quick_action, skip=skip,
        entity=entity, loop=loop)[:3]


def _previous_assistant_text(messages: Any) -> str:
    """The assistant's last message before the latest analyst ask, as text.
    The declaration reads it so a reply to the assistant's own question
    ("6 hours" for a staged block) is labelled by the work it answers."""
    seen_user = False
    for m in reversed(list(messages or [])):
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if role == "user" and not seen_user:
            seen_user = True
            continue
        if role == "assistant" and seen_user:
            if isinstance(content, list):
                content = " ".join(str(b.get("text") or "") for b in content
                                   if isinstance(b, dict))
            text = " ".join(str(content or "").split())
            if text:
                return text[-400:]
    return ""


def _session_reply(storage: Any, session_id: Any) -> tuple[str | None, str | None]:
    """(language, scope) stored for the session; (None, None) on any miss."""
    try:
        got = storage.get_session_reply(session_id)
        return (got[0], got[1]) if got else (None, None)
    except Exception:  # noqa: BLE001 - a memory, never a failure
        return None, None


def _remember_reply(storage: Any, session_id: Any, *, language: str | None,
                    scope: str | None) -> None:
    try:
        storage.set_session_reply(session_id, language=language or None,
                                  scope=scope or None)
    except Exception:  # noqa: BLE001
        pass


def _resolve_ask_scope_and_language(provider: Any, text: Any, *, intent: str,
                                    quick_action: Any, skip: bool = False,
                                    entity: Any = None, loop: Any = None,
                                    previous: str = "", prior_language: str = ""
                                    ) -> tuple[str | None, str, str, str]:
    """(scope, how, reason, language). The language is the one the model said
    the ask is written in, "" when nothing was declared (or the framework
    predates it). A quick-action chip is declared for its language only (the
    framework returns no scope for it)."""
    if skip:
        return None, "skipped", "", ""
    if not isinstance(text, str) or not text.strip():
        return None, "default", "", ""   # no text: no model call
    try:
        from fsr_playbooks.llm import ask_scope
    except ImportError:  # older framework: no scope
        return None, "unavailable", "", ""
    resolve = getattr(ask_scope, "resolve_scope_and_language", None)
    import inspect
    try:
        if resolve is None:   # framework without the declared language
            if quick_action:
                return None, "default", "", ""
            coro = ask_scope.resolve_scope(
                provider, text, intent=intent or "triage",
                quick_action=quick_action, record=_record_label(entity))
        elif "previous" in inspect.signature(resolve).parameters:
            coro = resolve(provider, text, intent=intent or "triage",
                           quick_action=quick_action, record=_record_label(entity),
                           previous=previous, prior_language=prior_language or "")
        else:   # framework that declares the language but takes no context
            if quick_action:
                return None, "default", "", ""
            coro = resolve(provider, text, intent=intent or "triage",
                           quick_action=quick_action, record=_record_label(entity))
        out = loop.run_until_complete(coro) if loop is not None else asyncio.run(coro)
        return tuple(out) if len(out) == 4 else (*out, "")
    except Exception:  # noqa: BLE001 - scope is an optimisation, never a failure
        return None, "error", "", ""


def _close_loop(loop: Any) -> None:
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()


def _bind_ask_scope(scope: str | None) -> Any:
    try:
        from fsr_playbooks.llm import ask_scope
        return ask_scope.bind_scope(scope)
    except Exception:  # noqa: BLE001
        return None


def _bind_reply_language(language: str | None) -> Any:
    try:
        from fsr_playbooks.llm import ask_scope
        return ask_scope.bind_language(language)
    except Exception:  # noqa: BLE001 - older framework: the tail quotes instead
        return None


def _reset_reply_language(token: Any) -> None:
    if token is None:
        return
    try:
        from fsr_playbooks.llm import ask_scope
        ask_scope.reset_language(token)
    except Exception:  # noqa: BLE001
        pass


def _reset_ask_scope(token: Any) -> None:
    try:
        from fsr_playbooks.llm import ask_scope
        ask_scope.reset_scope(token)
    except Exception:  # noqa: BLE001
        pass
