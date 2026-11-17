"""Outbound notifications (X6): tell a person when an unattended run needs them.

Two things send a notification, both only when the `notify_unattended`
configuration field is on (the default is off):

- an unattended turn (a scheduled hunt or an auto-triage) that ended on an
  escalation or on a true positive / suspicious verdict;
- a shift summary the caller asked to have sent (`notify` on shift_summary).

The decision reads structured fields only: the outcome's `needs_human` flag and
the verdict card's `disposition`. Nothing here looks at the words of the verdict,
the transcript or the analyst's text. The channel is the one the box already has
configured (see `ops_scheduling._send_notification`: email, Slack or Teams), the
recipient is the `notification_recipient` field.

Every decision is recorded in the `notifications` table, one row per (kind, ref),
claimed before the send. A retried step or a second worker therefore sends
nothing the second time, and a skip or a failure is visible, not silent.
"""
from __future__ import annotations

from typing import Any

# A verdict the person wants to hear about whatever else happened.
NOTIFY_DISPOSITIONS = ("true_positive", "suspicious")

_LABELS = {"true_positive": "true positive", "false_positive": "false positive",
           "benign": "benign", "suspicious": "suspicious",
           "needs_more_info": "needs more information"}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def notifications_enabled(config: Any) -> bool:
    return isinstance(config, dict) and _truthy(config.get("notify_unattended"))


def _recipient(config: Any) -> str:
    return str((config or {}).get("notification_recipient") or "").strip()


def trigger_for(outcome: dict) -> str | None:
    """Why this turn's end is worth a notification: `escalation`, `verdict`, or
    None. From the outcome's structured fields only."""
    if (outcome.get("verdict") or {}).get("disposition") in NOTIFY_DISPOSITIONS:
        return "verdict"
    if outcome.get("needs_human"):
        return "escalation"
    return None


def _dispatch(kind: str, ref: str, trigger_code: str, subject: str, body: str,
              recipient: str) -> dict:
    from .ops_scheduling import _send_notification
    from .storage import default_storage
    storage = default_storage()
    nid = storage.claim_notification(kind, ref, trigger_code, subject)
    if nid is None:
        return {"sent": False, "status": "duplicate"}
    try:
        res = _send_notification(subject, body, kind, ref, recipient)
    except Exception as e:  # noqa: BLE001 - a notification never fails the run
        res = {"ok": False, "error": {"code": "send_failed", "message": str(e)}}
    if res.get("ok") is False:
        status, channel = "failed", (res.get("error") or {}).get("channel")
        detail = str((res.get("error") or {}).get("code") or "send_failed")
    elif res.get("skipped"):
        status, channel, detail = "skipped", res.get("channel"), str(res["skipped"])
    else:
        status, channel, detail = "sent", res.get("channel"), res.get("channel_label")
    storage.finish_notification(nid, status, channel, detail)
    return {"sent": status == "sent", "status": status, "channel": channel,
            "detail": detail}


def notify_turn(config: dict, *, kind: str, name: str, session_id: str,
                outcome: dict, case_iri: str | None = None) -> dict | None:
    """Notify about the end of one unattended turn, if it qualifies. `kind` is
    `hunt` or `auto_triage`; `outcome` is the structured result of the turn
    (`needs_human`, `why`, `verdict`). Returns the dispatch result, or None when
    notifications are off or the turn does not qualify."""
    if not notifications_enabled(config):
        return None
    trigger_code = trigger_for(outcome)
    if trigger_code is None:
        return None
    verdict = outcome.get("verdict") or {}
    label = _LABELS.get(verdict.get("disposition"), "no verdict")
    what = "Scheduled hunt" if kind == "hunt" else "Auto-triage"
    head = ("needs a person" if trigger_code == "escalation"
            else f"found a {label} verdict")
    subject = f"[Agentic Assistant] {what} {name}: {head}"
    lines = [f"{what} \"{name}\" ended on: {label}."]
    if verdict.get("severity") is not None:
        lines.append(f"Severity: {verdict['severity']}")
    if isinstance(verdict.get("confidence"), (int, float)):
        lines.append(f"Confidence: {round(verdict['confidence'] * 100)}%")
    if verdict.get("summary"):
        lines += ["", str(verdict["summary"])]
    if outcome.get("needs_human"):
        lines += ["", "It is waiting for a person. Open the Agentic Assistant "
                      "Monitor to review it."]
    if case_iri:
        lines.append(f"Case: {case_iri}")
    return _dispatch(kind, session_id, trigger_code, subject,
                     "\n".join(lines) + "\n", _recipient(config))


def notify_shift(config: dict, digest: dict) -> dict | None:
    """Send a produced shift summary. The text is the digest's own fixed
    rendering, so the notification reads exactly as the Monitor view does."""
    if not notifications_enabled(config):
        return None
    ref = f"{int(digest['from'])}-{int(digest['to'])}"
    return _dispatch("shift", ref, "shift", f"[Agentic Assistant] {digest['subject']}",
                     digest["text"], _recipient(config))
