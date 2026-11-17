"""The terminal stream_end frame and the detached error frame, written once.

Every turn feed closes with exactly one terminal frame. The widget's `done`
flag, the poll path's lost-producer check and the detached backstop all read
it. Four sites used to assemble it by hand (chat finalize, the detached error
writer, _resume_suspended, _resume_conversation) and their shapes had drifted.
This module is the one writer. `source` only labels the failure log line.

Shape: built through `_frame(StreamEndFrame, ...)`, i.e. exclude_unset, which
is the chat path's shape. Every key passed explicitly is present, so a None
error or yaml goes out as an explicit null. Resume frames used to drop those
nulls (exclude_none); they now carry them too.
"""
from __future__ import annotations

import traceback
from typing import Any

from .ops_common import CONTRACT_VERSION, _frame, _log
from .pydantic_models import ErrorEvent as ErrorFrame, StreamEndFrame


def write_stream_end(storage, session_id: str, turn: int, *, source: str,
                     turn_id: str, ok: bool, stop_reason: str,
                     transcript: list, error: Any,
                     last_assistant_yaml: str | None, tags: dict,
                     carry_transcript: bool = True) -> bool:
    """Close the turn's feed with the authoritative terminal frame. Returns
    whether the write landed.

    `carry_transcript=False` closes the feed with a signal-only terminal (ok,
    stop reason, turn id, yaml, tags; no transcript). Only a BLOCKING chat_turn
    uses it: its caller already holds the whole transcript from the return, so
    the copy in the feed is dead weight in the feed store and in every poll
    that reads it. A detached turn, and both resume paths, keep the transcript:
    there the terminal frame is the delivery (or the fallback for a dropped
    return). A failure is logged at error level: for a
    detached producer this frame is the only delivery of the transcript."""
    fields = dict(stop_reason=stop_reason, ok=ok, error=error, turn_id=turn_id,
                  contract_version=CONTRACT_VERSION,
                  last_assistant_yaml=last_assistant_yaml, tags=tags)
    if carry_transcript:
        fields["transcript"] = transcript
    try:
        storage.append_turn_progress(
            session_id, turn,
            _frame(StreamEndFrame, type="stream_end", **fields),
            terminal=True, stop_reason=stop_reason)
        return True
    except Exception as exc:  # noqa: BLE001 -- logged, never fails the turn
        _log("%s: terminal stream_end append FAILED session=%s turn=%s: %s: %s"
             % (source, session_id, turn, type(exc).__name__, exc), "error")
        return False


def write_terminal_error(storage, session_id: str, turn: int, *, turn_id: str,
                         exc: BaseException) -> bool:
    """The detached path's terminal ERROR frame. It names the exception class
    and the line that raised it, so the analyst sees a root cause rather than a
    generic "lost contact". Returns whether the write landed, so the backstop
    can escalate when even this fails."""
    detail = "%s: %s" % (type(exc).__name__, exc)
    tb = traceback.extract_tb(exc.__traceback__)
    if tb:
        last = tb[-1]
        detail += " (at %s:%s)" % (last.filename.rsplit("/", 1)[-1], last.lineno)
    try:
        storage.append_turn_progress(
            session_id, turn,
            _frame(StreamEndFrame, type="stream_end", stop_reason="error",
                   ok=False, error=detail, turn_id=turn_id,
                   contract_version=CONTRACT_VERSION,
                   transcript=[_frame(ErrorFrame, type="error", message=detail)]),
            terminal=True, stop_reason="error")
        return True
    except Exception as write_exc:  # noqa: BLE001 -- reported, returns False
        _log("detached: terminal ERROR frame append FAILED session=%s turn=%s: "
             "%s: %s" % (session_id, turn, type(write_exc).__name__, write_exc),
             "error")
        return False
