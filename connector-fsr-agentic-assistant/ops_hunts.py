"""Scheduled hunts (X6): a saved hunt question the assistant runs unattended.

A hunt is a question an admin or analyst wants asked again and again ("any host
talking to a known command-and-control address in the last hour") plus a
schedule. The schedule is FortiSOAR's own scheduler, the same one the scheduled
agent tasks use: one scheduler row (`agent-hunt:<name>`) fires one generated
runner playbook whose single step calls `run_hunt`. There is no second
scheduler. This module owns the definition and the run.

A run is one unattended investigation turn started by the system, so it gets
the usual guards: the same tool tiers and approval gates as an analyst's chat,
and nothing here approves anything. The run reads its own verdict card, never
the prose:

* a confirmed or suspicious verdict is a **finding**: the hunt opens a case (an
  alert or an incident, per the definition) with the cited verdict, or comments
  on the case it already opened while that case is still open;
* a benign or false-positive verdict is a **quiet** run: recorded, no case;
* no verdict, or a verdict that says it needs more information, is
  **inconclusive**; a turn that failed is an **error**.

Containment is never automatic. If the investigation proposes containment it
parks as an action card for an analyst, exactly as in auto-triage, and the hunt
never runs the autonomy policy.
"""
from __future__ import annotations

import html
import time
import uuid
from typing import Any

from connectors.core.connector import ConnectorError
from .pydantic_models import (
    DeleteHuntParams, ListHuntsParams, RunHuntParams, SaveHuntParams,
)
from .ops_approvals import comment_on_record
from .ops_autonomy import (
    _DISPOSITION_LABELS, _auto_triage_outcome, _picklist_text,
)
from .ops_chat import chat_turn
from .ops_common import _check_params, _err, _require, _require_transport
from .ops_health import _connector_name
from .ops_monitor import _is_closed_status, _sources
from .ops_playbooks_persist import push_playbook
from .ops_scheduling import (
    _copy_update, _parse_cron, _schedule_create, _schedule_delete,
    _schedule_get_by_name, _schedule_put, _schedule_trigger, _wf_iri_from_push,
)

# Who started a hunt's session. Shown in the history browser in place of an analyst.
_HUNT_ACTOR = {"name": "Scheduled hunt", "iri": "system:hunt", "system": True}

_SCHEDULE_PREFIX = "agent-hunt:"
_HUNT_MODULES = ("alerts", "incidents")
_RECENT_RUNS = 5
# A hunt is an LLM investigation per fire; refuse a schedule tighter than this.
_MIN_INTERVAL_MINUTES = 15

OUTCOME_LABELS = {
    "finding": "Found something",
    "quiet": "Quiet run",
    "inconclusive": "Inconclusive",
    "error": "Failed",
}
_FINDING_DISPOSITIONS = ("true_positive", "suspicious")
_QUIET_DISPOSITIONS = ("benign", "false_positive")

# Verdict severity (the card's vocabulary) -> the alert/incident severity item.
_SEVERITY_ITEMS = {"critical": "Critical", "high": "High", "medium": "Medium",
                   "low": "Low", "info": "Minimal"}

_HUNT_PROMPT = (
    "This is a scheduled hunt. No analyst is watching this conversation, so do "
    "not ask questions and do not wait for an answer: nobody will give one. "
    "Answer the hunt question below by searching with your tools. Use the "
    "log-search tools in your tool list first (they are there only for the "
    "sources configured here), find() the other log, SIEM and threat-"
    "intelligence sources configured here, and never guess a connector. A "
    "question that names no address or host is a broad hunt, not a missing "
    "input: search the log source for the window the question gives (for "
    "example the outbound connections in the last day), enrich the "
    "destinations or indicators you find with threat intelligence, then "
    "conclude. Cite what you searched in your verdict: a source that returned "
    "nothing is a result, so name it. Deliver your conclusion as a verdict "
    "card. Use true_positive when you confirmed what the question asks about, "
    "suspicious when something matches but is not confirmed, benign when you "
    "searched the sources the question needs and found nothing, and "
    "needs_more_info only when you could not search a source the question "
    "needs. If the verdict is true_positive and containment is warranted, "
    "propose it as an action card: it will wait for an analyst to approve it. "
    "Do not change, close or create any record, and stage no other "
    "action.\n\n")


def _hunt_prompt(question: str) -> str:
    """The hunt's opening message: the standing rules, the log sources this box
    can search right now (so a turn with no record to read them from still
    knows), and the question. The sources come from the same availability pass
    that gates the tools; unknown (off a box, a failed lookup) adds nothing."""
    try:
        from fsr_soc_triage.source_availability import (
            ready_search_tools, source_availability)
        ready = ready_search_tools(source_availability())
    except Exception:  # noqa: BLE001 - never fail a hunt over its prompt
        ready = []
    sources = ""
    if ready:
        sources = ("Log sources you can search here: "
                   + "; ".join(f"{label} with {tool}" for label, tool in ready)
                   + ".\n\n")
    return _HUNT_PROMPT + sources + "Hunt question: " + question


# ---------- schedule helpers ------------------------------------------------

def _schedule_name(name: str) -> str:
    return f"{_SCHEDULE_PREFIX}{name}"


def _check_cron(cron: str) -> None:
    """Refuse a malformed or too-frequent cron before anything is written."""
    fields = _parse_cron(cron)   # raises ConnectorError unless 5 fields
    minute = fields["minute"]
    if minute == "*":
        raise ConnectorError(
            f"a hunt cannot run every minute; use at least {_MIN_INTERVAL_MINUTES} minutes")
    if minute.startswith("*/"):
        try:
            step = int(minute[2:])
        except ValueError:
            raise ConnectorError(f"unreadable minute field in cron: {cron!r}") from None
        if step < _MIN_INTERVAL_MINUTES:
            raise ConnectorError(
                f"a hunt cannot run every {step} minutes; use at least "
                f"{_MIN_INTERVAL_MINUTES}")


def _hhmm(hour: str, minute: str) -> str | None:
    if hour.isdigit() and minute.isdigit() and int(hour) < 24 and int(minute) < 60:
        return f"{int(hour):02d}:{int(minute):02d}"
    return None


def schedule_label(cron: str, timezone: str = "UTC") -> str:
    """The schedule as an analyst reads it, from the structured cron fields
    (never from free text). A shape this does not recognise reads as a custom
    schedule."""
    try:
        f = _parse_cron(cron)
    except ConnectorError:
        return "Custom schedule"
    minute, hour, dom, month, dow = (f["minute"], f["hour"], f["day_of_month"],
                                     f["month_of_year"], f["day_of_week"])
    tz = f" {timezone}" if timezone else ""
    rest_any = dom == "*" and month == "*"
    if rest_any and dow == "*":
        if hour == "*" and minute.startswith("*/") and minute[2:].isdigit():
            return f"Every {int(minute[2:])} minutes"
        if hour == "*" and minute.isdigit():
            return "Every hour" if int(minute) == 0 else f"Every hour at :{int(minute):02d}"
        if hour.startswith("*/") and hour[2:].isdigit() and minute.isdigit():
            n = int(hour[2:])
            return "Every hour" if n == 1 else f"Every {n} hours"
        at = _hhmm(hour, minute)
        if at:
            return f"Every day at {at}{tz}"
    if rest_any and dow in ("1-5", "MON-FRI", "mon-fri"):
        at = _hhmm(hour, minute)
        if at:
            return f"Weekdays at {at}{tz}"
    return "Custom schedule"


def _compile_hunt_runner(name: str, agent_config: str) -> dict:
    """Compile the runner playbook: one Connectors step calling run_hunt with the
    hunt's name. The question lives in the definition, not in the playbook, so
    editing it never needs a republish. Returns {ok, workflow_json?, errors}."""
    import yaml
    from .ops_common import (
        _import_fsr_playbooks, _reference_db_path, _serialize_errors,
    )
    fsr_playbooks = _import_fsr_playbooks()
    playbook = {"playbooks": [{
        "name": f"Scheduled Hunt Runner -- {name}",
        "steps": [
            {"name": "start", "type": "start", "next": "run_hunt"},
            {"name": "run_hunt", "type": "Connectors",
             "connector": _connector_name(), "operation": "run_hunt",
             "config": agent_config, "params": {"name": name}},
        ],
    }]}
    yaml_text = yaml.dump(playbook, default_flow_style=False, sort_keys=False,
                          allow_unicode=True)
    result = fsr_playbooks.compile_yaml(yaml_text, _reference_db_path())
    return {"ok": result.ok,
            "workflow_json": result.fsr_json if result.ok else None,
            "errors": _serialize_errors(result.errors)}


def _default_agent_config(mr) -> str | None:
    """The connector configuration name the runner's step calls: the default
    configuration of this connector, else its first. None when it cannot be told."""
    try:
        resp = mr("/api/integration/connectors/?search=assistant", "GET")
    except Exception:  # noqa: BLE001
        return None
    members = (resp or {}).get("data") if isinstance(resp, dict) else resp
    for rec in members or []:
        if isinstance(rec, dict) and rec.get("name") == _connector_name():
            cfgs = [c for c in rec.get("configuration") or [] if isinstance(c, dict)]
            chosen = next((c for c in cfgs if c.get("default")), cfgs[0] if cfgs else None)
            return (chosen or {}).get("name")
    return None


# ---------- definitions -----------------------------------------------------

def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ConnectorError("required parameter missing: name")
    if ":" in name or "/" in name:
        raise ConnectorError("a hunt name cannot contain a colon or a slash")
    if len(name) > 120:
        raise ConnectorError("a hunt name is at most 120 characters")
    return name


def _run_view(run: dict) -> dict:
    return {
        "at": run["started_at"], "finished_at": run["finished_at"],
        "outcome": run["outcome"],
        "outcome_label": OUTCOME_LABELS.get(run["outcome"], run["outcome"]),
        "verdict": _DISPOSITION_LABELS.get(run.get("disposition") or "") or None,
        "severity": run.get("severity"), "confidence": run.get("confidence"),
        "summary": run.get("summary"), "case_iri": run.get("case_iri"),
        "case_action": run.get("case_action"), "waiting": run.get("waiting") or 0,
        "error": run.get("error"), "session_id": run["session_id"],
    }


def _hunt_view(hunt: dict, runs: list[dict]) -> dict:
    """A hunt definition as the Monitor lists it: schedule in words, the last
    run's outcome, and the case its findings go to."""
    last = runs[0] if runs else None
    finding_case = next((r["case_iri"] for r in runs if r.get("case_iri")), None)
    return {
        "name": hunt["name"], "question": hunt["question"], "cron": hunt["cron"],
        "timezone": hunt["timezone"],
        "schedule_label": schedule_label(hunt["cron"], hunt["timezone"]),
        "module": hunt["module"], "enabled": hunt["enabled"],
        "case_iri": finding_case or hunt.get("case_iri"),
        "last_run_at": last["started_at"] if last else None,
        "last_outcome": last["outcome"] if last else None,
        "last_outcome_label": OUTCOME_LABELS.get(last["outcome"]) if last else None,
        "last_summary": last.get("summary") if last else None,
        "runs": [_run_view(r) for r in runs[:_RECENT_RUNS]],
        "created_by": hunt.get("created_by"),
    }


def list_hunts(config: dict, params: dict) -> dict:
    """Every saved hunt with its schedule in words, its last run and its recent
    runs, by name. Pure read from the store (no scheduler call), so it works
    without the FortiSOAR runtime. Returns {ok, hunts}."""
    _check_params(ListHuntsParams, params)
    from .storage import default_storage
    storage = default_storage()
    hunts = [_hunt_view(h, storage.list_hunt_runs(h["name"], limit=_RECENT_RUNS))
             for h in storage.list_hunts()]
    return {"ok": True, "hunts": hunts}


def _write_schedule(mr, name: str, wf_iri: str, cron: str, timezone: str,
                    enabled: bool) -> None:
    sched = _schedule_name(name)
    existing = _schedule_get_by_name(mr, sched)
    if existing is None:
        _schedule_create(mr, sched, wf_iri, cron, timezone, enabled)
        return
    body = _copy_update(
        existing, crontab={**_parse_cron(cron), "timezone": timezone},
        kwargs={**existing.get("kwargs", {}), "wf_iri": wf_iri, "timezone": timezone},
        enabled=enabled)
    _schedule_put(mr, body)


def save_hunt(config: dict, params: dict) -> dict:
    """Create or edit a hunt (idempotent on `name`; an omitted field keeps its
    current value). Persists the definition, pushes the runner playbook when it
    is new or its configuration changed, and writes the FortiSOAR scheduler row
    (paused when `enabled` is false). Returns {ok, name, created, enabled}."""
    _check_params(SaveHuntParams, params)
    name = _clean_name(params.get("name"))
    from .storage import default_storage
    storage = default_storage()
    cur = storage.get_hunt(name)
    question = str(params.get("question") or (cur or {}).get("question") or "").strip()
    if not question:
        raise ConnectorError("required parameter missing: question")
    cron = str(params.get("cron") or (cur or {}).get("cron") or "").strip()
    if not cron:
        raise ConnectorError("required parameter missing: cron")
    _check_cron(cron)
    timezone = params.get("timezone") or (cur or {}).get("timezone") or "UTC"
    module = params.get("module") or (cur or {}).get("module") or "alerts"
    if module not in _HUNT_MODULES:
        raise ConnectorError(f"a hunt opens its case in {' or '.join(_HUNT_MODULES)}, "
                             f"not {module!r}")
    enabled = bool(params["enabled"]) if params.get("enabled") is not None \
        else bool((cur or {}).get("enabled", True))

    mr, err = _require_transport(
        "save_hunt requires the FortiSOAR runtime to push the runner playbook "
        "and write the schedule.")
    if err:
        return err
    agent_config = (params.get("agent_config") or (cur or {}).get("agent_config")
                    or _default_agent_config(mr))
    if not agent_config:
        return _err("no_config", "No assistant configuration could be found to run the hunt.")

    wf_iri = (cur or {}).get("wf_iri")
    wf_uuid = (cur or {}).get("wf_uuid")
    coll_uuid = (cur or {}).get("collection_uuid")
    if cur is None or not wf_iri or agent_config != cur.get("agent_config"):
        compiled = _compile_hunt_runner(name, agent_config)
        if not compiled["ok"]:
            return _err("compile_failed", "hunt runner playbook did not compile",
                        errors=compiled["errors"])
        pushed = push_playbook(config, {"workflow_json": compiled["workflow_json"],
                                        "overwrite": True})
        if not pushed.get("ok"):
            return _err("push_failed", f"hunt runner push failed: "
                        f"{pushed.get('error') or pushed}")
        wf_iri = _wf_iri_from_push(pushed)
        wf_uuid, coll_uuid = pushed.get("workflow_uuid"), pushed.get("collection_uuid")
    try:
        _write_schedule(mr, name, wf_iri, cron, timezone, enabled)
    except Exception as e:  # noqa: BLE001
        return _err("schedule_failed", str(e), wf_iri=wf_iri)

    actor = params.get("actor")
    if isinstance(actor, dict):
        actor = actor.get("name") or actor.get("iri")
    storage.upsert_hunt({
        "name": name, "question": question, "cron": cron, "timezone": timezone,
        "module": module, "enabled": enabled, "agent_config": agent_config,
        "wf_iri": wf_iri, "wf_uuid": wf_uuid, "collection_uuid": coll_uuid,
        "case_iri": (cur or {}).get("case_iri"),
        "created_by": (cur or {}).get("created_by") or (str(actor) if actor else None),
        "created_at": (cur or {}).get("created_at") or time.time()})
    return {"ok": True, "name": name, "created": cur is None, "enabled": enabled,
            "schedule_label": schedule_label(cron, timezone)}


def delete_hunt(config: dict, params: dict) -> dict:
    """Delete a hunt: its scheduler row, its runner playbook collection and its
    definition with the run history. A scheduler or playbook that is already gone
    is not a failure. Returns {ok, name, deleted}."""
    _check_params(DeleteHuntParams, params)
    name = _require(params, "name")
    from .storage import default_storage
    storage = default_storage()
    cur = storage.get_hunt(name)
    mr, err = _require_transport()
    if err is None:
        try:
            _schedule_delete(mr, _schedule_name(name))
        except Exception:  # noqa: BLE001
            pass
        coll = (cur or {}).get("collection_uuid")
        if coll:
            try:
                mr(f"/api/3/workflow_collections/{coll}?hardDelete=true", "DELETE")
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True, "name": name, "deleted": storage.delete_hunt(name)}


# ---------- a run -----------------------------------------------------------

def _verdict_card_of(transcript: list) -> dict:
    return next((f for f in reversed(transcript or [])
                 if isinstance(f, dict) and f.get("type") == "verdict_card"), {})


def classify_run(outcome: dict, error: Any = None) -> str:
    """finding | quiet | inconclusive | error, from the verdict card's
    disposition and whether the turn failed. Nothing reads the prose."""
    if error:
        return "error"
    disposition = (outcome.get("verdict") or {}).get("disposition")
    if disposition in _FINDING_DISPOSITIONS:
        return "finding"
    if disposition in _QUIET_DISPOSITIONS:
        return "quiet"
    return "inconclusive"


def _finding_paragraphs(hunt: dict, card: dict, sources: list[str]) -> list[str]:
    label = _DISPOSITION_LABELS.get(card.get("disposition"), "finding").capitalize()
    conf = card.get("confidence")
    pct = f", {round(float(conf) * 100)}% confident" if isinstance(conf, (int, float)) else ""
    out = [f"Scheduled hunt \"{hunt['name']}\": {label}{pct}.",
           f"Question: {hunt['question']}"]
    if card.get("summary"):
        out.append(f"Verdict: {card['summary']}")
    claims = [str(f.get("claim")) for f in card.get("findings") or []
              if isinstance(f, dict) and f.get("claim")]
    if claims:
        out.append("What it found: " + " ".join(f"({i}) {c}" for i, c in enumerate(claims, 1)))
    if sources:
        out.append("Checked: " + ", ".join(sources) + ".")
    out.append("Review it in the Agentic Assistant Monitor, Scheduled hunts.")
    return out


def _severity_iri(mr, card: dict) -> str | None:
    """The severity picklist item for the verdict's severity, or None when it
    cannot be resolved (the case is then opened without one)."""
    item = _SEVERITY_ITEMS.get(str(card.get("severity") or "").lower())
    if not item:
        return None
    try:
        resp = mr("/api/3/picklist_names?name=Severity&$relationships=true", "GET") or {}
        for pl in resp.get("hydra:member") or []:
            for it in (pl or {}).get("picklists") or []:
                if it.get("itemValue") == item and it.get("@id"):
                    return it["@id"]
    except Exception:  # noqa: BLE001
        return None
    return None


def _case_open(mr, case_iri: str) -> bool:
    """True when the case still exists and is not closed."""
    try:
        rec = mr(case_iri, "GET") or {}
    except Exception:  # noqa: BLE001
        return False
    if not rec or not (rec.get("uuid") or rec.get("@id")):
        return False
    return not _is_closed_status(_picklist_text(rec.get("status")))


def _file_finding(hunt: dict, card: dict, sources: list[str]) -> tuple[str | None, str]:
    """Open a case for the finding, or comment on the one the hunt already
    opened while it is still open. Returns (case iri, action) with action
    opened | updated | failed. Never raises: a failed write must not lose the run."""
    mr, err = _require_transport()
    if err:
        return hunt.get("case_iri"), "failed"
    paragraphs = _finding_paragraphs(hunt, card, sources)
    module = hunt["module"]
    prior = hunt.get("case_iri")
    try:
        if prior and _case_open(mr, prior):
            uuid_ = prior.rstrip("/").rsplit("/", 1)[-1]
            ok = comment_on_record(module, uuid_, "\n".join(paragraphs[:1] + paragraphs[2:]))
            return prior, "updated" if ok else "failed"
        body: dict[str, Any] = {
            "name": f"Hunt finding: {hunt['name']}",
            "description": "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)}
        sev = _severity_iri(mr, card)
        if sev:
            body["severity"] = sev
        rec = mr(f"/api/3/{module}", "POST", body=body) or {}
        iri = rec.get("@id") or (f"/api/3/{module}/{rec['uuid']}" if rec.get("uuid") else None)
        return iri, "opened" if iri else "failed"
    except Exception:  # noqa: BLE001
        return prior, "failed"


def _execute_hunt(config: dict, hunt: dict) -> dict:
    from .storage import default_storage
    storage = default_storage()
    started = time.time()
    session_id = f"hunt:{hunt['name']}:{uuid.uuid4().hex}"
    storage.claim_session(session_id, _HUNT_ACTOR)
    result = chat_turn(config, {
        "session_id": session_id,
        "intent": "triage",
        "messages": [{"role": "user", "content": _hunt_prompt(hunt["question"])}],
        "actor": _HUNT_ACTOR,
        "detached": False,
    })
    transcript = result.get("transcript") or []
    error = result.get("error")
    outcome = _auto_triage_outcome(transcript, result.get("stop_reason"), error)
    kind = classify_run(outcome, error)
    card = _verdict_card_of(transcript)
    case_iri, action = hunt.get("case_iri"), "none"
    if kind == "finding":
        case_iri, action = _file_finding(hunt, card, _sources(transcript))
        if action in ("opened", "updated") and case_iri != hunt.get("case_iri"):
            storage.set_hunt_case(hunt["name"], case_iri)
    verdict = outcome.get("verdict") or {}
    run = {
        "hunt_name": hunt["name"], "session_id": session_id,
        "started_at": started, "finished_at": time.time(), "outcome": kind,
        "disposition": verdict.get("disposition"), "severity": verdict.get("severity"),
        "confidence": verdict.get("confidence"), "summary": verdict.get("summary"),
        "case_iri": case_iri if kind == "finding" else None,
        "case_action": action, "waiting": outcome.get("waiting") or 0,
        "error": str(error) if error else None,
    }
    storage.record_hunt_run(run)
    from .ops_notify import notify_turn
    notified = notify_turn(config, kind="hunt", name=hunt["name"],
                           session_id=session_id, outcome=outcome,
                           case_iri=run["case_iri"])
    return {"ok": True, "name": hunt["name"], "session_id": session_id,
            "notified": (notified or {}).get("status"),
            "outcome": kind, "outcome_label": OUTCOME_LABELS[kind],
            "disposition": run["disposition"], "severity": run["severity"],
            "confidence": run["confidence"], "summary": run["summary"],
            "case_iri": run["case_iri"], "case_action": action,
            "waiting": run["waiting"]}


def run_hunt(config: dict, params: dict) -> dict:
    """Run one hunt. Called by the hunt's runner playbook on each schedule fire;
    with `trigger` true it asks the scheduler to fire the hunt now instead (the
    Monitor's Run now). A paused hunt does not fire on its schedule, but a manual
    run still works. Returns the run's outcome, or {ok, triggered} for a trigger."""
    _check_params(RunHuntParams, params)
    name = _require(params, "name")
    from .storage import default_storage
    hunt = default_storage().get_hunt(name)
    if hunt is None:
        return _err("not_found", f"no hunt named {name!r}")
    if params.get("trigger"):
        mr, err = _require_transport("run_hunt needs the FortiSOAR runtime to trigger.")
        if err:
            return err
        try:
            _schedule_trigger(mr, _schedule_name(name))
        except ConnectorError as e:
            return _err("not_found", str(e))
        except Exception as e:  # noqa: BLE001
            return _err("trigger_failed", str(e))
        return {"ok": True, "name": name, "triggered": True}
    return _execute_hunt(config, hunt)
