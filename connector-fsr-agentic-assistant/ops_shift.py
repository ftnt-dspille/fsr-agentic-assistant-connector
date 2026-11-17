"""Shift summary (X6): an end-of-shift digest for a time window.

Every number comes from stored data, computed here with no model: the cases the
assistant handled (the Monitor's own rows), what is waiting for a human, the
notable verdicts, the sources that would have helped most, and what the
scheduled hunts found. The text rendering is a fixed template over those
numbers, so a copy or export reads the same as the view.

`build_shift_digest` is pure over the store, and the result carries a `subject`
and `text`. With `notify` set, shift_summary hands them to the configured
notification channel (see ops_notify); a model could rephrase the prose without
touching a figure.
"""
from __future__ import annotations

import time
from typing import Any

from .ops_common import _check_params, _err
from .ops_monitor import (
    _build_rows, _hours, _would_help_summary,
)
from .ops_hunts import OUTCOME_LABELS
from .pydantic_models import ShiftSummaryParams

_DEFAULT_HOURS = 8
_MAX_HOURS = 24 * 31
_TOP = 5            # notable verdicts and would-help items listed
_TOP_WAITING = 10   # waiting cases listed (the count is always the full total)

_NOTABLE = ("true_positive", "suspicious")
_NOTABLE_LABELS = {"true_positive": "True positive", "suspicious": "Suspicious"}


def _case_line(r: dict) -> dict:
    return {"record_name": r.get("record_name"), "record_iri": r.get("record_iri"),
            "verdict": r.get("verdict"), "confidence": r.get("confidence"),
            "action": r.get("action"), "reasons": list(r.get("reasons") or []),
            "priority": r.get("_priority"), "at": r.get("at")}


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def build_shift_digest(since: float, until: float, params: dict | None = None) -> dict:
    """The digest for [since, until) as plain data (see the module doc)."""
    from .storage import default_storage
    storage = default_storage()
    hours = (until - since) / 3600.0
    # _build_rows reads from `since` to now; the window's end is cut here.
    rows, _ = _build_rows(params or {}, (time.time() - since) / 3600.0, True, False)
    rows = [r for r in rows if r["at"] < until]

    handled = {
        "triaged": len(rows),
        "closed": sum(1 for r in rows if r["_closed"]),
        "handled": sum(1 for r in rows if r["bucket"] == "handled"),
        "sent_to_you": sum(1 for r in rows if r["action_kind"] == "escalated"),
        "grouped": sum(max(0, (r.get("group_size") or 1) - 1) for r in rows),
        "deferred": storage.triage_deferred_count(since, until),
        "would_have_acted": sum(1 for r in rows if r["action_kind"] == "would_have"),
        "reopened": sum(1 for r in rows if r["bucket"] == "reopened"),
    }

    waiting_rows = sorted((r for r in rows if r["bucket"] == "needs_you"),
                          key=lambda r: (r["_rank"] or 9, -r["at"]))
    waiting = {"total": len(waiting_rows),
               "cases": [_case_line(r) for r in waiting_rows[:_TOP_WAITING]]}

    notable_rows = sorted((r for r in rows if r["_disposition"] in _NOTABLE),
                          key=lambda r: (r["_disposition"] != "true_positive",
                                         -(r.get("confidence") or 0), -r["at"]))
    notable = {"total": len(notable_rows),
               "cases": [_case_line(r) for r in notable_rows[:_TOP]]}

    help_ = _would_help_summary(rows)
    would_help = {"uncertain": help_["uncertain"], "with_help": help_["with_help"],
                  "items": [{"label": i["label"], "problem": i["problem"],
                             "cases": i["cases"]} for i in help_["items"][:_TOP]]}

    runs = storage.list_hunt_runs(None, limit=10000, since=since, until=until)
    by_outcome = {k: sum(1 for r in runs if r["outcome"] == k) for k in OUTCOME_LABELS}
    findings = [{"hunt": r["hunt_name"], "summary": r.get("summary"),
                 "severity": r.get("severity"), "confidence": r.get("confidence"),
                 "case_iri": r.get("case_iri"), "case_action": r.get("case_action"),
                 "at": r["started_at"]} for r in runs if r["outcome"] == "finding"]
    hunts = {"runs": len(runs), "findings": by_outcome["finding"],
             "quiet": by_outcome["quiet"], "inconclusive": by_outcome["inconclusive"],
             "failed": by_outcome["error"], "cases": findings[:_TOP_WAITING]}

    digest = {"from": since, "to": until, "hours": round(hours, 2),
              "handled": handled, "waiting": waiting, "notable": notable,
              "would_help": would_help, "hunts": hunts}
    digest["subject"] = f"Shift summary: {_stamp(since)} to {_stamp(until)}"
    digest["text"] = render_text(digest)
    return digest


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def render_text(d: dict) -> str:
    """The digest as plain text: a fixed template over the numbers in `d`."""
    h, w, n, wh, hu = d["handled"], d["waiting"], d["notable"], d["would_help"], d["hunts"]
    lines = [d["subject"], ""]
    lines += ["What the assistant handled",
              f"- Triaged {_n(h['triaged'], 'case')}: {h['handled']} handled, "
              f"{h['closed']} closed for you, {h['sent_to_you']} sent to you.",
              f"- Grouped {_n(h['grouped'], 'related alert')} under a lead case"
              f"; {h['deferred']} held back by the storm cap.",
              f"- {_n(h['would_have_acted'], 'case')} where it would have acted if allowed; "
              f"{h['reopened']} closed, then reopened.", ""]
    lines.append(f"Waiting for you ({w['total']})")
    if not w["cases"]:
        lines.append("- Nothing is waiting on you.")
    for c in w["cases"]:
        why = f" - {c['reasons'][0]}" if c["reasons"] else ""
        prio = f"{c['priority']} " if c["priority"] else ""
        lines.append(f"- {prio}{c['record_name'] or 'Unnamed case'}{why}")
    if w["total"] > len(w["cases"]):
        lines.append(f"- and {w['total'] - len(w['cases'])} more.")
    lines += ["", f"Notable verdicts ({n['total']})"]
    if not n["cases"]:
        lines.append("- No confirmed or suspicious verdicts.")
    for c in n["cases"]:
        conf = f", {round(c['confidence'] * 100)}% confident" if c["confidence"] is not None else ""
        lines.append(f"- {c['record_name'] or 'Unnamed case'}: {c['verdict']}{conf}")
    lines += ["", "Would help most"]
    if not wh["items"]:
        lines.append(f"- No missing or failing source held back {_n(wh['uncertain'], 'uncertain case')}."
                     if wh["uncertain"] else "- No uncertain cases.")
    for i in wh["items"]:
        lines.append(f"- {i['problem']}: would have helped {_n(i['cases'], 'uncertain case')}.")
    lines += ["", "Scheduled hunts",
              f"- {_n(hu['runs'], 'run')}: {hu['findings']} found something, "
              f"{hu['quiet']} quiet, {hu['inconclusive']} inconclusive, {hu['failed']} failed."]
    for f in hu["cases"]:
        lines.append(f"- {f['hunt']}: {f['summary'] or 'a finding'}")
    return "\n".join(lines) + "\n"


def shift_summary(config: dict, params: dict) -> dict:
    """The end-of-shift digest for the last `hours` (default 8), ending at
    `until` (epoch seconds; default now). Deterministic over stored data; the
    reply carries the numbers by section and the same digest as `text`. Read
    only."""
    _check_params(ShiftSummaryParams, params)
    hours = _hours(params, _DEFAULT_HOURS)
    if hours > _MAX_HOURS:
        return _err("bad_window", f"hours must be at most {_MAX_HOURS}, got {hours}")
    until: Any = params.get("until")
    try:
        until = float(until) if until else time.time()
    except (TypeError, ValueError):
        return _err("bad_window", f"until must be epoch seconds, got {until!r}")
    digest = build_shift_digest(until - hours * 3600.0, until, params)
    out = {"ok": True, **digest}
    if params.get("notify"):
        from .ops_notify import notify_shift, notifications_enabled
        sent = notify_shift(config, digest)
        out["notified"] = (sent or {}).get("status") or (
            "off" if not notifications_enabled(config) else None)
    return out
