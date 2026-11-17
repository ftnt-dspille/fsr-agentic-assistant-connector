"""An approved action card must EXECUTE, deterministically, and leave a trace.

The approve path used to render the human's decision as a natural-language
instruction ("Execute X now using exactly these arguments… Then report the
result") and hand it back to the model, leaving execution to the model's
discretion. Live on GA the model ignored it, spent the turn hunting for
unrelated FortiEDR search ops, and still opened its answer with "The host
'The-Flame' was successfully isolated" — an approved containment action that
never ran, reported as done, with nothing in the transcript to contradict it.

Approval is a human decision about a specific operation. Honouring it must not
be probabilistic, and it must be auditable afterwards.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


CARD = {
    "type": "action_card",
    "id": "isolate-host-The-Flame",
    "connector": "fortinet-fortiedr",
    "operation": "isolate_collector",
    "args": {"type": "Name", "devices": "The-Flame"},
}


class _Result:
    transcript = []
    tags = {}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


def _seed(tmp_path):
    from storage import Storage

    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", CARD)
    return storage


def _run(operations, storage, monkeypatch, dispatch_result, decision="approve",
         card_id=None, extra=None):
    """Drive a resume, stubbing dispatch, and return (dispatched, user_msg)."""
    seen: list[tuple] = []

    def _fake_dispatch(name, args, **kw):
        seen.append((name, args, kw))
        return dispatch_result

    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", _fake_dispatch)

    captured = {}

    async def _capture(**kw):
        captured["messages"] = kw.get("messages")
        return _Result()

    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _capture)

    out = operations._resume_conversation(
        {}, {"session_id": "sess", "card_id": card_id or CARD["id"], **(extra or {})}, decision,
        storage, provider=object())
    user_msg = captured["messages"][-1].content
    return seen, user_msg, out


def test_approve_dispatches_the_card_without_asking_the_model(
        operations, tmp_path, monkeypatch):
    """The regression: approval must not be a suggestion."""
    storage = _seed(tmp_path)
    seen, user_msg, _ = _run(
        operations, storage, monkeypatch,
        {"ok": True, "data": {"status": "Success"}})

    assert len(seen) == 1, f"card was not dispatched exactly once: {seen}"
    name, args, kw = seen[0]
    assert name == "run_op"
    assert args["connector"] == "fortinet-fortiedr"
    assert args["op"] == "isolate_collector"
    assert args["params"] == {"type": "Name", "devices": "The-Flame"}
    # The internal-only post-approval bypass, the same one the framework's
    # tier-3 resume uses. Without it the tier-4 gate would re-suspend the
    # action we were just told to run.
    assert args["_approved"] is True
    assert kw.get("_internal") is True

    # The model is told it ALREADY happened — there is no reading of this
    # input under which execution is still pending.
    assert "ALREADY BEEN EXECUTED" in user_msg
    assert "do not propose it again" in user_msg
    assert "Success" in user_msg, "the real result must reach the model"
    # Live, these lines came back to the analyst verbatim ("Confirmed: I did
    # not run it again", a "what this means for the case" section).
    assert "Do NOT run it again" not in user_msg
    assert "for the case" not in user_msg


def test_execution_is_recorded_in_the_transcript(operations, tmp_path,
                                                 monkeypatch):
    """'The assistant said so' is not an audit record."""
    storage = _seed(tmp_path)
    _, _, out = _run(operations, storage, monkeypatch,
                     {"ok": True, "data": {"status": "Success"}})

    kinds = [e.get("type") for e in out["transcript"]]
    assert "tool_use" in kinds and "tool_result" in kinds, (
        f"approved execution left no trace in the transcript: {kinds}")
    use = next(e for e in out["transcript"] if e["type"] == "tool_use")
    res = next(e for e in out["transcript"] if e["type"] == "tool_result")
    assert use["name"] == "run_op"
    assert use["input"]["op"] == "isolate_collector"
    assert res["tool_use_id"] == use["id"]
    assert res["content"]["ok"] is True

    # And on the live poll feed, so the analyst watches it run.
    frames = storage.read_turn_progress("sess", 0, 0)["frames"]
    assert any(f.get("type") == "tool_use" for f in frames)
    assert any(f.get("type") == "tool_result" for f in frames)


def test_failed_execution_is_never_narrated_as_success(operations, tmp_path,
                                                       monkeypatch):
    """A 400 from the vendor must reach the model as a failure, plainly."""
    storage = _seed(tmp_path)
    seen, user_msg, out = _run(
        operations, storage, monkeypatch,
        {"ok": False, "code": "op_request_rejected",
         "message": "No matching collectors"})

    assert len(seen) == 1
    assert "FAILED" in user_msg
    assert "Do not claim the action succeeded" in user_msg
    assert "No matching collectors" in user_msg
    res = next(e for e in out["transcript"] if e["type"] == "tool_result")
    assert res["content"]["ok"] is False


def test_reject_does_not_dispatch(operations, tmp_path, monkeypatch):
    """The gate still has to be a gate."""
    storage = _seed(tmp_path)
    seen, user_msg, out = _run(
        operations, storage, monkeypatch, {"ok": True}, decision="reject")

    assert seen == [], "a rejected card was executed anyway"
    assert "cancelled" in user_msg.lower()
    assert not any(e.get("type") == "tool_use" for e in out["transcript"])


def test_dispatch_raising_is_reported_not_swallowed(operations, tmp_path,
                                                    monkeypatch):
    """An exception mid-dispatch must land as a visible failure, not silence."""
    storage = _seed(tmp_path)

    def _boom(name, args, **kw):
        raise RuntimeError("connector exploded")

    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", _boom)

    captured = {}

    async def _capture(**kw):
        captured["messages"] = kw.get("messages")
        return _Result()

    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _capture)

    out = operations._resume_conversation(
        {}, {"session_id": "sess", "card_id": CARD["id"]}, "approve",
        storage, provider=object())

    user_msg = captured["messages"][-1].content
    assert "FAILED" in user_msg
    assert "connector exploded" in user_msg
    res = next(e for e in out["transcript"] if e["type"] == "tool_result")
    assert res["content"]["ok"] is False


# --------------------------------------------------------------------------
# Every tier-3 tool, not just run_op.
#
# Live on .206 an analyst asked "Add a step to this device", the agent called
# run_playbook, it carded at tier 3, the analyst approved -- and nothing ran.
# No playbook run on the box, no tool_result in the transcript, the turn just
# ended. Cause: `_approval_to_action_card` returned None for every non-run_op
# tool, so the approval stayed a legacy `approval_request`; that type is not in
# `_GATE_TYPES`, so it was never persisted as a card; so `_execute_approved_card`
# found nothing to dispatch and fell back to *telling the model* an approval had
# happened. The run_op family was fixed for this in 2.5.1 and every other
# tier-3 tool kept the hole.
# --------------------------------------------------------------------------

# The REAL persisted shape: a tier-3 run_playbook stays an `approval_request`
# (that surface renders better than an action_card -- verified in the widget
# with Playwright), and its call lives under `preview.args`.
PB_CARD = {
    "type": "approval_request",
    "approval_id": "run-pb-add-step",
    "tool_use_id": "tu-1",
    "tool": "run_playbook",
    "tier": 3,
    "preview": {"tool": "run_playbook",
                "args": {"playbook": "Add Step from Action to Device",
                         "follow": True, "_summary": "internal"}},
}


def _seed_card(tmp_path, card):
    from storage import Storage

    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", card)
    return storage


def test_approved_run_playbook_actually_runs(operations, tmp_path, monkeypatch):
    """The .206 regression: approving a run_playbook card must RUN it."""
    storage = _seed_card(tmp_path, PB_CARD)
    seen, user_msg, out = _run(
        operations, storage, monkeypatch,
        {"ok": True, "playbook": "Add Step from Action to Device",
         "triggered": True, "run": {"status": "finished"}}, card_id=PB_CARD["approval_id"])

    assert len(seen) == 1, f"approved run_playbook was not dispatched: {seen}"
    name, args, kw = seen[0]
    assert name == "run_playbook", f"dispatched the wrong tool: {name}"
    assert args["playbook"] == "Add Step from Action to Device"
    assert args["follow"] is True
    # Same internal-only bypass run_op uses, or the tier gate re-suspends the
    # very call the analyst just approved.
    assert args["_approved"] is True
    assert kw.get("_internal") is True

    assert "ALREADY BEEN EXECUTED" in user_msg
    assert "run_playbook" in user_msg
    # `_summary` is plumbing -- it must not be re-sent as a call argument.
    assert "_summary" not in args

    use = next(e for e in out["transcript"] if e["type"] == "tool_use")
    assert use["name"] == "run_playbook"


def test_approved_run_playbook_failure_is_not_narrated_as_success(
        operations, tmp_path, monkeypatch):
    """`record_required` is the most likely real failure -- it must reach the
    analyst as a failure, with the reason, not as a cheerful 'done'."""
    storage = _seed_card(tmp_path, PB_CARD)
    _, user_msg, out = _run(
        operations, storage, monkeypatch,
        {"ok": False, "code": "record_required",
         "message": "runs against a selected record and none is bound"}, card_id=PB_CARD["approval_id"])

    assert "FAILED" in user_msg
    assert "Do not claim the action succeeded" in user_msg
    assert "record_required" in user_msg
    res = next(e for e in out["transcript"] if e["type"] == "tool_result")
    assert res["content"]["ok"] is False


def test_approval_with_nothing_to_dispatch_is_loud(operations, tmp_path,
                                                   monkeypatch):
    """The dead-end itself. An approval card with no tool must never resolve
    quietly -- that silence is exactly what shipped to .206."""
    storage = _seed_card(tmp_path, {"type": "approval_request",
                                    "approval_id": "empty-card"})
    seen, user_msg, out = _run(
        operations, storage, monkeypatch, {"ok": True}, card_id="empty-card")

    assert seen == [], "an empty card should not invent a call"
    assert "could NOT be executed" in user_msg
    assert "did not run" in user_msg
    res = next(e for e in out["transcript"] if e["type"] == "tool_result")
    assert res["content"]["ok"] is False
    assert res["content"]["code"] == "approval_not_dispatchable"


def test_non_runop_approval_keeps_its_own_rendering(operations):
    """A tool-only approval is NOT converted to an action_card. Measured in the
    widget: approval_request shows a labelled arg table + raw toggle + note
    field, while a tool-only action_card renders as a bare title chip with no
    arguments at all. Converting would make the approval surface worse. The
    execution fix lives at the persist/dispatch end instead."""
    assert operations._approval_to_action_card({
        "tool": "run_playbook",
        "approval_id": "appr-1",
        "tool_use_id": "call_abc",
        "preview": {"args": {"playbook": "Add Step from Action to Device"}},
    }) is None


def test_runop_card_shape_is_unchanged(operations):
    """The widget renders run_op cards off connector/operation -- generalizing
    the executor must not disturb that shape."""
    card = operations._approval_to_action_card({
        "tool": "run_op",
        "approval_id": "appr-2",
        "tool_use_id": "call_def",
        "preview": {"args": {"connector": "fortinet-fortiedr",
                             "op": "isolate_collector",
                             "params": {"devices": "The-Flame"}}},
    })

    assert card["connector"] == "fortinet-fortiedr"
    assert card["operation"] == "isolate_collector"
    assert card["args"] == {"devices": "The-Flame"}


# --- #78: record-write cards shaped with target/changes/context_match ---------

def test_update_record_card_has_target_and_changes(operations):
    """update_record approval is shaped as an action_card with target/changes
    instead of a raw args blob."""
    card = operations._approval_to_action_card({
        "tool": "update_record",
        "approval_id": "appr-upd",
        "tool_use_id": "call_upd",
        "preview": {"args": {"module": "alerts",
                             "uuid": "d39ecc9a-2968-42d5-948d-ce96fd76b227",
                             "fields": {"severity": "Critical",
                                        "status": "In Progress"}}},
    })
    assert card is not None
    assert card["type"] == "action_card"
    assert card["target"]["module"] == "alerts"
    assert card["target"]["uuid"] == "d39ecc9a-2968-42d5-948d-ce96fd76b227"
    assert card["context_match"] == "unknown"
    changes = card["changes"]
    assert len(changes) == 2
    assert {"field": "severity", "after": "Critical"} in changes
    assert {"field": "status", "after": "In Progress"} in changes
    assert card["summary"] == "Update this alert"


def test_create_record_card_has_target_and_changes(operations):
    """create_record approval is shaped with target/changes (no before values)."""
    card = operations._approval_to_action_card({
        "tool": "create_record",
        "approval_id": "appr-crt",
        "tool_use_id": "call_crt",
        "preview": {"args": {"module": "incidents",
                             "fields": {"name": "New Incident",
                                        "severity": "High"}}},
    })
    assert card is not None
    assert card["target"]["module"] == "incidents"
    assert "uuid" not in card["target"]
    assert card["changes"] == [
        {"field": "name", "after": "New Incident"},
        {"field": "severity", "after": "High"},
    ]
    assert card["summary"] == "Create an incident"


def test_delete_record_card_has_target_no_changes(operations):
    """delete_record approval identifies the target; no changes list needed."""
    card = operations._approval_to_action_card({
        "tool": "delete_record",
        "approval_id": "appr-del",
        "tool_use_id": "call_del",
        "preview": {"args": {"module": "alerts",
                             "uuid": "d39ecc9a-2968-42d5-948d-ce96fd76b227"}},
    })
    assert card is not None
    assert card["target"]["module"] == "alerts"
    assert card["target"]["uuid"] == "d39ecc9a-2968-42d5-948d-ce96fd76b227"
    assert "changes" not in card
    assert card["summary"] == "Delete this alert"


def test_record_write_card_without_module_returns_none(operations):
    """A malformed call with no module does not get a card."""
    assert operations._approval_to_action_card({
        "tool": "update_record",
        "approval_id": "appr-bad",
        "tool_use_id": "call_bad",
        "preview": {"args": {"uuid": "abc", "fields": {"x": 1}}},
    }) is None


def test_iri_uuid_extracts_last_segment(operations):
    """_iri_uuid parses /api/3/<module>/<uuid>."""
    assert operations._iri_uuid("/api/3/alerts/d39ecc9a-abc") == "d39ecc9a-abc"
    assert operations._iri_uuid("/api/3/alerts/d39ecc9a-abc/") == "d39ecc9a-abc"
    assert operations._iri_uuid("") == ""
    assert operations._iri_uuid(None) == ""


# ── record-write cards: args are fields-only, target carries the coordinates ──

RECORD_CARD = {
    "type": "action_card",
    "id": "update-alert-desc",
    "tool": "update_record",
    # `args` is the analyst-editable FIELD surface (see
    # _shape_record_write_card), NOT the tool's call shape.
    "args": {"description": "probe-nonce"},
    "target": {"module": "alerts", "uuid": "abcd1234-0000-0000-0000-000000000001"},
}


def test_approved_record_write_rebuilds_the_call_shape(operations, tmp_path,
                                                       monkeypatch):
    """Executing the card's fields-only `args` verbatim called
    update_record(description=...) -- no module, no uuid, no fields nesting --
    so the approve returned ok and the row never changed (probe T2). The
    approve path must rebuild {module, uuid, fields} from target + args, and a
    write persona must be ACTIVE during the dispatch -- the card executes
    before the resume turn's own profile bind, so without a bind of its own
    the write body refused an already-approved write."""
    from storage import Storage
    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "triage")
    storage.set_session_module("sess", "alerts")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", RECORD_CARD)
    seen, _, _ = _run(operations, storage, monkeypatch,
                      {"ok": True}, card_id=RECORD_CARD["id"])
    assert seen, "approve must dispatch"
    name, args, _ = seen[0]
    assert name == "update_record"
    assert args["module"] == "alerts"
    assert args["uuid"] == "abcd1234-0000-0000-0000-000000000001"
    assert args["fields"] == {"description": "probe-nonce"}
    assert "description" not in args, "fields must be nested, not flattened"


def test_approved_create_record_rebuilds_the_call_shape(operations, tmp_path,
                                                        monkeypatch):
    from storage import Storage
    card = {"type": "action_card", "id": "create-note", "tool": "create_record",
            "args": {"name": "n1"}, "target": {"module": "assistant_skills"}}
    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", card)
    seen, _, _ = _run(operations, storage, monkeypatch, {"ok": True},
                      card_id=card["id"])
    name, args, _ = seen[0]
    assert name == "create_record"
    args = {k: v for k, v in args.items() if not k.startswith("_")}
    assert args == {"module": "assistant_skills", "fields": {"name": "n1"}}


def test_write_persona_is_active_during_card_execution(operations, tmp_path,
                                                       monkeypatch):
    """The card executes BEFORE the resume turn's profile bind, so the exec
    path must bind one itself: with no Key Store persona, the host-record
    fallback scoped to the session's mounted module."""
    from storage import Storage
    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "triage")
    storage.set_session_module("sess", "alerts")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", RECORD_CARD)

    from fsr_soc_triage import profiles as _profiles
    bound = {}

    def _fake_dispatch(name, args, **kw):
        bound["profile"] = _profiles.get_active_profile()
        return {"ok": True}

    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", _fake_dispatch)

    class _R:
        transcript = []
        tags = {}
        error = None
        stop_reason = "end_turn"
        last_assistant_yaml = None

    async def _capture(**kw):
        return _R()

    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _capture)

    operations._resume_conversation(
        {}, {"session_id": "sess", "card_id": RECORD_CARD["id"]}, "approve",
        storage, provider=object())

    prof = bound.get("profile")
    assert prof is not None, (
        "no persona active while the approved card executed -- the write "
        "body would refuse an already-approved write")
    assert prof.may_write_module("alerts")
    assert _profiles.get_active_profile() is None, "bind must be reset"


# ── #149: an approved success that only QUEUED a playbook is not an outcome ──

def _run_async(operations, tmp_path, monkeypatch, outcome):
    """Approve CARD with a dispatch result that carries only a task_id, and
    the outcome poll stubbed to `outcome` (None = box unpollable)."""
    storage = _seed(tmp_path)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_poll_task_outcome", lambda tid, **kw: outcome)
    seen, user_msg, _ = _run(operations, storage, monkeypatch,
                             {"ok": True, "result": {"task_id": "wf-task-9"}})
    assert seen, "approve must dispatch"
    return user_msg


def test_queued_playbook_failure_is_narrated_as_failure(operations, tmp_path,
                                                        monkeypatch):
    """The T1 shape: block_indicator returns success+task_id, the stock
    playbook fails 2s later, and the model announced a landed block."""
    msg = _run_async(operations, tmp_path, monkeypatch,
                     {"status": "failed", "failing_step": "Block IP",
                      "error_message": "people/… 404"})
    assert "did NOT land" in msg
    assert "Block IP" in msg and "404" in msg
    assert "HAS ALREADY BEEN EXECUTED" not in msg


def test_unpollable_task_is_narrated_as_pending(operations, tmp_path,
                                                monkeypatch):
    msg = _run_async(operations, tmp_path, monkeypatch, None)
    assert "QUEUED" in msg and "task wf-task-9" in msg
    assert "outcome" in msg and "pending" in msg.lower()
    assert "HAS ALREADY BEEN EXECUTED" not in msg


def test_pending_task_is_narrated_as_pending(operations, tmp_path, monkeypatch):
    msg = _run_async(operations, tmp_path, monkeypatch, {"status": "pending"})
    assert "QUEUED" in msg
    assert "HAS ALREADY BEEN EXECUTED" not in msg


def test_finished_task_keeps_the_success_narration(operations, tmp_path,
                                                   monkeypatch):
    msg = _run_async(operations, tmp_path, monkeypatch, {"status": "finished"})
    assert "HAS ALREADY BEEN EXECUTED" in msg


def test_terminal_result_shapes_are_not_treated_as_async(operations):
    assert operations._async_task_id(
        {"ok": True, "status": "finished", "task_id": "t"}) is None
    assert operations._async_task_id({"ok": True, "records": [1]}) is None
    assert operations._async_task_id(
        {"ok": True, "result": {"task_id": "t"}}) == "t"


def test_mcp_funnel_enriches_async_handoffs(monkeypatch):
    """The native-MCP call funnel must splice the queued run's outcome (or an
    honest pending verdict) into a task_id-only result -- this is the path
    the framework's tier-3 resume reads, which _execute_approved_card never
    sees (T1 narrated success via approval_request while the run had failed)."""
    from fsr_soc_triage import async_outcome
    monkeypatch.setattr(async_outcome, "poll_task_outcome",
                        lambda tid, **kw: {"status": "failed",
                                           "error_message": "people 404"})
    out = async_outcome.enrich_async_result(
        {"status": "success", "result": {"task_id": "t-1"}})
    ap = out["async_playbook"]
    assert ap["status"] == "failed" and ap["task_id"] == "t-1"
    assert "did NOT land" in ap["verdict"]

    # unpollable box -> pending verdict, never silence
    monkeypatch.setattr(async_outcome, "poll_task_outcome", lambda tid, **kw: None)
    out2 = async_outcome.enrich_async_result({"result": {"task_id": "t-2"}})
    assert "pending" in out2["async_playbook"]["verdict"]

    # a result already carrying a terminal status passes through untouched
    sync = {"status": "finished", "task_id": "t-3"}
    assert async_outcome.enrich_async_result(dict(sync)) == sync


def test_an_already_enriched_async_result_is_not_polled_again(
        operations, tmp_path, monkeypatch):
    """#149 residual: the native-MCP funnel already spliced `async_playbook`
    into the result (and left task_id at top level). The card path must trust
    that outcome, not chase the task a second time -- each extra poll is up to
    12 s plus a duplicate listing call on the box."""
    polls = []

    def _counting_poll(tid, **kw):
        polls.append(tid)
        return {"status": "finished"}

    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"],
                        "_poll_task_outcome", _counting_poll)
    storage = _seed(tmp_path)
    prefilled = {"status": "failed", "task_id": "wf-task-9",
                 "failing_step": "Block IP", "error_message": "people 404",
                 "verdict": "the queued playbook FAILED"}
    seen, user_msg, _ = _run(
        operations, storage, monkeypatch,
        {"ok": True, "result": {"task_id": "wf-task-9"},
         "async_playbook": dict(prefilled)})
    assert seen, "approve must dispatch"
    assert polls == [], f"already-enriched result was polled again: {polls}"
    # The pre-filled (failed) outcome is what the model is told, not a fresh
    # poll's success.
    assert "did NOT land" in user_msg
    assert "HAS ALREADY BEEN EXECUTED" not in user_msg


# -- batched approval: one card, several gated calls -------------------------
STEPS = "ztpf_automation_profile_steps"


def _step_call(n):
    return {"tool": "create_record", "tool_use_id": f"call_{n}",
            "summary": f"Create step {n}",
            "args": {"module": STEPS, "fields": {"name": f"Step {n}",
                                                 "stepSequence": n}}}


def test_a_batched_approval_card_lists_every_call(operations):
    """The rows keep the raw call (what executes) and the display shape the
    lead call gets (what the analyst reads)."""
    card = operations._approval_to_action_card({
        "tool": "create_record", "approval_id": "appr-b", "tool_use_id": "call_1",
        "summary": "Create step 1",
        "preview": {"args": {"module": STEPS, "fields": {"name": "Step 1"}}},
        "batch": [_step_call(2), _step_call(3)],
    })
    assert [r["summary"] for r in card["batch"]] == ["Create step 2", "Create step 3"]
    row = card["batch"][0]
    assert row["tool"] == "create_record"
    assert row["args"] == {"module": STEPS, "fields": {"name": "Step 2", "stepSequence": 2}}
    assert row["target"] == {"module": STEPS, "module_label": "Ztpf automation profile step"}
    assert {"field": "name", "after": "Step 2"} in row["changes"]


def test_a_single_call_card_has_no_batch(operations):
    card = operations._approval_to_action_card({
        "tool": "create_record", "approval_id": "a", "tool_use_id": "c",
        "preview": {"args": {"module": STEPS, "fields": {"name": "x"}}}})
    assert "batch" not in card


def _batch_storage(tmp_path, batch):
    from storage import Storage
    card = {"type": "action_card", "id": "appr-b", "tool": "create_record",
            "args": {"name": "Step 1", "stepSequence": 1},
            "target": {"module": STEPS}, "batch": batch}
    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", card)
    return storage, card


def test_approving_a_batched_card_runs_every_call_in_order(operations, tmp_path,
                                                            monkeypatch):
    storage, card = _batch_storage(tmp_path, [_step_call(2), _step_call(3)])
    seen, user_msg, _ = _run(operations, storage, monkeypatch,
                             {"ok": True, "iri": "/api/3/x/new"}, card_id=card["id"])
    names = [(n, a["fields"]["name"], a["_approved"]) for n, a, _ in seen]
    assert names == [("create_record", "Step 1", True),
                     ("create_record", "Step 2", True),
                     ("create_record", "Step 3", True)]
    # Every executed call is on the turn's record, lead and batch alike.
    prog = storage.read_turn_progress("sess")
    frames = prog.get("frames") or prog.get("events") or []
    uses = [f for f in frames if f.get("type") == "tool_use"]
    assert [u["input"]["fields"]["name"] for u in uses] == ["Step 1", "Step 2", "Step 3"]
    # The model is told all of them already ran, so it does not re-issue them.
    assert "2 more action(s)" in user_msg
    assert "Create step 2: done" in user_msg and "Create step 3: done" in user_msg
    assert "Do NOT call any of them again" in user_msg


def test_a_failed_batch_member_is_reported_as_failed(operations, tmp_path,
                                                     monkeypatch):
    storage, card = _batch_storage(tmp_path, [_step_call(2)])
    results = iter([{"ok": True}, {"ok": False, "code": "http_400",
                                   "message": "bad field"}])
    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", lambda n, a, **k: next(results))
    captured = {}

    async def _capture(**kw):
        captured["messages"] = kw.get("messages")
        return _Result()
    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _capture)
    operations._resume_conversation({}, {"session_id": "sess", "card_id": card["id"]},
                                    "approve", storage, provider=object())
    msg = captured["messages"][-1].content
    assert "Create step 2: FAILED -- bad field" in msg
    assert "do not claim they succeeded" in msg


def test_rejecting_a_batched_card_runs_nothing(operations, tmp_path, monkeypatch):
    storage, card = _batch_storage(tmp_path, [_step_call(2), _step_call(3)])
    seen, user_msg, _ = _run(operations, storage, monkeypatch, {"ok": True},
                             decision="reject", card_id=card["id"])
    assert seen == []
    # ...and the model hears the whole batch was declined, not just the lead.
    assert "2 more action(s)" in user_msg and "none of them ran" in user_msg


# -- where an approved card runs ----------------------------------------------
# The card used to carry no config, so an approved action always ran on the
# connector's default -- live, a block the analyst asked to run on the
# agent-bound FortiGate config ran on the master's default instead.

AGENT_CARD = {**CARD, "id": "block-on-agent",
              "config": {"id": "agt-1", "name": "Lab FortiGate (edge-agent)",
                         "runs_on_agent": True},
              "config_options": [{"id": "loc-1", "name": "fortigate-lab"},
                                 {"id": "agt-1", "name": "Lab FortiGate (edge-agent)"}]}


def _seed_agent(tmp_path):
    storage = _seed(tmp_path)
    storage.save_card("sess", AGENT_CARD)
    return storage


def test_approved_card_runs_on_the_cards_config(operations, tmp_path, monkeypatch):
    seen, _, out = _run(operations, _seed_agent(tmp_path), monkeypatch,
                        {"ok": True, "data": {}}, card_id="block-on-agent")
    assert seen[0][1]["config"] == "agt-1"
    use = next(e for e in out["transcript"] if e["type"] == "tool_use")
    assert use["input"]["config"] == "agt-1", "the audit record must say where it ran"


def test_the_analysts_pick_from_the_cards_options_wins(operations, tmp_path, monkeypatch):
    seen, _, _ = _run(operations, _seed_agent(tmp_path), monkeypatch,
                      {"ok": True, "data": {}}, card_id="block-on-agent",
                      extra={"config": "loc-1"})
    assert seen[0][1]["config"] == "loc-1"


def test_a_config_the_card_never_offered_is_ignored(operations, tmp_path, monkeypatch):
    seen, _, _ = _run(operations, _seed_agent(tmp_path), monkeypatch,
                      {"ok": True, "data": {}}, card_id="block-on-agent",
                      extra={"config": "some-other-config"})
    assert seen[0][1]["config"] == "agt-1"


def test_a_card_without_config_runs_on_the_default(operations, tmp_path, monkeypatch):
    seen, _, _ = _run(operations, _seed(tmp_path), monkeypatch, {"ok": True, "data": {}})
    assert "config" not in seen[0][1]


@pytest.mark.parametrize("extra, want", [({}, "agt-1"), ({"config": "loc-1"}, "loc-1"),
                                         ({"config": "elsewhere"}, "agt-1")])
def test_the_direct_execute_path_also_runs_on_the_cards_config(
        operations, tmp_path, monkeypatch, extra, want):
    """chat_resume's action_card APPROVE branch executes through
    _resume_action_card_execute, a second path with its own dispatch."""
    storage = _seed_agent(tmp_path)
    seen = []

    def _fake_dispatch(name, args, **kw):
        seen.append((name, args))
        return {"ok": True, "data": {}}

    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", _fake_dispatch)

    async def _capture(**kw):
        return _Result()

    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _capture)
    out = operations._resume_action_card_execute(
        {}, {"session_id": "sess", "card_id": "block-on-agent", **extra}, "approve",
        storage, provider=object())
    assert [a["config"] for n, a in seen if n == "run_op"] == [want]
    use = next(e for e in out["transcript"] if e["type"] == "tool_use" and e["name"] == "run_op")
    assert use["input"]["config"] == want


def test_the_dispatch_names_who_decided(operations, tmp_path, monkeypatch):
    """The audit row's actor is the decider chat_resume bound, and "human"
    when nothing bound one."""
    approvals = sys.modules["_fsrpb_conn.ops_approvals"]
    seen, _, _ = _run(operations, _seed(tmp_path), monkeypatch, {"ok": True})
    assert seen[0][2].get("approved_by") == "human"

    token = approvals._DECIDER.set("Ana Reviewer")
    try:
        seen, _, _ = _run(operations, _seed(tmp_path / "b"), monkeypatch, {"ok": True})
    finally:
        approvals._DECIDER.reset(token)
    assert seen[0][2].get("approved_by") == "Ana Reviewer"


def test_only_a_verified_name_is_recorded_as_the_approver(operations):
    """The widget's `actor` is a claim any caller can set: without the reviewer
    check's verified name, the audit row must not read as if it named them."""
    approvals = sys.modules["_fsrpb_conn.ops_approvals"]
    label = approvals._approver_label
    assert label("Ana", {"actor": "claimed"}) == "Ana"
    assert label(None, {"actor": {"name": "Bo"}}) == "human (unverified: Bo)"
    assert label(None, {}) == "human"


def test_call_mcp_tool_is_authorized_and_audited(operations, monkeypatch):
    """A playbook (or the action panel) calling a tool directly goes through
    the same decision and audit log as the chat, under its own actor."""
    from fsr_playbooks.llm import authorization
    mcp = sys.modules["_fsrpb_conn.ops_mcp"]
    monkeypatch.setattr(mcp, "_mcp_registry_for",
                        lambda config: {"list_configured_connectors": lambda: {"ok": True}})
    authorization.clear_audit_log()
    out = operations.call_mcp_tool({}, {"tool": "list_configured_connectors"})
    assert out == {"ok": True}
    row = authorization.snapshot_audit_log()[-1]
    assert (row["tool"], row["decision"], row["actor"]) == (
        "list_configured_connectors", "approved", "connector:call_mcp_tool")



def _approve_write(operations, tmp_path, monkeypatch, fields, stored):
    from storage import Storage
    card = dict(RECORD_CARD, args=fields,
                target={"module": "ztpf_automation_actions", "uuid": "a1"})
    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "build")
    storage.set_session_module("sess", "ztpf_automation_actions")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", card)
    result = {"ok": True, "module": "ztpf_automation_actions", "action": "update",
              "iri": "/api/3/ztpf_automation_actions/a1", "uuid": "a1",
              "record": stored}
    _, user_msg, _ = _run(operations, storage, monkeypatch, result, card_id=card["id"])
    return user_msg


_STORED = {"@id": "/api/3/ztpf_automation_actions/a1", "name": "Interfaces",
           "outputTemplateJSON": "{{ x }}" * 900,
           "exampleJinjaVars": {"data": {"blob": "z" * 100000}}}


def test_an_approved_write_is_read_back_not_dumped(operations, tmp_path, monkeypatch):
    """Live, the resume turn after a template save carried the whole stored
    record (117K characters) and cost 50-60K input tokens. It needs what the
    write saved, not the record."""
    msg = _approve_write(operations, tmp_path, monkeypatch,
                         {"outputTemplateJSON": "{{ x }}" * 900}, dict(_STORED))
    assert len(msg) < 8000, len(msg)
    assert "zzzz" not in msg, "the record's other fields must not ride along"
    assert '"saved"' in msg and "outputTemplateJSON" in msg
    assert "6300 characters saved" in msg
    assert "not_saved" not in msg


def test_a_field_the_record_does_not_hold_is_named_as_not_saved(operations, tmp_path,
                                                                 monkeypatch):
    """Live: outputTemplateJson (the field is outputTemplateJSON) answered ok
    and saved nothing, and the model said the update succeeded."""
    msg = _approve_write(operations, tmp_path, monkeypatch,
                         {"outputTemplateJson": "[]"}, dict(_STORED))
    assert '"not_saved": [' in msg and "outputTemplateJson" in msg
    assert "anything that did not save" in msg
