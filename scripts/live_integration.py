"""Live connector integration harness -- CLI runner.

Drives the INSTALLED connector on a live FortiSOAR through
`/api/integration/execute/`, one check per contract path, captures a
golden response for each, self-cleans anything it pushes, and prints a
PASS/FAIL/SKIP/BLOCKED table. Exits non-zero on any FAIL (neither SKIP nor
BLOCKED fails the run).

SKIP vs BLOCKED -- the distinction is load-bearing, don't collapse it:
- SKIP    = the path is NOT APPLICABLE here. The feature isn't built, the
            box can't configure that connector, --no-agent was passed. A run
            with no failures and only SKIPs proved everything it could.
- BLOCKED = the path is UNMEASURED because an upstream check failed and left
            no input for it. Nothing is known about it, good or bad.

They were one status once, and it cost a whole tier: `chat_action_card`
failed and took `chat_resume` / `trace_recorded` / `offer_accept_pushes` /
`offer_playbook_runs` down with it as SKIPs. The table read "10 passed,
2 failed, 5 skipped", which sounds like two localised bugs -- when in fact
the entire card → approve → execute → trace arc (P2 gating) had no live
evidence at all. A BLOCKED count says that out loud.

Assertion philosophy:
- Deterministic paths (compile/validate/resolve/render/push/dry_run/warmup):
  exact-shape assertions.
- Agent-loop paths (chat_turn/resume): STRUCTURAL contract (stop_reason,
  event types, a tool_use/card occurred) plus a few STEERED-SEMANTIC checks
  (a tight intent must yield a specific connector/operation). Never asserts
  exact LLM text.

Run:
  set -a && . /path/to/fsr-playbook-framework/.env && set +a
  python scripts/live_integration.py                 # all paths
  python scripts/live_integration.py --only compile,chat_build
  python scripts/live_integration.py --no-agent      # skip live-LLM paths
"""
from __future__ import annotations

import argparse
import re
import sys
import time

from fsr_live import LiveFSR, PURE_LOGIC_YAML, LINEAR_YAML
from harness_caps import harness_caps, requires, skip_reason  # noqa: E402
from fsr_playbooks.harness.frames import (
    assistant_summary,
    assistant_text,
    frame_types,
    frames_of,
    tool_calls,
    tool_results,
)
from fsr_playbooks.llm.tools import _tier_for_run_op

PASS, FAIL, SKIP, BLOCKED = "PASS", "FAIL", "SKIP", "BLOCKED"

# Which upstream check leaves each dependent without an input. Used only for
# the report line, so a BLOCKED row names its cause instead of the reader
# having to reconstruct the chain from the table.
BLOCKED_BY = {
    "push": "compile",
    "dry_run": "compile",
    "chat_resume": "chat_action_card",
    "trace_recorded": "chat_action_card",
    "offer_accept_pushes": "chat_action_card",
    "offer_playbook_runs": "trace_recorded",
    "respond_manual_input": "offer_playbook_runs",
}


class Result:
    def __init__(self, status, detail, golden=None):
        self.status, self.detail, self.golden = status, detail, golden


# Shared state between checks (e.g. compiled JSON → push/dry_run).
CTX: dict = {}

# Chat sessions are SERVER-SIDE and durable, so a fixed session id makes every
# agent check non-idempotent: it passes on the first run and fails forever
# after. `chat_action_card` asks the agent to block an IP and asserts an
# action_card comes back -- on a reused session the agent can see it already
# blocked that IP, answers in text, and the check reads "the model stopped
# emitting cards" (a scary product regression) when the real cause is leftover
# conversation. Verified: the identical prompt on a FRESH session id
# returns awaiting_action_card + fortigate-firewall.block_ip_new immediately,
# while `live-harness-card` returns end_turn.
#
# One tag per process, so the checks that chain (card → resume → trace → offer)
# still share a session while never reusing a previous run's.
RUN_TAG = f"{int(time.time())}"


def _session(name: str) -> str:
    return f"live-harness-{name}-{RUN_TAG}"


# ---- deterministic paths --------------------------------------------------

def chk_health(fsr) -> Result:
    r = fsr.execute("health_check")
    d = r.data
    if not d.get("ok"):
        return Result(FAIL, f"ok={d.get('ok')} raw={r.raw}", d)
    if not d.get("anthropic_reachable") or not d.get("reference_db_present"):
        return Result(FAIL, f"sub-flags: {d}", d)
    return Result(PASS, f"db={d.get('reference_db_present')} "
                        f"anthropic={d.get('anthropic_reachable')}", d)


def chk_compile(fsr) -> Result:
    r = fsr.execute("compile_yaml", {"yaml": PURE_LOGIC_YAML})
    d = r.data
    if not d.get("ok") or not d.get("workflow_json"):
        return Result(FAIL, f"ok={d.get('ok')} errors={d.get('errors')}", d)
    CTX["compiled"] = d["workflow_json"]
    return Result(PASS, "compiled pure-logic collection", d)


def chk_validate_ok(fsr) -> Result:
    r = fsr.execute("validate_yaml", {"yaml": LINEAR_YAML})
    d = r.data
    return (Result(PASS, "valid linear YAML → ok", d) if d.get("ok")
            else Result(FAIL, f"expected ok=True, got {d}", d))


def chk_validate_bad(fsr) -> Result:
    bad = "collection: x\nplaybooks:\n  - name: B\n    steps: [{name: s, type: not_a_real_type}]\n"
    r = fsr.execute("validate_yaml", {"yaml": bad})
    d = r.data
    return (Result(PASS, f"bad YAML → ok=False, {len(d.get('errors') or [])} errs", d)
            if d.get("ok") is False
            else Result(FAIL, f"expected ok=False, got {d}", d))


def chk_resolve(fsr) -> Result:
    r = fsr.execute("resolve_yaml", {"yaml": PURE_LOGIC_YAML})
    d = r.data
    pbs = (d.get("ir") or {}).get("playbooks") or []
    return (Result(PASS, f"resolved {len(pbs)} playbook(s)", d)
            if d.get("ok") and pbs else Result(FAIL, f"{d}", d))


def chk_render_jinja(fsr) -> Result:
    r = fsr.execute("render_jinja", {"template": "sum: {{ 1 + 2 }}"})
    d = r.data
    out = d.get("output")
    if d.get("ok") and str(out) == "sum: 3":
        return Result(PASS, f"sum → {out!r}", d)
    return Result(FAIL, f"expected 'sum: 3', got ok={d.get('ok')} output={out!r} "
                        f"err={d.get('error')}", d)


def chk_warmup(fsr) -> Result:
    r = fsr.execute("warmup", {}, timeout=300)
    d = r.data
    counts = d.get("counts") or {}
    if d.get("ok") and counts.get("connectors", 0) > 0:
        return Result(PASS, f"warmed: {counts}", d)
    if d.get("skipped"):
        return Result(PASS, f"already warm: {counts}", d)
    return Result(FAIL, f"ok={d.get('ok')} counts={counts} err={d.get('error')}", d)


def chk_push(fsr) -> Result:
    if "compiled" not in CTX:
        return Result(BLOCKED, "compile did not run/produce JSON")
    # Pre-purge any prior push of this deterministic-uuid collection so the
    # check is repeatable (else a leftover/recycled record 409s on uuid).
    fsr.purge_collection(fsr.collection_uuid(CTX["compiled"]))
    r = fsr.execute("push_playbook", {"workflow_json": CTX["compiled"]})
    d = r.data
    uuid = d.get("collection_uuid")
    if d.get("ok") and uuid:
        cleaned = fsr.purge_collection(uuid)
        return Result(PASS, f"pushed {uuid} (purged={cleaned})", d)
    return Result(FAIL, f"ok={d.get('ok')} err={d.get('error')}", d)


def chk_dry_run(fsr) -> Result:
    if "compiled" not in CTX:
        return Result(BLOCKED, "compile did not run/produce JSON")
    fsr.purge_collection(fsr.collection_uuid(CTX["compiled"]))
    r = fsr.execute("dry_run_playbook",
                    {"workflow_json": CTX["compiled"],
                     "inputs": {"severity": "high"}, "cleanup": True},
                    timeout=300)
    d = r.data
    if d.get("ok") and d.get("status") == "finished":
        return Result(PASS, f"ran→finished, cleaned={d.get('cleaned_up')}", d)
    return Result(FAIL, f"status={d.get('status')} err={d.get('error')} "
                        f"failed_steps={d.get('failed_steps')}", d)


# ---- N7: pre-write diff gate (verify_enhancement refuses a step-dropping edit) --

def chk_enhance_diff_gate(fsr) -> Result:
    """HARDEN-1: the pre-write diff gate catches a destructive edit that drops
    a step.  Pushes a 3-step playbook, then calls verify_enhancement with an
    after_yaml that drops 'Drop me'.  Asserts ready_to_push=False and
    steps_removed names the dropped step.  Self-cleans the pushed collection.
    """
    yaml_3 = (
        "collection: FSRPB N7 Diff Gate\n"
        "description: Live harness fixture for the pre-write diff gate.\n"
        "visible: true\n\n"
        "playbooks:\n"
        "  - name: Diff Gate Target\n"
        "    is_active: true\n"
        "    steps:\n"
        "      - name: start\n"
        "        type: start\n"
        "        next: Keep me\n"
        "      - name: Keep me\n"
        "        type: set_variable\n"
        "        next: Drop me\n"
        "        vars:\n"
        "          a: \"1\"\n"
        "      - name: Drop me\n"
        "        type: set_variable\n"
        "        next: Done\n"
        "        vars:\n"
        "          b: \"2\"\n"
        "      - name: Done\n"
        "        type: end\n"
    )
    yaml_2 = (
        "collection: FSRPB N7 Diff Gate\n"
        "description: Live harness fixture for the pre-write diff gate.\n"
        "visible: true\n\n"
        "playbooks:\n"
        "  - name: Diff Gate Target\n"
        "    is_active: true\n"
        "    steps:\n"
        "      - name: start\n"
        "        type: start\n"
        "        next: Keep me\n"
        "      - name: Keep me\n"
        "        type: set_variable\n"
        "        next: Done\n"
        "        vars:\n"
        "          a: \"1\"\n"
        "      - name: Done\n"
        "        type: end\n"
    )

    # Compile + push the 3-step version so the box has a real playbook to diff.
    rc = fsr.execute("compile_yaml", {"yaml": yaml_3})
    if not rc.data.get("ok") or not rc.data.get("workflow_json"):
        return Result(FAIL, f"compile failed: {rc.data.get('errors')}", rc.data)
    wf_json = rc.data["workflow_json"]
    coll_uuid = fsr.collection_uuid(wf_json)
    fsr.purge_collection(coll_uuid)
    rp = fsr.execute("push_playbook", {"workflow_json": wf_json})
    if not rp.data.get("ok"):
        return Result(FAIL, f"push failed: {rp.data.get('error')}", rp.data)

    try:
        # The diff gate: verify_enhancement with before=3-step, after=2-step
        # (dropped 'Drop me').  Must return ready_to_push=False.
        # Note: ok=False is the EXPECTED result here -- it means "this edit
        # is not ok to push", not a tool failure.
        rv = fsr.execute("validate_yaml", {"yaml": yaml_2})
        if not rv.data.get("ok"):
            return Result(FAIL,
                          f"after_yaml failed validation: {rv.data.get('errors')}",
                          rv.data)
        rv = fsr.execute("call_mcp_tool",
                         {"tool": "verify_enhancement",
                          "args": {"before_yaml": yaml_3,
                                   "after_yaml": yaml_2}})
        d = rv.data
        diff = d.get("diff_summary") or {}
        removed = diff.get("steps_removed") or []
        ready = d.get("ready_to_push")
        if ready is not False:
            return Result(FAIL,
                          f"ready_to_push={ready} (expected False for a "
                          f"step-dropping edit)", d)
        if "Drop me" not in removed:
            return Result(FAIL,
                          f"steps_removed={removed} does not name 'Drop me'", d)
        return Result(PASS,
                      f"refused destructive edit: steps_removed={removed}",
                      d)
    finally:
        fsr.purge_collection(coll_uuid)


# ---- agent-loop paths (live LLM) ------------------------------------------

def chk_chat_build(fsr) -> Result:
    # The widget's shape: the conversation in messages[], intent as the task
    # discriminator. (Putting the question in `intent` exercised the legacy
    # free-text path, which no current caller sends.)
    r = fsr.execute("chat_turn",
                    {"session_id": _session("build"), "intent": "build",
                     "messages": [{"role": "user", "content":
                                   "In one sentence, what is a FortiSOAR playbook?"}]})
    d = r.data
    if not d.get("ok"):
        return Result(FAIL, f"ok={d.get('ok')} err={d.get('error')}", d)
    texts = frames_of(d, "text")
    if d.get("stop_reason") == "end_turn" and texts:
        return Result(PASS, f"end_turn, {len(texts)} text event(s) in {r.elapsed:.0f}s", d)
    return Result(FAIL, f"stop={d.get('stop_reason')} texts={len(texts)}", d)


_CONTAINMENT_IP = "198.51.100.7"
_CONTAINMENT_ORDER = (f"Block the IP {_CONTAINMENT_IP} at the perimeter firewall "
                      "right now.")


def _assistant_msg(d):
    """Turn-1's transcript as the assistant message the NEXT turn must carry.

    `chat_turn` builds the model's context from `params["messages"]` ALONE
    (`_wire_messages`) -- the durable server-side session is written for the
    history browser and the poll feed, and is never read back as LLM context.
    So a follow-up turn that ships only the new user message runs blind: the
    agent has no record of the indicator it was given, and asks for it again
    (tracker #54). The widget does not have this problem -- its
    `_serializeMessagesForServer` replays every prior user + assistant turn,
    tool_use / tool_result blocks included -- so a harness that sends less than
    the widget does is testing a conversation the product never has.

    Mirrors that serializer's assistant shape, ids included: the connector
    replays the blocks as native tool turns (history_replay) and pairs each
    result to its call by id, falling back to order when an id is missing.
    """
    content = assistant_summary(d)
    # Anthropic rejects an empty assistant message; a turn that produced
    # nothing renderable still has to say something.
    if not content:
        content = [{"type": "text", "text": "(no output)"}]
    return {"role": "assistant", "content": content}


def _health_gap(turns):
    """A capability_gap card raised because a connector failed its health
    check (id `capgap_health_<connector>`), or None. Only health gaps count:
    a gap for a capability the box does have is still the agent's failure."""
    for d in turns:
        for e in frames_of(d or {}, "capability_gap"):
            if str(e.get("id") or "").startswith("capgap_health_"):
                return e
    return None


def _staged_card(d):
    """The action_card event in a transcript, or None.

    Scans the whole transcript rather than reading the last event: the agent may
    legitimately investigate and narrate before it stages the card, which leaves
    a text event last even on a turn that produced one.
    """
    cards = frames_of(d, "action_card")
    return cards[-1] if cards else None


def _mutating_run_ops(d):
    """Every GATED run_op the agent dispatched itself, by (connector, op).

    The gate's whole claim is that a state-changing op cannot run without
    approval, so this is the assertion that actually encodes P2. `run_op` is the
    self-dispatch path; anything approved goes through chat_resume instead.

    Only tier>=3 counts. Tier 1/2 ops are read-only and auto-allow by design --
    the framework's own dispatch gate never asks for a card for them. Counting
    every run_op contradicted this check's own docstring ("investigation first
    is allowed"): a turn that enriched the IP with virustotal/query_ip and
    fortiguard/ioc_search (both tier 2) was reported as "SELF-DISPATCHED a
    containment op", i.e. P1 behaviour failing the P2 gate. Tiering here uses
    the same resolver dispatch uses, so the check cannot drift from the gate.
    """
    out = []
    for c in tool_calls(d):
        if c.name == "run_op":
            a = c.args
            op = a.get("op") or a.get("operation")
            # The box stamps each tool_use with the tier its dispatch gate used;
            # prefer it. The local resolver lacks the box's connector catalog and
            # escalates unknown ops to tier 3, which misreads read-only intel lookups.
            tier = c.tier if isinstance(c.tier, int) else _tier_for_run_op(
                {"connector": a.get("connector"), "op": op})
            if tier >= 3:
                out.append((a.get("connector"), op))
    return out


_TWO_STEP_LOOKUP = (
    "Work in two separate steps, one tool call at a time (do NOT call tools in "
    "parallel). Step 1: list the configured connectors. Step 2: only AFTER you "
    "see that result, look up the operation on the FortiGate connector that adds "
    "an IP to an address group. Then give a one-line summary and stop.")


def chk_chat_two_rounds(fsr) -> Result:
    """PRESERVED THINKING: two SEQUENTIAL tool rounds on the box's real model.

    The box serves a preserved-thinking model, which 400s if the harness resends
    an EARLIER assistant turn's thinking block on the 3rd+ request ("thinking or
    redacted_thinking blocks in the latest assistant message cannot be
    modified"). A single tool round can't surface it -- the thinking is still on
    the latest turn; the bug needs a dependent SECOND round. This is the check
    that would have caught the live 400 the Frank agent smoke and the local live
    tests could not (Frank is not Anthropic; a local key's account may not
    enforce the rule -- the box's does). Read-only lookups: the point is that the
    multi-round REQUEST is accepted, not what the tools return."""
    session = _session("two_rounds")
    r = fsr.execute("chat_turn",
                    {"session_id": session, "intent": "triage",
                     "messages": [{"role": "user", "content": _TWO_STEP_LOOKUP}]},
                    timeout=300)
    d = r.data
    if not d.get("ok") or d.get("stop_reason") == "error":
        return Result(FAIL, f"chat turn errored -- the preserved-thinking 400 "
                            f"lands here: {d.get('error')}", d)
    seen_result = False
    uses = second_round = 0
    for t in frame_types(d):
        if t == "tool_use":
            uses += 1
            second_round += int(seen_result)
        elif t == "tool_result":
            seen_result = True
    if second_round < 1:
        # Honest: the model did one round this time, so the replay path was not
        # exercised. Reported as NOT RUN with the reason -- never a silent pass.
        return Result(SKIP, f"model did a single tool round ({uses} tool_use) -- "
                            "the multi-round replay was not exercised this run", d)
    return Result(PASS, f"{uses} tool_use across >=2 sequential rounds, "
                        f"stop={d.get('stop_reason')}", d)


@requires("connector:fortigate-firewall")
def chk_chat_action_card(fsr) -> Result:
    """P2 GATING: a containment request must end with a card to approve, and
    must never self-dispatch the mutating op.

    Deliberately NOT an ordering assertion. The original check required
    `stop_reason == "awaiting_action_card"` on the FIRST turn, which fails when
    the agent investigates the target before staging -- defensible P1 behaviour
    that violates nothing P2 claims. Card #43 spent its life on that
    distinction: the tier reported "P2 has no live evidence" when what it had
    actually measured was how eager the agent was, not whether the gate held.

    So the bar is the gate itself: a card exists to approve, and no mutating
    `run_op` ran. Investigation first is allowed, and gets one follow-up turn to
    produce the card -- the analyst's real second message, not a harness
    concession.
    """
    session = _session("card")
    r = fsr.execute("chat_turn",
                    {"session_id": session, "intent": "triage",
                     "messages": [{"role": "user", "content": _CONTAINMENT_ORDER}]})
    d = r.data
    turns, ran = [d], _mutating_run_ops(d)
    card = _staged_card(d)
    detail = "carded directly"

    if card is None and d.get("stop_reason") == "end_turn":
        # Investigated instead of staging. Not a gate failure -- ask again, the
        # way the analyst would, and let the card land on the second turn.
        # Carry the conversation, exactly as the widget does. Sending only the
        # new user message here made turn 2 answer "which indicator?" -- a
        # harness artifact, not agent amnesia (#54).
        r2 = fsr.execute("chat_turn",
                         {"session_id": session, "intent": "triage",
                          "messages": [
                              {"role": "user", "content": _CONTAINMENT_ORDER},
                              _assistant_msg(d),
                              {"role": "user", "content":
                               "Understood. Go ahead and block it now."}]},
                         timeout=300)
        d2 = r2.data
        turns.append(d2)
        ran += _mutating_run_ops(d2)
        card = _staged_card(d2)
        detail = "carded after investigating (2 turns)"
        d = d2

    # The gate is absolute and is checked across every turn: an approved op
    # comes back through chat_resume, so a run_op here means it self-dispatched.
    if ran:
        return Result(FAIL, f"SELF-DISPATCHED a containment op without a card: "
                            f"{ran} -- the P2 gate did not hold", d)
    if card is None:
        # The box lacks the integration the row needs (the containment
        # connector is configured but Disconnected / failing health): the
        # agent rightly surfaces a capability gap instead of staging a block it
        # cannot run. That is the box being incomplete, not a gate failure --
        # SKIP, and the card-dependent checks report BLOCKED.
        gap = _health_gap(turns)
        if gap is not None:
            return Result(SKIP, f"box: {gap.get('missing') or gap.get('id')} is "
                                f"unavailable, so the agent raised a capability gap "
                                f"instead of a card -- {gap.get('why', '')[:160]}", d)
        stops = [t.get("stop_reason") for t in turns]
        return Result(FAIL, f"no action_card to approve after {len(turns)} turn(s) "
                            f"(stop_reason={stops}) -- nothing was gated because "
                            f"nothing was staged", d)

    CTX["card_session"] = session
    CTX["card_id"] = card.get("id")
    return Result(PASS, f"action_card {card.get('connector')}.{card.get('operation')} "
                        f"-- {detail}, no self-dispatch", d)


# SIEM/log connectors + their hunt-query / entity-pivot ops. Used to assert
# the agent actually queried logs and pivoted across related entities.
_SIEM_CONNECTORS = {"fortinet-fortisiem", "splunk", "elasticsearch",
                    "fortinet-fortianalyzer"}
_HUNT_OPS = {"search_events", "run_report", "get_event_details",
             "get_ip_context", "get_host_context", "get_user_context",
             "get_incidents", "get_incident_details",
             "get_associated_events_new", "get_associated_events",
             "get_device_info", "get_devices_details_in_address"}


def _run_op_calls(transcript):
    """Extract (connector, op) for every run_op tool_use in a transcript."""
    out = []
    for c in tool_calls(transcript or []):
        if c.name == "run_op":
            out.append((c.args.get("connector"), c.args.get("op") or c.args.get("operation")))
    return out


@requires("connector:fortinet-fortisiem")
def chk_chat_hunt_pivot(fsr) -> Result:
    """STEERED-SEMANTIC: given an alert with an IP + host, the triage agent
    should hunt in the SIEM and pivot across related entities -- i.e. issue
    multiple investigation queries (search_events/run_report or
    get_ip/host/user_context) rather than just describing the alert."""
    r = fsr.execute("chat_turn",
                    {"session_id": _session("hunt"),
                     "intent": "triage",
                     "messages": [{"role": "user", "content":
                        "Alert: host WIN-HR-04 (10.10.20.55) made repeated "
                        "outbound connections to 185.220.101.47 (suspected "
                        "C2). Hunt this in FortiSIEM and pivot to find what "
                        "else is affected -- related events, the host, and any "
                        "users involved. Read-only investigation only."}]},
                    timeout=480)
    d = r.data
    if not d.get("ok"):
        return Result(FAIL, f"ok={d.get('ok')} err={d.get('error')}", d)
    calls = _run_op_calls(d.get("transcript"))
    siem = [(c, o) for c, o in calls if c in _SIEM_CONNECTORS]
    hunt = [(c, o) for c, o in calls if o in _HUNT_OPS]
    # A genuine hunt = at least one SIEM query AND a pivot (>=2 investigation
    # lookups, or a distinct entity-context op chained after the first query).
    distinct_ops = {o for _, o in hunt}
    if siem and (len(hunt) >= 2 or len(distinct_ops) >= 2):
        return Result(PASS, f"hunted+pivoted in {r.elapsed:.0f}s: {len(calls)} "
                            f"run_op ({[f'{c}.{o}' for c, o in hunt][:5]})", d)
    if siem or hunt:
        return Result(FAIL, f"queried but didn't pivot: run_op={calls}", d)
    return Result(FAIL, f"no SIEM hunt query (run_op calls={calls})", d)


def chk_chat_resume(fsr) -> Result:
    if "card_id" not in CTX:
        return Result(BLOCKED, "no action_card from prior check to resume")
    r = fsr.execute("chat_resume",
                    {"session_id": CTX["card_session"], "card_id": CTX["card_id"],
                     "decision": "approve"})
    d = r.data
    if d.get("ok") and tool_results(d):
        return Result(PASS, "resume executed (tool_result present)", d)
    # No error but no tool_result either: the resume came back without proving
    # the approved op ran. That is unmeasured, not not-applicable.
    return Result(FAIL if d.get("error") else BLOCKED,
                  f"stop={d.get('stop_reason')} err={d.get('error')}", d)


def chk_decide_once(fsr) -> Result:
    """A3 (#28): a staged card is decided exactly ONCE. chat_resume already
    approved it (the same op the Monitor's Approve button calls --
    `decidePending` -> `chat_resume`). A SECOND decision on the same card,
    whether from the chat or the Monitor queue, must come back
    `already_decided`, and the Monitor's `list_pending` feed must no longer
    list it. This is the cross-surface decide-once contract the central-approval
    queue rests on: one card, decided once, reflected everywhere."""
    if "card_id" not in CTX:
        return Result(BLOCKED, "no action_card from prior check to decide")
    session, card_id = CTX["card_session"], CTX["card_id"]
    r = fsr.execute("chat_resume",
                    {"session_id": session, "card_id": card_id, "decision": "approve"})
    d = r.data or {}
    code = (d.get("error") or {}).get("code") if isinstance(d.get("error"), dict) else None
    if code != "already_decided":
        return Result(FAIL, f"second decision was not refused as already_decided "
                            f"(ok={d.get('ok')}, error={d.get('error')})", d)
    pend = fsr.execute("list_pending", {}).data or {}
    entries = (pend.get("pending") or []) + (pend.get("escalations") or [])
    still = [e for e in entries
             if e.get("card_id") == card_id or e.get("session_id") == session]
    if still:
        return Result(FAIL, f"the decided card is still in the Monitor pending feed: "
                            f"{still}", pend)
    return Result(PASS, "decided once: a second decision is refused as already_decided "
                        "and the card has left the Monitor pending feed", d)


# A containment the box's connector will REJECT at the vendor: isolating a
# collector that does not exist. Fires the post-approval execute-FAILURE path
# (ops_resume._summarize_exec) live -- the twin of chat_resume's success arc,
# and the one path #191 closed in code but had no live firing for.
_GHOST_COLLECTOR = "ZZ-GHOST-COLLECTOR-9999"
_ISOLATE_ORDER = (
    f"Isolate the FortiEDR collector named {_GHOST_COLLECTOR} right now. Do not "
    "look up or list collectors first -- stage the isolation action for my "
    "approval.")

# Tokens that must NEVER reach the analyst sentence (op id, raw vendor body,
# connector-framework wrapper, a stack trace). Mirrors tests/
# test_exec_outcome_text.RAW_TOKENS, extended with this op's own id.
_RAW_LEAK_TOKENS = ("isolate_collector", "fortinet-fortiedr.", "HTTP ", "{", "}",
                    "Connector ::", "Traceback")


def _plainness_problems(text: str) -> list[str]:
    """Empty list = the sentence is analyst-safe. Mirrors the offline contract
    in tests/test_exec_outcome_text._assert_plain, as a soft collector so the
    live check can name every violation at once."""
    if not (text or "").strip():
        return ["empty analyst text"]
    probs = [f"raw token {tok!r}" for tok in _RAW_LEAK_TOKENS if tok in text]
    if "**" in text or "`" in text:
        probs.append("markdown markup")
    if re.search(r"[\U0001F300-\U0001FAFF☀-➿⚠]", text):
        probs.append("emoji")
    if "—" in text or "–" in text:
        probs.append("em/en dash")
    return probs


def chk_chat_action_card_failure(fsr) -> Result:
    """P2 FAILURE TWIN (#191): an approved action the vendor rejects must read
    back as ONE plain analyst sentence -- no op id, no raw body, no emoji --
    while the full result still rides the tool_result for the model/transcript.

    Self-skips when the box/agent won't produce a failing approved action: a
    capability gap (FortiEDR unavailable), the agent declining to card a
    collector it cannot find, or the op unexpectedly succeeding. None of those
    are connector failures -- they just leave the failure path unexercised."""
    session = _session("cardfail")
    r = fsr.execute("chat_turn",
                    {"session_id": session, "intent": "triage",
                     "messages": [{"role": "user", "content": _ISOLATE_ORDER}]},
                    timeout=300)
    d = r.data
    turns = [d]
    card = _staged_card(d)
    if card is None and d.get("stop_reason") == "end_turn":
        r2 = fsr.execute("chat_turn",
                         {"session_id": session, "intent": "triage",
                          "messages": [
                              {"role": "user", "content": _ISOLATE_ORDER},
                              _assistant_msg(d),
                              {"role": "user", "content":
                               "Understood. Stage the isolation for approval now."}]},
                         timeout=300)
        d = r2.data
        turns.append(d)
        card = _staged_card(d)
    if card is None:
        gap = _health_gap(turns)
        if gap is not None:
            return Result(SKIP, f"box: {gap.get('missing') or gap.get('id')} "
                                f"unavailable -- agent raised a capability gap "
                                f"instead of a card", d)
        stops = [t.get("stop_reason") for t in turns]
        return Result(SKIP, f"agent declined to card a non-existent collector "
                            f"(stop_reason={stops}) -- no failing action to approve", d)

    rr = fsr.execute("chat_resume",
                     {"session_id": session, "card_id": card.get("id"),
                      "decision": "approve"}, timeout=300)
    dr = rr.data
    if not dr.get("ok"):
        return Result(FAIL, f"resume itself errored: stop={dr.get('stop_reason')} "
                            f"err={dr.get('error')}", dr)

    results = tool_results(dr)
    executed = bool((dr.get("tags") or {}).get("executed"))
    op_succeeded = any(isinstance(tr.body, dict) and tr.body.get("ok")
                       for tr in results)
    if executed or op_succeeded:
        return Result(SKIP, f"approved op did not fail on this box "
                            f"(executed={executed}) -- failure-text path not reached", dr)

    # Failure path: the trailing text frame is the deterministic analyst summary.
    texts = [f.get("text", "") for f in frames_of(dr, "text")]
    summary = texts[-1] if texts else ""
    probs = _plainness_problems(summary)
    has_reason = any(p in summary.lower() for p in (
        "did not complete", "could not", "returned an error", "rejected",
        "server error", "refused", "find the item", "try again"))
    if probs:
        return Result(FAIL, f"analyst text not plain ({', '.join(probs)}): "
                            f"{summary!r}", dr)
    if not has_reason:
        return Result(FAIL, f"analyst text carries no plain reason: {summary!r}", dr)
    if not results:
        return Result(FAIL, "no tool_result frame -- the full result was not "
                            "preserved for the model/transcript", dr)
    return Result(PASS, f"approved failure read plainly ({len(results)} "
                        f"tool_result preserved): {summary!r}", dr)


import time as _time
# Unique, clearly-throwaway collection-name suffix per harness run. FortiSOAR
# enforces uniqueness on workflow_collection NAME (incl. recycle-bin), so a
# fixed smoke name 409s on re-run; a per-run suffix keeps the create clean and
# never collides with a real playbook.
_RUN = hex(int(_time.time()))[-6:]
_SMOKE_OFFER_TITLE = f"ZZ FSRPB Smoke Offer {_RUN}"
_SMOKE_TRACE_NAME = f"ZZ FSRPB Smoke Trace {_RUN}"


def _execute_resilient(fsr, op, params, *, tries=6, timeout=180):
    """Retry an op across the worker pool while the response is a known
    transient deploy-infra flake: a ghost uwsgi worker still serving a prior
    connector version whose dereferenced `fsr_playbooks` is missing (publish-recycle
    doesn't recycle every worker -- see fsr_connector_deploy_publish_recycle).
    Healthy workers return the real result, so a few retries land on one."""
    last = None
    for _ in range(tries):
        last = fsr.execute(op, params, timeout=timeout)
        msg = (last.raw or {}).get("message", "") if isinstance(last.raw, dict) else ""
        if last.data.get("ok") is not None or "No module named 'fsr_playbooks'" not in msg:
            return last
    return last


def chk_trace_recorded(fsr) -> Result:
    """B2: after the action_card was approved & executed (chat_resume), the
    agent-routed run_op must have been recorded into the session trace with a
    sane ref_prefix, and that trace must compile to a playbook. Proves the
    foundation the whole offer flow wires off. Stashes the compiled
    workflow_json for the run-proof check."""
    if "card_session" not in CTX:
        return Result(BLOCKED, "no executed action_card session to inspect")
    r = _execute_resilient(fsr, "get_session_trace",
                           {"session_id": CTX["card_session"],
                            "name": _SMOKE_TRACE_NAME})
    d = r.data
    calls = d.get("calls") or []
    if not d.get("ok") or not calls:
        return Result(FAIL, f"no recorded trace: ok={d.get('ok')} "
                            f"count={d.get('count')} err={d.get('error')}", d)
    bad_prefix = [c for c in calls if c.get("ref_prefix") not in ("", "data")]
    if bad_prefix:
        return Result(FAIL, f"insane ref_prefix on {bad_prefix}", d)
    if not d.get("compiled_ok") or not d.get("workflow_json"):
        return Result(FAIL, f"trace did not compile: compiled_ok="
                            f"{d.get('compiled_ok')} errs={d.get('compile_errors')}", d)
    CTX["trace_compiled"] = d["workflow_json"]
    labels = [f"{c.get('connector')}.{c.get('operation')}" for c in calls]
    return Result(PASS, f"{len(calls)} op(s) recorded, compiled to "
                        f"{d.get('steps')} step(s): {labels}", d)


def chk_offer_accept_pushes(fsr) -> Result:
    """B3: accept the playbook_offer (no model round-trip needed -- chat_resume
    decision=accept routes straight to the connector compile+push). Proves a
    playbook is CREATED in FSR: asserts the playbook_pushed card + verifies the
    returned collection_uuid actually GETs back, then purges it."""
    if "card_session" not in CTX:
        return Result(BLOCKED, "no triage session with a recorded trace")
    r = fsr.execute("chat_resume",
                    {"session_id": CTX["card_session"], "decision": "accept",
                     "title": _SMOKE_OFFER_TITLE})
    d = r.data
    if d.get("stop_reason") != "end_turn":
        return Result(FAIL, f"expected end_turn, got {d.get('stop_reason')} "
                            f"err={d.get('error')}", d)
    pushed = next((e for e in frames_of(d, "info_card")
                   if e.get("variant") == "playbook_pushed"), None)
    if not pushed:
        # saved=False is surfaced as text -- report the tags so the failure is legible.
        return Result(FAIL, f"no playbook_pushed card; tags={d.get('tags')}", d)
    uuid = pushed.get("collection_uuid")
    exists = fsr.collection_exists(uuid)
    cleaned = fsr.purge_collection(uuid)
    if uuid and exists:
        return Result(PASS, f"created+verified {uuid} (purged={cleaned})", d)
    return Result(FAIL, f"collection_uuid={uuid} exists={exists}", d)


def _declared_parameters(wf_json) -> list:
    """The playbook's declared input parameter names, from the compiled JSON.

    A playbook that declares `parameters:` and is triggered with no `inputs`
    fails at the start step, and the failure is INVISIBLE: dry_run_playbook
    reports status=failed with error_message=None and failed_steps=[]. That is
    the signature that made this check unreadable for so long (see below), so
    the inputs are derived rather than hard-coded -- a trace playbook's
    parameters follow whatever ops the agent recorded.
    """
    try:
        out = []
        for wf in (wf_json.get("data") or [{}])[0].get("workflows") or []:
            for p in wf.get("parameters") or []:
                if p not in out:
                    out.append(p)
        return out
    except Exception:  # noqa: BLE001 -- shape drift must not break the check
        return []


# Environmental causes: the BOX cannot service this playbook, which is not a
# connector regression. Kept deliberately narrow -- a compile, push or engine
# fault must still FAIL.
_ENV_RUN_FAILURES = (
    "integration-12",                      # no connector configuration resolved
    "could not find a connector configuration",
    "not configured", "no config",
    "unreachable", "connection refused", "unauthorized", "forbidden",
)


def _failure_detail(fsr, wf_uuid: str) -> str:
    """The step-level reason a run failed, or "" if it can't be read.

    dry_run_playbook's envelope carries `error_message: None` and
    `failed_steps: []` for a large class of real failures -- the detail only
    populates on the run record fetched with step_detail=True. Without this the
    check reported `status=failed err=None failed_steps=[]`, which says nothing,
    and the true cause (INTEGRATION-12: no fortigate configuration resolved)
    was sitting on the box the whole time.
    """
    if not wf_uuid:
        return ""
    try:
        f = fsr.client.playbooks.why_failed(playbook_uuid=wf_uuid)
    except Exception as e:  # noqa: BLE001 -- diagnosis must never mask the failure
        return f"(could not read run detail: {e!r})"
    if not f:
        return ""
    return f"{f.failing_step or '?'}: {f.error_message or '(no message)'}"


_GATE_STEP = "Confirm Containment"     # insert_containment_confirm's step name
_GATE_STOP = "stop"                    # its non-containment branch


def _gate_for_run(fsr, task_id: str, retries: int = 10, delay: float = 2.0):
    """The pending manual-input record holding THIS run, or None.

    Keyed by task_id via pyfsr's `manual_input.pending_for_run`, which joins
    run → queue server-side (each row's `workflow` field is an opaque token, so
    a client-side join off `list()` is not possible). Retried because the
    manual-input record can lag behind the run reporting `awaiting`.
    """
    mi = getattr(fsr.client, "manual_input", None)
    if mi is None or not task_id:
        return None
    for _ in range(retries):
        try:
            rows = mi.pending_for_run(task_id, typed=False)
        except Exception:  # noqa: BLE001 -- transport hiccup; retry
            rows = []
        if rows:
            return rows[0]
        time.sleep(delay)
    return None


def _answer_gate(fsr, gate: dict, option_hint: str) -> tuple:
    """Answer a pending gate by its NON-primary branch (`option_hint`), so the
    gate is exercised without running the action it guards.

    Returns (ok, detail). The chosen branch is conveyed by its `step_iri` --
    picking the wrong button routes to the other branch, which here is the one
    that calls the firewall for real (a trace-compiled containment step carries
    no `mock_result`, so `use_mock_output` does not stop it -- see #51).
    """
    options = (gate.get("response_mapping") or {}).get("options") or []
    def _label(o):
        return f"{o.get('option') or ''} {o.get('display') or ''}".lower()
    chosen = next((o for o in options if option_hint in _label(o)), None)
    if chosen is None:
        chosen = next((o for o in options if not o.get("primary")), None)
    if chosen is None or not chosen.get("step_iri"):
        return False, f"no {option_hint!r} branch among {[_label(o) for o in options]}"
    try:
        fsr.client.manual_input.resume(
            gate.get("workflow"), step_iri=chosen["step_iri"],
            step_id=gate.get("step_id"), manual_input_id=int(gate.get("id")),
            user=fsr.client.manual_input._resolve_user_iri(), input={})
    except Exception as e:  # noqa: BLE001 -- the resume leg is what's under test
        return False, f"resume failed: {e!r}"
    return True, f"answered {chosen.get('option') or chosen.get('display')!r}"


def _poll_run(fsr, run_pk, until, timeout_s: int = 90) -> str:
    """Poll a run by pk until its status lands in `until`; return the last
    status seen (`"?"` if it was never readable)."""
    terminal = set(until)
    deadline = time.monotonic() + timeout_s
    status = "?"
    while time.monotonic() < deadline:
        try:
            d = fsr.client.get(f"/api/wf/api/workflows/{run_pk}/", params={"format": "json"})
        except Exception:  # noqa: BLE001
            d = None
        status = (d or {}).get("status") or status
        if status in terminal:
            return status
        time.sleep(2)
    return status


def chk_offer_playbook_runs(fsr) -> Result:
    """Prove the trace-compiled playbook RUNS in FSR: push → trigger → poll via
    dry_run_playbook → answer its human gate → run reaches a terminal state.

    REACHING THE GATE IS THE PASS CONDITION, not a failure to finish. Every
    trace-built containment playbook carries a `Confirm Containment`
    manual_input in front of the containment step (`insert_containment_confirm`)
    -- that is P2 gating carried into the artifact, and it is the whole point.
    A run that parks there is behaving correctly. This check used to wait for
    `finished`, which a gated playbook can never reach unattended: it burned the
    full read timeout and then reported `ReadTimeout` -- three minutes of wall
    clock to say "the gate worked", printed as if the compiled playbook were
    broken. Same wrong-assertion class as #43: the harness asserting the
    opposite of what the product promises.

    So: `awaiting` at the confirmation step is the pass, and the gate is then
    answered with **Stop** -- which exercises the gate and its skip branch, and
    drives the run to terminal, WITHOUT touching the firewall. Confirm is
    deliberately not used: a trace-compiled step has no `mock_result`, so
    `use_mock_output` substitutes nothing and the step would run live (#51).

    Inputs are supplied for every declared parameter. Omitting them silently
    fails the run at the start step with an empty error envelope -- verified
    directly: the same VT playbook reaches `finished` WITH inputs and `failed /
    err=None / failed_steps=[]` without them.

    NOTE on mocking, because this check used to claim safety it does not have
    ("useMockOutput=True so a containment step doesn't fire a real firewall
    change"). `useMockOutput=true` makes each step honor ITS OWN
    `arguments.mock_result`; it is a substitution, not a kill switch. A step
    that carries no mock_result has nothing to substitute and runs LIVE. A
    trace-compiled step has no mock_result, so this check really does call
    fortigate -- verified: with a resolvable config it reaches the appliance
    and returns the connector's own error. (`globalMock` must stay false; see
    tooling/cli.py -- with it true, some handlers ignore mock_result entirely.)

    So the IP is a TEST-NET-1 (RFC 5737) documentation address, never a live
    indicator, and the gate is answered with Stop rather than Confirm. Giving
    the containment step a mock_result is the real fix -- tracked on the board
    rather than done here, since it changes what the trace emits.
    """
    if "trace_compiled" not in CTX:
        return Result(BLOCKED, "no compiled trace from chk_trace_recorded")
    wf = CTX["trace_compiled"]
    fsr.purge_collection(fsr.collection_uuid(wf))
    inputs = {p: "192.0.2.1" for p in _declared_parameters(wf)}
    # cleanup=False: purging the collection removes the playbook, and the run
    # detail is looked up BY playbook uuid -- cleaning up first threw away the
    # only readable explanation of the failure. Purged by hand below instead.
    r = fsr.execute("dry_run_playbook",
                    {"workflow_json": wf, "inputs": inputs,
                     "use_mock_output": True, "cleanup": False},
                    timeout=300)
    d = r.data
    if d.get("status") == "finished":
        cleaned = fsr.purge_collection(d.get("collection_uuid"))
        return Result(PASS, f"ran→finished in {r.elapsed:.0f}s (ungated), "
                            f"cleaned={cleaned}", d)

    if d.get("status") == "awaiting":
        # The run reached its human gate. Assert WHICH gate, then answer Stop.
        awaiting = d.get("awaiting_steps") or []
        gate = _gate_for_run(fsr, d.get("task_id"))
        title = ((gate or {}).get("input") or {}).get("schema", {}).get("title", "")
        named = _GATE_STEP.lower() in " ".join(
            [str(s) for s in awaiting] + [str(title)]).lower()
        if gate is None:
            fsr.purge_collection(d.get("collection_uuid"))
            return Result(FAIL, f"run is awaiting {awaiting or '(unnamed)'} but no "
                                f"pending manual-input joined to task_id "
                                f"{d.get('task_id')}", d)
        # CTX for chk_respond_manual_input, which exercises the connector op
        # against this same gate id. Recorded BEFORE the resume below.
        CTX["gate"] = {"id": gate.get("id"), "run_pk": gate.get("workflow"),
                       "session": CTX.get("card_session")}
        answered, how = _answer_gate(fsr, gate, _GATE_STOP)
        final = _poll_run(fsr, gate.get("workflow"),
                          {"finished", "failed", "terminated", "skipped",
                           "finished_with_error", "rejected"}) if answered else "?"
        golden = dict(d, gate={"id": gate.get("id"), "title": title,
                               "answered": how, "final_status": final})
        fsr.purge_collection(d.get("collection_uuid"))
        if not answered:
            return Result(FAIL, f"reached the gate but could not answer it -- {how}",
                          golden)
        if final != "finished":
            # The Stop branch skips containment, so the rest of the playbook is
            # ordinary steps; anything but `finished` after it is a real fault.
            detail = _failure_detail(fsr, d.get("wf_uuid"))
            if any(s in detail.lower() for s in _ENV_RUN_FAILURES):
                return Result(SKIP, f"gate reached+answered, but the box cannot "
                                    f"run the rest -- {detail}", golden)
            return Result(FAIL, f"gate answered (Stop) but run ended {final!r} "
                                f"detail={detail or '(none)'}", golden)
        if not named:
            return Result(PASS, f"gated→awaiting in {r.elapsed:.0f}s, {how}, "
                                f"→finished (gate step not named "
                                f"{_GATE_STEP!r}: {awaiting or title!r})", golden)
        return Result(PASS, f"gated at {_GATE_STEP!r} in {r.elapsed:.0f}s, "
                            f"{how} → run {final}", golden)

    detail = _failure_detail(fsr, d.get("wf_uuid"))
    fsr.purge_collection(d.get("collection_uuid"))
    # A box that cannot configure the traced connector is not a regression --
    # mirrors chk_nonagent_playbook_runs, which skips when VT isn't usable.
    # Without this the check is permanently red, and a permanently red gate
    # gets read as scenery: the real regression hides behind the standing one.
    if any(s in detail.lower() for s in _ENV_RUN_FAILURES):
        return Result(SKIP, f"box cannot run the traced connector -- {detail}", d)
    return Result(FAIL, f"status={d.get('status')} inputs={sorted(inputs)} "
                        f"detail={detail or '(none)'}", d)


# A linear connector playbook (VirusTotal query_ip → set_variable). This is the
# green analog to chk_offer_playbook_runs: it proves a pushed, trace-STYLE
# playbook RUNS to `finished` against a real configured connector with a real
# API call.
#
# It exists because the fortigate trace can't show that on this box -- but NOT
# for the reason this comment used to give. It claimed "fortigate is agent-bound
# and the lab agent's execution runtime is dead". Both halves are false, and the
# claim cost a full misdiagnosis: every connector on the box (VirusTotal
# included, which passes) reports the SAME agent id, because that id is the
# local appliance, not a remote agent. Verified against the box:
#
#   fortigate, no config      -> INTEGRATION-12: Could not find a connector
#                                configuration matching the given id or name
#                                (its one config has default=false, so an empty
#                                `config:` resolves to nothing)
#   fortigate, explicit config -> the config DOES resolve; the fortigate
#                                connector itself then fails "Failed to block IP
#                                address with error list index out of range"
#
# So the traced connector is unusable here for its own reasons, and that second
# result also corrects the old "use_mock_output stops the real call" comment:
# mocking SUBSTITUTES a step's own `arguments.mock_result`, so a step without
# one runs live no matter how the run was triggered.
NONAGENT_CONNECTOR_YAML = """\
collection: FSRPB Smoke VT Linear
description: Linear non-agent connector run proof (VT query_ip -> set_variable).
visible: true
playbooks:
  - name: VT Linear Smoke
    description: Linear VT lookup, no decision branch.
    is_active: true
    parameters: [ip]
    steps:
      - name: start
        type: start
        next: Query VirusTotal
      - name: Query VirusTotal
        type: connector
        next: Extract malicious count
        # Step-level keys. The `arguments:` wrapper this used to nest them
        # under was removed from the schema, so the compiler rejected this
        # fixture outright -- meaning the check has not run since, and its
        # first execution (2026-08-01) was its first ever. A check that cannot
        # compile its own fixture reports a product failure and is really
        # reporting itself.
        connector: virustotal
        operation: query_ip
        config: ""
        params:
          ip: "{{ vars.input.params.ip }}"
      - name: Extract malicious count
        type: set_variable
        vars:
          malicious_count: "{{ vars.steps.Query_VirusTotal.data.attributes.last_analysis_stats.malicious | default(0) }}"
"""


@requires("connector:virustotal")
def chk_nonagent_playbook_runs(fsr) -> Result:
    """Prove a pushed playbook RUNS green on a real NON-agent connector
    (VirusTotal). Compile the linear YAML → dry_run with a real `ip` input and a
    real connector call → assert it reaches `finished`. Skips cleanly when
    VirusTotal isn't configured/reachable on the box (so it never falsely fails
    an instance without VT). Real call needs `inputs` AND use_mock_output=False:
    mock does NOT suppress the connector call, so a required param left blank
    fails the step."""
    c = fsr.execute("compile_yaml", {"yaml": NONAGENT_CONNECTOR_YAML})
    cd = c.data
    wf = cd.get("workflow_json")
    if not cd.get("ok") or not wf:
        return Result(FAIL, f"compile ok={cd.get('ok')} errors={cd.get('errors')}", cd)
    # cleanup=False for the same reason as chk_offer_playbook_runs: the failure
    # detail is fetched by playbook uuid, so purging first discards it.
    r = fsr.execute("dry_run_playbook",
                    {"workflow_json": wf, "inputs": {"ip": "8.8.8.8"},
                     "use_mock_output": False, "cleanup": False},
                    timeout=300)
    d = r.data
    if d.get("ok") and d.get("status") == "finished":
        d = dict(d, cleaned_up=fsr.purge_collection(d.get("collection_uuid")))
        return Result(PASS, f"ran→finished in {r.elapsed:.0f}s "
                            f"(real VT query_ip), cleaned={d.get('cleaned_up')}", d)
    # A VT-not-configured / unreachable box should SKIP, not FAIL. Consult the
    # step-level detail too: the envelope's error_message is None for a large
    # class of real failures, so matching on it alone missed the reason.
    detail = _failure_detail(fsr, d.get("wf_uuid"))
    fsr.purge_collection(d.get("collection_uuid"))
    blob = f"{d.get('error_message')} {d.get('failed_steps')} {detail}".lower()
    if any(s in blob for s in _ENV_RUN_FAILURES):
        return Result(SKIP, f"VirusTotal not usable on this box -- {detail or d.get('failed_steps')}", d)
    return Result(FAIL, f"status={d.get('status')} detail={detail or '(none)'} "
                        f"failed_steps={d.get('failed_steps')}", d)


def chk_respond_manual_input(fsr) -> Result:
    """Exercise the connector's HITL manual-input leg (contract §4) against the
    real gate id that `chk_offer_playbook_runs` produced.

    DECLINE, not approve. The op's forward always picks the manual-input's
    PRIMARY option -- here that is `Confirm`, which runs the containment step
    for real (no `mock_result`; see #51). `decision="reject"` short-circuits the
    forward by design, so this asserts exactly the branch a CI gate can safely
    take: no forward, `reason == "rejected"`, and a coherent resume envelope so
    the chat does not dead-end on the analyst. The approve leg is covered
    off-box by the contract harness and by probe_fsr.py's wfinput_resume.

    The run itself was already answered (Stop) and cleaned up by the offer
    check, so rejecting here mutates nothing -- the id is used only to drive the
    op's own code path.
    """
    gate = CTX.get("gate")
    if not gate:
        return Result(BLOCKED, "no live gate from chk_offer_playbook_runs")
    if not gate.get("session"):
        return Result(SKIP, "no agent session (--no-agent); op needs one to resume")
    r = fsr.execute("respond_manual_input",
                    {"session_id": gate["session"], "input_id": str(gate["id"]),
                     "decision": "reject"}, timeout=180)
    d = r.data
    fwd = d.get("manual_input_forward") or {}
    if d.get("error") and not d.get("stop_reason"):
        return Result(FAIL, f"error={d.get('error')}", d)
    if fwd.get("forwarded") is not False or fwd.get("reason") != "rejected":
        return Result(FAIL, f"decline should not forward; got {fwd}", d)
    # str() both sides: the platform coerces a numeric input_id to int in
    # flight, so the echo comes back as whatever type survived that trip.
    if str(d.get("input_id")) != str(gate["id"]):
        return Result(FAIL, f"input_id echoed {d.get('input_id')!r} != "
                            f"{gate['id']!r}", d)
    if not d.get("stop_reason"):
        return Result(FAIL, f"no stop_reason on the resume envelope; keys="
                            f"{sorted(d)[:12]}", d)
    return Result(PASS, f"declined gate {gate['id']} → {d.get('stop_reason')} "
                        f"in {r.elapsed:.0f}s", d)


CHECKS = [
    ("health", chk_health),
    # warmup is setup: a fresh $replace install ships an EMPTY slim catalog,
    # so it must run before compile/push/dry_run (which resolve cyops_utilities
    # for the end/stop → no_op step).
    ("warmup", chk_warmup),
    ("compile", chk_compile),
    ("validate_ok", chk_validate_ok),
    ("validate_bad", chk_validate_bad),
    ("resolve", chk_resolve),
    ("render_jinja", chk_render_jinja),
    ("push", chk_push),
    ("dry_run", chk_dry_run),
    ("enhance_diff_gate", chk_enhance_diff_gate),
    ("chat_build", chk_chat_build),
    ("chat_two_rounds", chk_chat_two_rounds),
    ("chat_action_card", chk_chat_action_card),
    ("chat_resume", chk_chat_resume),
    ("decide_once", chk_decide_once),
    # Full triage→playbook proof. These chain off the executed action_card
    # session (so they run after chat_resume) but are pure connector ops -- no
    # LLM round-trip -- so they aren't in _AGENT and self-skip under --no-agent
    # when the upstream agent checks didn't populate CTX.
    ("trace_recorded", chk_trace_recorded),
    ("offer_accept_pushes", chk_offer_accept_pushes),
    ("offer_playbook_runs", chk_offer_playbook_runs),
    ("nonagent_playbook_runs", chk_nonagent_playbook_runs),
    ("respond_manual_input", chk_respond_manual_input),
]

# ---- P3: agent-bound reach -------------------------------------------------
#
# The deck's most differentiated claim: an action reaches a target only a
# FortiSOAR agent can address, and the real result comes back. Agent-bound
# execute is fire-and-forget (`remote_status: in-progress`, no result), so the
# framework routes an agent config through a force-fail wrapper playbook to get
# the result back. This drives the whole product path on the INSTALLED
# connector: the analyst names the agent config, the card carries it, approval
# runs it on the agent, the result says so, and the firewall agrees.
#
# Discovered at run time, never hardcoded: the first agent-bound config of
# _AGENT_CONNECTOR is the target, and a config that runs on the box itself (if
# any) is the independent witness. A box with no agent-bound config SKIPs (not
# applicable); a dead agent SKIPs too -- lab agents live on throwaway hosts, and
# a dead one says nothing about the code.

_AGENT_CONNECTOR = "fortigate-firewall"
_QUARANTINE = {"method": "Quarantine Based", "vdom": ""}


def _agent_target(fsr):
    from fsr_playbooks.mcp_server.tools_execution import connector_config_options
    opts = connector_config_options(fsr.client, _AGENT_CONNECTOR)
    agent = next((o for o in opts if o.get("runs_on_agent")), None)
    local = next((o for o in opts if not o.get("runs_on_agent")), None)
    return agent, local


def _agent_last_sync(fsr, agent_id):
    for a in fsr.client.agents.list():
        a = a if isinstance(a, dict) else a.model_dump()
        if a.get("agentId") == agent_id:
            return a.get("healthCheckLastSync") or 0
    return 0


def _agent_alive(fsr, agent_id, wait_s=90):
    """A heartbeat's return proves nothing (it is fire-and-forget for a dead
    agent too); `healthCheckLastSync` advancing after one does."""
    before = _agent_last_sync(fsr, agent_id)
    fsr.client.agents.heartbeat(agent_id)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(5)
        if _agent_last_sync(fsr, agent_id) > before:
            return True
    return False


def _blocked_on(fsr, config_id, ip):
    import json
    r = fsr.client.connectors.execute(_AGENT_CONNECTOR, "get_blocked_ip",
                                      params=dict(_QUARANTINE), config=config_id)
    r = r.model_dump() if hasattr(r, "model_dump") else r
    return ip in json.dumps((r or {}).get("data"), default=str)


def _agent_run_op_result(transcript):
    """The run_op tool_result of an approved card: (input, content).

    The result frame carries only `tool_use_id`, so the call is matched by id:
    a run_op call's id picks its result."""
    run_ops = {c.id: c.args for c in tool_calls(transcript or []) if c.name == "run_op"}
    for r in tool_results(transcript or []):
        if r.tool_use_id in run_ops:
            return run_ops[r.tool_use_id], r.body or {}
    return None, None


@requires("connector:fortigate-firewall")
def chk_agent_block(fsr) -> Result:
    import json
    import random
    agent, local = _agent_target(fsr)
    if agent is None:
        return Result(SKIP, f"no agent-bound {_AGENT_CONNECTOR} config on this box")
    if not _agent_alive(fsr, agent["agent_id"]):
        return Result(SKIP, f"agent '{agent['agent_name']}' did not answer a heartbeat "
                            f"(healthCheckLastSync did not advance) -- environment, not code")
    ip = f"203.0.113.{random.randint(10, 250)}"   # TEST-NET-3: never a real host
    session = _session("agent")
    msg = (f"Block the IP {ip} on the FortiGate. Use the connector configuration "
           f"'{agent['name']}' for it.")
    d = fsr.execute("chat_turn", {"session_id": session, "intent": "triage",
                                  "messages": [{"role": "user", "content": msg}]},
                    timeout=300).data or {}
    card = _staged_card(d)
    if card is None:
        # The order was explicit (op, target, config), so a clarifying question
        # here is a defect too -- but name it, so the next run says which.
        asked_frames = frames_of(d, "choice_card", "choice")
        asked = asked_frames[-1] if asked_frames else None
        if asked:
            opts = [o.get("label") or o.get("value") for o in asked.get("options") or []]
            return Result(FAIL, f"asked instead of carding: {asked.get('prompt')!r} "
                                f"options={opts}", d)
        return Result(FAIL, f"no action_card (stop={d.get('stop_reason')})", d)
    if ip not in json.dumps(card, default=str):
        return Result(FAIL, f"card is not this block: {card.get('summary')}", d)
    chosen = (card.get("config") or {}).get("id")
    if chosen != agent["id"]:
        return Result(FAIL, f"card runs on {card.get('config')!r}, not the named agent "
                            f"config -- approval would land on the master", d)
    try:
        r = fsr.execute("chat_resume", {"session_id": session, "card_id": card.get("id"),
                                        "decision": "approve",
                                        "args": card.get("args") or {}}, timeout=300).data or {}
        args, content = _agent_run_op_result(r.get("transcript"))
        if content is None:
            return Result(FAIL, f"approve ran no run_op (stop={r.get('stop_reason')})", r)
        if (args or {}).get("config") != agent["id"]:
            return Result(FAIL, f"run_op config={args.get('config')!r}, not the card's", r)
        if not (isinstance(content, dict) and content.get("_via_agent")):
            return Result(FAIL, "run_op result did not come back through the agent "
                                f"(no _via_agent): {str(content)[:200]}", r)
        if ip not in json.dumps(content.get("data"), default=str):
            return Result(FAIL, f"agent result does not name {ip}: "
                                f"{str(content.get('data'))[:200]}", r)
        witness = "no on-box config to cross-check"
        if local is not None:
            for _ in range(12):
                if _blocked_on(fsr, local["id"], ip):
                    witness = f"confirmed via on-box config '{local['name']}'"
                    break
                time.sleep(5)
            else:
                return Result(FAIL, f"agent said blocked, firewall does not list {ip}", r)
        return Result(PASS, f"card on agent config -> approved -> ran on agent "
                            f"'{agent['agent_name']}' -> {ip} blocked; {witness}", r)
    finally:
        cleanup = local or agent
        try:
            fsr.client.connectors.execute(_AGENT_CONNECTOR, "unblock_ip",
                                          params={"method": "Quarantine Based",
                                                  "ip_addresses": ip, "vdom": ""},
                                          config=cleanup["id"])
        except Exception as e:  # noqa: BLE001 -- cleanup must not mask the verdict
            print(f"    (cleanup: unblock {ip} failed: {e})")


# On demand, like hunt/pivot: it needs a live agent on a lab host.
#   make live MODE=p3    (= live_integration.py --only agent_block)
AGENT_CHECKS = [
    ("agent_block", chk_agent_block),
    ("chat_action_card_failure", chk_chat_action_card_failure),
]

# Hunt/pivot is NOT part of the upload-and-validate gate: its runtime is
# dominated by live FortiSIEM op latency (~30s/op on a busy box) and how deep
# the agent chooses to pivot, which is an investigation-effectiveness question
# we tune separately -- not a connector-correctness check. Run it on demand:
#   python scripts/live_integration.py --only chat_hunt_pivot
HUNT_CHECKS = [
    ("chat_hunt_pivot", chk_chat_hunt_pivot),
]

# ---- live sanity (every dev push) -------------------------------------------
#
# ONE cheap real turn that proves the loop end to end on the box's DEFAULT
# config: the agent must call a tool to read a real record and quote a fact
# only that record holds. Content, not shape: the newest alert's name is read
# off the box first, and the answer must contain it. Read-only (a tier>=3
# run_op fails it). Run by `make live-sanity` in the widgets repo.

def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def chk_sanity_record_lookup(fsr) -> Result:
    page = fsr.client.get("/api/3/alerts", params={"$limit": 25, "$orderby": "-createDate"})
    members = (page or {}).get("hydra:member") or []
    rec = next((m for m in members if len(_norm(m.get("name"))) >= 6), None)
    if rec is None:
        return Result(SKIP, "box: no alert with a usable name to look up")
    uuid = str(rec.get("@id") or rec.get("uuid") or "").rsplit("/", 1)[-1]
    name = rec.get("name")
    r = fsr.execute("chat_turn", {
        "session_id": _session("sanity"), "intent": "triage",
        "messages": [{"role": "user", "content":
                      f"Look up the alert record /api/3/alerts/{uuid} with your tools "
                      "and tell me its exact name, quoted. One sentence."}]},
        timeout=240)
    d = r.data or {}
    calls = [c.name for c in tool_calls(d)]
    if not calls:
        return Result(FAIL, f"no tool call (stop_reason={d.get('stop_reason')}, "
                            f"error={r.error})", d)
    ran = _mutating_run_ops(d)
    if ran:
        return Result(FAIL, f"a read-only lookup dispatched a gated op: {ran}", d)
    text = assistant_text(d)
    if _norm(name) not in _norm(text):
        return Result(FAIL, f"answer does not quote the record name {name!r} "
                            f"(tools={calls}; answer={text[:160]!r})", d)
    return Result(PASS, f"tools={calls} answer quotes the record name "
                        f"in {r.elapsed:.0f}s", d)


SANITY_CHECKS = [
    ("sanity_record_lookup", chk_sanity_record_lookup),
]

_ALL_CHECKS = CHECKS + HUNT_CHECKS + AGENT_CHECKS + SANITY_CHECKS

_AGENT = {"chat_build", "chat_two_rounds", "chat_hunt_pivot", "chat_action_card",
          "chat_resume", "respond_manual_input", "agent_block",
          "chat_action_card_failure", "sanity_record_lookup"}


def _cleanup_containment(fsr) -> None:
    """Unblock the fixed containment-order IP if a run approved its block.

    `chat_action_card` always orders the same address, and nothing undid the
    block: the next run's agent could see it already banned and answer instead
    of carding -- a harness leftover reading as an agent regression. Best
    effort, on an on-box config; never changes the verdict."""
    if "card_session" not in CTX:
        return
    try:
        _agent, local = _agent_target(fsr)
        if local is None:
            return
        fsr.client.connectors.execute(
            _AGENT_CONNECTOR, "unblock_ip",
            params={"method": "Quarantine Based", "ip_addresses": _CONTAINMENT_IP,
                    "vdom": ""}, config=local["id"])
    except Exception as e:  # noqa: BLE001
        print(f"    (cleanup: unblock {_CONTAINMENT_IP} failed: {e})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-list of check names to run")
    ap.add_argument("--no-agent", action="store_true",
                    help="skip live-LLM paths")
    ap.add_argument("--golden", action="store_true",
                    help="save each check's response under tests/golden/live/")
    args = ap.parse_args(argv)

    only = set(args.only.split(",")) if args.only else None
    fsr = LiveFSR.from_env()
    print(f"live integration → {fsr.base}  connector={fsr.version}  "
          f"config={fsr.config}\n")

    # Default run = the upload/validate gate (CHECKS). When --only names a
    # hunt check explicitly, pull it from the combined registry.
    run_list = _ALL_CHECKS if only else CHECKS

    # One probe of the box's connector inventory, shared by every check that
    # declares a requirement. A check the box cannot run is a SKIP with the
    # reason, never a FAIL: the box is incomplete, the product was not tested.
    needed = {r for _, fn in run_list for r in getattr(fn, "requires", ())}
    caps = harness_caps(fsr.client) if needed else None

    results = []
    for name, fn in run_list:
        if only and name not in only:
            continue
        if args.no_agent and name in _AGENT:
            results.append((name, Result(SKIP, "--no-agent")))
            continue
        reason = skip_reason(getattr(fn, "requires", ()), caps) if caps is not None else None
        if reason:
            results.append((name, Result(SKIP, f"box: {reason}")))
            print(f"  - {name:<22} {SKIP:<7} box: {reason}")
            continue
        try:
            res = fn(fsr)
        except Exception as e:  # noqa: BLE001
            res = Result(FAIL, f"{type(e).__name__}: {e}")
        results.append((name, res))
        mark = {PASS: "✓", FAIL: "✗", SKIP: "-", BLOCKED: "⊘"}[res.status]
        detail = res.detail
        if res.status == BLOCKED and name in BLOCKED_BY:
            detail = f"{detail} (upstream: {BLOCKED_BY[name]})"
        print(f"  {mark} {name:<22} {res.status:<7} {detail}")
        if args.golden and res.golden is not None:
            fsr.save_golden(name, res.golden)

    _cleanup_containment(fsr)

    n_fail = sum(1 for _, r in results if r.status == FAIL)
    n_pass = sum(1 for _, r in results if r.status == PASS)
    n_skip = sum(1 for _, r in results if r.status == SKIP)
    blocked = [n for n, r in results if r.status == BLOCKED]
    print(f"\n{n_pass} passed, {n_fail} failed, {n_skip} skipped, "
          f"{len(blocked)} BLOCKED (unmeasured)")
    if blocked:
        # Spell it out: a blocked check proved nothing, and the reader should
        # not bank the tier as "everything measurable was measured".
        print(f"  unmeasured -- no evidence either way: {', '.join(blocked)}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    raise SystemExit(main())
