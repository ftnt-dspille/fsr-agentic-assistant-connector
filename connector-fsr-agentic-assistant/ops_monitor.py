"""Monitor revamp data layer (V2 M1-M5): what the AI handled, was it right, and
what to let it do next.

Two operations, both read by the Monitor widget:

* ``monitor_cases`` -- one op, five views. ``list`` is the handled-cases queue
  (M1), ``kpis`` the outcome tiles (M2), ``policy`` the per-rule readiness
  badge (M4), ``tuning`` the per-detection-rule reopen rate (M5),
  ``would_help`` the sources that would have settled the most uncertain
  cases (X7).
* ``monitor_feedback`` -- an analyst's right/wrong on one decision (M3),
  read back with the M1 row.

Everything derives from tables that already exist (the auto-triage sessions,
their stored cards, decisions and executions, the shadow matrix, the usage
store) plus one new table, ``decision_feedback``. Nothing here runs a model or
changes a record.

Strings the analyst reads are plain: labels, never tool names or ids. The
rule and session ids that come back are data for the widget to link with.
"""
from __future__ import annotations

import time
from typing import Any

from .ops_approvals import _CALLER, RECORD_WRITE_OP
from .ops_autonomy import (
    _DISPOSITION_LABELS, _picklist_text, _rule_matrix, _tuning_counts,
)
from .ops_common import _check_params, _err, _require, _require_transport
from .pydantic_models import MonitorCasesParams, MonitorFeedbackParams

BUCKETS = ("handled", "needs_you", "reopened")
VIEWS = ("list", "kpis", "policy", "tuning", "would_help")

_DEFAULT_HOURS = 24
_POLICY_HOURS = 336          # two weeks, like the shadow table
_HELP_HOURS = 336            # the would-help ranking reads as long as the policy views
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_MAX_STATUS_READS = 100      # live record reads per request, newest first

# --- readiness thresholds (M4) ----------------------------------------------
# Whole percents so the boundary maths is integer, never a float compare.
READY_MIN_SAMPLE = 20
READY_MIN_AGREEMENT_PCT = 95
READY_MAX_REOPEN_PCT = 5
# --- tuning thresholds (M5) -------------------------------------------------
TUNE_MIN_CLOSES = 5
TUNE_MAX_REOPEN_PCT = 10


# ---------------------------------------------------------------------------
# Pure maths (unit-tested at the boundaries)
# ---------------------------------------------------------------------------

def rule_readiness(approved: int, rejected: int, right: int, wrong: int,
                   acted: int, reopened: int, *,
                   min_sample: int = READY_MIN_SAMPLE,
                   min_agreement_pct: int = READY_MIN_AGREEMENT_PCT,
                   max_reopen_pct: int = READY_MAX_REOPEN_PCT) -> dict:
    """Is a rule ready to enforce?

    ``approved`` / ``rejected``: analyst decisions on cards the rule would have
    acted on (shadow). ``right`` / ``wrong``: analyst feedback on the rule's
    decisions. ``acted`` / ``reopened``: what the rule did on its own and how
    many of those came back open.

    sample = approved + rejected + right + wrong; agreement = (approved +
    right) / sample; reopen_rate = reopened / acted. Ready when the sample is
    at least ``min_sample``, agreement is at least ``min_agreement_pct`` percent
    and the reopen rate is at most ``max_reopen_pct`` percent. Every boundary is
    inclusive, and the compare is integer cross-multiplication. A rule that has
    not acted has no reopen rate (None) and passes that check."""
    sample = approved + rejected + right + wrong
    agree = approved + right
    agreement = round(agree / sample, 4) if sample else None
    reopen_rate = round(reopened / acted, 4) if acted else None
    blockers: list[str] = []
    if sample < min_sample:
        need = min_sample - sample
        blockers.append(f"Needs {need} more reviewed decision"
                        f"{'' if need == 1 else 's'} ({sample} of {min_sample}).")
    if sample and agree * 100 < min_agreement_pct * sample:
        blockers.append(f"Analysts agree with it {round(100 * agree / sample)}% "
                        f"of the time; it needs {min_agreement_pct}%.")
    if acted and reopened * 100 > max_reopen_pct * acted:
        blockers.append(f"{round(100 * reopened / acted)}% of what it closed was "
                        f"reopened; the limit is {max_reopen_pct}%.")
    return {"ready": not blockers, "sample": sample, "agreement": agreement,
            "reopen_rate": reopen_rate, "blockers": blockers,
            "inputs": {"approved": approved, "rejected": rejected,
                       "right": right, "wrong": wrong,
                       "acted": acted, "reopened": reopened},
            "thresholds": {"min_sample": min_sample,
                           "min_agreement": min_agreement_pct / 100,
                           "max_reopen_rate": max_reopen_pct / 100}}


def tuning_flag(closed: int, reopened: int, *, min_closes: int = TUNE_MIN_CLOSES,
                max_reopen_pct: int = TUNE_MAX_REOPEN_PCT) -> dict:
    """Is it safe to tune a detection rule at its source? Yes when the AI
    closed enough of its records (``min_closes``) and few came back open (at
    most ``max_reopen_pct`` percent): the closes were right, so the rule is
    just noisy. Boundaries inclusive."""
    reopen_rate = round(reopened / closed, 4) if closed else None
    safe = closed >= min_closes and reopened * 100 <= max_reopen_pct * closed
    if closed < min_closes:
        note = f"Too few closes to tell ({closed} of {min_closes})."
    elif not safe:
        note = "Reopened too often; the closes may have been wrong."
    else:
        note = "Its alerts are closed as false positives and stay closed."
    return {"count": closed, "reopened": reopened, "reopen_rate": reopen_rate,
            "safe_to_tune": safe, "note": note}


# ---------------------------------------------------------------------------
# Row building (M1)
# ---------------------------------------------------------------------------

# Tool families that are a place the AI looked, as the analyst names them.
_SOURCE_PREFIXES = (("siem_", "FortiSIEM"), ("faz_", "FortiAnalyzer"),
                    ("fmg_", "FortiManager"), ("edr_", "FortiEDR"))
_SOURCE_TOOLS = {"prior_cases": "Earlier cases", "search_module_records": "Records",
                 "get_record": "The record"}
# A threat-intel connector name, as shown.
_INTEL_HINTS = ("virustotal", "otx", "abuseipdb", "intel", "urlscan", "shodan",
                "greynoise", "fortiguard", "recorded")


def _source_label(name: str, args: Any) -> str | None:
    for prefix, label in _SOURCE_PREFIXES:
        if name.startswith(prefix):
            return label
    if name in _SOURCE_TOOLS:
        return _SOURCE_TOOLS[name]
    if name == "run_op" and isinstance(args, dict):
        conn = str(args.get("connector") or "").lower()
        if not conn:
            return None
        if any(h in conn for h in _INTEL_HINTS):
            return "Threat intelligence"
        return conn.replace("-", " ").replace("_", " ").title()
    return None


def frames_of(frames: list[dict], *types: str) -> list[dict]:
    """The framework's frame reader, imported on use so this module loads
    without the framework (the missing-dependency path must still report)."""
    from fsr_playbooks.harness.frames import frames_of as _frames_of
    return _frames_of(frames, *types)


def _sources(frames: list[dict]) -> list[str]:
    """Where the investigation looked, in the order it first looked, each once."""
    out: list[str] = []
    for f in frames_of(frames, "tool_use"):
        label = _source_label(str(f.get("name") or ""), f.get("input"))
        if label and label not in out:
            out.append(label)
    return out


def _is_closed_status(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith(("closed", "resolved"))


def _record_iri(session_id: str) -> str | None:
    parts = session_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "auto":
        return None
    return f"/api/3/{parts[1]}/{parts[2]}"


def _action_label(card: dict) -> str:
    if card.get("tool") == RECORD_WRITE_OP or card.get("operation") == RECORD_WRITE_OP:
        return "Closed the record"
    return str(card.get("title") or card.get("summary") or "Took an action")


def _would_label(card: dict) -> str:
    base = _action_label(card)
    return "Would have closed the record" if base == "Closed the record" \
        else f"Would have: {base}"


def _verdict_card(frames: list[dict], esc: dict | None) -> dict | None:
    """The case's final verdict card as stored: the last one the assistant
    emitted, else the one the escalation carries."""
    cards = frames_of(frames, "verdict_card")
    v = cards[-1] if cards else ((esc or {}).get("verdict") or None)
    return v if isinstance(v, dict) else None


def _verdict(frames: list[dict], esc: dict | None) -> dict | None:
    v = _verdict_card(frames, esc)
    if v is None:
        return None
    disp = v.get("disposition")
    conf = v.get("confidence")
    label = _DISPOSITION_LABELS.get(disp, str(disp or "")).capitalize() or None
    return {"label": label,
            "confidence": float(conf) if isinstance(conf, (int, float)) else None}


def _feedback_view(entries: list[dict], analyst: str | None) -> dict:
    right = sum(1 for e in entries if e["rating"] == "right")
    wrong = sum(1 for e in entries if e["rating"] == "wrong")
    mine = next((e for e in entries if analyst and e["analyst"] == analyst), None)
    return {"mine": mine["rating"] if mine else None,
            "note": mine["note"] if mine else None,
            "right": right, "wrong": wrong}


def _row(case: dict, analyst: str | None) -> dict:
    sid = case["session_id"]
    cards = case["cards"]
    esc = next((c for c in cards if c.get("type") == "escalation_card"), None)
    # What the AI itself did: cards the policy (or the close path) ran.
    acted = [c for c in cards if isinstance(c.get("enforced"), dict)
             and c["enforced"].get("ok")]
    verified = [c for c in acted if c["enforced"].get("verified")]
    would = [c for c in cards if (c.get("policy") or {}).get("outcome") == "would_act"
             and not isinstance(c.get("enforced"), dict)]
    rule = next(((c.get("policy") or {}).get("rule") for c in cards
                 if (c.get("policy") or {}).get("rule")), None)

    action, kind, action_card = None, "none", None
    if verified:
        action_card = verified[-1]
        action, kind = _action_label(action_card), "taken"
    elif esc is not None:
        action, kind = "Sent to you", "escalated"
    elif would:
        action_card = would[-1]
        action, kind = _would_label(action_card), "would_have"
    else:
        action = "No action taken"

    closed = bool(action_card) and kind == "taken" \
        and _action_label(action_card) == "Closed the record"
    undone = bool(action_card) and bool(
        case["executions"].get(action_card.get("id") or action_card.get("approval_id"),
                               {}).get("undone_at"))
    enforced = (action_card or {}).get("enforced") or {}
    detection = enforced.get("detection") if closed else None
    record_card = (esc or {}).get("record") or {}
    verdict = _verdict(case["frames"], esc)
    vcard = _verdict_card(case["frames"], esc) or {}
    iri = _record_iri(sid) or record_card.get("iri")

    if closed and undone:
        bucket = "reopened"
    elif esc is not None:
        bucket = "needs_you"
    elif kind == "taken":
        bucket = "handled"
    else:
        bucket = "needs_you"

    row = {
        "record_iri": iri,
        "record_module": iri.split("/")[-2] if iri else None,
        "record_name": record_card.get("name"),
        "session_id": sid,
        "bucket": bucket,
        "verdict": (verdict or {}).get("label"),
        "confidence": (verdict or {}).get("confidence"),
        "sources": _sources(case["frames"]),
        "action": action,
        "action_kind": kind,
        "action_card_id": (action_card or {}).get("id") or (action_card or {}).get("approval_id"),
        "undoable": bool(enforced.get("undoable")) and not undone,
        "reasons": list((esc or {}).get("reasons") or []),
        "at": case["started_at"],
        "group_size": case["group_size"],
        "rule_id": rule,
        "feedback": _feedback_view(case["feedback"], analyst),
    }
    # Kept for the aggregates, stripped before the row leaves.
    row["_closed"] = closed
    row["_detection"] = detection if isinstance(detection, dict) else None
    row["_undone"] = undone
    row["_uncertain"] = vcard.get("disposition") == "needs_more_info"
    row["_disposition"] = vcard.get("disposition")
    row["_rank"] = (esc or {}).get("priority_rank")
    row["_priority"] = (esc or {}).get("priority")
    helps = vcard.get("what_would_help")
    row["_help"] = [h for h in helps if isinstance(h, dict)] if isinstance(helps, list) else []
    return row


def _live_reopen_and_names(rows: list[dict], mr: Any, check_status: bool) -> None:
    """Read the record once for each row that needs it. A close the AI made on
    a record that is open again is reopened; a row with no stored name gets
    the record's own. A failed read changes nothing."""
    reads = 0
    for row in rows:   # newest first
        needs_status = check_status and row["_closed"] and not row["_undone"]
        if not (needs_status or (row["record_name"] is None)):
            continue
        if mr is None or reads >= _MAX_STATUS_READS or not row["record_iri"]:
            continue
        reads += 1
        try:
            rec = mr(row["record_iri"], "GET") or {}
        except Exception:  # noqa: BLE001
            continue
        if isinstance(rec, dict):
            if row["record_name"] is None and rec.get("name"):
                row["record_name"] = rec["name"]
            status = _picklist_text(rec.get("status"))
            if needs_status and status and not _is_closed_status(status):
                row["bucket"] = "reopened"
                row["_live_reopen"] = True
                row["undoable"] = False


def _drop_deleted(rows: list[dict], mr: Any) -> list[dict]:
    """Rows whose record was deleted from SOAR are dropped: one existence
    query per module (uuid in [...]). A query that fails or answers in an
    unexpected shape keeps every row -- never hide a case on a bad read."""
    by_module: dict[str, list[str]] = {}
    for row in rows:
        parts = (row.get("record_iri") or "").rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] and parts[-1]:
            by_module.setdefault(parts[-2], []).append(parts[-1])
    gone: set[str] = set()
    for module, uuids in by_module.items():
        uuids = sorted(set(uuids))[:_MAX_LIMIT]
        body = {"logic": "AND", "filters": [
            {"field": "uuid", "operator": "in", "value": uuids, "type": "primitive"}]}
        try:
            resp = mr(f"/api/query/{module}?$limit={len(uuids) + 1}&$fields=uuid",
                      "POST", body=body)
        except Exception:  # noqa: BLE001
            continue
        members = resp.get("hydra:member") if isinstance(resp, dict) else None
        if not isinstance(members, list):
            continue
        present = set()
        for m in members:
            if isinstance(m, dict):
                u = m.get("uuid") or str(m.get("@id") or "").rsplit("/", 1)[-1]
                if u:
                    present.add(u)
        gone.update(f"/api/3/{module}/{u}" for u in uuids if u not in present)
    return [r for r in rows if r.get("record_iri") not in gone]


def _caller_analyst(params: dict) -> tuple[str | None, dict | None]:
    """(analyst key, error). The gateway-verified caller when there is one,
    else the widget's named actor."""
    caller = _CALLER.get()
    if isinstance(caller, dict):
        if caller.get("tampered"):
            return None, _err("not_authorized", "The request named more than one "
                              "user, so who you are could not be told.")
        if caller.get("uuid"):
            return str(caller["uuid"]), None
    actor = params.get("actor")
    if isinstance(actor, dict):
        actor = actor.get("iri") or actor.get("name") or actor.get("id")
    return (str(actor) if actor else None), None


def _hours(params: dict, default: float) -> float:
    try:
        h = float(params.get("hours") or default)
    except (TypeError, ValueError):
        h = default
    return h if h > 0 else default


def _build_rows(params: dict, hours: float, with_transcript: bool,
                check_live: bool) -> tuple[list[dict], list[dict]]:
    """(rows newest first, raw cases)."""
    from .storage import default_storage
    storage = default_storage()
    analyst, _ = _caller_analyst(params)
    since = time.time() - hours * 3600
    cases = storage.auto_triage_cases(since, with_transcript=with_transcript)
    rows = [_row(c, analyst) for c in cases]
    mr = None
    if rows:
        mr, _err_ = _require_transport("monitor_cases reads the record.")
    if mr is not None:
        rows = _drop_deleted(rows, mr)
    _live_reopen_and_names(rows, mr, check_live)
    return rows, cases


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _counts(rows: list[dict]) -> dict:
    return {b: sum(1 for r in rows if r["bucket"] == b) for b in BUCKETS}


def _view_list(params: dict) -> dict:
    hours = _hours(params, _DEFAULT_HOURS)
    bucket = params.get("bucket") or "all"
    if bucket != "all" and bucket not in BUCKETS:
        return _err("bad_bucket", f"bucket must be all or one of "
                    f"{', '.join(BUCKETS)}, got {bucket!r}")
    try:
        limit = max(1, min(int(params.get("limit") or _DEFAULT_LIMIT), _MAX_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    rows, _ = _build_rows(params, hours, True, params.get("check_reopened") is not False)
    counts = _counts(rows)
    picked = [r for r in rows if bucket == "all" or r["bucket"] == bucket]
    return {"ok": True, "view": "list", "hours": hours, "bucket": bucket,
            "total": len(picked), "counts": counts,
            "rows": [_public(r) for r in picked[:limit]]}


def _cost_today() -> float | None:
    from .storage import default_storage
    storage = default_storage()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    got = storage.usage_summary(bucket="day", metric="cost", from_ts=today)
    vals = [g["value"] for g in got if g.get("bucket") == today]
    return round(float(vals[0] or 0), 4) if vals else None


def _view_kpis(params: dict) -> dict:
    from .storage import default_storage
    hours = _hours(params, _DEFAULT_HOURS)
    rows, _ = _build_rows(params, hours, False, params.get("check_reopened") is not False)
    counts = _counts(rows)
    right = sum(r["feedback"]["right"] for r in rows)
    wrong = sum(r["feedback"]["wrong"] for r in rows)
    rated = right + wrong
    kpis = {
        "handled": counts["handled"],
        "auto_closed": sum(1 for r in rows if r["_closed"]),
        "escalated": sum(1 for r in rows if r["action_kind"] == "escalated"),
        "needs_you": counts["needs_you"],
        "reopened": counts["reopened"],
        "rated": rated, "right": right, "wrong": wrong,
        "agreement": round(right / rated, 4) if rated else None,
        "deferred": default_storage().triage_deferred_count(time.time() - hours * 3600),
        "cost_today_usd": _cost_today(),
    }
    return {"ok": True, "view": "kpis", "hours": hours, "kpis": kpis}


def _view_policy(params: dict) -> dict:
    from .ops_policy import _load_autonomy_policy
    from .storage import default_storage
    storage = default_storage()
    hours = _hours(params, _POLICY_HOURS)
    since = time.time() - hours * 3600
    matrix = _rule_matrix(storage.shadow_decisions(since))
    fb = storage.rule_feedback_counts(since)
    rows, _ = _build_rows(params, hours, False, params.get("check_reopened") is not False)
    policy, _err_text = _load_autonomy_policy()
    rule_ids = set(matrix) | set(fb) | {r["rule_id"] for r in rows if r["rule_id"]}
    rules = []
    for rid in sorted(rule_ids):
        m = matrix.get(rid, {})
        mine = [r for r in rows if r["rule_id"] == rid and r["action_kind"] == "taken"]
        out = rule_readiness(
            m.get("would_act_approved", 0), m.get("would_act_rejected", 0),
            fb.get(rid, {}).get("right", 0), fb.get(rid, {}).get("wrong", 0),
            len(mine), sum(1 for r in mine if r["bucket"] == "reopened"))
        rules.append({"rule_id": rid, "label": rid.replace("-", " ").capitalize(),
                      **out})
    thresholds = rule_readiness(0, 0, 0, 0, 0, 0)["thresholds"]
    return {"ok": True, "view": "policy", "hours": hours,
            "mode": (policy or {}).get("mode", "shadow") if policy else None,
            "thresholds": thresholds, "rules": rules}


def _view_tuning(params: dict) -> dict:
    from .storage import default_storage
    hours = _hours(params, _POLICY_HOURS)
    since = time.time() - hours * 3600
    counts = _tuning_counts(default_storage().shadow_decisions(since))
    rows, _ = _build_rows(params, hours, False, params.get("check_reopened") is not False)
    # A close the AI made that is open again on the record, which the stored
    # undo count cannot see.
    live: dict[tuple[str, str], int] = {}
    for r in rows:
        if r.get("_live_reopen") and r["_detection"]:
            d = r["_detection"]
            key = (d.get("source") or "", d.get("rule") or "")
            live[key] = live.get(key, 0) + 1
    out = []
    for t in counts:
        reopened = t["reopened"] + live.get((t["source"], t["rule"]), 0)
        out.append({"source": t["source"], "rule": t["rule"],
                    "last_at": t["last_at"],
                    **tuning_flag(t["closed"], reopened)})
    return {"ok": True, "view": "tuning", "hours": hours,
            "thresholds": {"min_closes": TUNE_MIN_CLOSES,
                           "max_reopen_rate": TUNE_MAX_REOPEN_PCT / 100},
            "rules": out}


# The source types a verdict card can name as not connected, as the analyst
# reads them (the catalog's own words for the kind).
_HELP_KIND_LABELS = {"tickets": "Ticket system", "intel": "Threat intelligence source",
                     "logs": "Log source (SIEM or firewall logs)"}
_HELP_ORDER = {"not_connected": 0, "failed": 1, "no_data": 2}


def _help_items(entry: dict) -> list[dict]:
    """The ranked-panel items one what-would-help entry stands for: a missing
    source type is one item, a failed or empty source is one item per source."""
    state = entry.get("state")
    if state == "not_connected":
        kind = str(entry.get("kind") or "")
        label = _HELP_KIND_LABELS.get(kind) or kind.replace("_", " ").capitalize()
        return [{"kind": kind, "state": state, "label": label,
                 "problem": f"{label} is not connected"}] if label else []
    if state in ("failed", "no_data"):
        text = ("returned an error" if state == "failed"
                else "was searched and returned nothing")
        return [{"kind": str(entry.get("kind") or ""), "state": state, "label": str(s),
                 "problem": f"{s} {text}"}
                for s in (entry.get("sources") or []) if isinstance(s, str) and s.strip()]
    return []


def _would_help_summary(rows: list[dict]) -> dict:
    """{uncertain, with_help, items}: over the uncertain rows, how many each
    missing or failing source would have helped, most first. A case counts once
    per source however many unknowns it names."""
    uncertain = [r for r in rows if r["_uncertain"]]
    tally: dict[tuple[str, str], dict] = {}
    with_help = 0
    for r in uncertain:
        seen: set[tuple[str, str]] = set()
        for entry in r["_help"]:
            for item in _help_items(entry):
                key = (item["state"], item["label"])
                if key in seen:
                    continue
                seen.add(key)
                tally.setdefault(key, {**item, "cases": 0})["cases"] += 1
        if seen:
            with_help += 1
    items = sorted(tally.values(), key=lambda i: (-i["cases"], _HELP_ORDER[i["state"]],
                                                  i["label"].lower()))
    return {"uncertain": len(uncertain), "with_help": with_help, "items": items}


def _view_would_help(params: dict) -> dict:
    """Over the uncertain cases (verdict: needs more information), how many
    each missing or failing source would have helped, most first. A case counts
    once per source however many unknowns it names. Read only."""
    hours = _hours(params, _HELP_HOURS)
    rows, _ = _build_rows(params, hours, True, False)
    return {"ok": True, "view": "would_help", "hours": hours,
            **_would_help_summary(rows)}


_VIEW_FNS = {"list": _view_list, "kpis": _view_kpis,
             "policy": _view_policy, "tuning": _view_tuning,
             "would_help": _view_would_help}


def monitor_cases(config: dict, params: dict) -> dict:
    """The Monitor's read side. ``view`` picks the shape:

    list (default): the handled-cases queue, newest first, filtered by
    ``bucket`` (all | handled | needs_you | reopened) over the last ``hours``
    (default 24), capped at ``limit`` (default 50, max 200) with the matching
    ``total`` and the per-bucket ``counts``. kpis: outcome tiles. policy:
    per-rule readiness to enforce. tuning: per detection rule reopen rate and
    safe-to-tune. would_help: the missing or failing sources ranked by how many
    uncertain cases each would have helped. Read only."""
    _check_params(MonitorCasesParams, params)
    view = (params or {}).get("view") or "list"
    if view not in _VIEW_FNS:
        return _err("bad_view", f"view must be one of {', '.join(VIEWS)}, got {view!r}")
    return _VIEW_FNS[view](params or {})


# ---------------------------------------------------------------------------
# M3: feedback
# ---------------------------------------------------------------------------

def monitor_feedback(config: dict, params: dict) -> dict:
    """Record one analyst's right / wrong (and a short note) on the AI's
    decision for a record. One row per (record, analyst); a later call
    replaces it, an empty ``rating`` clears it. Returns the stored feedback
    read back with that decision's totals."""
    _check_params(MonitorFeedbackParams, params)
    record = _require(params, "record")
    rating = (params.get("rating") or "").strip().lower() or None
    if rating not in (None, "right", "wrong"):
        return _err("bad_rating", f"rating must be right or wrong, got {rating!r}")
    note = (params.get("note") or "").strip()[:500] or None
    analyst, err = _caller_analyst(params)
    if err:
        return err
    if not analyst:
        return _err("no_analyst", "Could not tell who is giving feedback.")
    parts = [p for p in str(record).split("/") if p]
    if len(parts) < 4 or parts[0] != "api":
        return _err("bad_iri", f"record must be a record IRI like "
                    f"/api/3/alerts/<uuid>, got {record!r}")
    session_id = f"auto:{parts[-2]}:{parts[-1]}"
    from .storage import default_storage
    storage = default_storage()
    if storage.get_session_initiator(session_id) is None:
        return _err("not_found", "The assistant has not triaged that record.")
    rule = storage.session_policy_rule(session_id)
    storage.set_decision_feedback(str(record), analyst, session_id, rating, note, rule)
    entries = storage.get_decision_feedback(str(record))
    return {"ok": True, "record": str(record), "session_id": session_id,
            "feedback": _feedback_view(entries, analyst)}
