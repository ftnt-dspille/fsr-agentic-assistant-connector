"""manual domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
from typing import Any
from connectors.core.connector import ConnectorError
from .pydantic_models import (
    RespondManualInputParams,
)

from .ops_cards import _CARD_STOP_REASON, _manual_input_card_from_awaiting
from .ops_common import _check_params, _require
from .ops_config import _build_provider, _config_for_persona
from .ops_health import _check_health
from .ops_resume import _resume_conversation
from .ops_turn_state import _load_resume_profile



def _run_pk_for_manual_input(mid: Any) -> str | None:
    """The workflow run pk a manual_input_id belongs to, if this process
    surfaced that gate (see ``tools_playbook_run._MANUAL_INPUT_RUNS``). Used only to
    NAME the run in the bridge message -- a miss just omits it."""
    try:
        from fsr_playbooks.mcp_server.tools_playbook_run import _MANUAL_INPUT_RUNS
        return _MANUAL_INPUT_RUNS.get(str(mid))
    except Exception:  # noqa: BLE001 -- cosmetic; never fail the leg over it
        return None


def _forward_manual_input(input_id: str, decision: str,
                          values: dict, option: str | None = None) -> dict:
    """Best-effort bridge: submit the user's manual-input values to the paused
    FortiSOAR workflow run via pyfsr's typed manual-input API. Returns a small
    status dict the agent's next turn can quote. Never raises -- a forward failure
    (or no runtime at all, in dev) degrades to a conversational-only resume so the
    chat never dead-ends on the user.

    Backed by pyfsr's typed `ManualInputAPI` (retrieve → resume), NOT the one-call
    `answer()`: `answer()` retrieves the prompt with `owners=<resolved user>`, and
    through the connector's on-box crudhub transport that owner-scoped retrieve
    comes back with an EMPTY `response_mapping.options`, so `answer()` raises
    "exposes no response options" even though the run is resumable (live-observed,
    connector 0.4.71 on 206). The connector's own card path (`_pending_input` →
    `retrieve_manual_input`, which delegates to `manual_input.retrieve` WITHOUT
    `owners`) returns the options fine -- so we mirror it: retrieve without owners,
    pick the PRIMARY option's `step_iri`, and POST `mi.resume`. `reject` never
    reaches here (the caller short-circuits it), so primary (approve/continue) is
    always correct. Still all pyfsr typed methods (see [[feedback_use_pyfsr_methods]])
    -- just not the owner-scoping `answer()` wrapper."""
    try:
        from fsr_playbooks.mcp_server import _shared
        client = _shared._live_client()
    except Exception as e:  # noqa: BLE001 -- setup failure ⇒ degrade to dev-only
        return {"forwarded": False, "reason": "client_unavailable", "error": str(e)}
    if client is None:
        return {"forwarded": False, "reason": "crudhub_unavailable",
                "phase": "dev-only"}
    mi = getattr(client, "manual_input", None)
    if mi is None:
        return {"forwarded": False, "reason": "no_manual_input_api"}
    try:
        iid = int(str(input_id).strip())
    except (TypeError, ValueError):
        return {"forwarded": False, "reason": "bad_input_id",
                "input_id": input_id}
    try:
        # Retrieve WITHOUT owners (the path that returns options on-box) to get the
        # response buttons + the paused step's ids.
        full = mi.retrieve(iid, typed=False)
        full = full if isinstance(full, dict) else {}
        options = (full.get("response_mapping") or {}).get("options") or []
        if option and options:
            # The analyst pressed a specific button. Honour it exactly; never
            # fall back to the primary, which may be the opposite choice.
            want = str(option).strip().lower()
            chosen = next((o for o in options
                           if str(o.get("option") or "").strip().lower() == want), None)
            if chosen is None:
                return {"forwarded": False, "manual_input_id": iid,
                        "reason": "bad_option", "option": option,
                        "options": [o.get("option") for o in options]}
        else:
            chosen = next((o for o in options if o.get("primary")),
                          options[0] if options else None)
        if not chosen or not chosen.get("step_iri"):
            # Distinguish "the gate EXPIRED" from "this gate has no buttons".
            # A manual_input step can carry its own timeout (the ZTPF
            # metadata-source playbook: ONE MINUTE); once it lapses the platform
            # resolves the step down its timeout branch and the run finishes, so
            # a submit arriving afterwards can never land. Reporting that as
            # `no_response_options` sent the analyst chasing a retrieve bug and
            # -- worse -- the bridge message then told them to narrate as if the
            # input had been accepted. Name it, and say so.
            out = {"forwarded": False, "manual_input_id": iid,
                   # The record names its own run (`workflow` is the numeric pk
                   # on the retrieve, unlike the list's encrypted token). Prefer
                   # it: the id→run map is per-WORKER memory and the box runs a
                   # pool, so the worker answering the submit is usually not the
                   # one that carded the gate (live: `run` came back null).
                   "run": (str(full.get("workflow")) if full.get("workflow")
                           else _run_pk_for_manual_input(iid))}
            expires_at = full.get("timeout")
            from fsr_playbooks.mcp_server.tools_playbook_run import _seconds_until
            left = _seconds_until(expires_at)
            if not full:
                # An EMPTY body is the platform's signature for a gate that no
                # longer exists -- live on .206, a lapsed input retrieves as
                # `{}` while a live one returns the full record (verified on two
                # parked runs). So this is expiry, evidenced, not inferred.
                out["reason"] = "gate_expired"
            elif expires_at and left is not None and left <= 0:
                out["reason"] = "gate_expired"
                out["expires_at"] = expires_at
                out["expired_seconds_ago"] = -left
            else:
                out["reason"] = "no_response_options"
            return out
        user_iri = mi._resolve_user_iri()  # a people IRI to submit as (prefer admin)
        ack = mi.resume(full.get("workflow"), step_iri=chosen["step_iri"],
                        step_id=full.get("step_id"), manual_input_id=iid,
                        user=user_iri, input=values or {})
    except Exception as e:  # noqa: BLE001 -- transport/resolution; degrade cleanly
        return {"forwarded": False, "reason": "forward_failed",
                "error": str(e), "manual_input_id": iid}
    raw = ack
    if hasattr(ack, "model_dump"):
        try:
            raw = ack.model_dump()
        except Exception:  # noqa: BLE001
            raw = str(ack)
    return {"forwarded": True, "manual_input_id": iid,
            # Carry the run pk on the SUCCESS path too. The failure paths above
            # have always included it; this one didn't, and the omission cost a
            # visibly broken turn on the demo box (2026-08-05, 0.5.102): with no
            # `run`, the resume message reads "manual_input_id=545" with no run
            # pk, while the directive tells the model to "report what the run
            # does next, using get_playbook_run". Having nothing to pass it, the
            # model asked the ANALYST -- "I need the playbook run identifier
            # (the workflow run PK / @id) ... Please paste the run_pk" -- one
            # turn after they watched that same run get resumed. Asking a human
            # for an id we are holding is the `agent_asks_for_data_it_holds`
            # shape; the fix is to hand it over, not to word the ask better.
            # `full["workflow"]` is the numeric pk from the un-owned retrieve
            # (the list endpoint's `workflow` is an encrypted token instead).
            "run": (str(full.get("workflow")) if full.get("workflow")
                    else _run_pk_for_manual_input(iid)),
            "option": chosen.get("option"), "raw": raw}


def _manual_input_user_message(input_id: str, decision: str, values: dict,
                               forward: dict) -> tuple[str, str]:
    """The manual-input outcome as a user turn: ``(durable, directive)``.

    **`durable` is history; `directive` is for THIS turn only.** They are
    returned separately because conflating them stranded the session live (connector 0.5.100, the demo box).

    Every branch below needs to tell the model what to do about the gate right
    now -- "do NOT call resume_playbook again", "report what the run does next",
    "say the values could not be delivered". Those are correct instructions for
    the resumed turn and each was added to fix a real bug. But the whole string
    was appended to the canonical conversation as a **user message**, so on every
    later turn the model saw a standing order from the analyst about a run that
    finished minutes ago -- competing with whatever the analyst actually just
    asked. Live, "How do I triage a phishing report?" came back as:

        "I will not resume the run again. I don't yet have the playbook run
         identifier (run_pk) needed to fetch its current workflow status."

    which is close to a readback of the directive: "Do NOT call resume_playbook
    for this gate again" -> "I will not resume the run again"; "using
    get_playbook_run if you need its current status" -> "needed to fetch its
    current workflow status". The turn ran zero tools -- it was not confused
    about the run, it was obeying a still-live instruction.

    Same class as [[record_referent_drift_in_chat]] (fixed in 0.5.90): stale
    context outliving the turn it was written for. The rule is that a
    turn-scoped imperative must never be persisted as something the analyst
    said -- the analyst said no such thing, and they certainly did not say it
    about every future turn.
    """
    # NEVER write the id bare ("gate '508'"): the model read the nearest bare
    # int as a run pk and called resume_playbook(run='508') for run 9046. Label
    # it, and name the actual run pk alongside when we know it.
    gate = f"manual_input_id={input_id}"
    run_pk = forward.get("run")
    if run_pk:
        gate += f" (on workflow run pk {run_pk})"
    if decision == "reject":
        return (f"I declined the manual-input gate ({gate}).",
                "Do not proceed with the gated step; acknowledge and ask how "
                "I'd like to continue.")
    vals = json.dumps(values or {}, indent=2, default=str)
    # The LEAD sentence is part of the claim, not scaffolding around it. It used
    # to read "I approved the gate with these values" on every branch --
    # including the two where the values never reached the run -- so the model
    # was handed an assertion of acceptance and a bridge retracting it in the
    # same turn, and the lead is the half that sounds like the analyst speaking.
    # Same P2-honesty shape as the expired-gate bug itself: only say "approved
    # with these values" where a submit was actually acknowledged.
    #
    # The split below keeps every FACT in `lead`/`fact` (persisted) and every
    # INSTRUCTION in `bridge` (this turn only). A branch whose fact lived in the
    # bridge would otherwise leave history asserting a success that did not
    # happen -- the exact P2-honesty bug the lead was written to fix.
    lead = (f"I approved the manual-input gate ({gate}) with these values"
            if forward.get("forwarded") else
            f"I tried to answer the manual-input gate ({gate}) with these "
            "values, and they did NOT reach the run")
    fact = ""
    if forward.get("forwarded"):
        # State that the resume is DONE, and forbid the retry explicitly.
        # "continue and report what happens next" alone read as an instruction
        # to continue the RUN, and the model obliged by calling resume_playbook
        # a second time -- reaching for the nearest id in context, which is this
        # gate id, not the run pk. Live on 206: resume_playbook(run='388') where
        # 388 was the manual_input_id and the run was 6839 → 400 "Select a valid
        # choice", surfacing to the analyst as "approved, then nothing happens".
        # Same imperative-hijack class as the T2 containment drift: the fix is
        # to remove the instruction that invites the wrong call, not to add a
        # guard downstream of it.
        fact = ("The paused workflow run has already been resumed with these "
                "values; that leg is complete.")
        # Do NOT name a tool here unless it is REGISTERED. This clause used to
        # end "...using get_playbook_run if you need its current status", and
        # `get_playbook_run` has never existed in this connector's tool set --
        # the registry has run_playbook / resume_playbook /
        # list_module_playbooks and no run-status tool at all. Live on .206
        # (0.5.103) the model dutifully went looking for the run anyway and
        # improvised REST paths through get_record, then narrated the wreckage
        # to the analyst one turn after they submitted the form:
        #     "/wf/api/workflows/9335 -> not_found
        #      /api/3/wf/workflow-runs/9335 -> http_400 ..."
        # Instructing a capability we do not have manufactures exactly the
        # dead-end the honesty rules exist to prevent. Gate:
        # tests/test_directives_name_real_tools.py.
        bridge = ("Do NOT call resume_playbook for this gate again, and do not "
                  "go looking up the run's status -- you have the outcome "
                  "above. Briefly confirm the values were submitted and the run "
                  "continued, then ask what I'd like to do next.")
    elif forward.get("reason") == "gate_expired":
        # NEVER narrate an expired gate as accepted. The step's own timeout
        # lapsed, the run took its timeout branch, and nothing we say makes the
        # values land -- claiming otherwise is a false statement about a
        # state-changing gate (P2). Say what happened and offer the re-run.
        ago = forward.get("expired_seconds_ago")
        run_pk = forward.get("run")
        fact = (
            "IMPORTANT -- these values were NOT applied. The playbook's "
            "manual-input step has its own timeout and it lapsed"
            + (f" {ago}s before I submitted" if ago is not None else "")
            + ", so the run already continued down its timeout/cancel branch"
            + (f" (run {run_pk})" if run_pk else "") + ".")
        bridge = (
            "Tell me plainly that the form expired before the answer "
            "reached the run and that nothing was applied -- do NOT describe "
            "the input as accepted and do NOT report any result as if the "
            "gated step had run. Then offer to run the playbook again so I can "
            "answer within the window. Do NOT call resume_playbook: the run is "
            "no longer awaiting input.")
    else:
        fact = ("The workflow run was not reachable -- "
                f"{forward.get('reason')}.")
        bridge = ("Say the values could not be delivered to the run, and do "
                  "NOT report the gated step's result as if it had run. Do NOT "
                  "call resume_playbook to retry it, and never pass "
                  f"{input_id} as `run` -- it is the input form's id, not a "
                  "run pk.")
    # `bridge` is the turn-scoped half -- see this function's docstring for why
    # it must not be persisted alongside the facts.
    durable = f"{lead}:\n{vals}"
    if fact:
        durable += f"\n{fact}"
    return durable, bridge


def respond_manual_input(config: dict, params: dict) -> dict:
    """HITL manual-input leg. The widget calls this (NOT chat_resume) when
    a `manual_input` card is on screen: it carries the paused workflow run
    plus the user's submitted field values (contract §4).

    Two things happen: (1) the values are forwarded to the paused
    FortiSOAR workflow run (best-effort -- degrades cleanly off-platform),
    and (2) the agent conversation is resumed Model-A style so the chat
    keeps moving and narrates the result."""
    _check_params(RespondManualInputParams, params)
    _check_health(config)
    _require(params, "session_id")  # presence guard; _resume_conversation reads it from params
    input_id = _require(params, "input_id")
    decision = params.get("decision") or "approve"
    if decision not in ("approve", "reject"):
        raise ConnectorError(
            f"decision must be approve|reject, got {decision!r}")
    values = params.get("values") or {}
    # The button the analyst pressed, when the card offered several. Absent =
    # the primary (approve) or the first non-primary (reject), as before.
    option = params.get("option") or None

    from .storage import default_storage, PersistedApprovalGateway

    storage = default_storage()
    gateway = PersistedApprovalGateway(storage)
    provider = _build_provider(
        _config_for_persona(config, _load_resume_profile(storage, params.get("session_id"))),
        gateway)

    forward = ({"forwarded": False, "reason": "rejected"}
               if decision == "reject" and not option
               else _forward_manual_input(input_id, decision, values, option))

    user_msg, turn_directive = _manual_input_user_message(
        input_id, decision, values, forward)
    nxt = _next_pause_after_forward(forward)
    if nxt is not None:
        fact, turn_directive = _next_pause_message(nxt, forward.get("run"))
        user_msg = f"{user_msg}\n{fact}"
    out = _resume_conversation(config, params, decision, storage, provider,
                               user_msg=user_msg, resume_tag="manual_input",
                               turn_directive=turn_directive)
    if isinstance(out, dict):
        out["input_id"] = input_id
        out["manual_input_forward"] = forward
        form = (nxt or {}).get("form")
        card = (_manual_input_card_from_awaiting(
            {"code": "awaiting_input", "awaiting": form, "run_pk": forward.get("run")},
            None, len(out.get("transcript") or [])) if form else None)
        if card:
            # The run's NEXT pause is the analyst's to answer, so it ends this
            # turn the same way the first one did.
            out.setdefault("transcript", []).append(card)
            out["stop_reason"] = _CARD_STOP_REASON["manual_input"]
    return out


# How much of a pause's text the model is handed. A results gate prints the
# collected data as raw JSON, which can run to tens of KB.
_NEXT_PAUSE_TEXT_CAP = 6000


def _next_pause_after_forward(forward: dict) -> dict | None:
    """After a submit landed, wait briefly for the run's next state: the next
    pause (``{"form", "status"}``), its end (``{"form": None, "status"}``), or
    ``None`` when nothing could be read. Never raises.

    Live: the metadata-source validation run paused a second time on
    "Data Collected" with the collected firmware list in its text and
    Ok / Save Example buttons. Nothing read it, so the chat said "the workflow
    continued, what next?" while the answer to the analyst's question sat on
    the box."""
    if not forward.get("forwarded") or not forward.get("run"):
        return None
    try:
        from fsr_playbooks.mcp_server import _shared
        from fsr_playbooks.mcp_server.tools_playbook_run import _reawait_after_resume
        client = _shared._live_client()
        playbooks = getattr(client, "playbooks", None) if client else None
        if playbooks is None:
            return None
        form, status = _reawait_after_resume(client, playbooks, str(forward["run"]), 45,
                                             answered=forward.get("manual_input_id"))
    except Exception:  # noqa: BLE001 -- best effort; the submit itself already landed
        return None
    return {"form": form, "status": status}


def _next_pause_message(nxt: dict, run_pk: Any) -> tuple[str, str]:
    """``(fact, directive)`` describing where the run went after the submit."""
    from fsr_playbooks.mcp_server.tools_playbook_run import is_analyst_form
    form, status = nxt.get("form"), nxt.get("status")
    run = f"run {run_pk}" if run_pk else "the run"
    if not form:
        return (f"After that, {run} ended with status {status or 'unknown'}.",
                "Report how the run ended in a short line. Do NOT call "
                "resume_playbook.")
    title = form.get("title") or "a form"
    text = str(form.get("description") or "").strip()
    if len(text) > _NEXT_PAUSE_TEXT_CAP:
        text = text[:_NEXT_PAUSE_TEXT_CAP] + "\n...[truncated]"
    buttons = " / ".join(str(o.get("option")) for o in (form.get("options") or [])
                         if isinstance(o, dict) and o.get("option"))
    fact = (f"After that, {run} paused again on {title!r}"
            + (f" with the buttons {buttons}" if buttons else "") + "."
            + (f" It shows:\n{text}" if text else ""))
    if is_analyst_form(form):
        return (fact, "That form is now in front of me with its own buttons. "
                      "Tell me in a few lines what it shows (the data it "
                      "collected, if any), then stop. Do NOT call "
                      "resume_playbook or answer it for me.")
    return (fact, "Tell me in a few lines what it shows. It only needs "
                  f"acknowledging, so you may call resume_playbook(run={str(run_pk)!r}, "
                  "decision='approve') to let the run finish.")
