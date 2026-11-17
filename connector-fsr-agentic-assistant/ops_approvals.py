"""approvals domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any
from .pydantic_models import (
    ResolveEscalationParams, UndoActionParams,
    GetAuditTrailParams,
)
import contextvars as _contextvars

from .ops_common import (
    _check_params, _err, _log, _reference_db_path, _require, _require_transport,
)
from .ops_entity import _iri_uuid
from .ops_policy import _load_autonomy_policy
from .ops_turn_state import _PROFILE_CACHE, _PROFILE_NEG_TS
from .ops_usage import list_usage



# Decisions that act on a card. `choice` answers a question and may be asked
# again; these approve, refuse, save or drop something, exactly once.
_ONCE_DECISIONS = ("approve", "reject", "accept", "decline")


#: Who decided the card the current resume answers, for the approved
#: dispatch's audit row (`authorize(approved_by=...)`). Bound by chat_resume
#: from `_approver_label`.
_DECIDER: ContextVar[str | None] = ContextVar("fsr_card_decider", default=None)


def _actor_name(verified_actor: str | None, params: dict) -> str | None:
    actor = verified_actor or params.get("actor")
    if isinstance(actor, dict):
        actor = actor.get("name") or actor.get("iri") or actor.get("id")
    return str(actor) if actor else None


def _approver_label(verified_actor: str | None, params: dict) -> str:
    """`approved_by` for an audit row. Only a server-verified name (the
    reviewer check, or the enforcing policy) is recorded as-is. The widget's
    `actor` is a claim anyone calling the op can set, so without a verified
    name the row says the claim is unverified rather than naming them."""
    if verified_actor:
        return str(verified_actor)
    claimed = _actor_name(None, params)
    return f"human (unverified: {claimed})" if claimed else "human"


def _decider() -> str:
    """`approved_by` for a dispatch the current resume approved."""
    return _DECIDER.get() or "human"


def _claim_decision(storage, params: dict, decision: str,
                    verified_actor: str | None = None) -> dict | None:
    """Claim ``decision`` on the card this resume answers. Returns None when
    there is no card to claim, else {session_id, card_id, prior} where
    ``prior`` is the earlier decision when one already claimed the card.
    ``verified_actor`` (the reviewer check's answer) wins over the widget's
    claimed ``actor``."""
    sid = params.get("session_id")
    cid = params.get("card_id") or params.get("approval_id") or params.get("offer_id")
    if decision not in _ONCE_DECISIONS or not sid or not cid:
        return None
    try:
        prior = storage.claim_card_decision(sid, cid, decision,
                                            _actor_name(verified_actor, params))
    except Exception:  # noqa: BLE001 -- a broken claim table must not block approvals
        return None
    return {"session_id": sid, "card_id": cid, "prior": prior}


def _already_decided(card_id: str, prior: dict) -> dict:
    who = f" by {prior['actor']}" if prior.get("actor") else ""
    verb = {"approve": "approved", "reject": "rejected", "accept": "accepted",
            "decline": "declined"}.get(prior.get("decision"), prior.get("decision"))
    return _err("already_decided",
                f"This card was already {verb}{who}; it runs at most once.",
                card_id=card_id, decided=prior)


# ---- who may decide (D2: the central queue's reviewer check) ----------------
# The connector runs as a service account, so the widget's `actor` is a claim,
# not an identity. The platform gateway names the real caller in the X-User
# header (their people uuid). It APPENDS to a client-sent X-User rather than
# replacing it, so the gateway's value is the LAST one and more than one value
# means someone sent their own: refuse rather than guess.

_CALLER: _contextvars.ContextVar = _contextvars.ContextVar("_CALLER", default=None)


def _caller_from_request(request) -> dict | None:
    """``{"uuid": ...}`` or ``{"tampered": True}`` from the request the
    platform hands ``execute``; None when there is no header to read (a
    playbook step, a test)."""
    try:
        meta = getattr(getattr(request, "_request", request), "META", None) or {}
        raw = meta.get("HTTP_X_USER")
    except Exception:  # noqa: BLE001
        return None
    if not raw or not isinstance(raw, str):
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if len(values) != 1:
        return {"tampered": True} if values else None
    return {"uuid": values[0].rsplit("/", 1)[-1]}


def bind_caller(request):
    """Bind the verified caller for one operation. Returns the reset token.

    Also binds it in the framework, which scopes the connector configurations
    the agent can see, offer and run on to the caller's teams. A tampered
    header binds a uuid that resolves to no teams: fail closed."""
    who = _caller_from_request(request)
    fw_token = None
    try:
        from fsr_playbooks.mcp_server.caller_scope import set_caller
        if who:
            fw_token = set_caller("tampered-x-user" if who.get("tampered")
                                  else who.get("uuid"))
    except ImportError:  # framework without caller scoping
        pass
    return (_CALLER.set(who), fw_token)


def reset_caller(token) -> None:
    own, fw_token = token if isinstance(token, tuple) else (token, None)
    try:
        _CALLER.reset(own)
    except Exception:  # noqa: BLE001
        pass
    if fw_token is not None:
        try:
            from fsr_playbooks.mcp_server.caller_scope import reset_caller as _r
            _r(fw_token)
        except Exception:  # noqa: BLE001
            pass


def _reviewer_roles(config: dict) -> list[str]:
    raw = (config or {}).get("reviewer_roles") or ""
    if isinstance(raw, list):
        names = raw
    else:
        names = str(raw).replace("\n", ",").split(",")
    return [n.strip() for n in names if str(n).strip()]


_ROLE_CACHE_TTL_S = 60.0
_ROLE_CACHE: dict[str, Any] = {"at": 0.0, "by_iri": None}


def _role_names_by_iri(client) -> dict[str, str]:
    import time as _t
    if _ROLE_CACHE["by_iri"] is not None and _t.time() - _ROLE_CACHE["at"] < _ROLE_CACHE_TTL_S:
        return _ROLE_CACHE["by_iri"]
    base = getattr(client, "base_url", "") or ""
    r = client.session.get(f"{base}/api/3/roles?$limit=500",
                           verify=getattr(client, "verify_ssl", False), timeout=10)
    if getattr(r, "status_code", None) != 200:
        raise RuntimeError(f"roles lookup returned {getattr(r, 'status_code', '?')}")
    by_iri = {m.get("@id"): m.get("name") for m in (r.json() or {}).get("hydra:member") or []
              if m.get("@id")}
    _ROLE_CACHE.update(at=_t.time(), by_iri=by_iri)
    return by_iri


def _person(client, person_uuid: str) -> dict:
    base = getattr(client, "base_url", "") or ""
    r = client.session.get(f"{base}/api/3/people/{person_uuid}?$relationships=true",
                           verify=getattr(client, "verify_ssl", False), timeout=10)
    if getattr(r, "status_code", None) != 200:
        raise RuntimeError(f"people lookup returned {getattr(r, 'status_code', '?')}")
    return r.json() or {}


def _decision_gate(config: dict, storage, session_id: str,
                   initiator_may_decide: bool) -> tuple[dict | None, str | None]:
    """(refusal or None, verified actor name or None).

    Off until ``reviewer_roles`` is configured, so a box that never set it keeps
    today's behavior. Once set, a decision passes when the caller the gateway
    named is the conversation's own initiator (when ``initiator_may_decide``)
    or holds one of those roles. Anything we cannot verify is refused."""
    roles = _reviewer_roles(config)
    if not roles:
        return None, None
    caller = _CALLER.get()
    if not caller:
        return _err("not_authorized", "Could not tell who is deciding, so the "
                    "decision was not recorded. Only reviewers can decide here."), None
    if caller.get("tampered"):
        return _err("not_authorized", "The request named more than one user, so "
                    "the decision was not recorded."), None
    uuid_ = caller["uuid"]
    if initiator_may_decide and session_id:
        try:
            init = storage.get_session_initiator(session_id)
        except Exception:  # noqa: BLE001
            init = None
        if isinstance(init, dict) and _iri_uuid(init.get("iri") or "") == uuid_:
            return None, init.get("name")
    try:
        from fsr_playbooks.mcp_server import _shared
        client = _shared._live_client()
        if client is None:
            raise RuntimeError("no FortiSOAR client")
        person = _person(client, uuid_)
        names = _role_names_by_iri(client)
    except Exception as e:  # noqa: BLE001 -- cannot check the roles: refuse
        return _err("not_authorized", "Could not check your roles, so the "
                    "decision was not recorded.", detail=str(e)[:200]), None
    role_iris = [r if isinstance(r, str) else (r or {}).get("@id")
                 for r in person.get("roles") or []]
    held = {names.get(i) for i in role_iris if i}
    who = " ".join(p for p in (person.get("firstname"), person.get("lastname")) if p) or None
    if held & set(roles):
        return None, who
    return _err("not_authorized", "Only reviewers can decide this. Ask someone "
                f"with the {' or '.join(roles)} role.", required_roles=roles), None


def _policy_edit_gate(config: dict) -> dict | None:
    """Refusal dict, or None when the caller may edit the autonomy policy.

    Reuses the D2 reviewer machinery, but editing the policy is STRICTER than
    deciding a card: changing what may act on its own REQUIRES `reviewer_roles`
    to be configured (a decision gate is off until it is; a policy edit refuses
    until it is). Anything we cannot verify is refused -- fail closed."""
    roles = _reviewer_roles(config)
    if not roles:
        return _err("not_configured", "Editing the autonomy policy needs the "
                    "connector's reviewer_roles set first, so only reviewers can "
                    "change it.")
    caller = _CALLER.get()
    if not caller:
        return _err("not_authorized", "Could not tell who is editing, so the "
                    "policy was not changed. Only reviewers can edit it.")
    if caller.get("tampered"):
        return _err("not_authorized", "The request named more than one user, so "
                    "the policy was not changed.")
    try:
        from fsr_playbooks.mcp_server import _shared
        client = _shared._live_client()
        if client is None:
            raise RuntimeError("no FortiSOAR client")
        person = _person(client, caller["uuid"])
        names = _role_names_by_iri(client)
    except Exception as e:  # noqa: BLE001 -- cannot check roles: refuse
        return _err("not_authorized", "Could not check your roles, so the policy "
                    "was not changed.", detail=str(e)[:200])
    role_iris = [r if isinstance(r, str) else (r or {}).get("@id")
                 for r in person.get("roles") or []]
    held = {names.get(i) for i in role_iris if i}
    if held & set(roles):
        return None
    return _err("not_authorized", "Only reviewers can edit the autonomy policy. "
                f"Ask someone with the {' or '.join(roles)} role.",
                required_roles=roles)


def _grant_from_card(storage, session_id: str | None, approval_id: str) -> dict | None:
    """Apply an "Always allow" chosen on run card ``approval_id``: add its
    playbook to the persona's allow + auto lists and drop this worker's cached
    copy of that persona. None when the card offered nothing to grant."""
    card = storage.get_card(session_id, approval_id) if session_id else None
    offer = (card or {}).get("always_allow")
    if not isinstance(offer, dict) or not offer.get("persona") or not offer.get("playbook"):
        return None
    try:
        from fsr_playbooks.mcp_server import _shared
        from fsr_soc_triage.profiles import grant_playbook
        res = grant_playbook(_shared._live_client(), offer["persona"], offer["playbook"])
    except Exception as e:  # noqa: BLE001 -- the approved run still goes ahead
        res = {"ok": False, "code": "grant_failed", "message": str(e)[:300]}
    if res.get("ok"):
        for ck in [k for k in list(_PROFILE_CACHE) if isinstance(k, tuple)
                   and k[-1] == offer["persona"]]:
            _PROFILE_CACHE.pop(ck, None)
            _PROFILE_NEG_TS.pop(ck, None)
    return res


_APPROVAL_CARD_TYPES = ("action_card", "approval_request")
# A card still waiting after a day is a conversation someone abandoned, not a
# decision a lead should be asked for.
_PENDING_MAX_AGE_S = 24 * 3600


def _clip(v: Any, n: int = 400) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"... ({len(v)} chars)"
    if isinstance(v, (dict, list)):
        try:
            text = json.dumps(v, default=str)
        except (TypeError, ValueError):
            text = str(v)
        return v if len(text) <= n else text[:n] + f"... ({len(text)} chars)"
    return v


def _pending_card_view(card: dict) -> dict:
    """What a pending approval asks for, for a reviewer who is not in the chat:
    the action, its target, and (for a record write) each field's change."""
    args = card.get("args") if isinstance(card.get("args"), dict) else (
        (card.get("preview") or {}).get("args") if isinstance(card.get("preview"), dict) else {}) or {}
    tool = card.get("tool") or ("run_op" if card.get("connector") else None)
    view = {
        "type": card.get("type"),
        "tool": tool,
        "summary": card.get("summary") or card.get("title") or card.get("question"),
        "target": card.get("target") if isinstance(card.get("target"), dict) else None,
        "connector": card.get("connector"),
        "operation": card.get("operation"),
        "playbook": args.get("playbook") if tool == "run_playbook" else None,
        "record": args.get("record") if tool == "run_playbook" else None,
        "changes": [{"field": c.get("field"), "before": _clip(c.get("before")),
                     "after": _clip(c.get("after"))}
                    for c in card.get("changes") or [] if isinstance(c, dict)],
        "batch_count": len(card.get("batch") or []),
    }
    if not view["changes"] and args and tool not in ("run_playbook",):
        view["args"] = {k: _clip(v) for k, v in args.items() if not str(k).startswith("_")}
    view["title"] = _pending_title(view, args)
    return view


def _pending_title(view: dict, args: dict) -> str | None:
    """A reviewer's one-line reading of a record write or a run. The card's
    own summary for these is "Update <module>/<uuid>", which names nothing a
    person recognises; other cards keep the summary the model wrote."""
    tool, target = view.get("tool"), view.get("target") or {}
    name = target.get("name")
    fields = [c["field"] for c in view.get("changes") or [] if c.get("field")]
    listed = ", ".join(fields[:3]) + (f" and {len(fields) - 3} more" if len(fields) > 3 else "")
    if tool == "update_record" and name:
        return f"Change {listed} on {name}" if listed else f"Change {name}"
    if tool == "create_record":
        new_name = (args.get("fields") or args).get("name") if isinstance(args, dict) else None
        return f"Create {new_name}" if new_name else None
    if tool == "delete_record" and name:
        return f"Delete {name}"
    if tool == "run_playbook" and view.get("playbook"):
        return f"Run {view['playbook']}"
    return None


def list_pending(config: dict, params: dict) -> dict:
    """Approvals waiting on an analyst, across every session: the Monitor's
    pending queue. Each entry: {approval_id, card_id, session_id, parked_at,
    intent, age_seconds, user, record_uuid, record_module, card}, where `card`
    says what is being asked (`_pending_card_view`).

    Sourced from the stored approval cards, not only the suspended-session
    store: an approved card executes from its stored copy, so it stays
    answerable after the 10-minute suspend TTL, and a central reviewer needs it
    listed. A card is pending while nobody has decided it
    (`session_card_decisions`), the conversation has not run on past it
    (`storage._gate_is_live`, shared with the Live Sessions status), and it is
    under a day old. A suspended gate with no stored card is still listed, with
    `card: null`."""
    from .storage import default_storage, _gate_is_live
    storage = default_storage()
    import time as _time
    now = _time.time()
    suspended = storage.suspended_gates()
    cards = storage.list_cards_created_since(now - _PENDING_MAX_AGE_S)
    last_activity = storage.last_activity_by_session()
    found: dict[tuple[str, str], dict] = {}
    for session_id, card_id, card_json, created_at in cards:
        try:
            card = json.loads(card_json)
        except (TypeError, ValueError):
            continue
        if card.get("type") not in _APPROVAL_CARD_TYPES:
            continue
        found[(session_id, card_id)] = {"card": card, "at": created_at,
                                        "approval_id": card.get("approval_id") or card_id}
    for approval_id, session_id, created_at in suspended:
        hit = next((k for k, v in found.items()
                    if k[0] == session_id and v["approval_id"] == approval_id), None)
        if hit is None:
            found[(session_id, approval_id)] = {"card": None, "at": created_at,
                                                "approval_id": approval_id}

    out: list[dict] = []
    for (session_id, card_id), row in found.items():
        if not _gate_is_live(row["at"], last_activity.get(session_id)):
            continue
        if storage.card_decision(session_id, card_id) is not None:
            continue
        intent = storage.get_session_intent(session_id)
        user = storage.get_session_initiator(session_id)
        if user is None:
            user = storage.transcript_author(session_id)
        rec_uuid = rec_mod = None
        try:
            sess = storage.get_chat_session(session_id)
            if sess:
                rec_uuid = sess.get("record_uuid")
                rec_mod = sess.get("record_module")
        except Exception:
            pass
        out.append({
            "approval_id": row["approval_id"],
            "card_id": card_id,
            "session_id": session_id,
            "parked_at": row["at"],
            "intent": intent,
            "age_seconds": int(now - row["at"]),
            "user": user,
            "record_uuid": rec_uuid,
            "record_module": rec_mod,
            "card": _pending_card_view(row["card"]) if row["card"] else None,
        })
    out.sort(key=lambda p: p["parked_at"], reverse=True)
    return {"pending": out, "escalations": _open_escalations(storage, out, now)}


# An escalation stays in the queue until someone resolves it, up to a week.
_ESCALATION_MAX_AGE_S = 7 * 24 * 3600


def _open_escalations(storage, pending: list[dict], now: float) -> list[dict]:
    """Unresolved auto-triage escalations, most urgent first and, within a
    priority, oldest first. `approvals` counts the session's actions still
    waiting in `pending`, so a reviewer sees what is left to decide."""
    rows = storage.list_cards_created_since(now - _ESCALATION_MAX_AGE_S)
    out = []
    for session_id, card_id, card_json, created_at in rows:
        try:
            card = json.loads(card_json)
        except (TypeError, ValueError):
            continue
        if card.get("type") != "escalation_card":
            continue
        if storage.card_decision(session_id, card_id) is not None:
            continue
        out.append({
            "session_id": session_id, "card_id": card_id,
            "created_at": created_at, "age_seconds": int(now - created_at),
            "priority": card.get("priority"),
            "priority_rank": card.get("priority_rank") or 4,
            "record": card.get("record"), "verdict": card.get("verdict"),
            "reasons": card.get("reasons") or [],
            "approvals": sum(1 for p in pending if p.get("session_id") == session_id),
        })
    out.sort(key=lambda e: (e["priority_rank"], -e["age_seconds"]))
    return out


_ESCALATION_OUTCOMES = ("handled", "dismissed")


def resolve_escalation(config: dict, params: dict) -> dict:
    """Take an escalation off the queue: `handled` (a reviewer dealt with it)
    or `dismissed` (nothing to do). Decided once, like an approval card; a
    second answer gets `already_decided` naming who resolved it. Actions
    still waiting in the session stay in the queue on their own."""
    _check_params(ResolveEscalationParams, params)
    session_id = _require(params, "session_id")
    card_id = _require(params, "card_id")
    outcome = params.get("outcome") or "handled"
    if outcome not in _ESCALATION_OUTCOMES:
        return _err("bad_outcome", f"outcome must be one of "
                    f"{', '.join(_ESCALATION_OUTCOMES)}, got {outcome!r}")
    from .storage import default_storage
    storage = default_storage()
    card = storage.get_card(session_id, card_id)
    if not card or card.get("type") != "escalation_card":
        return _err("not_found", f"no escalation {card_id!r} in session {session_id!r}")
    # An escalation came from auto-triage, not from someone's own chat, so
    # only a reviewer may close it.
    refusal, verified = _decision_gate(config, storage, session_id,
                                       initiator_may_decide=False)
    if refusal is not None:
        return refusal
    actor = verified or params.get("actor")
    if isinstance(actor, dict):
        actor = actor.get("name") or actor.get("iri") or actor.get("id")
    prior = storage.claim_card_decision(session_id, card_id, outcome,
                                        str(actor) if actor else None)
    if prior is not None:
        who = f" by {prior['actor']}" if prior.get("actor") else ""
        return _err("already_decided",
                    f"This escalation was already {prior.get('decision')}{who}.",
                    card_id=card_id, decided=prior)
    return {"ok": True, "session_id": session_id, "card_id": card_id,
            "outcome": outcome}


# ---- one-click undo (tier-1 phase D) ----------------------------------------
#
# Reverses an approved run_op card that ran. The reverse op comes from the
# autonomy policy rule covering the action (its `undo`), else from the pairs
# below; the args are what actually ran (session_card_executions), cut to the
# params the reverse op declares. Decided once, like an approval.

# (connector, op) -> reverse op, for actions no policy rule names an undo for.
_BUILTIN_UNDO = {
    ("fortigate-firewall", "block_ip"): "unblock_ip",
    ("fortigate-firewall", "block_ip_new"): "unblock_ip",
    ("fortigate-firewall", "block_url"): "unblock_url",
    ("fortigate-firewall", "block_applications"): "unblock_applications",
    ("fortigate-firewall", "quarantine_host"): "unquarantine_host",
}


# A record write the policy approved (phase F: an auto-closed false
# positive). Its execution row keeps the fields' values from before the write,
# and undo writes them back.
RECORD_WRITE_CONNECTOR = "fortisoar"
RECORD_WRITE_OP = "update_record"


def _writable(value: Any) -> Any:
    """A field value as read from /api/3, in the shape a PUT accepts: a
    picklist or related record by its IRI, everything else as is."""
    if isinstance(value, dict):
        return value.get("@id", value)
    if isinstance(value, list):
        return [_writable(v) for v in value]
    return value


def comment_on_record(module: str, uuid: str, text: str) -> bool:
    """Add a plain-text comment to a record. Best-effort: False when it could
    not be written, never raises."""
    import html
    mr, err = _require_transport()
    if err:
        return False
    try:
        mr("/api/3/comments", "POST", body={
            "content": f"<p>{html.escape(text)}</p>",
            module: [f"/api/3/{module}/{uuid}"]})
    except Exception as e:  # noqa: BLE001
        _log(f"autonomy: could not comment on {module}/{uuid}: {e}", "warning")
        return False
    return True


def _undo_record_write(args: dict) -> dict:
    """Write back the values a policy-approved record update replaced, then
    read the record to confirm them."""
    module, uuid, prior = args.get("module"), args.get("uuid"), args.get("prior")
    if not (module and uuid and isinstance(prior, dict) and prior):
        return {"ok": False, "message": "the values from before the change were "
                "not recorded, so they cannot be restored"}
    mr, err = _require_transport("undo needs the FortiSOAR runtime")
    if err:
        return {"ok": False, "message": err["error"]["message"]}
    try:
        mr(f"/api/3/{module}/{uuid}", "PUT", body=dict(prior))
        rec = mr(f"/api/3/{module}/{uuid}", "GET") or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
    wrong = [k for k, v in prior.items() if _writable(rec.get(k)) != v]
    if wrong:
        return {"ok": False, "message": "the record did not take back "
                + ", ".join(sorted(wrong))}
    return {"ok": True, "restored": dict(prior)}


def _undo_spec(connector: str, operation: str) -> dict | None:
    """{connector, op, source} that reverses connector.operation, or None."""
    if operation == RECORD_WRITE_OP:
        return {"connector": RECORD_WRITE_CONNECTOR, "op": "restore_fields",
                "source": "builtin"}
    policy, _err_ = _load_autonomy_policy()
    for rule in (policy or {}).get("rules") or []:
        if not isinstance(rule, dict):
            continue
        action = rule.get("action") or {}
        undo = rule.get("undo") or {}
        if action.get("connector") == connector and action.get("op") == operation \
                and undo.get("op"):
            return {"connector": undo.get("connector") or connector,
                    "op": undo["op"], "source": f"policy:{rule.get('id')}"}
    op = _BUILTIN_UNDO.get((connector, operation))
    return {"connector": connector, "op": op, "source": "builtin"} if op else None


def _op_param_names(connector: str, operation: str) -> set[str] | None:
    """Every param the op declares (top level and onchange children), or None
    when the reference DB does not know the op."""
    import sqlite3
    try:
        with sqlite3.connect(f"file:{_reference_db_path()}?mode=ro", uri=True) as con:
            names = {r[0] for r in con.execute(
                "SELECT DISTINCT param_name FROM operation_params "
                "WHERE connector_name=? AND op_name=?", (connector, operation))}
    except Exception:  # noqa: BLE001
        return None
    return names or None


def undo_action(config: dict, params: dict) -> dict:
    """Reverse an approved action card that ran: unblock what it blocked.
    Params: session_id, card_id. The chat's initiator or a reviewer may undo;
    a second undo gets `already_undone` naming who did it."""
    _check_params(UndoActionParams, params)
    session_id = _require(params, "session_id")
    card_id = _require(params, "card_id")
    from .storage import default_storage
    storage = default_storage()
    ex = storage.get_card_execution(session_id, card_id)
    if not ex:
        return _err("not_executed", f"card {card_id!r} in session {session_id!r} "
                    "has no recorded execution, so there is nothing to undo")
    if not ex["ok"]:
        return _err("not_executed", "the approved action failed when it ran, "
                    "so there is nothing to undo")
    spec = _undo_spec(ex["connector"], ex["operation"])
    if spec is None:
        return _err("no_undo", f"{ex['connector']}.{ex['operation']} has no "
                    "reverse operation (none in the autonomy policy or built in)")
    refusal, verified = _decision_gate(config, storage, session_id,
                                       initiator_may_decide=True)
    if refusal is not None:
        return refusal
    actor = verified or params.get("actor")
    if isinstance(actor, dict):
        actor = actor.get("name") or actor.get("iri") or actor.get("id")
    prior = storage.claim_card_undo(session_id, card_id,
                                    str(actor) if actor else None)
    if prior is not None:
        who = f" by {prior['undone_by']}" if prior.get("undone_by") else ""
        return _err("already_undone", f"This action was already undone{who}.",
                    card_id=card_id, undone=prior)

    if ex["operation"] == RECORD_WRITE_OP:
        result = _undo_record_write(ex["args"])
        ok = bool(result.get("ok"))
        storage.finish_card_undo(session_id, card_id,
                                 {"ok": ok, "op": spec["op"], "args": ex["args"],
                                  "result": result}, release=not ok)
        if not ok:
            return _err("undo_failed", f"The record could not be restored: "
                        f"{result.get('message')}", card_id=card_id, result=result)
        who = str(actor) if actor else "an analyst"
        comment_on_record(ex["args"]["module"], ex["args"]["uuid"],
                          f"Reopened by {who} with Undo in the Agentic Assistant. "
                          "It had been closed automatically as a false positive.")
        return {"ok": True, "session_id": session_id, "card_id": card_id,
                "undo": {"connector": spec["connector"], "op": spec["op"],
                         "source": spec["source"], "args": ex["args"]},
                "result": result}

    accepted = _op_param_names(spec["connector"], spec["op"])
    args = {k: v for k, v in ex["args"].items()
            if accepted is None or k in accepted}
    tool_args: dict[str, Any] = {"connector": spec["connector"], "op": spec["op"],
                                 "params": args, "confirm": True}
    if ex.get("config") and spec["connector"] == ex["connector"]:
        tool_args["config"] = ex["config"]
    from fsr_playbooks.llm.tools import dispatch
    try:
        result = dispatch("run_op", {**tool_args, "_approved": True},
                          _internal=True, session_id=session_id,
                          approved_by=_approver_label(verified, params))
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "code": "undo_failed",
                  "message": f"{type(exc).__name__}: {exc}"}
    ok = bool(isinstance(result, dict) and result.get("ok"))
    # A reversal that did not run releases the claim so it can be retried.
    storage.finish_card_undo(session_id, card_id,
                             {"ok": ok, "op": spec["op"], "args": args,
                              "result": result}, release=not ok)
    if not ok:
        msg = (result or {}).get("message") if isinstance(result, dict) else None
        return _err("undo_failed", f"{spec['connector']}.{spec['op']} failed: "
                    f"{msg or result}", card_id=card_id, result=result)
    return {"ok": True, "session_id": session_id, "card_id": card_id,
            "undo": {"connector": spec["connector"], "op": spec["op"],
                     "source": spec["source"], "args": args},
            "result": result}


def get_audit_trail(config: dict, params: dict) -> dict:
    """Per-LLM-call audit records (prompt, response, tokens, cost, user,
    status) from sqlite agent_usage. The platform module
    /api/3/llm_activity_logs is the canonical audit home the widget reads
    directly via FormEntityService; this op supplies the richer drill-down
    fields (tool_calls, cache stats, tags)."""
    _check_params(GetAuditTrailParams, params)
    return list_usage(config, params)
