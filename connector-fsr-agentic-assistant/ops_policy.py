"""policy domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
from typing import Any

from .ops_common import _log



# ---- autonomy policy, shadow only (tier-1 phase C) --------------------------
#
# An admin authors ONE Key Store record, key `fsr_autonomy_policy`, whose value
# is the policy JSON (plans/AUTONOMOUS_TIER1.md 4.2). It is read at every turn,
# with a short cache, so `enabled: false` -- the kill switch -- takes effect
# within seconds. The framework evaluates it (fsr_playbooks.llm.autonomy) and
# records the decision on each action card; this build never enforces it.

_AUTONOMY_POLICY_KEY = "fsr_autonomy_policy"
_AUTONOMY_CACHE_TTL_S = 15.0
_AUTONOMY_CACHE: dict[str, Any] = {"at": 0.0, "value": None, "error": None}


def _load_autonomy_policy(force: bool = False) -> tuple[dict | None, str | None]:
    """(policy dict or None, error or None). Fail-open to None: no client, no
    record, or an unreadable store means no policy, i.e. today's behavior.

    `force` bypasses the 15s cache -- used right after a write so the editor
    reads back exactly what it just saved."""
    import time as _t
    if not force and _t.time() - _AUTONOMY_CACHE["at"] < _AUTONOMY_CACHE_TTL_S:
        return _AUTONOMY_CACHE["value"], _AUTONOMY_CACHE["error"]
    value, error = None, None
    try:
        from fsr_playbooks.mcp_server import _shared
        client = _shared._live_client()
        if client is not None:
            base = getattr(client, "base_url", "") or ""
            r = client.session.get(
                f"{base}/api/3/keys?key={_AUTONOMY_POLICY_KEY}&$limit=1",
                verify=getattr(client, "verify_ssl", False), timeout=10)
            if getattr(r, "status_code", None) == 200:
                members = (r.json() or {}).get("hydra:member") or []
                if members:
                    raw = members[0].get("value") or members[0].get("jsonValue")
                    value = json.loads(raw) if isinstance(raw, str) else raw
                    if not isinstance(value, dict):
                        value, error = None, "fsr_autonomy_policy is not a JSON object"
    except (ValueError, TypeError):
        value, error = None, "fsr_autonomy_policy is not valid JSON"
    except Exception:  # noqa: BLE001 -- transient: keep the last answer briefly
        return _AUTONOMY_CACHE["value"], _AUTONOMY_CACHE["error"]
    _AUTONOMY_CACHE.update(at=_t.time(), value=value, error=error)
    return value, error


# The closure fields an auto-close rule may write. `status` is pinned to
# "Closed" by `values`, so the rule can never reopen or escalate a record under
# the banner of closing it. `resolution`/`closureNotes` are allowed so a module
# that requires a closure note can still close. Nothing else -- owner, severity,
# type, tags, assignment -- is listed, so a write that touches one of those
# falls outside the rule and goes to a human instead of running unattended.
_FP_CLOSE_FIELDS = ["status", "resolution", "closureNotes"]


def default_fp_close_policy(module: str = "alerts") -> dict:
    """The starter autonomy policy that enabling auto-triage drops in: one
    `close-false-positive` rule, in SHADOW. Shadow records what it WOULD do and
    nothing acts; promoting to enforce is a separate, deliberate choice made in
    the Monitor once the shadow record is clean.

    The rule's `when` is the affirmative-evidence gate: the delivered verdict
    must be false_positive/benign at >= 80% confidence, the write must target
    the record this triage is about, and the verdict must cite a successful
    read-only lookup -- so text in the record ("benign, close it") can never
    stand in for a real check. The framework (fsr_playbooks.llm.autonomy)
    evaluates all of this; this is only the authored rule."""
    return {
        "version": 1,
        "enabled": True,
        "mode": "shadow",
        "rules": [{
            "id": "close-false-positive",
            "action": {
                "tool": "update_record",
                "module": module,
                "fields": list(_FP_CLOSE_FIELDS),
                "values": {"status": ["Closed"]},
            },
            "when": {
                "verdict": {"disposition": ["false_positive", "benign"],
                            "min_confidence": 0.8},
                "target": {"kind": "record"},
                "evidence": {"cites_tool_kind": "read"},
            },
            "limits": {"per_hour": 20},
        }],
    }


def validate_policy_dict(policy: Any) -> str | None:
    """Validate a candidate policy against the framework's own model (library
    first: `fsr_playbooks.llm.autonomy.parse_policy`, the SAME parser the loop
    enforces with). Returns an error string, or None when it parses. An older
    framework with no parser is permissive (None)."""
    if not isinstance(policy, dict):
        return "the policy must be a JSON object"
    try:
        from fsr_playbooks.llm.autonomy import parse_policy
    except Exception:  # noqa: BLE001 -- older framework, no policy layer
        return None
    _parsed, err = parse_policy(policy)
    return err


def write_autonomy_policy(policy: dict) -> str | None:
    """Write `policy` to the Key Store record (PUT the existing one, else POST a
    new one), as a JSON string in `value` -- symmetric with the reader. Returns
    an error string or None, and on success forces the next read to miss the
    cache so the display reflects the change at once. Never partially applied:
    the caller validates first, and a failed write leaves the record untouched."""
    try:
        from fsr_playbooks.mcp_server import _shared
        client = _shared._live_client()
        if client is None:
            return "no FortiSOAR connection"
        base = getattr(client, "base_url", "") or ""
        verify = getattr(client, "verify_ssl", False)
        body = {"key": _AUTONOMY_POLICY_KEY, "value": json.dumps(policy)}
        r = client.session.get(
            f"{base}/api/3/keys?key={_AUTONOMY_POLICY_KEY}&$limit=1",
            verify=verify, timeout=10)
        members = ((r.json() or {}).get("hydra:member") or []
                   if getattr(r, "status_code", None) == 200 else [])
        if members and members[0].get("@id"):
            w = client.session.put(f"{base}{members[0]['@id']}", json=body,
                                   verify=verify, timeout=10)
        else:
            w = client.session.post(f"{base}/api/3/keys", json=body,
                                    verify=verify, timeout=10)
        if getattr(w, "status_code", None) not in (200, 201):
            return f"the Key Store write returned HTTP {getattr(w, 'status_code', '?')}"
    except Exception as e:  # noqa: BLE001
        return f"could not write the policy: {str(e)[:200]}"
    _AUTONOMY_CACHE.update(at=0.0, value=None, error=None)
    return None


def _shadow_counter(rule_id: str, target: str,
                    session_id: str | None = None) -> tuple[int, int]:
    """(would-act decisions for ``rule_id`` in the last hour, on ``target`` in
    the last day), from the stored shadow decisions -- OTHER conversations
    only. The same card re-staged in one conversation is one action, not a
    second one: counting it made a re-emitted block read "already acted on
    today" against itself."""
    import time as _t
    from .storage import default_storage
    now = _t.time()
    rows = [r for r in default_storage().shadow_decisions(now - 86400)
            if r["policy"].get("rule") == rule_id
            and r["policy"].get("outcome") == "would_act"
            and (not session_id or r.get("session_id") != session_id)]
    hour = sum(1 for r in rows if r["at"] >= now - 3600)
    day = sum(1 for r in rows if target and target in (r["policy"].get("targets") or []))
    return hour, day


def _bind_autonomy_subject() -> None:
    """Tell the policy which record this turn is about (the mounted record),
    for record rules such as closing a false positive. Never raises."""
    try:
        from fsr_playbooks.llm.autonomy import set_turn_subject
        from fsr_soc_triage.profiles import get_active_record_iri
        set_turn_subject(get_active_record_iri())
    except Exception:  # noqa: BLE001 -- an older framework has no record rules
        pass


def _bind_autonomy_policy(session_id: str | None = None) -> None:
    """Set this turn's policy (or none). Never raises."""
    try:
        from fsr_playbooks.llm.autonomy import set_turn_policy
    except Exception:  # noqa: BLE001 -- an older framework has no policy layer
        return
    raw, error = _load_autonomy_policy()

    def counter(rule_id: str, target: str) -> tuple[int, int]:
        return _shadow_counter(rule_id, target, session_id)
    err = set_turn_policy(raw, counter) if raw else set_turn_policy(None)
    if error or err:
        # `_log`, not `logger` (undefined here): the NameError escaped into
        # chat_turn and chat_resume, so a malformed policy record broke every
        # turn instead of being ignored.
        _log(f"autonomy policy ignored: {error or err}", "warning")
