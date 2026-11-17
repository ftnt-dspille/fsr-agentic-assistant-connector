"""Model-provider redaction: the connector's seam.

The masking itself is the framework's (`fsr_playbooks.llm.redaction`, applied at
the one provider boundary in the agent loop). This module decides WHETHER a turn
is redacted (the `redaction_mode` configuration field), loads and saves the
session's value map so a placeholder means the same value in every turn and in a
resume on another worker, and binds the redactor for the duration of a turn.

Mode `off` (the default) binds nothing: no import, no storage call, the wire is
what it always was. Redaction that was asked for and cannot be provided fails the
turn instead of sending the text unmasked.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

MODES = ("off", "standard", "strict")

# Used when the session store has no redaction table (a test double, an older
# store). Per process, so correct for one worker only.
_FALLBACK_MAPS: dict[str, str] = {}


def redaction_mode(config: Any) -> str:
    """The configured mode, normalised. Anything unrecognised is `off`."""
    raw = (config or {}).get("redaction_mode") if isinstance(config, dict) else None
    mode = str(raw or "off").strip().lower()
    return mode if mode in MODES else "off"


def _free_text_fields(config: Any) -> frozenset[str]:
    raw = (config or {}).get("redaction_free_text_fields") if isinstance(config, dict) else ""
    return frozenset(p.strip() for p in str(raw or "").split(",") if p.strip())


def _load_state(storage: Any, session_id: str) -> dict | None:
    raw = None
    getter = getattr(storage, "get_session_redaction", None)
    try:
        raw = getter(session_id) if callable(getter) else _FALLBACK_MAPS.get(session_id)
    except Exception:  # noqa: BLE001 - an unreadable map starts fresh
        raw = None
    try:
        state = json.loads(raw) if raw else None
    except ValueError:
        state = None
    return state if isinstance(state, dict) else None


def _save_state(storage: Any, session_id: str, redactor: Any) -> None:
    payload = json.dumps(redactor.to_dict())
    setter = getattr(storage, "set_session_redaction", None)
    if callable(setter):
        setter(session_id, payload)
    else:
        _FALLBACK_MAPS[session_id] = payload


@contextmanager
def redaction_scope(storage: Any, config: Any, session_id: str | None,
                    entity: Any = None) -> Iterator[Any]:
    """Bind the session's redactor for the model calls made inside the block.

    Yields the redactor, or None when the mode is off (nothing is bound). The
    mounted record's typed fields teach the map its hostnames and usernames
    before the first model call. The map is saved on the way out, also when the
    turn raised, so a placeholder the provider has seen is never lost."""
    mode = redaction_mode(config)
    if mode == "off" or not session_id:
        yield None
        return
    try:
        from fsr_playbooks.llm import redaction as fw
    except ImportError as exc:
        raise RuntimeError(
            "Model provider redaction is enabled, but the installed fsr_playbooks "
            "does not support it. The turn was not sent to the model.") from exc
    redactor = fw.Redactor(mode, state=_load_state(storage, session_id),
                           free_text_fields=_free_text_fields(config))
    if isinstance(entity, dict) and isinstance(entity.get("fields"), dict):
        redactor.learn_record(entity["fields"])
    token = fw.bind(redactor)
    try:
        yield redactor
    finally:
        fw.reset(token)
        if redactor.dirty:
            try:
                _save_state(storage, session_id, redactor)
            except Exception:  # noqa: BLE001 - never mask the turn's own outcome
                pass
