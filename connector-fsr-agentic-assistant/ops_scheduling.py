"""scheduling domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

from connectors.core.connector import ConnectorError
from .pydantic_models import (
    CreateScheduledTaskParams, GetScheduledTaskParams,
    UpdateScheduledTaskParams, DeleteScheduledTaskParams,
    RunScheduledTaskNowParams, ListScheduledTaskRunsParams,
    NotifyFindingParams,
)

from .ops_common import (
    _check_params, _err, _import_fsr_playbooks, _reference_db_path, _require,
    _require_transport, _serialize_errors,
)
from .ops_health import _connector_name
from .ops_playbooks_persist import push_playbook



# ---------- warmup ---------------------------------------------------------
#
# The shipped reference DB carries the hard-mined reference tables and leaves
# the FortiSOAR-derivable tables (connectors, operations, operation_params,
# op_safety) empty. `warmup` populates them at install / config-save (lifecycle
# hooks) or on the first chat that still finds them cold, by walking
# `/api/integration/connectors/` through the platform's authenticated loopback
# client. Idempotent: re-running on a populated DB is a no-op (skips when
# `connectors` has any rows unless `force=True`).
#
# MVP scope:
# - connectors + operations + operation_params (action_card prefill)
# - op_safety (HITL tier resolution)
#
# Deferred (not needed for the contract's demo flows):
# - picklists, modules + module_fields (record-step grounding)


# Map FortiSOAR operation category → tier-resolver safety value.
# Mirrors fsr_playbooks.llm.tools._tier_for_run_op's classifier heuristic so
# the tier wrapper gets a deterministic row instead of falling through
# to the "unknown → tier 3" default.
_CATEGORY_TO_SAFETY = {
    "investigation": "safe",
    "query": "safe",
    "utilities": "safe",
    "enrichment": "safe",
    "verification": "safe",
    "remediation": "unsafe",
    "containment": "unsafe",
    "management": "unsafe",
}


# ---------- Scheduled agent tasks (scheduled-agent-tasks plan) ------------
#
# Each scheduled task = one generated runner playbook + one FortiSOAR
# scheduler row (django-celery-beat PeriodicTask at /api/wf/api/scheduled/).
# The schedule payload carries only wf_iri (no free-form data -- live-verified
# on .159 8.0.0), so the playbook IS the task: prompt/intent are baked into the
# runner playbook (SetVariable steps for readability + a Connectors step calling
# chat_turn). The task *definition* (prompt, intent, allow_mutating) lives in
# sqlite (storage.scheduled_tasks); the schedule *row* is plumbing keyed by
# `name` (the stable lookup key, since the schedule `id` is a rotating Fernet
# token). Transport reuses the existing crudhub `_make_request()` -- the
# connector already reaches /api/wf/ (operations.py:6963 dry_run_playbook).

_SCHEDULES_ENDPOINT = "/api/wf/api/scheduled/"
_SCHEDULES_TRIGGER = f"{_SCHEDULES_ENDPOINT}trigger-now/"
_CRON_FIELDS = ("minute", "hour", "day_of_month", "month_of_year", "day_of_week")


def _parse_cron(cron: str) -> dict:
    """Split a 5-field cron string into the crontab field map the scheduler
    expects (minute hour day_of_month month_of_year day_of_week). Raises
    ConnectorError on a non-5-field expression -- validated before any POST."""
    parts = str(cron).split()
    if len(parts) != 5:
        raise ConnectorError(
            f"cron must be 5 fields (minute hour day_of_month month_of_year "
            f"day_of_week), got {len(parts)}: {cron!r}")
    return dict(zip(_CRON_FIELDS, parts, strict=True))


def _utc_offset(timezone: str) -> str | None:
    """Best-effort UTC±HH:MM for an IANA timezone (mirrors the display hint
    FortiSOAR's scheduler UI sends). None when the tz is unknown so the
    caller omits kwargs.utcOffset rather than send a wrong value."""
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo(timezone))
        offset = now.utcoffset()
        if offset is None:
            return None
        total = int(offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"
    except Exception:
        return None


def _compile_runner_playbook(name: str, intent: str, prompt: str,
                             agent_config: str,
                             allow_mutating: bool = True) -> dict:
    """Compile the runner playbook YAML → FSR workflow_json. Returns
    {ok, workflow_json?, errors}. The runner calls chat_turn then notify_finding.
    session_id uses {{ uuid() }} for a fresh per-fire session (plan §7).

    Values are baked directly into the Connectors step params — NOT through a
    SetVariable intermediary (the inter-step Jinja chain was unreliable on 8.0).
    notify_finding receives only the static task name; it reads the latest run's
    session_id from storage when one isn't passed.

    When allow_mutating is False, the runner enforces read-only mode by passing
    a read-only quick_action chip to chat_turn."""
    import yaml
    fsr_playbooks = _import_fsr_playbooks()

    # Build chat_turn params, with read_only flag enforcing restricted writes if needed
    chat_params = {
        "session_id": f"sched:{name}:{{{{ uuid() }}}}",
        "intent": intent,
        "messages": [{"role": "user", "content": prompt}],
    }
    if not allow_mutating:
        # Pass read_only flag to restrict write tools (no auto-approval of writes)
        chat_params["read_only"] = True

    playbook = {
        "playbooks": [
            {
                "name": f"Scheduled Agent Runner -- {name}",
                "steps": [
                    {"name": "start", "type": "start", "next": "run_agent_turn"},
                    {
                        "name": "run_agent_turn",
                        "type": "Connectors",
                        "connector": _connector_name(),
                        "operation": "chat_turn",
                        "config": agent_config,
                        "params": chat_params,
                        "next": "notify_finding",
                    },
                    {
                        "name": "notify_finding",
                        "type": "Connectors",
                        "connector": _connector_name(),
                        "operation": "notify_finding",
                        "config": agent_config,
                        "params": {"name": name},
                    },
                ],
            }
        ]
    }
    yaml_text = yaml.dump(playbook, default_flow_style=False, sort_keys=False,
                         allow_unicode=True)
    result = fsr_playbooks.compile_yaml(yaml_text, _reference_db_path())
    return {
        "ok": result.ok,
        "workflow_json": result.fsr_json if result.ok else None,
        "errors": _serialize_errors(result.errors),
    }


def _schedules_list(mr) -> list[dict]:
    """GET /api/wf/api/scheduled/ → hydra:member rows (raw dicts)."""
    resp = mr(_SCHEDULES_ENDPOINT, "GET",
              params={"format": "json", "offset": 0, "limit": 2147483647})
    members = (resp or {}).get("hydra:member") or []
    if not members and isinstance(resp, dict) and isinstance(resp.get("results"), list):
        members = resp["results"]
    return members


def _schedule_get_by_name(mr, name: str) -> dict | None:
    for row in _schedules_list(mr):
        if row.get("name") == name:
            return row
    return None


def _schedule_create(mr, name: str, wf_iri: str, cron: str,
                     timezone: str = "UTC", enabled: bool = True,
                     exit_if_running: bool = True) -> dict:
    """POST a PeriodicTask row mirroring what FortiSOAR's scheduler UI sends."""
    crontab = _parse_cron(cron)
    crontab["timezone"] = timezone
    kwargs = {"exit_if_running": exit_if_running, "wf_iri": wf_iri,
              "timezone": timezone}
    utc = _utc_offset(timezone)
    if utc is not None:
        kwargs["utcOffset"] = utc
    body = {"name": name, "crontab": crontab, "kwargs": kwargs,
            "expires": None, "start_time": None, "enabled": enabled}
    return mr(_SCHEDULES_ENDPOINT, "POST", body=body,
              params={"format": "json"})


def _schedule_put(mr, row: dict) -> dict:
    """Full-record PUT (the wf API accepts no PATCH). `row` is the current
    record (with a fresh Fernet-token id)."""
    return mr(f"{_SCHEDULES_ENDPOINT}{row['id']}/", "PUT", body=row,
              params={"format": "json"})


def _schedule_delete(mr, name: str) -> None:
    row = _schedule_get_by_name(mr, name)
    if row is None:
        return
    mr(f"{_SCHEDULES_ENDPOINT}{row['id']}/", "DELETE",
       params={"format": "json"})


def _schedule_trigger(mr, name: str) -> dict:
    row = _schedule_get_by_name(mr, name)
    if row is None:
        raise ConnectorError(f"No scheduled task named {name!r}")
    return mr(_SCHEDULES_TRIGGER, "POST", body={"id": row["id"]},
              params={"format": "json"})


def _wf_iri_from_push(push_resp: dict) -> str | None:
    """Extract the runner playbook's workflow IRI from a push_playbook result.
    push_playbook returns {collection_uuid, workflows:[{uuid}], raw:{...}}."""
    raw = push_resp.get("raw") or {}
    wf_uuid = push_resp.get("workflow_uuid")
    if not wf_uuid:
        wfs = push_resp.get("workflows") or raw.get("workflows") or []
        if wfs:
            wf_uuid = wfs[0].get("uuid")
    return f"/api/3/workflows/{wf_uuid}" if wf_uuid else None


def create_scheduled_task(config: dict, params: dict) -> dict:
    """Create a scheduled agent task: persist the def → compile + push the
    runner playbook → create the scheduler row. Idempotent on `name`: a task
    with that name already existing is treated as an update (recompile +
    get_or_create the schedule). Returns {ok, name, wf_iri, schedule_name}."""
    _check_params(CreateScheduledTaskParams, params)
    name = _require(params, "name")
    cron = _require(params, "cron")
    intent = params.get("intent") or "triage"
    prompt = _require(params, "prompt")
    agent_config = _require(params, "agent_config")
    allow_mutating = bool(params.get("allow_mutating", False))
    timezone = params.get("timezone") or "UTC"
    enabled = bool(params.get("enabled", True))
    notify_target = params.get("notify_target") or ""

    mr, err = _require_transport(
        "create_scheduled_task requires the FortiSOAR runtime to push the "
        "runner playbook and create the schedule.")
    if err:
        return err

    # 1. Compile the runner playbook.
    compiled = _compile_runner_playbook(name, intent, prompt, agent_config,
                                       allow_mutating=allow_mutating)
    if not compiled["ok"]:
        return _err("compile_failed",
                    "runner playbook did not compile",
                    errors=compiled["errors"])

    # 2. Push (conflict-safe; overwrite any prior runner for this name).
    pushed = push_playbook(config, {"workflow_json": compiled["workflow_json"],
                                    "overwrite": True})
    if not pushed.get("ok"):
        return _err("push_failed", f"runner playbook push failed: "
                    f"{pushed.get('error') or pushed}")
    wf_iri = _wf_iri_from_push(pushed)

    # 3. Create / update the schedule row.
    sched_name = f"agent-sched:{name}"
    try:
        existing = _schedule_get_by_name(mr, sched_name)
        if existing is None:
            _schedule_create(mr, sched_name, wf_iri, cron, timezone, enabled)
        else:
            body = _copy_update(existing, crontab={**_parse_cron(cron),
                                                   **{"timezone": timezone}},
                               kwargs={**existing.get("kwargs", {}),
                                       "wf_iri": wf_iri, "timezone": timezone},
                               enabled=enabled)
            _schedule_put(mr, body)
    except Exception as e:
        return _err("schedule_create_failed", str(e), wf_iri=wf_iri)

    # 4. Persist the task def.
    from .storage import default_storage
    storage = default_storage()
    storage.upsert_scheduled_task({
        "name": name, "cron": cron, "intent": intent, "prompt": prompt,
        "allow_mutating": allow_mutating, "agent_config": agent_config,
        "wf_iri": wf_iri, "wf_uuid": pushed.get("workflow_uuid"),
        "collection_uuid": pushed.get("collection_uuid"),
        "timezone": timezone, "enabled": enabled,
        "notify_target": notify_target,
    })
    return {"ok": True, "name": name, "wf_iri": wf_iri,
            "schedule_name": sched_name}


def _copy_update(row: dict, **overrides) -> dict:
    """Deep-copy a schedule row and apply field overrides for a PUT."""
    import copy
    body = copy.deepcopy(row)
    for k, v in overrides.items():
        if v is not None:
            body[k] = v
    return body


def list_scheduled_tasks(config: dict, params: dict) -> dict:
    """List task defs, joined with live schedule state (last_run_at,
    total_run_count, enabled). Pure read."""
    from .storage import default_storage
    storage = default_storage()
    defs = storage.list_scheduled_tasks()
    mr, err = _require_transport()
    live: dict[str, dict] = {}
    if err is None:
        try:
            for row in _schedules_list(mr):
                nm = row.get("name") or ""
                if nm.startswith("agent-sched:"):
                    live[nm[len("agent-sched:"):]] = {
                        "schedule_enabled": bool(row.get("enabled")),
                        "last_run_at": row.get("last_run_at"),
                        "total_run_count": row.get("total_run_count"),
                        "schedule_id": row.get("id"),
                    }
        except Exception:
            pass  # live state is best-effort; defs still return
    out = []
    for d in defs:
        live_state = live.get(d["name"], {})
        out.append({**d, **live_state})
    return {"tasks": out}


def get_scheduled_task(config: dict, params: dict) -> dict:
    """One task def + its live schedule state."""
    _check_params(GetScheduledTaskParams, params)
    name = _require(params, "name")
    from .storage import default_storage
    storage = default_storage()
    d = storage.get_scheduled_task(name)
    if d is None:
        return _err("not_found", f"no scheduled task named {name!r}")
    mr, err = _require_transport()
    if err is None:
        try:
            row = _schedule_get_by_name(mr, f"agent-sched:{name}")
            if row:
                d["schedule_enabled"] = bool(row.get("enabled"))
                d["last_run_at"] = row.get("last_run_at")
                d["total_run_count"] = row.get("total_run_count")
        except Exception:
            pass
    return {"task": d}


def update_scheduled_task(config: dict, params: dict) -> dict:
    """Update a task's cron / intent / prompt / enabled. Recompiles the
    runner (prompt/intent changed) and get_or_create-updates the schedule.
    Omitted fields keep their current value."""
    _check_params(UpdateScheduledTaskParams, params)
    name = _require(params, "name")
    from .storage import default_storage
    storage = default_storage()
    cur = storage.get_scheduled_task(name)
    if cur is None:
        return _err("not_found", f"no scheduled task named {name!r}")
    cron = params.get("cron") or cur["cron"]
    intent = params.get("intent") or cur["intent"]
    prompt = params.get("prompt") or cur["prompt"]
    agent_config = params.get("agent_config") or cur.get("agent_config")
    allow_mutating = params.get("allow_mutating", cur["allow_mutating"])
    timezone = params.get("timezone") or cur.get("timezone", "UTC")
    enabled = params.get("enabled", cur["enabled"])
    notify_target = params.get("notify_target") if params.get("notify_target") is not None else cur.get("notify_target")
    mr, err = _require_transport(
        "update_scheduled_task requires the FortiSOAR runtime.")
    if err:
        return err
    # Recompile + re-push only when prompt/intent/agent_config changed.
    if (prompt != cur["prompt"] or intent != cur["intent"]
            or agent_config != cur.get("agent_config")):
        compiled = _compile_runner_playbook(name, intent, prompt, agent_config,
                                           allow_mutating=allow_mutating)
        if not compiled["ok"]:
            return _err("compile_failed", "runner playbook did not compile",
                        errors=compiled["errors"])
        pushed = push_playbook(config, {"workflow_json": compiled["workflow_json"],
                                        "overwrite": True})
        if not pushed.get("ok"):
            return _err("push_failed", f"runner playbook push failed: "
                        f"{pushed.get('error') or pushed}")
        wf_iri = _wf_iri_from_push(pushed)
    else:
        wf_iri = cur.get("wf_iri")
    # Update the schedule row (cron/timezone/wf_iri/enabled).
    sched_name = f"agent-sched:{name}"
    try:
        existing = _schedule_get_by_name(mr, sched_name)
        if existing is None:
            _schedule_create(mr, sched_name, wf_iri, cron, timezone, enabled)
        else:
            new_kwargs = {**existing.get("kwargs", {}),
                          "wf_iri": wf_iri, "timezone": timezone}
            body = _copy_update(existing,
                               crontab={**_parse_cron(cron),
                                        **{"timezone": timezone}},
                               kwargs=new_kwargs, enabled=enabled)
            _schedule_put(mr, body)
    except Exception as e:
        return _err("schedule_update_failed", str(e), wf_iri=wf_iri)
    storage.upsert_scheduled_task({
        "name": name, "cron": cron, "intent": intent, "prompt": prompt,
        "allow_mutating": allow_mutating, "agent_config": agent_config,
        "wf_iri": wf_iri, "wf_uuid": cur.get("wf_uuid"),
        "collection_uuid": cur.get("collection_uuid"),
        "timezone": timezone, "enabled": enabled,
        "notify_target": notify_target,
    })
    return {"ok": True, "name": name, "wf_iri": wf_iri}


def delete_scheduled_task(config: dict, params: dict) -> dict:
    """Delete the schedule row + the runner playbook collection + the sqlite
    def. Best-effort on the playbook delete (a missing collection is not a
    failure)."""
    _check_params(DeleteScheduledTaskParams, params)
    name = _require(params, "name")
    from .storage import default_storage
    storage = default_storage()
    cur = storage.get_scheduled_task(name)
    mr, err = _require_transport()
    if err is None:
        try:
            _schedule_delete(mr, f"agent-sched:{name}")
        except Exception:
            pass
        # Hard-purge the runner playbook collection.
        coll_uuid = (cur or {}).get("collection_uuid")
        if coll_uuid:
            try:
                mr(f"/api/3/workflow_collections/{coll_uuid}?hardDelete=true",
                   "DELETE")
            except Exception:
                pass
    removed = storage.delete_scheduled_task(name)
    return {"ok": True, "name": name, "deleted": removed}


def run_scheduled_task_now(config: dict, params: dict) -> dict:
    """Force-trigger a task immediately (bypasses cron; fires regardless of
    `enabled`). The trigger is async -- the response confirms acceptance."""
    _check_params(RunScheduledTaskNowParams, params)
    name = _require(params, "name")
    mr, err = _require_transport(
        "run_scheduled_task_now requires the FortiSOAR runtime to trigger.")
    if err:
        return err
    try:
        out = _schedule_trigger(mr, f"agent-sched:{name}")
    except ConnectorError as e:
        return _err("not_found", str(e))
    except Exception as e:
        return _err("trigger_failed", str(e))
    return {"ok": True, "name": name, "trigger": out}


def list_scheduled_task_runs(config: dict, params: dict) -> dict:
    """List the runs (one per cron fire) of a scheduled task, newest-first.
    Each fire is a stored chat session whose id is ``sched:<name>:<uuid>``;
    this returns the run summaries (timestamp, entries, verdict, stop_reason)
    plus a ``parked`` flag for HITL-gated runs the analyst can resume in chat.

    Pure read from sqlite storage -- no scheduler transport -- so it works in
    dev/hermetic without the FortiSOAR runtime. Returns ``not_found`` when the
    task def itself does not exist (so the widget can distinguish 'no such task'
    from 'task with zero runs yet')."""
    _check_params(ListScheduledTaskRunsParams, params)
    name = _require(params, "name")
    limit = int(params.get("limit") or 25)
    from .storage import default_storage
    storage = default_storage()
    if storage.get_scheduled_task(name) is None:
        return _err("not_found", f"no scheduled task named {name!r}")
    return {"ok": True, "runs": storage.list_scheduled_task_runs(name, limit=limit)}


# ---------- Scheduled tasks: finding notification (plan §2 steps 3-4) --------
#
# The runner playbook calls chat_turn (the agent investigates), then calls
# notify_finding (this op) to push the verdict to the analyst via whatever
# notification connector is configured on the box. The agent stays read-only
# (triage); the notify decision is a deterministic post-turn step, not an
# agent action -- so no HITL gate parks the run on the notify.
#
# Channel discovery is live: list_configured_connectors (crudhub) → intersect
# with the known notification-channel map below. The first match in priority
# order wins. The map is extensible -- a new connector just adds a row.

# Each entry: connector_name → (op_name, label, param_builder).
# `param_builder(subject, body, task_name, session_id) -> dict` builds the op's
# input params for that channel. Only connectors that are actually CONFIGURED
# on the box are dispatched to (a configured connector has a saved config and
# can run; an installed-but-unconfigured one cannot -- run_op would reject it).

def _smtp_params(subject, body, task_name, session_id):
    """Params for smtp.send_email. The smtp connector's send_email op takes
    to/cc/subject/body -- `to` comes from the task's notify_target (an email
    address the analyst set), defaulting to the box's admin if absent."""
    return {
        "to": "",  # filled from the task def's notify_target by the caller
        "subject": subject,
        "body": body,
    }


def _slack_params(subject, body, task_name, session_id):
    """Params for slack.post_message. Channel + text."""
    return {
        "channel": "",  # filled from notify_target
        "text": f"*{subject}*\n{body}",
    }


def _teams_params(subject, body, task_name, session_id):
    """Params for microsoft-teams post (title + text)."""
    return {
        "title": subject,
        "text": body,
    }


# Priority order: email is the universal default (every box has smtp); chat
# tools are added when configured. A box with none configured → no_channels
# (the run still completes; the finding is in the history drawer).
# NOTE: twilio is omitted pending correct SMS param implementation (currently
# reuses _slack_params which provides "channel", but twilio's send_sms op
# requires a "phone_number" field matching FortiSOAR's integration API).
_NOTIFICATION_CHANNELS = [
    ("smtp", "send_email", "Email (SMTP)", _smtp_params),
    ("smtp_ng", "send_email", "Email (SMTP NG)", _smtp_params),
    ("slack", "post_message", "Slack", _slack_params),
    ("microsoft-teams", "post_message", "Microsoft Teams", _teams_params),
    ("teams", "post_message", "Teams", _teams_params),
]

# Connector names we recognize as notification channels (for fast intersection).
_NOTIFICATION_CONNECTOR_NAMES = {row[0] for row in _NOTIFICATION_CHANNELS}


def _discover_notification_channels(mr) -> list[dict]:
    """Return the configured notification channels on this box, in priority
    order. Each row: {connector, op, label}. Empty when none are configured.

    Uses the same crudhub transport as run_op. Hits
    ``/api/integration/connectors/`` — the configured-connectors listing
    endpoint (NOT ``/api/3/connectors``, which 404s on the crudhub loopback).
    Fail-open: a transport error returns [] (the run still completes; the
    finding lives in the history drawer)."""
    try:
        resp = mr("/api/integration/connectors/?page=1&page_size=200", "GET")
    except Exception:  # noqa: BLE001 - never fail a run over channel discovery
        return []
    # The crudhub response wraps the list under `data` (IntegrationListEnvelope).
    members = []
    if isinstance(resp, dict):
        members = resp.get("data") or resp.get("hydra:member") or []
        if not members and isinstance(resp.get("results"), list):
            members = resp["results"]
    elif isinstance(resp, list):
        members = resp
    configured = {m.get("name") for m in members
                  if isinstance(m, dict) and m.get("name")}
    out = []
    for conn, op, label, _builder in _NOTIFICATION_CHANNELS:
        if conn in configured and conn not in {r["connector"] for r in out}:
            out.append({"connector": conn, "op": op, "label": label})
    return out


def _build_finding_message(task_name: str, verdict: str, session_id: str,
                          stop_reason: str | None) -> tuple[str, str]:
    """Format the (subject, body) for a scheduled-finding notification.

    The subject carries the task name + a short outcome tag so an inbox scan
    tells the analyst which task fired and whether it parked. The body carries
    the verdict (the agent's plain-text finding) + the session id so the
    analyst can resume in chat. A parked run's body tells them to open the chat
    drawer and approve the gated action."""
    parked = stop_reason is not None and str(stop_reason).startswith("awaiting_")
    tag = "ACTION NEEDED" if parked else "finding"
    subject = f"[Agentic Assistant] {task_name}: {tag}"
    body = verdict or "(the agent produced no verdict text)"
    if parked:
        body += (f"\n\nThis run parked on a human gate ({stop_reason}). "
                 f"Open the Agentic Assistant chat drawer and resume session "
                 f"{session_id} to approve the action.")
    else:
        body += f"\n\nSession: {session_id}"
        body += "\nOpen the Agentic Assistant → Scheduled Tasks → History to review."
    return subject, body


def _send_notification(subject: str, body: str, name: str, session_id: str,
                       target: str) -> dict:
    """Send (subject, body) on the box's highest-priority notification channel
    (SMTP / Slack / Teams / SMS). Returns {ok, channel, channel_label, subject}
    when sent, {ok, skipped, message} when nothing could be sent (no runtime,
    no channel, no recipient), or an error envelope when the send failed."""
    # Discover channels via crudhub. No transport in dev → no_channels (the
    # finding still lands in the history drawer).
    mr, terr = _require_transport()
    if terr:
        return {"ok": True, "skipped": "no_channels",
                "message": "no FortiSOAR runtime to discover notification channels"}
    channels = _discover_notification_channels(mr)
    if not channels:
        return {"ok": True, "skipped": "no_channels",
                "message": "no notification connector (smtp/slack/teams/sms) is configured"}

    ch = channels[0]
    builder = next((b for c, o, l, b in _NOTIFICATION_CHANNELS
                   if c == ch["connector"]), None)
    op_params = builder(subject, body, name, session_id) if builder else {}
    # Fill the channel-specific target (email address / slack channel).
    if target:
        if "to" in op_params:
            op_params["to"] = target
        elif "channel" in op_params:
            op_params["channel"] = target
    # A channel that needs a recipient but has none → skip with a clear reason
    # (dispatching would 400 on the SMTP connector's builtins validation).
    elif "to" in op_params or "channel" in op_params:
        return {"ok": True, "skipped": "no_target", "channel": ch["connector"],
                "message": f"no notify_target set for channel {ch['connector']}"}

    # Dispatch via /api/integration/execute (the same path run_op uses).
    execute_body = {
        "connector": ch["connector"],
        "operation": ch["op"],
        "params": op_params,
    }
    try:
        resp = mr("/api/integration/execute/", "POST", body=execute_body)
    except Exception as e:  # noqa: BLE001
        return _err("dispatch_failed", str(e), channel=ch["connector"])

    exec_status = (resp or {}).get("status", "")
    if exec_status not in ("Success", "success", "Completed", "completed", ""):
        msg = (resp or {}).get("message", "") or str(resp)
        return _err("send_failed", msg, channel=ch["connector"])

    return {"ok": True, "channel": ch["connector"], "channel_label": ch["label"],
            "subject": subject}


def notify_finding(config: dict, params: dict) -> dict:
    """Push a scheduled task's finding to the analyst via a configured
    notification channel (SMTP email / Slack / Teams / SMS). Called by the
    runner playbook AFTER chat_turn completes, so the agent stays read-only
    and the notify is a deterministic post-turn step (no HITL gate).

    Discovers what's configured on the box (crudhub), picks the highest-priority
    channel, formats the finding (task name + verdict + resume link), and
    dispatches via ``/api/integration/execute`` (the same path run_op uses).
    When ``only_on_finding`` is True (default), a run with no verdict or a
    ``no_finding`` verdict skips the notify -- so a clean sweep does not spam.

    Returns ``{ok, channel, skipped?, error?}``. ``skipped`` names the reason
    when no notify was sent (no finding / no channel / disabled)."""
    _check_params(NotifyFindingParams, params)
    name = _require(params, "name")
    session_id = params.get("session_id") or ""
    only_on_finding = params.get("only_on_finding", True)
    verdict_override = params.get("verdict")
    target = params.get("notify_target") or ""

    from .storage import default_storage
    storage = default_storage()
    task = storage.get_scheduled_task(name)
    if task is None:
        return _err("not_found", f"no scheduled task named {name!r}")

    # The verdict: explicit override (the runner passed chat_turn's result) is
    # authoritative; otherwise read the last assistant transcript for the run.
    verdict = verdict_override or ""
    stop_reason = None
    if session_id:
        if not verdict:
            try:
                runs = storage.list_scheduled_task_runs(name, limit=1)
                for r in runs:
                    if r.get("session_id") == session_id:
                        verdict = r.get("verdict") or ""
                        stop_reason = r.get("stop_reason")
                        break
            except Exception:  # noqa: BLE001
                pass
    else:
        # No session_id passed (the runner bakes only the task name). Read the
        # most recent run for this task — it's the one that just fired.
        try:
            runs = storage.list_scheduled_task_runs(name, limit=1)
            if runs:
                session_id = runs[0].get("session_id") or ""
                verdict = verdict or runs[0].get("verdict") or ""
                stop_reason = runs[0].get("stop_reason")
        except Exception:  # noqa: BLE001
            pass

    if only_on_finding:
        # Decided from the run's structured outcome (verdict card disposition,
        # needs_human, a parked gate), never from the wording of the verdict.
        # A run with no output at all has nothing to report.
        from .ops_autonomy import _auto_triage_outcome
        from .ops_notify import trigger_for
        frames = storage.last_assistant_frames(session_id) if session_id else []
        outcome = _auto_triage_outcome(frames, stop_reason)
        has_output = bool(verdict.strip() or outcome["verdict"])
        parked = bool(stop_reason and str(stop_reason).startswith("awaiting_"))
        clean = (not has_output) or (trigger_for(outcome) is None and not parked)
        if clean:
            return {"ok": True, "skipped": "no_finding",
                    "name": name, "session_id": session_id}

    subject, body = _build_finding_message(name, verdict, session_id, stop_reason)
    # A channel that needs a recipient and was given none falls back to the
    # task def's stored notify_target.
    return {**_send_notification(subject, body, name, session_id,
                                 target or task.get("notify_target") or ""),
            "name": name, "session_id": session_id}
