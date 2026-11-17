"""Operation entry points.

FortiSOAR calls each operation by name with `(config, params)` and expects a
JSON-serializable dict. This module is the seam: it re-exports the handlers
and holds the dispatch and response-model tables. The handlers live in the
`ops_*.py` modules, one per domain:

  ops_chat       chat_turn, chat_poll, chat_cancel, history, sessions
  ops_resume     chat_resume and approved-card execution
  ops_approvals  who may decide a card, pending items, escalations, undo
  ops_autonomy   auto-triage and the autonomy policy's decisions
  ops_turn_io / ops_turn_state   wire frames, persistence, per-turn state
  ops_tools / ops_personas / ops_prompts / ops_policy   what a turn is given
  ops_cards / ops_offers / ops_manual   cards, offers, manual input
  ops_playbooks / ops_playbooks_persist   compile/validate/render, push/update
  ops_mcp, ops_config, ops_health, ops_usage, ops_entity, ops_scheduling,
  ops_warmup, ops_common (validation, framework bridge, crudhub transport)

The agent loop itself is the framework's (`fsr_playbooks.llm.run_agent_turn`);
FortiSOAR invokes operations synchronously, so each handler runs it under
asyncio.run().
"""

from __future__ import annotations



from .pydantic_models import (
    UpdatePlaybookResponse,
)
# Response models. Wired at the dispatch seam (`_RESPONSE_MODELS` at the foot
# of this file), not at each `return` -- see the comment there.
from .pydantic_models import (
    HealthCheckResponse, EchoParamTypesResponse, WarmupResponse,
    ResolveYamlResponse, ListSessionsResponse, RespondManualInputResponse,
    PushPlaybookResponse, DecompilePlaybookResponse, DryRunPlaybookResponse,
    RenderJinjaResponse, QueryStoreResponse, GetSessionTraceResponse,
    ListMcpToolsResponse, ModelChoice,
    CreateScheduledTaskResponse, GetScheduledTaskResponse,
    ListScheduledTasksResponse, UpdateScheduledTaskResponse,
    DeleteScheduledTaskResponse, RunScheduledTaskNowResponse,
    ListScheduledTaskRunsResponse, NotifyFindingResponse,
    AutoTriageResponse, SetAutoTriageResponse, ResolveEscalationResponse,
    UndoActionResponse,
    RateSessionResponse, ProbeNativeMcpResponse,
    ListUsageResponse, GetAuditTrailResponse, GetUsageSummaryResponse,
    ListPendingResponse, ListActiveSessionsResponse, ListShadowDecisionsResponse,
    GetAutonomyPolicyResponse, SetAutonomyPolicyResponse,
    CompileYamlResponse, ValidateYamlResponse, ResolvePersonaResponse,
    MonitorCasesResponse, MonitorFeedbackResponse,
    ListHuntsResponse, SaveHuntResponse, DeleteHuntResponse, RunHuntResponse,
    ShiftSummaryResponse, ExportAuditResponse,
)
# Wire-frame models. Aliased to `*Frame` because `_event_to_wire` imports
# same-named event classes from `fsr_playbooks.llm.provider` -- the names would
# otherwise collide and silently shadow each other.
from .wire_enrichment import _ioc_extract, _ioc_cards  # noqa: E402,F401

from connectors.core.connector import ConnectorError  # noqa: F401

from .ops_approvals import get_audit_trail
from .ops_autonomy import set_auto_triage
from .ops_chat import get_session_trace, list_active_sessions, list_sessions, rate_session
from .ops_common import _set_simulation_mode
from .ops_health import echo_param_types, health_check
from .ops_mcp import call_mcp_tool, list_mcp_tools, probe_native_mcp
from .ops_playbooks import (
    compile_yaml, dry_run_playbook, query_store, render_jinja, resolve_yaml, validate_yaml,
)
from .ops_playbooks_persist import decompile_playbook, push_playbook
from .ops_playbook_health import playbook_health  # noqa: F401
from .ops_monitor import monitor_cases, monitor_feedback  # noqa: F401
from .ops_hunts import delete_hunt, list_hunts, run_hunt, save_hunt  # noqa: F401
from .ops_shift import shift_summary  # noqa: F401
from .ops_audit import export_audit  # noqa: F401
from .ops_scheduling import update_scheduled_task
from .ops_warmup import warmup
from .ops_approvals import (
    list_pending, resolve_escalation, undo_action,
)  # noqa: F401
from .ops_autonomy import (
    auto_triage, get_autonomy_policy, list_shadow_decisions, set_autonomy_policy,
)  # noqa: F401
from .ops_chat import (
    chat_cancel, chat_history, chat_poll, chat_resume,
    chat_turn,
)  # noqa: F401
from .ops_common import (
    _check_response,
)  # noqa: F401
from .ops_config import _build_provider, _check_model_choice, _config_for_persona, list_models  # noqa: F401
from .ops_manual import (
    respond_manual_input,
)  # noqa: F401
from .ops_mcp import (
    list_mcp_servers,
)  # noqa: F401
from .ops_personas import ResolvedPersona, resolve_persona  # noqa: F401
from .ops_playbooks_persist import (
    update_playbook,
)  # noqa: F401
from .ops_scheduling import (
    create_scheduled_task, delete_scheduled_task,
    get_scheduled_task, list_scheduled_task_runs, list_scheduled_tasks, notify_finding,
    run_scheduled_task_now,
)  # noqa: F401
from .ops_tools import (
    list_agent_tools,
)  # noqa: F401
from .ops_usage import (
    get_usage_summary, list_usage,
)  # noqa: F401
from . import (ops_common, ops_mcp, ops_config, ops_health, ops_playbooks, ops_playbooks_persist, ops_entity, ops_usage, ops_cards, ops_offers, ops_prompts, ops_personas, ops_tools, ops_turn_state, ops_turn_io, ops_policy, ops_chat, ops_approvals, ops_resume, ops_manual, ops_scheduling, ops_autonomy, ops_warmup, ops_hunts, ops_shift)  # noqa: F401



# ---------- mock-mode wrapper ----------------------------------------------

# Operations that can be replayed from a fixture in mock mode. health_check
# / list_models / warmup are infrastructure and always run live (the demo
# needs a real health signal); the rest mirror the widget's fixtures.
_MOCKABLE = {
    "compile_yaml", "validate_yaml", "resolve_yaml",
    "chat_turn", "chat_resume", "respond_manual_input", "chat_history",
    "push_playbook", "update_playbook", "dry_run_playbook", "render_jinja",
    "create_scheduled_task", "list_scheduled_tasks", "get_scheduled_task",
    "update_scheduled_task", "delete_scheduled_task", "run_scheduled_task_now",
    "list_scheduled_task_runs", "notify_finding",
    "list_usage", "get_usage_summary", "list_pending", "get_audit_trail",
    "list_active_sessions",
}


def _mock_aware(op_name: str, handler):
    """Wrap a handler so that when invoked with params["mode"] == "mock"
    it replays a fixture response instead of running the live path. Keeps
    the live handler untouched for the default (live) case."""
    if op_name not in _MOCKABLE:
        return handler

    def wrapper(config: dict, params: dict):
        if (params or {}).get("mode") == "mock":
            from . import mock_replay
            return mock_replay.respond(op_name, config, params or {})
        return handler(config, params)

    wrapper.__name__ = getattr(handler, "__name__", op_name)
    wrapper.__doc__ = handler.__doc__
    return wrapper


# ---------- dispatch table -------------------------------------------------

_LIVE_OPERATIONS = {
    "health_check": health_check,
    "echo_param_types": echo_param_types,
    "list_models": list_models,
    "warmup": warmup,
    "compile_yaml": compile_yaml,
    "validate_yaml": validate_yaml,
    "resolve_yaml": resolve_yaml,
    "chat_turn": chat_turn,
    "chat_poll": chat_poll,
    "chat_cancel": chat_cancel,
    "playbook_health": playbook_health,
    "monitor_cases": monitor_cases,
    "monitor_feedback": monitor_feedback,
    "list_hunts": list_hunts,
    "save_hunt": save_hunt,
    "delete_hunt": delete_hunt,
    "run_hunt": run_hunt,
    "shift_summary": shift_summary,
    "export_audit": export_audit,
    "chat_resume": chat_resume,
    "respond_manual_input": respond_manual_input,
    "chat_history": chat_history,
    "resolve_persona": resolve_persona,
    "list_sessions": list_sessions,
    "rate_session": rate_session,
    "push_playbook": push_playbook,
    "update_playbook": update_playbook,
    "decompile_playbook": decompile_playbook,
    "dry_run_playbook": dry_run_playbook,
    "render_jinja": render_jinja,
    "query_store": query_store,
    "get_session_trace": get_session_trace,
    "call_mcp_tool": call_mcp_tool,
    "list_mcp_tools": list_mcp_tools,
    "list_agent_tools": list_agent_tools,
    "list_mcp_servers": list_mcp_servers,
    "probe_native_mcp": probe_native_mcp,
    "create_scheduled_task": create_scheduled_task,
    "list_scheduled_tasks": list_scheduled_tasks,
    "get_scheduled_task": get_scheduled_task,
    "update_scheduled_task": update_scheduled_task,
    "delete_scheduled_task": delete_scheduled_task,
    "run_scheduled_task_now": run_scheduled_task_now,
    "list_scheduled_task_runs": list_scheduled_task_runs,
    "notify_finding": notify_finding,
    "auto_triage": auto_triage,
    "set_auto_triage": set_auto_triage,
    "resolve_escalation": resolve_escalation,
    "undo_action": undo_action,
    "list_usage": list_usage,
    "get_usage_summary": get_usage_summary,
    "list_pending": list_pending,
    "list_shadow_decisions": list_shadow_decisions,
    "get_autonomy_policy": get_autonomy_policy,
    "set_autonomy_policy": set_autonomy_policy,
    "get_audit_trail": get_audit_trail,
    "list_active_sessions": list_active_sessions,
}

def _sim_aware(handler):
    """Apply the connector-config simulation switch before EVERY op (incl.
    warmup, which reads connector_details), so the whole turn resolves the
    same backend. mock-mode replay is layered on top and is unaffected."""
    def wrapper(config: dict, params: dict):
        _set_simulation_mode(config or {})
        return handler(config, params)
    wrapper.__name__ = getattr(handler, "__name__", "op")
    wrapper.__doc__ = handler.__doc__
    return wrapper


# ---------- outbound response models --------------------------------------
#
# Wired HERE rather than at each `return`, because these ops return from many
# places: `update_playbook` has ten return statements, `dry_run_playbook` nine,
# and most of them are `_err(...)` envelopes. Checking at the dispatch seam
# covers every path -- including the error ones, which is where a shape defect
# is least likely to be noticed -- for one line per op instead of forty.
#
# Ops absent from this map return either a passthrough payload with no fixed
# shape (`call_mcp_tool` returns the tool's own result) or a response model
# that is already validated inline (`chat_turn`, `chat_poll`, `chat_resume`,
# `chat_history`, `list_agent_tools`, `list_mcp_servers`).
_RESPONSE_MODELS = {
    "health_check": HealthCheckResponse,
    "echo_param_types": EchoParamTypesResponse,
    "warmup": WarmupResponse,
    "resolve_yaml": ResolveYamlResponse,
    "list_sessions": ListSessionsResponse,
    "respond_manual_input": RespondManualInputResponse,
    "push_playbook": PushPlaybookResponse,
    "update_playbook": UpdatePlaybookResponse,
    "decompile_playbook": DecompilePlaybookResponse,
    "dry_run_playbook": DryRunPlaybookResponse,
    "render_jinja": RenderJinjaResponse,
    "query_store": QueryStoreResponse,
    "get_session_trace": GetSessionTraceResponse,
    "list_mcp_tools": ListMcpToolsResponse,
    "compile_yaml": CompileYamlResponse,
    "validate_yaml": ValidateYamlResponse,
    "rate_session": RateSessionResponse,
    "probe_native_mcp": ProbeNativeMcpResponse,
    "create_scheduled_task": CreateScheduledTaskResponse,
    "list_scheduled_tasks": ListScheduledTasksResponse,
    "get_scheduled_task": GetScheduledTaskResponse,
    "update_scheduled_task": UpdateScheduledTaskResponse,
    "delete_scheduled_task": DeleteScheduledTaskResponse,
    "run_scheduled_task_now": RunScheduledTaskNowResponse,
    "list_scheduled_task_runs": ListScheduledTaskRunsResponse,
    "notify_finding": NotifyFindingResponse,
    "auto_triage": AutoTriageResponse,
    "set_auto_triage": SetAutoTriageResponse,
    "resolve_escalation": ResolveEscalationResponse,
    "undo_action": UndoActionResponse,
    "list_usage": ListUsageResponse,
    "get_usage_summary": GetUsageSummaryResponse,
    "list_pending": ListPendingResponse,
    "list_shadow_decisions": ListShadowDecisionsResponse,
    "get_autonomy_policy": GetAutonomyPolicyResponse,
    "set_autonomy_policy": SetAutonomyPolicyResponse,
    "get_audit_trail": GetAuditTrailResponse,
    "list_active_sessions": ListActiveSessionsResponse,
    "resolve_persona": ResolvePersonaResponse,
    "monitor_cases": MonitorCasesResponse,
    "monitor_feedback": MonitorFeedbackResponse,
    "list_hunts": ListHuntsResponse,
    "save_hunt": SaveHuntResponse,
    "delete_hunt": DeleteHuntResponse,
    "run_hunt": RunHuntResponse,
    "shift_summary": ShiftSummaryResponse,
    "export_audit": ExportAuditResponse,
}

# `list_models` is the one op whose response is a bare list (it feeds a
# picklist), so it gets an element model instead of an envelope.
_RESPONSE_ROW_MODELS = {
    "list_models": ModelChoice,
}


def _response_checked(name, handler):
    """Route an op's return value through its response model, if it has one."""
    model = _RESPONSE_MODELS.get(name)
    row_model = _RESPONSE_ROW_MODELS.get(name)
    if model is None and row_model is None:
        return handler

    def wrapper(config: dict, params: dict):
        out = handler(config, params)
        if row_model is not None and isinstance(out, list):
            for row in out:
                if isinstance(row, dict):
                    _check_response(row_model, row)
        elif model is not None and isinstance(out, dict):
            _check_response(model, out)
        return out
    wrapper.__name__ = getattr(handler, "__name__", "op")
    wrapper.__doc__ = handler.__doc__
    return wrapper


# The response check sits OUTSIDE `_mock_aware` on purpose: a replayed fixture
# is a response the widget will render, so a fixture that has drifted from the
# live shape is exactly the defect worth catching.
operations = {name: _sim_aware(_response_checked(name, _mock_aware(name, fn)))
              for name, fn in _LIVE_OPERATIONS.items()}


_FORWARD_MODULES = (ops_common, ops_mcp, ops_config, ops_health, ops_playbooks, ops_playbooks_persist, ops_entity, ops_usage, ops_cards, ops_offers, ops_prompts, ops_personas, ops_tools, ops_turn_state, ops_turn_io, ops_policy, ops_chat, ops_approvals, ops_resume, ops_manual, ops_scheduling, ops_autonomy, ops_warmup,)


def __getattr__(name):
    """Read-only fallback: a name that lives in a domain module but is not
    re-exported above resolves there, so reads of rebinding globals (e.g.
    `_BUILD_ID_CACHE`) see the live value. Writes belong on the owning module."""
    for mod in _FORWARD_MODULES:
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
