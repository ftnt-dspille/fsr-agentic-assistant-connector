"""offers domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

from typing import Any

from .ops_cards import _CARD_STOP_REASON, _emitted_card
from .ops_common import _log

#: Connector-side mechanisms that card a turn the model left uncarded. They ride
#: `tags.guards_fired` next to the framework's (`ops_turn_io._with_guard_fires`).
#: Every non-None return of a `_salvage_*` function goes through `_salvaged`
#: with one of these; `tests/test_salvage_sites_are_recorded.py` closes it.
SALVAGE_BUILD_OFFER = "connector.salvage_build_offer"
SALVAGE_ENHANCEMENT_OFFER = "connector.salvage_enhancement_offer"
SALVAGE_MECHANISMS = frozenset({SALVAGE_BUILD_OFFER, SALVAGE_ENHANCEMENT_OFFER})


class _Salvaged(list):
    """A salvaged wire transcript that remembers which salvage produced it.
    A plain `list` to every reader (equality, slicing, iteration)."""
    mechanism: str = ""


def _salvaged(mechanism: str, wire_transcript: list[dict]) -> "_Salvaged":
    out = _Salvaged(wire_transcript)
    out.mechanism = mechanism
    return out



_YAML_CARRYING_TOOLS = frozenset({"verify_playbook", "validate_yaml",
                                  "verify_enhancement"})
# Arg names those tools carry the authored YAML under. `verify_enhancement`
# uses before_yaml/after_yaml (it verifies a DIFF), so a turn that only called
# it recovered nothing here and the salvage had no YAML to work with -- the
# live step-rename turn called exactly that one tool and nothing else.
_YAML_ARG_KEYS = ("yaml_text", "yaml", "after_yaml")


def _is_whole_playbook_yaml(text: Any) -> bool:
    """True only when `text` parses as YAML AND carries a full `playbooks` list.

    Model-authored tool args CANNOT be trusted to hold a complete document. A
    live rename turn on 0.5.117 passed `verify_enhancement` a document the model
    had elided itself:

        "collection: 00-Workshop\\n... (truncated) ... }'\\n    next: do_we_want\\n"

    That is not a size cap the connector applied -- the MODEL wrote the marker to
    save tokens, and verify_enhancement duly "verified" the stub. Anything we put
    behind an Apply button gets WRITTEN to the analyst's playbook, so a patch
    body must be structurally whole before it is offered, never merely non-empty.
    """
    if not (isinstance(text, str) and text.strip()):
        return False
    try:
        import yaml as _yaml
        doc = _yaml.safe_load(text)
    except Exception:  # noqa: BLE001 -- any parse failure disqualifies it
        return False
    if not isinstance(doc, dict):
        return False
    pbs = doc.get("playbooks")
    return isinstance(pbs, list) and bool(pbs) and all(
        isinstance(p, dict) and p.get("steps") for p in pbs)


def _enhancement_diff(wire_transcript: list[dict]) -> tuple[str, str] | None:
    """The (before, after) pair from the most recent `verify_enhancement` call,
    ONLY when both sides are structurally whole playbook documents.

    When the model elided either side (see `_is_whole_playbook_yaml`) this
    returns None and the caller falls back to the mounted entity + the prose
    fence, which the model writes in full."""
    for w in reversed(wire_transcript):
        if w.get("type") != "tool_use" or w.get("name") != "verify_enhancement":
            continue
        args = w.get("input") or {}
        before, after = args.get("before_yaml"), args.get("after_yaml")
        if _is_whole_playbook_yaml(before) and _is_whole_playbook_yaml(after):
            return before, after
    return None


def _last_authored_yaml(wire_transcript: list[dict]) -> str | None:
    """Extract the most recent YAML the model fed into a verification or
    validation tool call.  When the model authored a real playbook but
    ended the turn with prose instead of an offer card (e.g. verify_playbook
    failed on an environment issue like a missing connector config), this
    recovers the YAML so ``_salvage_build_offer`` can synthesize the offer
    the model should have emitted (#1).
    """
    for w in reversed(wire_transcript):
        if w.get("type") != "tool_use":
            continue
        if w.get("name") not in _YAML_CARRYING_TOOLS:
            continue
        args = w.get("input") or {}
        for key in _YAML_ARG_KEYS:
            yt = args.get(key)
            if isinstance(yt, str) and yt.strip():
                return yt
    return None


def _already_terminated_by_card(wire_transcript: list[dict]) -> bool:
    """True when the turn already ends on a gating card / approval / error --
    i.e. the flow lands on its own and no salvage should fire."""
    if not wire_transcript:
        return False
    last = wire_transcript[-1]
    return (last.get("type") in _CARD_STOP_REASON
            or last.get("type") in ("approval_request", "error"))


def _salvage_enhancement_offer(
    wire_transcript: list[dict],
    *,
    before_yaml: str,
    after_yaml: str,
    session_id: str,
    turn_idx: int,
) -> list[dict] | None:
    """Deterministic backstop for an OPEN-playbook edit turn that authored the
    edited YAML but never carded it.

    Verifies the exact bytes with `verify_enhancement` and cards them through
    `emit_enhancement_offer` -- the same verified_id -> card pair the model is
    told to use -- so the turn halts on the Apply button whose accept writes
    through `update_playbook`'s snapshot-then-PUT, rather than ending on a
    prose claim the analyst has no way to act on. See the call site in
    `_salvage_build_offer` for the live repro.

    Returns None (leave the transcript alone) when the turn already lands on a
    card, or when the YAML is unchanged -- a rename the model only *described*
    and never actually applied to the document is not an edit, and offering an
    identical before/after would be a fake Apply button."""
    if _already_terminated_by_card(wire_transcript):
        return None
    if not (isinstance(before_yaml, str) and before_yaml.strip()):
        return None
    if before_yaml.strip() == after_yaml.strip():
        return None
    # The Apply button WRITES this to the analyst's playbook. A partial document
    # would silently delete every step the model elided, so an edit that isn't
    # structurally whole is dropped -- better an unactioned turn than a
    # one-click playbook truncation.
    if not _is_whole_playbook_yaml(after_yaml):
        _log("salvage: refusing enhancement_offer, after_yaml is not a whole "
             "playbook document (session=%s turn=%s)" % (session_id, turn_idx),
             "warn")
        return None
    # The Apply button also must not carry bytes the gate never passed. Live:
    # this salvage fired right after two failed verifies (a cycle, an
    # unreachable ref) and offered the prose YAML behind a one-click Apply.
    # Same rule as `_salvage_build_offer`: verify the exact bytes, or no card.
    from fsr_playbooks.mcp_server.tools_enhancement import verify_enhancement
    try:
        verdict = verify_enhancement(before_yaml=before_yaml, after_yaml=after_yaml)
    except Exception as e:  # noqa: BLE001
        verdict = {"ready_to_push": False, "error": f"{type(e).__name__}: {e}"}
    verified_id = (verdict or {}).get("verified_id") if isinstance(verdict, dict) else None
    if not (isinstance(verdict, dict) and verdict.get("ready_to_push") and verified_id):
        _log("salvage: refusing enhancement_offer, after_yaml fails verify "
             "(session=%s turn=%s): %s" % (
                 session_id, turn_idx,
                 [f.get("code") for f in (verdict or {}).get("required_fixes") or []]
                 or (verdict or {}).get("error") or (verdict or {}).get("code")),
             "warn")
        return None
    from fsr_playbooks.mcp_server.tools_emit import emit_enhancement_offer
    offer = emit_enhancement_offer(
        id=f"enhance-{session_id}-{turn_idx}",
        summary=("The assistant authored this edit but never staged it. "
                 "Review the changes and Apply to write them to the playbook."),
        verified_id=verified_id,
    )
    if not (isinstance(offer, dict) and offer.get("ok")
            and isinstance(offer.get("card"), dict)):
        return None
    card = dict(offer["card"])
    for w in reversed(wire_transcript):
        if (w.get("type") == "tool_use"
                and _emitted_card(w) is None):
            card["preceding_tool_use_id"] = w.get("id")
            break
    return _salvaged(SALVAGE_ENHANCEMENT_OFFER, wire_transcript + [card])


def _salvage_build_offer(
    wire_transcript: list[dict],
    *,
    intent: str,
    entity: Any,
    last_assistant_yaml: str | None,
    session_id: str,
    turn_idx: int,
    read_only: bool = False,
) -> list[dict] | None:
    """Phase 2.4 -- unify the build-completion path.

    A NEW-playbook authoring turn that ends by *narrating* the finished YAML
    (prose + a ```yaml fence) instead of calling `emit_playbook_offer` leaves
    the analyst with a design and no Deploy button -- the flow dead-ends. The
    build prompt mandates the offer, but nothing enforced it, so a stochastic
    turn could skip it. This is the deterministic backstop: when a build turn
    would otherwise end in prose carrying a final YAML fence and no card, we
    synthesize the SAME `playbook_offer` card the tool would have emitted (via
    the framework's `_offer_from_yaml`, so shape + `final_yaml` are identical)
    and append it, turning `end_turn` into `awaiting_playbook_offer`.

    EXEMPT -- editing an OPEN playbook. When the designer supplied
    `entity.playbook_yaml`, the terminal action is deliberately a raw fence the
    analyst's Save pushes OVER the open record; an offer there would compile a
    *duplicate* new playbook and leave the open one unchanged (the exact
    anti-pattern `system_prompt_build.md` warns against). So we never salvage in
    that case.

    The offer verifies the YAML it carries, so YAML that fails `verify_playbook`
    is never salvaged: a turn that ended by explaining a blocking problem must
    not get a Deploy button for the broken playbook. Nor is a start -> end draft
    (`offer_has_no_actions`): live, "I want to create a new playbook." got a
    Save card for a playbook that did nothing.

    Returns a new wire_transcript with the offer card appended, or None when no
    salvage applies (leaving the caller's transcript/stop_reason untouched)."""
    if intent != "build":
        return None
    # A read-only turn (the "explain" / "find issues" quick actions) may print a
    # changed YAML fence while answering a question. Salvaging it into a
    # playbook_offer or enhancement_offer would hand the analyst an Apply button
    # for a change they never asked to make -- the one write path a read-only
    # turn must never open. The framework refuses the writing tools on these
    # turns; this is the connector-side half of the same promise.
    if read_only:
        return None
    yaml_text = last_assistant_yaml
    if not (isinstance(yaml_text, str) and yaml_text.strip()):
        # No prose YAML fence. Fall back to the last YAML the model fed
        # into a verification/validation tool -- the model authored a
        # real playbook but ended with prose (asking a clarifying
        # question, explaining a blocking issue) instead of emitting an
        # offer card.  The analyst still gets a Deploy button (#1).
        yaml_text = _last_authored_yaml(wire_transcript)
        if not yaml_text:
            return None
    # Open-playbook EDIT. A `playbook_offer` here would compile a duplicate new
    # playbook and leave the open one untouched, so the offer path stays out --
    # but bailing outright was itself the defect. The reasoning was "the raw
    # fence is fine, the analyst's Save pushes it over the open record": true on
    # the designer page, FALSE in chat, where a fence is just text with no Save
    # button attached. So an edit turn that narrated its YAML instead of calling
    # an offer card fell through the ONLY backstop and dead-ended -- and
    # the model, believing it had delivered, narrated the edit as DONE.
    #
    # Live repro: "fix the step name j, call it get joke" produced a correct
    # rename in a ```yaml fence, called only `verify_enhancement`, persisted
    # NOTHING, and closed with "I renamed the step ... and updated all
    # references" (then, contradicting itself, asked to confirm). The analyst's
    # playbook was unchanged.
    #
    # The in-place equivalent of the offer card is the enhancement_offer: same
    # deterministic backstop, but its accept goes through `update_playbook`,
    # which writes the open record. The analyst gets an Apply button instead of
    # a claim.
    if isinstance(entity, dict) and entity.get("playbook_yaml"):
        # Prefer the model's OWN before/after from verify_enhancement -- it is
        # the diff it actually reasoned about. Fall back to the mounted entity.
        diff = _enhancement_diff(wire_transcript)
        before, after = diff if diff else (entity["playbook_yaml"], yaml_text)
        return _salvage_enhancement_offer(
            wire_transcript, before_yaml=before, after_yaml=after,
            session_id=session_id, turn_idx=turn_idx)
    # Only salvage a genuinely card-less turn. If any gating card already
    # terminates the transcript (the model DID offer, or a manual_input/approval
    # gate fired), leave it alone -- the flow already lands.
    if _already_terminated_by_card(wire_transcript):
        return None
    from fsr_playbooks.mcp_server.tools_emit import _offer_from_yaml
    offer = _offer_from_yaml(
        f"offer-{session_id}-{turn_idx}",
        "I've finished authoring this playbook. Save it as a new playbook?",
        yaml_text,
        title_suggestion=None,
        editable_title=True,
    )
    if not (isinstance(offer, dict) and offer.get("ok")
            and isinstance(offer.get("card"), dict)):
        return None
    card = dict(offer["card"])
    # Nest under the last visible (non-emit_) tool call the build turn made --
    # typically the passing `verify_playbook` -- so the widget threads the card
    # under the step it reports on, matching the explicit-emit path.
    for w in reversed(wire_transcript):
        if (w.get("type") == "tool_use"
                and _emitted_card(w) is None):
            card["preceding_tool_use_id"] = w.get("id")
            break
    return _salvaged(SALVAGE_BUILD_OFFER, wire_transcript + [card])
