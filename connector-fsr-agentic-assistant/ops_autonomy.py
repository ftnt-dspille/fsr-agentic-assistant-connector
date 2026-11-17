"""autonomy domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
import time
from typing import Any
from .pydantic_models import (
    AutoTriageParams, SetAutoTriageParams,
    ListShadowDecisionsParams, SetAutonomyPolicyParams,
)

from .ops_approvals import (
    RECORD_WRITE_CONNECTOR, RECORD_WRITE_OP, _policy_edit_gate, _undo_spec, _writable,
    comment_on_record,
)
from .ops_chat import chat_resume, chat_turn
from .ops_common import (
    _check_params, _err, _import_fsr_playbooks, _log, _reference_db_path, _require,
    _require_transport, _serialize_errors,
)
from .ops_health import _connector_name
from .ops_playbooks_persist import push_playbook
from .ops_policy import _load_autonomy_policy
from .ops_prompts import _session_trace_scope
from .ops_resume import _approved_card_config, _record_execution
from .ops_scheduling import _send_notification, _wf_iri_from_push



# ---------- Auto-triage: triage a record when it is created ----------------
#
# `set_auto_triage` installs an on-create playbook for a module; each new record
# fires `auto_triage`, which runs one unattended triage turn on it. Nothing here
# approves anything: an action the agent proposes parks as a card in the queue
# (`list_pending`) exactly as it would in an analyst's chat. Which records are
# triaged is decided here, not in the trigger, so the filter is plain code that
# can change without republishing the playbook.
# Plan: tracker plans/AUTONOMOUS_TIER1.md, phase A.

# Who started an auto-triage session. Shown in the Monitor and the history
# browser in place of an analyst.
_AUTO_TRIAGE_ACTOR = {"name": "Auto-triage", "iri": "system:auto-triage",
                      "system": True}

_AUTO_TRIAGE_PROMPT = (
    "Triage this record. No analyst is watching this conversation, so do not "
    "ask questions: nobody will answer them. Investigate it with your tools, "
    "including a threat-intelligence lookup of each external address (find "
    "the lookups configured here with find(kind='action', "
    "action_type='enrichment', target_type='ip'); never guess a connector), "
    "and cite those lookups in your verdict: traffic logs show that a connection "
    "happened, not that the address is malicious. Then deliver your verdict as "
    "a verdict card. If the verdict is "
    "true_positive and containment is warranted, propose it as an action card; "
    "it will wait for an analyst to approve it. A false_positive or benign "
    "verdict closes the record: stage that close with update_record (set it to "
    "a closed status, resolved as a false positive) and propose no other "
    "action. Whether the close runs on its own or waits for an analyst is "
    "decided by the autonomy policy, not by you; stage it either way, and stage "
    "no containment or other action.")

_AUTO_TRIAGE_DEFAULT_SEVERITIES = ("High", "Critical")

# Card frames that leave something for a human to decide.
_AUTO_TRIAGE_WAITING_FRAMES = ("action_card", "approval_request")

# Stop reasons that mean the turn parked on an approval. Any other
# `awaiting_*` stop parked on a question (a choice, a form, an offer), which in
# an unattended session nobody is there to answer.
_AUTO_TRIAGE_APPROVAL_STOPS = ("awaiting_approval", "awaiting_action_card")

# Verdicts a human should look at even when nothing is waiting.
_AUTO_TRIAGE_REVIEW_DISPOSITIONS = ("suspicious", "needs_more_info")


def _picklist_text(value: Any) -> str:
    """A field's display text: a picklist/lookup dict's itemValue, else the
    value itself."""
    if isinstance(value, dict):
        value = value.get("itemValue") or value.get("name") or ""
    return str(value or "")


def _auto_triage_skip(record: dict, severities: list, sources: list) -> str | None:
    """Why this record is outside the auto-triage filter, or None to triage it.
    Empty `severities` / `sources` mean any."""
    if severities:
        sev = _picklist_text(record.get("severity")).lower()
        if sev not in {str(s).lower() for s in severities}:
            return "severity"
    if sources:
        src = _picklist_text(record.get("source")).lower()
        if src not in {str(s).lower() for s in sources}:
            return "source"
    return None


def _record_entity(record: dict, module: str, iri: str) -> dict:
    """The `entity` the chat drawer would mount for this record: scalar fields,
    picklists and lookups collapsed to their display text, empties dropped."""
    fields: dict = {}
    for k, v in record.items():
        if k.startswith("@") or k in ("__self", "modelMetadata"):
            continue
        if isinstance(v, dict):
            v = _picklist_text(v) or None
        if isinstance(v, (str, int, float, bool)) and v != "":
            fields[k] = v
    return {"module": module, "iri": iri, "uuid": record.get("uuid"),
            "id": record.get("id"), "fields": fields}


def _auto_triage_outcome(transcript: list, stop_reason: Any = None,
                         error: Any = None) -> dict:
    """What the turn ended with: the delivered verdict (the last verdict card),
    how many cards wait on a human, and why a human is needed at all."""
    frames = [f for f in transcript or [] if isinstance(f, dict)]
    verdict_card = next((f for f in reversed(frames)
                         if f.get("type") == "verdict_card"), None)
    verdict = None
    if verdict_card is not None:
        verdict = {k: verdict_card.get(k)
                   for k in ("disposition", "severity", "confidence", "summary")
                   if verdict_card.get(k) is not None}
    # A card the policy already acted on (phase E) waits on nobody.
    waiting = sum(1 for f in frames if f.get("type") in _AUTO_TRIAGE_WAITING_FRAMES
                  and not f.get("enforced"))
    unverified = [f for f in frames if (f.get("enforced") or {}).get("verified") is False]
    why = []
    if error:
        # A turn that failed part-way is not a triage, whatever it delivered.
        why.append("turn_error")
    if verdict is None:
        why.append("no_verdict")
    elif verdict.get("disposition") in _AUTO_TRIAGE_REVIEW_DISPOSITIONS:
        why.append("needs_review")
    if unverified:
        # The policy acted, but the block could not be read back (or the op
        # failed): a human must check what actually happened.
        why.append("action_unverified")
    stop = str(stop_reason or "")
    acted = any(f.get("enforced") for f in frames)
    if waiting or (stop in _AUTO_TRIAGE_APPROVAL_STOPS and not acted):
        why.append("awaiting_approval")
    elif stop.startswith("awaiting_") and stop not in _AUTO_TRIAGE_APPROVAL_STOPS:
        why.append("awaiting_answer")
    elif (verdict is not None and verdict.get("disposition") == "true_positive"
          and not acted):
        # Live on .159: a C2 true positive with nothing staged reached no queue
        # at all. Until a policy may act alone (phase E), a confirmed threat
        # that nothing is acting on needs a human.
        why.append("confirmed_threat")
    return {"verdict": verdict, "waiting": waiting,
            "needs_human": bool(why), "why": why}


# Severity text -> queue rank. Anything else (low, minimal, informational,
# unknown) ranks last.
_SEVERITY_RANK = {"critical": 1, "high": 2, "medium": 3}

_DISPOSITION_LABELS = {
    "true_positive": "true positive", "false_positive": "false positive",
    "benign": "benign", "suspicious": "suspicious",
    "needs_more_info": "needs more information",
}


def _escalation_reasons(outcome: dict) -> list[str]:
    """Why a human is needed, in the words a reviewer reads."""
    verdict = outcome.get("verdict") or {}
    out = []
    for code in outcome.get("why") or []:
        if code == "no_verdict":
            out.append("The assistant did not reach a verdict.")
        elif code == "needs_review":
            label = _DISPOSITION_LABELS.get(verdict.get("disposition"),
                                            str(verdict.get("disposition")))
            conf = verdict.get("confidence")
            pct = f", {round(float(conf) * 100)}% confident" \
                if isinstance(conf, (int, float)) else ""
            out.append(f"The verdict needs review: {label}{pct}.")
        elif code == "awaiting_approval":
            n = outcome.get("waiting") or 0
            out.append(f"{n} action{'' if n == 1 else 's'} waiting for approval."
                       if n else "An action is waiting for approval.")
        elif code == "awaiting_answer":
            out.append("The assistant stopped on a question nobody has answered.")
        elif code == "confirmed_threat":
            out.append("The assistant confirmed a threat and nothing has acted on it yet.")
        elif code == "action_unverified":
            out.append("The autonomy policy ran an action that could not be "
                       "confirmed afterwards; check it was applied.")
        elif code == "turn_error":
            err = (outcome.get("error") or {}).get("message")
            out.append("The triage stopped on an error" + (f": {err}" if err else "."))
    return out


def _escalation_card(session_id: str, record: dict, module: str, iri: str,
                     outcome: dict) -> dict:
    """The queue entry for a record auto-triage could not finish alone: what
    the record is, what the assistant concluded, why it stopped, and how
    urgent it is (the higher of the record's and the verdict's severity)."""
    verdict = outcome.get("verdict") or {}
    rec_sev = _picklist_text(record.get("severity"))
    rank = min(_SEVERITY_RANK.get(rec_sev.lower(), 4),
               _SEVERITY_RANK.get(str(verdict.get("severity") or "").lower(), 4))
    return {
        "type": "escalation_card",
        "id": "esc_" + session_id.rsplit(":", 1)[-1],
        "record": {"name": record.get("name"), "module": module, "iri": iri,
                   "uuid": record.get("uuid"), "severity": rec_sev or None},
        "verdict": outcome.get("verdict"),
        "why": list(outcome.get("why") or []),
        "reasons": _escalation_reasons(outcome),
        "waiting": outcome.get("waiting") or 0,
        "priority": f"P{rank}",
        "priority_rank": rank,
    }


def _notify_escalation(card: dict, session_id: str, target: str) -> str:
    """Tell a reviewer about an escalation. Returns the channel it went out on,
    or why it did not (no_channels / no_target / send_failed ...)."""
    rec = card.get("record") or {}
    name = rec.get("name") or rec.get("iri") or session_id
    subject = f"[Agentic Assistant] {card['priority']} needs review: {name}"
    lines = [f"Auto-triage could not finish {rec.get('module') or 'record'} "
             f"\"{name}\" on its own."]
    lines += card.get("reasons") or []
    summary = (card.get("verdict") or {}).get("summary")
    if summary:
        lines += ["", f"Verdict: {summary}"]
    lines += ["", "Review it in the Agentic Assistant Monitor, Pending tab."]
    try:
        sent = _send_notification(subject, "\n".join(lines), "auto-triage",
                                  session_id, target)
    except Exception as e:  # noqa: BLE001 -- a notify failure never fails triage
        return f"send_failed: {e}"
    if sent.get("ok") is False:
        return str((sent.get("error") or {}).get("code") or "send_failed")
    # A skip still names the channel it would have used, so the skip is read
    # first: live, a skip for want of a recipient reported "smtp".
    return sent.get("skipped") or sent.get("channel") or "unknown"


# ---- phase E: the policy acts (auto-triage only) ---------------------------
#
# A card the turn staged carries the policy's decision (framework autonomy).
# When the LIVE policy is enabled and in `enforce` mode and that decision is
# `would_act`, the connector runs the card itself, exactly as an approval
# would, as `system:autonomy`. Only unattended auto-triage enforces: in an
# analyst's chat the analyst is there to click. Every other card still waits.

_AUTONOMY_ACTOR = "system:autonomy"

# op -> (read-back op, args copied from the action) proving the action landed.
_ENFORCE_READBACK = {
    ("fortigate-firewall", "block_ip_new"): ("get_blocked_ip", ("method", "ip_block_policy",
                                                                "ip_group_name", "ngfw_mode", "vdom")),
    ("fortigate-firewall", "block_ip"): ("get_blocked_ip", ("method", "ip_block_policy",
                                                            "ip_group_name", "ngfw_mode", "vdom")),
}


def _enforceable_rules() -> set[str]:
    """Rule ids the live policy enforces right now (empty when the policy is
    off, in shadow, missing or unreadable). Read fresh-ish (15s cache), so the
    kill switch stops enforcement within seconds."""
    policy, error = _load_autonomy_policy()
    if error or not isinstance(policy, dict) or policy.get("enabled") is False \
            or policy.get("mode") != "enforce":
        return set()
    return {r.get("id") for r in policy.get("rules") or [] if isinstance(r, dict) and r.get("id")}


def _read_back(connector: str, operation: str, args: dict, targets: list,
               config: Any) -> bool | None:
    """True when every target shows up in the read-back, False when one does
    not, None when this op has no read-back."""
    spec = _ENFORCE_READBACK.get((connector, operation))
    if spec is None:
        return None
    op, keys = spec
    params = {k: args[k] for k in keys if args.get(k) not in (None, "")}
    from fsr_playbooks.llm.tools import dispatch
    call = {"connector": connector, "op": op, "params": params}
    if config:
        call["config"] = config
    try:
        res = dispatch("run_op", call, _internal=True)
    except Exception:  # noqa: BLE001
        return False
    if not (isinstance(res, dict) and res.get("ok")):
        return False
    blob = json.dumps(res, default=str)
    return all(str(t) in blob for t in targets)


def _enforce_policy_cards(session_id: str, transcript: list) -> list[dict]:
    """Run every card in ``transcript`` the live policy enforces and marks
    would_act. Mutates each acted card frame (``enforced``) and its stored
    copy. Returns what ran. Never raises."""
    try:
        rules = _enforceable_rules()
    except Exception:  # noqa: BLE001
        return []
    if not rules:
        return []
    from fsr_playbooks.llm.tools import dispatch
    from .storage import default_storage
    storage = default_storage()
    acted: list[dict] = []
    for f in transcript:
        if not isinstance(f, dict) or f.get("type") != "action_card":
            continue
        pol = f.get("policy") or {}
        if pol.get("outcome") != "would_act" or pol.get("rule") not in rules:
            continue
        connector, operation = f.get("connector"), f.get("operation")
        card_id = f.get("id")
        if not (connector and operation and card_id):
            continue
        # Decided once, like any approval: a card someone already answered is
        # left alone.
        if storage.claim_card_decision(session_id, card_id, "approve",
                                       _AUTONOMY_ACTOR) is not None:
            continue
        args = {k: v for k, v in (f.get("args") or {}).items()
                if not str(k).startswith("_")}
        call = {"connector": connector, "op": operation, "params": dict(args),
                "confirm": True, "_approved": True}
        config = _approved_card_config(f, None)
        if config:
            call["config"] = config
        try:
            with _session_trace_scope(storage, session_id):
                res = dispatch("run_op", call, _internal=True, session_id=session_id,
                               approved_by=_AUTONOMY_ACTOR)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "code": "exec_dispatch_failed",
                   "error": f"{type(e).__name__}: {e}"}
        ok = bool(isinstance(res, dict) and res.get("ok"))
        _record_execution(storage, session_id, card_id, connector, operation,
                          config, args, ok)
        verified = (_read_back(connector, operation, args,
                               pol.get("targets") or [], config) if ok else False)
        enforced = {"by": _AUTONOMY_ACTOR, "rule": pol.get("rule"), "ok": ok,
                    # None (no read-back for this op) counts as unverified:
                    # an action nobody can confirm goes to a human.
                    "verified": bool(verified), "result": res}
        f["enforced"] = enforced
        f["policy"] = {**pol, "mode": "enforced"}
        try:
            storage.save_card(session_id, f)
        except Exception:  # noqa: BLE001
            pass
        _log(f"autonomy: {pol.get('rule')} ran {connector}.{operation} on "
             f"{pol.get('targets')} in {session_id}: ok={ok} verified={verified}",
             "info")
        acted.append({"card_id": card_id, "connector": connector,
                      "operation": operation, "targets": pol.get("targets") or [],
                      "rule": pol.get("rule"), "ok": ok, "verified": bool(verified)})
    return acted


# Approval-carded tools the policy may approve itself (phase F). A record write
# is approved through chat_resume -- the same route an analyst's click takes,
# so it re-runs the exact suspended call through every guard.
_ENFORCEABLE_APPROVAL_TOOLS = frozenset({"update_record"})


def _picklist_matches(stored: Any, written: Any) -> bool:
    """A record's stored value equals what was written: picklists come back as
    {"@id", "itemValue"}, and either the IRI or the label may have been sent."""
    w = written.get("@id") if isinstance(written, dict) else written
    if isinstance(stored, dict):
        return str(w) in (str(stored.get("@id")), str(stored.get("itemValue")))
    return str(stored) == str(w)


def _record_write(frame: dict) -> dict:
    """{module, uuid, fields} of the record write a card stages. On the wire a
    record write arrives as an action_card (``target`` + ``args`` = the
    fields, see ops_cards._approval_to_action_card); the raw approval_request
    shape (``preview.args``) is read too."""
    if frame.get("type") == "action_card":
        target = frame.get("target") or {}
        return {"module": target.get("module"), "uuid": target.get("uuid"),
                "fields": frame.get("args")}
    return dict(((frame.get("preview") or {}).get("args")) or {})


def _read_back_record(frame: dict) -> bool:
    """True when the record now holds every field the approved update wrote."""
    args = _record_write(frame)
    module, uuid, fields = args.get("module"), args.get("uuid"), args.get("fields")
    if not (module and uuid and isinstance(fields, dict) and fields):
        return False
    mr, err = _require_transport("read-back needs the FortiSOAR runtime")
    if err:
        return False
    try:
        rec = mr(f"/api/3/{module}/{uuid}", "GET") or {}
    except Exception:  # noqa: BLE001
        return False
    return all(_picklist_matches(rec.get(k), v) for k, v in fields.items())


def _prior_values(frame: dict) -> dict | None:
    """The values the approved update is about to replace, read from the
    record, in writable form. None when the record cannot be read: the close
    still runs, but it cannot be undone."""
    args = _record_write(frame)
    module, uuid, fields = args.get("module"), args.get("uuid"), args.get("fields")
    if not (module and uuid and isinstance(fields, dict) and fields):
        return None
    mr, err = _require_transport("reading prior values needs the FortiSOAR runtime")
    if err:
        return None
    try:
        rec = mr(f"/api/3/{module}/{uuid}", "GET") or {}
    except Exception:  # noqa: BLE001
        return None
    return {k: _writable(rec.get(k)) for k in fields}


def _close_comment(transcript: list) -> str:
    """The note left on a record the policy closed: why, and how to reverse it."""
    verdict = next((f for f in reversed(transcript) if isinstance(f, dict)
                    and f.get("type") == "verdict_card"), {}) or {}
    why = str(verdict.get("summary") or "").strip()
    conf = verdict.get("confidence")
    sure = (f" ({round(float(conf) * 100)}% confidence)"
            if isinstance(conf, (int, float)) else "")
    text = f"Closed automatically by the Agentic Assistant as a false positive{sure}."
    if why:
        text += f" {why.rstrip('.')}."
    return text + " To reopen it, use Undo in the Agentic Assistant Monitor."


def _detection(record: dict | None) -> dict | None:
    """The detection rule that raised the record: its source and rule name,
    the alert type when no rule is named. Keys the Monitor's tuning table."""
    if not isinstance(record, dict):
        return None
    rtype = record.get("type")
    if isinstance(rtype, dict):
        rtype = rtype.get("itemValue")
    rule = str(record.get("rule") or rtype or "").strip()
    source = str(record.get("source") or "").strip()
    return {"source": source, "rule": rule} if (rule or source) else None


def _run_staged_close(config: dict, session_id: str, transcript: list,
                      f: dict, approval_id: str, record: dict | None,
                      rule: str, targets: list) -> dict:
    """Approve one staged record-write close as ``system:autonomy``, read it
    back, comment on the record, and record it for Undo. Mutates ``f``
    (``enforced``/``policy``) and its stored copy. Returns the acted summary.
    Shared by the policy-enforced close (phase F) and the agent's self-close of
    its own false positive (phase A). Never raises."""
    from .storage import default_storage
    storage = default_storage()
    prior = _prior_values(f)
    try:
        # Who decided travels only as the keyword argument: the `actor`
        # param is the widget's {name, iri} claim, and a bare string there
        # failed validation, so no autonomous close ever ran.
        res = chat_resume(config, {"session_id": session_id,
                                   "approval_id": approval_id,
                                   "decision": "approve"},
                          _policy_decider=_AUTONOMY_ACTOR)
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "code": "exec_resume_failed",
               "error": f"{type(e).__name__}: {e}"}
    ok = isinstance(res, dict) and res.get("ok") is not False
    verified = _read_back_record(f) if ok else False
    written = _record_write(f)
    # What ran and what it replaced, so the Monitor can offer Undo.
    _record_execution(storage, session_id, approval_id,
                      RECORD_WRITE_CONNECTOR, RECORD_WRITE_OP, None,
                      {"module": written.get("module"), "uuid": written.get("uuid"),
                       "fields": written.get("fields"), "prior": prior}, ok)
    commented = (comment_on_record(written["module"], written["uuid"],
                                   _close_comment(transcript))
                 if verified else False)
    f["enforced"] = {"by": _AUTONOMY_ACTOR, "rule": rule, "ok": ok,
                     "verified": bool(verified), "commented": commented,
                     "undoable": prior is not None,
                     "detection": _detection(record),
                     "result": {k: res.get(k) for k in ("ok", "code", "error",
                                                        "stop_reason")
                                if isinstance(res, dict) and k in res}}
    f["policy"] = {**(f.get("policy") or {}), "rule": rule, "mode": "enforced"}
    # The card was stored when the turn ended; store what ran against it.
    try:
        storage.save_card(session_id, f)
    except Exception:  # noqa: BLE001
        pass
    _log(f"autonomy: {rule} approved {f.get('tool')} on {targets} in "
         f"{session_id}: ok={ok} verified={verified}", "info")
    return {"card_id": approval_id, "tool": f.get("tool"),
            "targets": targets, "rule": rule,
            "ok": ok, "verified": bool(verified)}


def _enforce_policy_approvals(config: dict, session_id: str,
                              transcript: list,
                              record: dict | None = None) -> list[dict]:
    """Approve every approval card the live policy enforces and marks
    would_act, as ``system:autonomy``, then read the record back. Mutates each
    acted frame (``enforced``). Returns what ran. Never raises."""
    try:
        rules = _enforceable_rules()
    except Exception:  # noqa: BLE001
        return []
    if not rules:
        return []
    acted: list[dict] = []
    for f in transcript:
        # A record write reaches the transcript as an action_card carrying its
        # approval_id (the converted approval); this matched only the raw
        # approval_request, so no policy close ever ran.
        if not isinstance(f, dict) or f.get("type") not in ("approval_request",
                                                            "action_card"):
            continue
        pol = f.get("policy") or {}
        approval_id = f.get("approval_id") or (
            f.get("id") if f.get("type") == "action_card" else None)
        if (pol.get("outcome") != "would_act" or pol.get("rule") not in rules
                or f.get("tool") not in _ENFORCEABLE_APPROVAL_TOOLS
                or not approval_id):
            continue
        acted.append(_run_staged_close(config, session_id, transcript, f,
                                       approval_id, record, pol.get("rule"),
                                       pol.get("targets") or []))
    return acted


# ---- alert-storm grouping ---------------------------------------------------
# Related alerts arriving together (same address or host, same rule) are one
# incident, not one session each. The first is the lead and is triaged; later
# ones are members: they get a note on the record and no model run. A rate cap
# bounds sessions per window overall; a record over it is deferred (noted, no
# session), never dropped. All knobs ride in the `storm` param.
_STORM_DEFAULTS = {
    "group": True,                 # group related alerts under one lead
    "window_minutes": 15,          # a lead stays open for members this long
    "keys": ["sourceIp", "destinationIp", "computerName"],
    "same_name": True,             # members must also share the alert name
    "max_sessions": 100,           # leads per cap window; 0 = no cap
    "cap_window_minutes": 60,
}


def _now() -> float:
    return time.time()


def _storm_settings(raw: Any) -> dict:
    cfg = dict(_STORM_DEFAULTS)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in cfg and v is not None})
    return cfg


def _storm_keys(record: dict, cfg: dict) -> list[str]:
    """The group keys of a record: one per set key field, each qualified by the
    alert name when `same_name`. Empty when grouping is off or no field is set."""
    if not cfg["group"]:
        return []
    name = _picklist_text(record.get("name")).strip().lower()
    if cfg["same_name"] and not name:
        return []
    keys = []
    for field in cfg["keys"] or []:
        val = _picklist_text(record.get(field)).strip().lower()
        if val:
            keys.append(f"{field}={val}|{name if cfg['same_name'] else ''}")
    return keys


def _storm_note(route: dict) -> str:
    if route["role"] == "member":
        return (f"Grouped with an earlier related alert: it shares an address "
                f"or host and a rule with it, so one triage covers all "
                f"{route['members']} alerts so far. See the lead alert's triage "
                f"for the verdict.")
    return ("Triage deferred: too many alerts started triage in the last "
            "window (storm cap). This alert is not dropped; triage it by hand "
            "or re-run auto-triage on it once the window passes.")


def auto_triage(config: dict, params: dict) -> dict:
    """Triage one record unattended. Called by the on-create playbook that
    `set_auto_triage` installs. Each record is triaged once (session
    `auto:<module>:<uuid>`); records outside the severity/source filter are
    skipped. Returns {ok, session_id, verdict, waiting, needs_human, why} or
    {ok, skipped}."""
    _check_params(AutoTriageParams, params)
    iri = _require(params, "record")
    parts = [p for p in str(iri).split("/") if p]
    if len(parts) < 4 or parts[0] != "api":
        return _err("bad_iri", f"record must be a record IRI like "
                    f"/api/3/alerts/<uuid>, got {iri!r}")
    module = parts[-2]
    mr, err = _require_transport(
        "auto_triage requires the FortiSOAR runtime to read the record.")
    if err:
        return err
    try:
        record = mr(iri, "GET") or {}
    except Exception as e:  # noqa: BLE001
        return _err("record_unreadable", str(e), record=iri)

    severities = params.get("severities")
    if severities is None:
        severities = list(_AUTO_TRIAGE_DEFAULT_SEVERITIES)
    skip = _auto_triage_skip(record, severities, params.get("sources") or [])
    if skip:
        return {"ok": True, "skipped": skip, "record": iri}

    session_id = f"auto:{module}:{record.get('uuid') or parts[-1]}"
    from .storage import default_storage
    cfg = _storm_settings(params.get("storm"))
    route = default_storage().route_triage(
        iri, session_id, _storm_keys(record, cfg),
        float(cfg["window_minutes"]) * 60, int(cfg["max_sessions"]),
        float(cfg["cap_window_minutes"]) * 60, _now())
    if route["role"] in ("member", "deferred"):
        if not route.get("repeat"):
            comment_on_record(module, str(record.get("uuid") or parts[-1]),
                              _storm_note(route))
        out = {"ok": True, "record": iri, "skipped": (
            "grouped" if route["role"] == "member" else "storm_cap")}
        if route["role"] == "member":
            out.update(lead_session=route["lead_session"],
                       lead_record=route["lead_record"],
                       group_size=route["members"])
        return out
    if not default_storage().claim_session(session_id, _AUTO_TRIAGE_ACTOR):
        return {"ok": True, "skipped": "already_triaged", "record": iri,
                "session_id": session_id}

    result = chat_turn(config, {
        "session_id": session_id,
        "intent": "triage",
        "messages": [{"role": "user", "content": _AUTO_TRIAGE_PROMPT}],
        "entity": _record_entity(record, module, iri),
        "actor": _AUTO_TRIAGE_ACTOR,
        "detached": False,
    })
    acted = _enforce_policy_cards(session_id, result.get("transcript") or [])
    # A false-positive close is authorized by the autonomy policy (a
    # close-false-positive rule in enforce mode), never by the agent directly:
    # the model proposes the close, the deterministic policy decides and runs
    # it. In shadow -- the default -- nothing acts and the close waits for an
    # analyst in the Monitor queue.
    acted += _enforce_policy_approvals(config, session_id,
                                       result.get("transcript") or [], record)
    outcome = _auto_triage_outcome(result.get("transcript") or [],
                                   result.get("stop_reason"), result.get("error"))
    if acted:
        outcome["acted"] = acted
    if result.get("error"):
        outcome["error"] = {"code": "turn_error", "message": str(result.get("error"))}
    out = {"ok": bool(result.get("ok", True)), "record": iri,
           "session_id": session_id, "stop_reason": result.get("stop_reason"),
           **outcome}
    # A playbook that names its own notify target keeps the per-playbook path;
    # otherwise the configured channel (X6, off by default) sends the one
    # notification, so nobody is told twice.
    from .ops_notify import notifications_enabled, notify_turn
    own_target = params.get("notify_target") or ""
    if outcome["needs_human"]:
        card = _escalation_card(session_id, record, module, iri, outcome)
        default_storage().save_card(session_id, card)
        out["escalation_id"] = card["id"]
        out["priority"] = card["priority"]
        if own_target or not notifications_enabled(config):
            out["notified"] = _notify_escalation(card, session_id, own_target)
    if not own_target:
        sent = notify_turn(config, kind="auto_triage", name=str(record.get("name") or iri),
                           session_id=session_id, outcome=outcome)
        if sent:
            out["notified"] = sent.get("channel") or sent["status"]
    return out


def _compile_auto_triage_playbook(module: str, agent_config: str,
                                  severities: list, sources: list,
                                  enabled: bool, notify_target: str = "") -> dict:
    """Compile the on-create playbook: a create trigger on `module` and one
    Connectors step calling auto_triage on the new record. Returns
    {ok, workflow_json?, errors}."""
    import yaml
    fsr_playbooks = _import_fsr_playbooks()
    playbook = {
        "playbooks": [{
            "name": f"Agentic Assistant Auto-Triage -- {module}",
            "is_active": bool(enabled),
            "steps": [
                {"name": "On Create", "type": "start_on_create",
                 "module": module, "next": "auto_triage"},
                {"name": "auto_triage", "type": "Connectors",
                 "connector": _connector_name(), "operation": "auto_triage",
                 "config": agent_config,
                 "params": {"record": "{{ vars.input.records[0]['@id'] }}",
                            "severities": list(severities),
                            "sources": list(sources),
                            "notify_target": notify_target}},
            ],
        }]
    }
    yaml_text = yaml.dump(playbook, default_flow_style=False, sort_keys=False,
                          allow_unicode=True)
    result = fsr_playbooks.compile_yaml(yaml_text, _reference_db_path())
    return {
        "ok": result.ok,
        "workflow_json": result.fsr_json if result.ok else None,
        "errors": _serialize_errors(result.errors),
    }


def set_auto_triage(config: dict, params: dict) -> dict:
    """Install (or replace) the auto-triage trigger for a module: an on-create
    playbook that calls auto_triage for every new record. `enabled: false`
    keeps the playbook but deactivates it. Returns {ok, module, wf_iri,
    enabled, severities, sources}."""
    _check_params(SetAutoTriageParams, params)
    agent_config = _require(params, "agent_config")
    module = params.get("module") or "alerts"
    severities = params.get("severities")
    if severities is None:
        severities = list(_AUTO_TRIAGE_DEFAULT_SEVERITIES)
    sources = params.get("sources") or []
    enabled = bool(params.get("enabled", True))

    _, err = _require_transport(
        "set_auto_triage requires the FortiSOAR runtime to push the trigger "
        "playbook.")
    if err:
        return err
    notify_target = params.get("notify_target") or ""
    compiled = _compile_auto_triage_playbook(module, agent_config, severities,
                                             sources, enabled, notify_target)
    if not compiled["ok"]:
        return _err("compile_failed", "auto-triage playbook did not compile",
                    errors=compiled["errors"])
    pushed = push_playbook(config, {"workflow_json": compiled["workflow_json"],
                                    "overwrite": True})
    if not pushed.get("ok"):
        return _err("push_failed", f"auto-triage playbook push failed: "
                    f"{pushed.get('error') or pushed}")
    seeded = _seed_default_policy(module) if enabled else {"seeded": False}
    return {"ok": True, "module": module, "wf_iri": _wf_iri_from_push(pushed),
            "enabled": enabled, "severities": list(severities),
            "sources": list(sources), "notify_target": notify_target,
            "policy_seeded": seeded.get("seeded", False)}


def _seed_default_policy(module: str) -> dict:
    """Turning auto-triage on, with no autonomy policy yet, drops in the default
    FP-close rule IN SHADOW -- so the Monitor shows "would it have been right?"
    from day one, while acting still waits for a deliberate promotion to
    enforce. Never overwrites an existing policy, and a seed failure never fails
    enabling auto-triage (it only means the admin authors the policy by hand).
    Returns {seeded: bool, error?}."""
    from .ops_policy import (default_fp_close_policy, validate_policy_dict,
                             write_autonomy_policy)
    existing, _ = _load_autonomy_policy(force=True)
    if existing:
        return {"seeded": False}
    policy = default_fp_close_policy(module)
    err = validate_policy_dict(policy) or write_autonomy_policy(policy)
    if err:
        _log(f"auto-triage: default policy not seeded: {err}", "warning")
        return {"seeded": False, "error": err}
    return {"seeded": True}


def _tuning_counts(rows: list[dict]) -> list[dict]:
    """Per detection rule: how many of its records the policy closed as false
    positives, and how many of those a person reopened. A rule that keeps
    raising false positives is one to tune at its source; reopened closes say
    the policy itself was wrong. Most closes first."""
    by: dict[tuple[str, str], dict] = {}
    for r in rows:
        enf = r.get("enforced") or {}
        det = enf.get("detection")
        if (r.get("operation") != RECORD_WRITE_OP or not isinstance(det, dict)
                or not (enf.get("ok") and enf.get("verified"))):
            continue
        key = (det.get("source") or "", det.get("rule") or "")
        row = by.setdefault(key, {"source": key[0], "rule": key[1],
                                  "closed": 0, "reopened": 0, "last_at": 0.0})
        row["closed"] += 1
        row["reopened"] += 1 if r.get("undone_at") else 0
        row["last_at"] = max(row["last_at"], float(r.get("at") or 0))
    return sorted(by.values(), key=lambda t: (-t["closed"], t["source"], t["rule"]))


def _rule_matrix(rows: list[dict]) -> dict[str, dict]:
    """Per policy rule: the shadow matrix of what the policy would have done
    against what the human decided. Shared by the shadow table and the
    Monitor's readiness view."""
    rules: dict[str, dict] = {}
    for r in rows:
        rid = r["policy"]["rule"]
        agg = rules.setdefault(rid, {
            "rule": rid, "would_act_approved": 0, "would_act_rejected": 0,
            "would_not_act_approved": 0, "would_not_act_rejected": 0,
            "undecided": 0})
        human = r.get("human")
        if human not in ("approve", "reject"):
            agg["undecided"] += 1
            continue
        key = (f"{r['policy'].get('outcome')}_"
               f"{'approved' if human == 'approve' else 'rejected'}")
        if key in agg:
            agg[key] += 1
    return rules


def list_shadow_decisions(config: dict, params: dict) -> dict:
    """The shadow table (plans/AUTONOMOUS_TIER1.md 4.3): for each rule, what
    the policy would have done next to what the human actually decided.
    `would_act_rejected` is the number that matters -- a rule is not enforced
    until it stays 0 through the shadow period (decision D3). Params:
    since_hours (default 336 = 2 weeks)."""
    _check_params(ListShadowDecisionsParams, params)
    import time as _t
    from .storage import default_storage
    hours = float(params.get("since_hours") or 336)
    rows = default_storage().shadow_decisions(_t.time() - hours * 3600)
    rules = _rule_matrix(rows)
    policy, error = _load_autonomy_policy()
    tuning = _tuning_counts(rows)
    decisions = rows[::-1][:200]
    for r in decisions:
        spec = _undo_spec(r.get("connector") or "", r.get("operation") or "")
        r["undo"] = {"connector": spec["connector"], "op": spec["op"]} if spec else None
    return {"rules": sorted(rules.values(), key=lambda a: a["rule"]),
            "decisions": decisions,
            "tuning": tuning,
            "policy": ({"enabled": policy.get("enabled", True),
                        "mode": policy.get("mode", "shadow"),
                        "rules": [r.get("id") for r in policy.get("rules") or []
                                  if isinstance(r, dict)]}
                       if policy else ({"error": error} if error else None)),
            # The full document, so the Monitor can SHOW each rule's conditions
            # (not just the summary above). Null when no policy / unreadable.
            "policy_document": policy if policy else None}


def get_autonomy_policy(config: dict, params: dict) -> dict:
    """The current autonomy policy document (fresh read, past the cache) plus
    its validation status -- what the Monitor's policy editor loads to edit."""
    from .ops_policy import _load_autonomy_policy, validate_policy_dict
    policy, error = _load_autonomy_policy(force=True)
    problem = validate_policy_dict(policy) if policy else None
    return {"ok": True, "policy": policy, "error": error,
            "valid": problem is None, "problem": problem}


def set_autonomy_policy(config: dict, params: dict) -> dict:
    """Validate and write the autonomy policy to the Key Store (reviewer-gated).

    Never partially applied: the policy is validated against the framework's own
    model BEFORE any write, and a reviewer-role check gates the write. On success
    returns the stored document read back fresh."""
    from .ops_policy import (
        _load_autonomy_policy, validate_policy_dict, write_autonomy_policy)
    _check_params(SetAutonomyPolicyParams, params)
    gate = _policy_edit_gate(config)
    if gate is not None:
        return gate
    policy = params.get("policy")
    if isinstance(policy, str):
        try:
            policy = json.loads(policy)
        except (ValueError, TypeError):
            return _err("bad_policy", "The policy is not valid JSON.") | {"valid": False}
    problem = validate_policy_dict(policy)
    if problem:
        return _err("bad_policy", problem) | {"valid": False}
    werr = write_autonomy_policy(policy)
    if werr:
        return _err("write_failed", werr)
    fresh, ferr = _load_autonomy_policy(force=True)
    return {"ok": True, "policy": fresh, "error": ferr, "valid": True}
