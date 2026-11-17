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
}


def _directive_text(scope: str) -> str:
    return _SCOPE_TEXT[scope] + _BUDGET + _escalation()


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
    if skip:
        return None, "skipped", ""
    if quick_action or not isinstance(text, str) or not text.strip():
        return None, "default", ""   # no typed ask: no model call
    try:
        from fsr_playbooks.llm import ask_scope
    except ImportError:  # older framework: no scope
        return None, "unavailable", ""
    try:
        coro = ask_scope.resolve_scope(
            provider, text, intent=intent or "triage", quick_action=quick_action,
            record=_record_label(entity))
        return loop.run_until_complete(coro) if loop is not None else asyncio.run(coro)
    except Exception:  # noqa: BLE001 - scope is an optimisation, never a failure
        return None, "error", ""


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


def _reset_ask_scope(token: Any) -> None:
    try:
        from fsr_playbooks.llm import ask_scope
        ask_scope.reset_scope(token)
    except Exception:  # noqa: BLE001
        pass
