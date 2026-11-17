"""Shared turn-grading vocabulary — the Python reader.

P0a of `docs/plans/agentic-tooling-best-practices-alignment.md`: three repos had
independently grown turn-drive harnesses that graded a turn differently (or not
at all). The only real grader is the widget harness's ``matrixDriver.js``, which
is JS; this module lets the Python side (connector, framework) speak the SAME
verdict vocabulary without hand-porting the grader.

The vocabulary is language-neutral DATA (`verdict_vocabulary.json`). This file
is a **vendored copy** of the canonical one that ships with the widget harness
(`fortisoar-widget-harness/tests/live/lib/verdict-vocabulary.json`) — the two
repos have no build-time link, so a copy is the only honest option. What makes
the copy safe is `tests/test_verdict_vocab_parity.py`, which diffs it against
the canonical whenever the harness is reachable (set ``FSR_WIDGET_HARNESS`` to
its checkout) and skips-with-warning when it isn't, per the house convention for
guards blocked on an external artifact.

Deliberately NO grading logic here. Porting `evaluate()` would recreate the
drift this exists to kill. This exposes the vocabulary; a caller decides what to
do with it.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

VOCAB_PATH = Path(__file__).with_name("verdict_vocabulary.json")


def _strip_comments(obj: Any) -> Any:
    """Drop the ``$comment`` documentation keys so callers see only data."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items()
                if not k.startswith("$")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


@lru_cache(maxsize=1)
def vocabulary() -> dict[str, Any]:
    """The parsed vocabulary, `$comment` keys removed."""
    return _strip_comments(json.loads(VOCAB_PATH.read_text()))


@lru_cache(maxsize=1)
def error_pattern() -> re.Pattern[str]:
    """The compiled tool-error classifier.

    The JSON carries a JS-flavoured source, restricted to constructs `re`
    accepts unchanged (no lookbehind, no named groups — asserted on the JS side
    by `tests/verdictVocabulary.test.js`).
    """
    spec = vocabulary()["errorPattern"]
    flags = re.IGNORECASE if "i" in spec.get("flags", "") else 0
    return re.compile(spec["source"], flags)


@lru_cache(maxsize=1)
def card_alias() -> dict[str, str]:
    """Card types that are ONE deliverable for acceptance (ioc_card→info_card)."""
    return dict(vocabulary()["cardAlias"])


def canon_card(card_type: str) -> str:
    """Canonicalize a card type through the alias map."""
    return card_alias().get(card_type, card_type)


@lru_cache(maxsize=1)
def parked_terminals() -> frozenset[str]:
    """stop_reasons that mean the turn PAUSED on a human gate having already
    done its work — not that it failed to finish.

    They satisfy a *derived* ``end_turn`` expectation; a row that names
    ``expectTerminal`` explicitly still matches exactly. ``approval_required``
    is deliberately absent: on an auto-approve row it means a SECOND gate was
    raised and nothing ran.
    """
    return frozenset(vocabulary().get("terminals", {}).get("parked", ()))


def is_parked_terminal(stop_reason: str) -> bool:
    return stop_reason in parked_terminals()


@lru_cache(maxsize=1)
def hard_fail_verdicts() -> frozenset[str]:
    """Verdicts that block a soft gate."""
    return frozenset(v["name"] for v in vocabulary()["verdicts"]["ladder"]
                     if v.get("hardFail"))


@lru_cache(maxsize=1)
def drive_error_verdicts() -> frozenset[str]:
    """Verdicts that mean the HARNESS failed, not the agent — these block on
    every gate, xfail included."""
    return frozenset(v["name"] for v in vocabulary()["verdicts"]["ladder"]
                     if v.get("driveError"))


def is_hard_fail(verdict: str) -> bool:
    return verdict in hard_fail_verdicts()


@lru_cache(maxsize=1)
def gates() -> tuple[str, ...]:
    return tuple(vocabulary()["gates"]["names"])


def default_gate() -> str:
    return vocabulary()["gates"]["default"]


def defaults() -> dict[str, int]:
    """``errBudget`` / ``minTools`` — the acceptance floors."""
    return dict(vocabulary()["defaults"])


def is_error_payload(payload: Any) -> bool:
    """Classify a tool_result payload as an error, matching ``matrixDriver.isErr``
    for the structured cases.

    Mirrors the JS ordering exactly: a steering ``guard_redirect`` is not a
    failure, nor is a not-an-error ``code`` such as ``awaiting_input`` (an
    approved ``run_playbook`` parked on a manual_input gate — the run happened
    and is resumable, so it is not a refusal); ``ok:false`` / ``error`` /
    ``exception`` are; ``ok:true`` is a
    success even when it carries a nested "error" STRING (that is data — an
    unconfigured imap in a connector-health listing once red-flagged the whole
    call); otherwise fall back to the shared pattern.
    """
    vocab = vocabulary()
    if isinstance(payload, dict):
        if payload.get("kind") in set(vocab["notAnError"]["kinds"]):
            return False
        if payload.get("code") in set(vocab["notAnError"].get("codes") or ()):
            return False
        if payload.get("ok") is False:
            return True
        if payload.get("error") or payload.get("code") == "error" \
                or payload.get("exception"):
            return True
        if payload.get("ok") is True:
            return False
        return bool(error_pattern().search(json.dumps(payload)))
    return isinstance(payload, str) and bool(error_pattern().search(payload))
