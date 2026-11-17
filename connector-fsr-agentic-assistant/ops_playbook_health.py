"""playbook_health (X4): run health for the playbooks the assistant bottled.

A bottled playbook is one the chat saved (set_session_saved_playbook writes a
row to bottled_playbook). For each one this reads its run history through the
framework's list_playbook_runs, counts runs and failures, and, for the most
recent failure, asks why_did_playbook_fail for the failing step. It reads only;
nothing is run or changed.
"""
from __future__ import annotations

from typing import Any

from .ops_common import _require, _validate_model  # noqa: F401  (_require kept for parity)
from .pydantic_models import PlaybookHealthParams, PlaybookHealthResponse

_FAILED_STATUSES = {"failed", "finished_with_error", "terminated"}
_RUN_WINDOW = 50       # runs read per playbook
_DEFAULT_LIMIT = 20    # bottled playbooks listed
_MAX_LIMIT = 50


def playbook_health(config: dict, params: dict) -> dict:
    """Per-playbook run counts, failures, last failure and failure reason.

    Params:
      limit (optional) -- how many bottled playbooks to list, newest first (max 50).

    Returns: {ok, count, playbooks: [...]}. Each playbook: workflow_uuid,
    session_id, saved_at, name, runs, failures, failures_since_last_ok,
    last_success, health ("ok" | "failing" | "no_runs"), last_failure
    ({task_id, modified, status, error_message, failing_steps, diagnosis_ok}).
    """
    from .storage import default_storage

    _validate_model(PlaybookHealthParams, params or {})
    raw_limit = (params or {}).get("limit")
    try:
        limit = int(raw_limit) if raw_limit not in (None, "") else _DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, _MAX_LIMIT))

    rows = default_storage().list_bottled_playbooks(limit)
    list_runs, why_failed = _framework_tools()
    playbooks = [_one(row, list_runs, why_failed) for row in rows]
    resp = {"ok": True, "count": len(playbooks), "playbooks": playbooks}
    _validate_model(PlaybookHealthResponse, resp)
    return resp


def _framework_tools():
    """The framework's own run tools (reused, not re-implemented)."""
    from fsr_playbooks.mcp_server.tools_connector_discovery import list_playbook_runs
    from fsr_playbooks.mcp_server.tools_recipe import why_did_playbook_fail
    return list_playbook_runs, why_did_playbook_fail


def _one(row: dict, list_runs, why_failed) -> dict[str, Any]:
    wf = row["workflow_uuid"]
    base: dict[str, Any] = {"workflow_uuid": wf, "session_id": row.get("session_id"),
                            "saved_at": row.get("saved_at")}
    try:
        got = list_runs(playbook_uuid=wf, limit=_RUN_WINDOW, include_finished=True)
    except Exception as exc:  # noqa: BLE001
        return {**base, "health": "unknown", "error": repr(exc)}
    if not isinstance(got, dict) or got.get("error"):
        err = got.get("error") if isinstance(got, dict) else "no run data"
        return {**base, "health": "unknown", "error": str(err)}

    # Newest first: the framework's `modified` is an ISO timestamp, so a string
    # sort is a time sort.
    runs = sorted(got.get("runs") or [], key=lambda r: str(r.get("modified") or ""),
                  reverse=True)
    name = next((r.get("name") for r in runs if r.get("name")), None)
    failures = [r for r in runs if r.get("status") in _FAILED_STATUSES]
    success = next((r for r in runs if r.get("status") and r.get("status") not in _FAILED_STATUSES), None)
    # Failures newer than the last success: the "broke after a change" count.
    since_ok = [r for r in failures
                if success is None or str(r.get("modified") or "") > str(success.get("modified") or "")]

    last_failure = None
    if failures:
        lf = failures[0]
        last_failure = {"task_id": lf.get("task_id") or str(lf.get("pk") or ""),
                        "modified": lf.get("modified"), "status": lf.get("status"),
                        "error_message": lf.get("error_message"),
                        **_diagnose(lf, why_failed)}

    if not runs:
        health = "no_runs"
    elif since_ok:
        health = "failing"
    else:
        health = "ok"
    return {**base, "name": name, "health": health, "runs": len(runs),
            "failures": len(failures), "failures_since_last_ok": len(since_ok),
            "last_success": (success or {}).get("modified"), "last_failure": last_failure}


def _diagnose(run: dict, why_failed) -> dict[str, Any]:
    key = run.get("task_id") or str(run.get("pk") or "")
    if not key:
        return {"failing_steps": [], "diagnosis_ok": False}
    try:
        diag = why_failed(key)
    except Exception as exc:  # noqa: BLE001
        return {"failing_steps": [], "diagnosis_ok": False, "diagnosis_error": repr(exc)}
    if not isinstance(diag, dict) or (not diag.get("ok", False) and "step_diagnostics" not in diag):
        return {"failing_steps": [], "diagnosis_ok": False,
                "diagnosis_error": (diag or {}).get("message") if isinstance(diag, dict) else None}
    steps: list[str] = []
    for d in diag.get("step_diagnostics") or []:
        if not d.get("ok") and d.get("step") and d["step"] not in steps:
            steps.append(str(d["step"]))
    return {"failing_steps": steps, "diagnosis_ok": True}
