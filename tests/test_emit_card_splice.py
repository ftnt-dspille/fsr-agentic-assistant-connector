"""The wire splice must recognize the consolidated `emit_card` tool.

Phase 1 retired the seven per-type emitters in favor of the `emit_card`
union, but the card splice still gated on the retired names only, so a
successful `emit_card(card_type='enhancement_offer')` was invisible to the
wire and the open-playbook salvage papered a `patch_proposal` over the
model's real card (effect probe A2 caught this live on 0.5.130).
"""
from __future__ import annotations

from connector_fsr_agentic_assistant import operations as ops


from fsr_playbooks.llm.provider import ToolResultEvent, ToolUseEvent


def _tu(name: str, input_args: dict, tid: str = "t1") -> ToolUseEvent:
    return ToolUseEvent(call_id=tid, name=name, arguments=input_args)


def _tr(tid: str, content) -> ToolResultEvent:
    return ToolResultEvent(call_id=tid, result=content)


def _tool_use(name: str, input_args: dict, tid: str = "t1") -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": input_args}


CARD = {"type": "enhancement_offer", "id": "abc123", "summary": "add a step",
        "verified_id": "v1", "after_yaml": "playbooks: []"}


def test_emitted_card_recognizes_union_short_and_long_vocab():
    for kt in ("enhancement_offer", "action", "choice", "manual_input"):
        got = ops._emitted_card(
            _tool_use("emit_card", {"card_type": kt, "payload": {"x": 1}}))
        assert got is not None, kt
        ctype, fields = got
        assert ctype in ops._CARD_STOP_REASON
        assert fields == {"x": 1}
    # retired per-type emitters still resolve
    ctype, _ = ops._emitted_card(_tool_use("emit_action_card", {"a": 2}))
    assert ctype == "action_card"
    # non-card tools and unknown card types do not
    assert ops._emitted_card(_tool_use("get_step_type", {})) is None
    assert ops._emitted_card(
        _tool_use("emit_card", {"card_type": "nonsense", "payload": {}})) is None


def test_wire_transcript_splices_emit_card_union():
    wired = ops._wire_transcript([
        _tu("verify_enhancement", {"before_yaml": "a"}, tid="t0"),
        _tr("t0", {"ok": True, "ready_to_push": True}),
        _tu("emit_card",
            {"card_type": "enhancement_offer", "payload": dict(CARD)},
            tid="t1"),
        _tr("t1", {"ok": True, "card": dict(CARD)}),
    ])
    last = wired[-1]
    assert last["type"] == "enhancement_offer", (
        "emit_card union was invisible to the splice")
    # nests under the last visible (non-emit) tool call
    assert last.get("preceding_tool_use_id") == "t0"
    # and the terminated transcript blocks any salvage backstop
    assert ops._already_terminated_by_card(wired)


def test_wire_transcript_skips_failed_emit_card():
    wired = ops._wire_transcript([
        _tu("emit_card", {"card_type": "nonsense", "payload": {}}, tid="t1"),
        _tr("t1", {"ok": False, "error": "unknown_card_type"}),
    ])
    assert not any(w.get("type") in ops._CARD_STOP_REASON for w in wired)


# --- the verdict is not a gating card ----------------------------------------
# ka_c2_beacon: the model staged a block card, then emitted its verdict. "First
# card wins" cut the verdict off, so the analyst got "block this" with no
# finding. The verdict must survive either order, and the gating card must
# stay the turn's last event (the awaiting_* contract).

VERDICT = {"type": "verdict_card", "id": "v1", "disposition": "true_positive",
           "severity": "High", "confidence": 0.9, "summary": "C2 beacon",
           "findings": []}
ACTION = {"type": "action_card", "id": "a1", "connector": "fortigate-firewall",
          "operation": "block_ip_new", "summary": "block", "args": {}}


def _turn(*order: str) -> list:
    evs = [_tu("siem_search", {"q": "x"}, tid="s0"), _tr("s0", {"ok": True})]
    for kind in order:
        if kind == "verdict":
            evs += [_tu("emit_card", {"card_type": "verdict",
                                      "payload": dict(VERDICT)}, tid="tv"),
                    _tr("tv", {"ok": True, "card": dict(VERDICT)})]
        elif kind == "action":
            evs += [_tu("emit_card", {"card_type": "action",
                                      "payload": dict(ACTION)}, tid="ta"),
                    _tr("ta", {"ok": True, "card": dict(ACTION)})]
    return evs


def _types(wired):
    return [w.get("type") for w in wired if w.get("type", "").endswith("_card")]


def test_a_verdict_after_the_action_card_survives_and_the_action_stays_last():
    wired = ops._wire_transcript(_turn("action", "verdict"))
    assert _types(wired) == ["verdict_card", "action_card"]
    assert wired[-1]["type"] == "action_card"
    assert ops._already_terminated_by_card(wired)


def test_a_verdict_before_the_action_card_does_not_cut_the_action_off():
    wired = ops._wire_transcript(_turn("verdict", "action"))
    assert _types(wired) == ["verdict_card", "action_card"]
    assert wired[-1]["type"] == "action_card"


def test_a_deferred_verdict_is_not_a_card():
    evs = _turn("action") + [
        _tu("emit_card", {"card_type": "verdict", "payload": dict(VERDICT)},
            tid="tv"),
        _tr("tv", {"ok": True, "kind": "guard_defer", "directive": "NOT RUN"}),
    ]
    assert _types(ops._wire_transcript(evs)) == ["action_card"]


def test_a_lone_verdict_still_ends_the_turn_and_nests_under_the_evidence():
    evs = _turn("verdict") + [_tu("get_record", {}, tid="late")]
    wired = ops._wire_transcript(evs)
    assert wired[-1]["type"] == "verdict_card"
    assert wired[-1]["preceding_tool_use_id"] == "s0"


# --- the LAST delivered gating card wins --------------------------------------
# build_plain_request_no_record: a capability_gap card, then a verified
# playbook_offer. "First card wins" gave the analyst the gap card and cut the
# deployable offer. A card later in the same turn supersedes the earlier one.

GAP = {"type": "capability_gap", "id": "g1", "summary": "no smtp"}
OFFER = {"type": "playbook_offer", "id": "o1", "summary": "email the summary",
         "final_yaml": "playbooks: []"}


def _emit(card, tid, result=None):
    short = {"capability_gap": "capability_gap",
             "playbook_offer": "playbook_offer"}[card["type"]]
    return [_tu("emit_card", {"card_type": short, "payload": dict(card)}, tid=tid),
            _tr(tid, result if result is not None else {"ok": True, "card": dict(card)})]


def test_a_later_gating_card_supersedes_an_earlier_one():
    evs = [_tu("find", {}, tid="f0"), _tr("f0", {"ok": True})]
    evs += _emit(GAP, "t1") + _emit(OFFER, "t2")
    wired = ops._wire_transcript(evs)
    kinds = [w.get("type") for w in wired
             if w.get("type") in ("capability_gap", "playbook_offer")]
    assert kinds == ["playbook_offer"], "the superseded card must not be spliced"
    assert wired[-1]["type"] == "playbook_offer"
    assert ops._envelope_stop_reason(wired, "end_turn") == "awaiting_playbook_offer"


def test_a_refused_later_card_does_not_displace_the_delivered_one():
    evs = _emit(GAP, "t1") + _emit(OFFER, "t2", {"ok": False, "error": "bad_payload"})
    wired = ops._wire_transcript(evs)
    assert wired[-1]["type"] == "capability_gap"


def test_a_deferred_later_card_does_not_displace_the_delivered_one():
    """A guard deferral carries ok: True but ran nothing."""
    evs = _emit(GAP, "t1") + _emit(OFFER, "t2", {"ok": True, "kind": "guard_defer",
                                                 "directive": "NOT RUN"})
    wired = ops._wire_transcript(evs)
    assert wired[-1]["type"] == "capability_gap"
