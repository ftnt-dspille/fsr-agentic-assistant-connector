"""Chase a fire-and-forget playbook trigger to a real outcome (#149).

Native containment tools (``mcp_soc__block_indicator`` and kin) return success
the moment the platform QUEUES the stock action playbook -- the payload
carries a ``task_id`` and nothing else. Narrated as-is, that reads as a landed
containment while the queued run may already have failed on the box. These
helpers detect that handoff shape and poll the workflow listing (the same
endpoint + terminal set ``run_playbook_test`` uses) so every consumer -- the
approved-card exec path AND the native MCP tool funnel -- splices a real
outcome (or an honest "pending") into the result the model sees.
"""
from __future__ import annotations

from typing import Any, Optional

_TERMINAL = {"finished", "failed", "terminated", "skipped",
             "finished_with_error", "rejected"}


def async_task_id(result: Any) -> Optional[str]:
    """The task_id of a fire-and-forget playbook trigger, or None.

    Only a result carrying a task_id and NO terminal outcome evidence is an
    async handoff; a payload already stating a terminal status is left alone.
    """
    if not isinstance(result, dict):
        return None
    inner = result.get("result") if isinstance(result.get("result"), dict) else {}
    tid = result.get("task_id") or inner.get("task_id")
    if not tid:
        return None
    for outcome_src in (result, inner):
        st = str(outcome_src.get("status") or "").lower()
        if st in _TERMINAL:
            return None
    return str(tid)


def poll_task_outcome(task_id: str, timeout_s: float = 12.0) -> Optional[dict]:
    """Best-effort outcome of the queued run: {status, error_message?, name?}.

    ``{"status": "pending"}`` when still running at the deadline; ``None`` when
    the box cannot be asked at all (dev/off-box) -- the caller must then
    narrate "queued, outcome pending", never success.
    """
    import time as _time
    try:
        from ._live_crudhub import _make_request
        mr = _make_request()
    except Exception:  # noqa: BLE001 -- off-box: unpollable
        return None
    if mr is None:
        return None
    terminal = _TERMINAL | {"awaiting"}
    deadline = _time.monotonic() + timeout_s
    while True:
        try:
            listing = mr(
                "/api/wf/api/workflows/?format=json&limit=1&ordering=-modified"
                f"&task_id={task_id}&parent_wf__isnull=True", "GET")
        except Exception:  # noqa: BLE001
            listing = None
        members = (listing or {}).get("hydra:member") or []
        rec = members[0] if members else None
        if rec and rec.get("status") in terminal:
            out = {"status": rec.get("status"), "name": rec.get("name")}
            if rec.get("status") != "finished":
                top = rec.get("result") or {}
                msg = ((top.get("Error message") if isinstance(top, dict) else None)
                       or rec.get("errorMessage") or rec.get("error"))
                for s in rec.get("steps") or []:
                    if s.get("status") in ("failed", "finished_with_error"):
                        msg = msg or s.get("errorMessage") or s.get("error")
                        out["failing_step"] = s.get("name")
                        break
                out["error_message"] = msg
            return out
        if _time.monotonic() >= deadline:
            return {"status": "pending"}
        _time.sleep(2)


def enrich_async_result(result: Any) -> Any:
    """Splice the queued run's outcome into an async-handoff result, in place
    where possible. Non-handoff results pass through untouched."""
    tid = async_task_id(result)
    if not tid:
        return result
    outcome = poll_task_outcome(tid)
    if not isinstance(result, dict):
        result = {"raw": result}
    result["async_playbook"] = dict(outcome or {"status": "unknown"},
                                    task_id=tid)
    st = result["async_playbook"].get("status")
    if st in ("failed", "terminated", "finished_with_error", "rejected"):
        result["async_playbook"]["verdict"] = (
            "the queued playbook FAILED -- the action did NOT land on the "
            "box; report the failure, never success")
    elif st != "finished":
        result["async_playbook"]["verdict"] = (
            "queued only -- outcome pending; do NOT report the action as "
            "completed or the target as blocked/changed")
    return result
