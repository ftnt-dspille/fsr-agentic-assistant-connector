"""`_envelope_stop_reason` must surface a tool-level approval suspension as the
widget-facing `approval_required`, even when the wire transcript's LAST event is
the trailing `usage` frame the provider emits after the ApprovalRequestEvent.

Regression: push_playbook (a non-run_op tier-gated tool) is left as a legacy
`approval_request` by `_approval_to_action_card`, NOT truncated to be last. The
trailing usage frame made the `t == "approval_request"` check miss, so the
envelope stayed `pending_approval` — a reason the widget doesn't handle, leaving
the chat stuck on a PENDING card (session yq8nhcix).
"""
from __future__ import annotations

from connector_fsr_agentic_assistant.operations import _envelope_stop_reason


def test_pending_approval_with_trailing_usage_maps_to_approval_required():
    wire = [
        {"type": "tool_use", "name": "push_playbook", "id": "tu1"},
        {"type": "approval_request", "tool": "push_playbook", "tool_use_id": "tu1"},
        {"type": "usage", "input_tokens": 10, "output_tokens": 5},
    ]
    assert _envelope_stop_reason(wire, "pending_approval") == "approval_required"


def test_approval_request_last_still_maps():
    wire = [{"type": "approval_request", "tool": "push_playbook"}]
    assert _envelope_stop_reason(wire, "pending_approval") == "approval_required"


def test_error_event_still_wins():
    wire = [{"type": "approval_request"}, {"type": "error", "message": "boom"}]
    assert _envelope_stop_reason(wire, "pending_approval") == "error"


def test_normal_turn_unaffected():
    wire = [{"type": "text", "text": "done"}, {"type": "usage"}]
    assert _envelope_stop_reason(wire, "end_turn") == "end_turn"
