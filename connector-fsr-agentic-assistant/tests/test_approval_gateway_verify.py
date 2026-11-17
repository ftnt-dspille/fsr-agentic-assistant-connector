"""S2 — PersistedApprovalGateway must fail closed on a tampered / unbound
suspended session.

A store leak or writable sqlite file lets an attacker swap the stored tool
args before the human approves. The HMAC binding (approval_id + tool +
args_hash + created_at under the server secret) is recomputed in pop()/peek();
a mismatch must yield None so the substituted call is never re-dispatched.
"""
from __future__ import annotations

import pickle

from fsr_playbooks.llm import approvals as _approvals
from fsr_playbooks.llm.approvals import SuspendedSession

from storage import Storage, PersistedApprovalGateway


def _session(approval_id="appr-x", args=None) -> SuspendedSession:
    s = SuspendedSession(
        approval_id=approval_id, session_id="sess", tool="run_op",
        tool_use_id="tu-1",
        args=args if args is not None else {"connector": "fortigate",
                                            "operation": "block_ip"},
        tier=3, history_snapshot=[], prior_tool_result_blocks=[],
        remaining_tool_calls=[], system="sys", tags={},
        summary="Block IP",
    )
    return s


def _gateway(tmp_path):
    return PersistedApprovalGateway(Storage(tmp_path / "s2.db"))


def test_bound_session_round_trips(tmp_path):
    gw = _gateway(tmp_path)
    s = _session()
    _approvals.bind(s)
    gw.stash(s)
    popped = gw.pop(s.approval_id)
    assert popped is not None
    assert popped.tool == "run_op"


def test_unbound_session_fails_closed(tmp_path):
    gw = _gateway(tmp_path)
    s = _session()  # never bind() → no token
    gw.stash(s)
    assert gw.peek(s.approval_id) is None
    assert gw.pop(s.approval_id) is None
    # single-use: still consumed even though it failed verification
    assert gw.pop(s.approval_id) is None


def test_tampered_args_fail_closed(tmp_path):
    gw = _gateway(tmp_path)
    s = _session()
    _approvals.bind(s)
    gw.stash(s)
    # Simulate a store-tampering attacker swapping the args after bind,
    # keeping the now-stale token, and rewriting the row directly.
    s.args = {"connector": "fortigate", "operation": "delete_everything"}
    st = gw._storage
    with st._write() as c:
        c.execute(
            "UPDATE suspended_sessions SET payload=? WHERE approval_id=?",
            (pickle.dumps(s), s.approval_id),
        )
    assert gw.peek(s.approval_id) is None
    assert gw.pop(s.approval_id) is None
