"""The Agentic assistant's playbook-run policy.

``run_playbook`` / ``resume_playbook`` / ``list_module_playbooks`` are the
framework's (``fsr_playbooks.mcp_server.tools_playbook_run``: the pyfsr typed
trigger, the manual-input seam, the pre-card checks). What is the assistant's
own, registered by ``registry.register_playbook_tools``:

  1. **Run gate** (``_playbook_run_gate``): no active persona and no human
     approval => refused. An approval is authorization.
  2. **Auto-run** (``_auto_resolver``, the framework's
     ``set_run_playbook_auto_resolver``): a playbook in the persona's
     ``run_playbook.auto`` subset runs without a card (tier 2), unless it is
     aimed past a record this conversation created. Everything else is carded.
  3. **Annotation** (``_annotate_run``): a run of a playbook outside the
     persona's advisory ``allow`` list is flagged ``unlisted``.

The persona allowlist is **advisory**; the approval card is the real gate, so
the agent never dead-ends on an unlisted playbook.
"""
from __future__ import annotations

from typing import Any, Optional

from .profiles import (
    get_active_profile,
    get_active_record_iri,
    get_created_record_source,
    record_iri_parts,
)


def _run_playbook_gate(playbook: str, *, approved: bool = False) -> Optional[dict]:
    """Refusal dict when a run must be blocked outright; ``None`` when it may
    proceed (subject to the tier/approval machinery upstream).

    Policy (allowlist is *advisory*; human approval is the real gate):
      * Human-approved ⇒ NOT refused, persona or no persona. The analyst was
        shown a card naming the playbook and clicked approve; discarding that
        authorization is strictly worse than running what they asked for.
      * No active persona AND not human-approved ⇒ refused. This guard exists to
        stop an *unattended* run, not to overrule a person.
      * Persona present ⇒ NOT refused here, even for a playbook outside
        ``run_playbook.allow``. The tier resolver has already decided how it
        reaches this body: an ``auto`` playbook auto-ran (tier 2); anything else
        was surfaced as an approval card (tier 3) and a human approved it. So
        reaching execution == authorized. `allow` membership only shapes the
        card (a not-listed playbook is flagged) and gates ``auto`` eligibility.

    The no-persona branch was documented "normally unreachable" on the premise
    that run_playbook only surfaces via a persona's ``tools.allow``. That
    premise is false: run_playbook is in the *build* intent's tool slice, which
    needs no persona. Live, that produced the worst possible arc -- a
    correct approval card (right playbook, right run history, working link), the
    analyst approves, and the run is refused ``run_not_permitted`` with nothing
    run and no way forward. The approval IS the authorization.
    """
    if approved:
        return None
    if get_active_profile() is None:
        return {
            "ok": False, "code": "run_not_permitted",
            "message": ("running a playbook requires an active assistant persona; "
                        "none is bound for this session"),
        }
    return None


def _auto_resolver(args: dict) -> bool:
    """Framework tier hook: True ⇒ run WITHOUT an approval card (tier 2). Only a
    playbook in the active persona's ``run_playbook.auto`` subset qualifies, and
    only when the run is not aimed at a record this conversation did not create
    (see :func:`_aims_past_created_record`). Everything else stays tier 3
    (carded). Fail-safe: any uncertainty -> False."""
    try:
        profile = get_active_profile()
        pb = (args or {}).get("playbook") or ""
        if not (profile and pb and profile.may_autorun_playbook(pb)):
            return False
        return not _aims_past_created_record(args)
    except Exception:  # noqa: BLE001
        return False


def _aims_past_created_record(args: dict) -> bool:
    """True when this run targets a record of a module the conversation created
    records in, and the target is NOT one of those records. Live: asked to
    create a template and then render it, the model ran the playbook on the
    template on screen instead. The run then reports the wrong record's output
    with no card to show the analyst which record it touched.

    The target is what the body will use: an explicit ``record``, else the
    record the turn is mounted on (the body defaults to it). An unparseable
    target while records exist is treated as a miss (carded). No bound source,
    no created records, or no target at all -> False (no change)."""
    source = get_created_record_source()
    if source is None:
        return False
    try:
        created = [parts for parts in map(record_iri_parts, source() or []) if parts]
    except Exception:  # noqa: BLE001 -- cannot tell what was created: card it
        return True
    if not created:
        return False
    target = (args or {}).get("record") or get_active_record_iri() or ""
    if not target:
        return False
    parts = record_iri_parts(target)
    if parts is None:
        return True
    module, uuid = parts
    created_here = {u for m, u in created if m == module}
    return bool(created_here) and uuid not in created_here


def _playbook_run_gate(playbook: str, approved: bool, action: str = "run") -> Optional[dict]:
    """The framework's run gate (``_shared.set_playbook_run_gate``): the persona
    rule above, for a run or a resume of an already-started run."""
    refused = _run_playbook_gate(playbook, approved=approved)
    if refused is None or action != "resume":
        return refused
    return {"ok": False, "code": "resume_not_permitted",
            "message": ("resuming a playbook requires an active assistant "
                        "persona; none is bound for this session")}


def _annotate_run(playbook: str) -> dict[str, Any]:
    """``{"unlisted": True}`` for a run of a playbook outside the persona's
    advisory allowlist -- only reachable after a human approved the
    (cautionary) card. For the audit trail, and so the agent notes it."""
    prof = get_active_profile()
    if prof and not prof.may_run_playbook(playbook):
        return {"unlisted": True}
    return {}
