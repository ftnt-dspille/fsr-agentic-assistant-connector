"""cards domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
import re
from typing import Any
from .pydantic_models import (
    ActionCardEvent as ActionCardFrame,
    InfoCardEvent as InfoCardFrame,
    ManualInputEvent as ManualInputFrame,
    StatusCardEvent as StatusCardFrame,
)
from .wire_enrichment import _ioc_extract

from .ops_common import (
    _HEALTHY_STATUSES, _HEALTH_FAIL_CODES, _as_frame, _frame_model_for, _reference_db_path,
)
from .ops_entity import (_context_match, _current_record, _display_new_value,
                         _display_value, _module_noun, _record_name)



# Map the loop's raw stop_reason / terminal event into the contract's
# stop_reason vocabulary. Order matters: ErrorEvent wins, then a
# pending ApprovalRequestEvent, then the loop's own reason.

_CARD_STOP_REASON = {
    "choice_card": "awaiting_choice",
    "action_card": "awaiting_action_card",
    "manual_input": "awaiting_manual_input",
    # capability_gap resumes through the SAME echo path as a choice_card
    # (the resume button / alternatives carry a `value` the widget echoes via
    # chat_resume). No new resume branch needed -- just a richer rendering.
    "capability_gap": "awaiting_choice",
    # playbook_offer is gating: the analyst accepts/declines on resume. The
    # connector (not the model) compiles + pushes on accept -- see
    # _resume_playbook_offer_accept.
    "playbook_offer": "awaiting_playbook_offer",
    # enhancement_offer is playbook_offer's UPDATE counterpart: it applies a
    # verified edit to the playbook the analyst already has open, rather than
    # creating a new one. Same gating shape, separate resume branch
    # (_resume_enhancement_offer_accept) because the write is an in-place
    # update_playbook, not a push.
    "enhancement_offer": "awaiting_enhancement_offer",
}


_CARD_EMITTER_TO_TYPE = {
    "emit_choice_card": "choice_card",
    "emit_action_card": "action_card",
    "emit_manual_input": "manual_input",
    "emit_capability_gap_card": "capability_gap",
    "emit_playbook_offer": "playbook_offer",
    "emit_enhancement_offer": "enhancement_offer",
}

# The consolidated `emit_card` union (Phase 1) retired the seven emitters
# above and carries its card type in a `card_type` argument, in the
# framework's SHORT vocabulary (fsr_playbooks CARD_TYPES keys). The splice
# must recognize it, or a delivered card is invisible to the wire and the
# salvage backstop papers a second card over the model's real one
# (effect probe A2 caught this live).
_EMIT_CARD_SHORT_TO_TYPE = {
    "choice": "choice_card",
    "action": "action_card",
    "manual_input": "manual_input",
    "capability_gap": "capability_gap",
    "playbook_offer": "playbook_offer",
    "enhancement_offer": "enhancement_offer",
    "verdict": "verdict_card",
}


def _emitted_card(w: dict) -> "tuple[str, dict] | None":
    """(card_type, field_args) when this tool_use frame emits a card --
    either a retired per-type emitter or the consolidated `emit_card`,
    whose card fields ride in its `payload` argument."""
    if w.get("type") != "tool_use":
        return None
    name = w.get("name")
    input_args = w.get("input") or {}
    if name in _CARD_EMITTER_TO_TYPE:
        return _CARD_EMITTER_TO_TYPE[name], input_args
    if name == "emit_card":
        kt = str(input_args.get("card_type") or "").strip().lower()
        ctype = _EMIT_CARD_SHORT_TO_TYPE.get(kt) or (
            kt if kt in _CARD_STOP_REASON else None)
        if ctype is not None:
            payload = input_args.get("payload")
            return ctype, (payload if isinstance(payload, dict) else {})
    return None


def _json_list(blob: Any) -> list:
    if isinstance(blob, list):
        return blob
    if not blob:
        return []
    try:
        parsed = json.loads(blob)
    except Exception:  # noqa: BLE001
        return []
    return parsed if isinstance(parsed, list) else []


def _json_default(blob: Any) -> Any:
    if blob in (None, ""):
        return None
    try:
        return json.loads(blob)
    except Exception:  # noqa: BLE001
        return blob


# A record IRI or bare path a model may drop into a card summary -- the analyst
# must never see `alerts/<uuid>` or `/api/3/alerts/<uuid>`. Replaced with a
# plain phrase ("the alert record"); this is display text only, not intent
# detection.
_RECORD_IRI_RE = re.compile(
    r"/?(?:api/3/)?([a-z][a-z_]*)/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


# Security acronyms that should stay uppercase in a humanized op title. A small
# stable constant, not a per-op map: it never needs an entry per connector.
_OP_ACRONYMS = frozenset({
    "ip", "url", "uri", "ioc", "iocs", "dns", "id", "ids", "edr", "api",
    "uuid", "cve", "md5", "sha", "sha1", "sha256", "http", "https", "siem",
    "soar", "ueba", "fqdn", "mac", "os", "vm", "ad", "csv", "json", "xml",
})


def _humanize_op(operation: str) -> str:
    """`isolate_collector` -> `Isolate collector`, `block_ip` -> `Block IP`. A
    readable fallback title so a card never shows a raw `connector.operation` id
    to the analyst. Known acronyms stay uppercase; already-cased tokens (SIR)
    are left as written."""
    words = str(operation or "").replace("_", " ").replace("-", " ").split()
    if not words:
        return str(operation or "")
    out = []
    for i, w in enumerate(words):
        if w.lower() in _OP_ACRONYMS:
            out.append(w.upper())
        elif i == 0:
            out.append(w[:1].upper() + w[1:])
        else:
            out.append(w)
    return " ".join(out)


def _derecord(text: str) -> str:
    def _repl(m: "re.Match[str]") -> str:
        module = m.group(1)
        singular = module[:-1] if module.endswith("s") else module
        return f"the {singular} record"
    return _RECORD_IRI_RE.sub(_repl, text)


def _clean_run_op_summary(summary: Any, connector: str, operation: str) -> str:
    """The run_op card's title/summary, free of raw ids and record IRIs.

    Falls back to a humanized operation name when the model gave none, strips
    record IRIs, and rewrites a bare `connector.operation` token to the readable
    verb -- so the header reads "Isolate collector", never
    "fortinet-fortiedr.isolate_collector" or "... alerts/<uuid>".
    """
    human = _humanize_op(operation)
    if not summary:
        return human
    cleaned = _derecord(str(summary)).replace(f"{connector}.{operation}", human)
    return cleaned.strip() or human


def _action_card_param_metadata(connector: str,
                                operation: str) -> dict[str, Any]:
    """Load connector-param metadata for widget action_card rendering.

    The reference DB stores FortiSOAR onchange children as
    parent_param_name/condition_value rows. This function compacts those rows
    into the contract 2.4.0 fields: param_schema, visibility, and
    param_groups_by_select. Best-effort: returns {} when the DB/schema is not
    available, preserving the legacy flat action_card shape.
    """
    import sqlite3
    try:
        db_path = _reference_db_path()
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(
                """SELECT param_name, title, type, required, default_value,
                          options_json, tooltip, placeholder, description,
                          visible, editable, parent_param_name, condition_value,
                          ord
                   FROM operation_params
                   WHERE connector_name=? AND op_name=?
                   ORDER BY ord""",
                (connector, operation),
            ).fetchall()]
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}

    by_name: dict[str, dict] = {}
    ordered: list[dict] = []
    rules: list[tuple] = []
    for r in rows:
        name = r.get("param_name")
        if not name:
            continue
        parent = r.get("parent_param_name") or None
        cond = r.get("condition_value") or None
        rules.append((name, parent, cond))
        entry = by_name.get(name)
        if entry is None:
            entry = {
                "name": name,
                "title": r.get("title") or name,
                "type": r.get("type") or "text",
                "required": bool(r.get("required")),
                "visible": bool(r.get("visible", 1)),
                "editable": bool(r.get("editable", 1)),
            }
            default = _json_default(r.get("default_value"))
            if default is not None:
                entry["default"] = default
            opts = _json_list(r.get("options_json"))
            if opts:
                entry["options"] = opts
            for key in ("description", "tooltip", "placeholder"):
                if r.get(key):
                    entry[key] = r[key]
            by_name[name] = entry
            ordered.append(entry)
        if parent:
            rule = {"parent": parent, "value": cond}
            entry.setdefault("applies_when", [])
            if rule not in entry["applies_when"]:
                entry["applies_when"].append(rule)

    always: list[str] = []
    seen_always: set[str] = set()
    when: dict[str, list[str]] = {}
    for name, parent, cond in rules:
        if parent is None:
            if name not in seen_always:
                always.append(name)
                seen_always.add(name)
        else:
            key = f"{parent}={cond}" if cond is not None else f"{parent}=*"
            if name not in when.setdefault(key, []):
                when[key].append(name)

    from collections import defaultdict
    children_of: dict[tuple[str, str], list[str]] = defaultdict(list)
    for name, parent, cond in rules:
        if parent is not None:
            children_of[(parent, str(cond))].append(name)
    param_types = {p["name"]: p.get("type") for p in ordered}
    param_options = {p["name"]: p.get("options") or [] for p in ordered}
    param_defaults = {p["name"]: p.get("default") for p in ordered}

    groups: dict[str, Any] = {}
    for sel in always:
        if param_types.get(sel) != "select":
            continue
        if not any(parent == sel for _, parent, _ in rules):
            continue
        options = param_options.get(sel) or []
        per_option: dict[str, Any] = {}
        for opt in options:
            visible: list[str] = []
            nested: dict[str, dict[str, list[str]]] = {}
            for child in children_of.get((sel, str(opt)), []):
                if child not in visible:
                    visible.append(child)
                if param_types.get(child) == "select":
                    child_map: dict[str, list[str]] = {}
                    for child_opt in param_options.get(child) or []:
                        kids = children_of.get((child, str(child_opt)), [])
                        if kids:
                            child_map[str(child_opt)] = list(kids)
                    if child_map:
                        nested[child] = child_map
            per_option[str(opt)] = {"params": visible}
            if nested:
                per_option[str(opt)]["nested_selects"] = nested
        per_option["_options"] = options
        if param_defaults.get(sel) is not None:
            per_option["_default"] = param_defaults[sel]
        groups[sel] = per_option

    out: dict[str, Any] = {"param_schema": ordered}
    if when:
        out["visibility"] = {"always": always, "when": when}
    if groups:
        out["param_groups_by_select"] = groups
    return out


def _enrich_action_card(card: dict) -> dict:
    if not isinstance(card, dict) or card.get("type") != "action_card":
        return card
    connector = card.get("connector")
    operation = card.get("operation")
    if not connector or not operation:
        return card
    meta = _action_card_param_metadata(str(connector), str(operation))
    if not meta:
        return card
    # Preserve explicit fields if a future emitter supplies them itself.
    return {**meta, **card}


def _humanize_age(iso: str) -> str:
    """'2026-07-29T15:05:16Z' -> 'on 2026-07-29 (5 days ago)'. Falls back to the
    raw stamp; a wrong-looking date beats a crash on a card."""
    import datetime
    try:
        s = iso.replace("Z", "+00:00")
        when = datetime.datetime.fromisoformat(s)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - when
        mins = int(delta.total_seconds() // 60)
        if mins < 1:
            rel = "just now"
        elif mins < 60:
            rel = f"{mins} min ago"
        elif mins < 60 * 24:
            rel = f"{mins // 60}h ago"
        else:
            rel = f"{mins // (60 * 24)}d ago"
        return f"{when.date().isoformat()} ({rel})"
    except Exception:  # noqa: BLE001
        return str(iso)


def _playbook_run_context(playbook: str, playbook_uuid: str = "") -> dict | None:
    """Recent-run facts for a playbook, for the approval card: when it last ran
    and how often in the last 24h.

    Why this is on the card and not left to the analyst's memory: approving
    "run this playbook" without knowing it already ran 30 times today (or has
    never run at all) is approving blind.

    Both filters here are VERIFIED to bite on a live appliance -- `template_iri`
    (1244 rows -> 21) and `modified_after` (1244 -> 139). `name=`, `template=`
    and `workflow=` are silently ignored by the endpoint and return the full
    unfiltered set, which would have produced confident nonsense. Run `name` is
    NOT the playbook name either (rows read like "> device step (Running) -
    ...") so matching by name client-side is not a fallback.

    Fail-open: any trouble returns None and the card renders exactly as before.
    """
    from urllib.parse import quote
    try:
        # `_shared` is NOT a module-level name in operations.py -- referencing
        # it as one raises NameError, which this function's own except would
        # swallow into a permanent None. That failure is invisible: the card
        # renders exactly as it did before the feature existed.
        from fsr_playbooks.mcp_server import _shared as _sh
        client = _sh._live_client()
        if client is None:
            return None
        uuid_ = (playbook_uuid or "").strip()
        if not uuid_:
            if not playbook:
                return None
            resp = client.get("/api/3/workflows?"
                              + f"name={quote(playbook)}&$limit=5")
            members = (resp.get("hydra:member")
                       if isinstance(resp, dict) else None) or []
            match = next((m for m in members
                          if (m.get("name") if isinstance(m, dict) else None)
                          == playbook), members[0] if members else None)
            uuid_ = (match or {}).get("uuid") or ""
        if not uuid_:
            return None
        iri = quote(f"/api/3/workflows/{uuid_}", safe="/")
        base = (client.base_url
                + "/api/wf/api/historical-workflows/?format=json"
                + "&ordering=-modified&parent_wf__isnull=True"
                + f"&template_iri={iri}")

        def _get(url):
            r = client.session.get(url, verify=client.verify_ssl, timeout=6)
            return r.json() if r.status_code == 200 else {}

        body = _get(base + "&limit=1")
        members = body.get("hydra:member") or []
        total = body.get("hydra:totalItems")
        last = (members[0].get("modified") if members else None)

        import datetime
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        recent = _get(base + "&limit=1&modified_after="
                      + quote(since, safe=":"))
        last_24h = recent.get("hydra:totalItems")
        # The analyst-facing URL for the playbook itself, so the card can link
        # out to what is being proposed. `/playbooks/<workflow-uuid>` is the
        # single-workflow editor route (the same one `fsrpb pull` accepts);
        # base_url is the appliance the connector is configured against, so the
        # link always points at the box the run would happen on.
        url = f"{str(client.base_url).rstrip('/')}/playbooks/{uuid_}"
        return {
            "playbook_uuid": uuid_,
            "playbook_url": url,
            "last_run": _humanize_age(last) if last else "never",
            "last_status": (members[0].get("status") if members else None),
            "runs_24h": last_24h if isinstance(last_24h, int) else None,
            "runs_total": total if isinstance(total, int) else None,
        }
    except Exception:  # noqa: BLE001
        return None


def _status_card_from_tool_result(w: dict, tool_name: str | None,
                                  idx: int) -> dict | None:
    """Map a tool_result into a non-gating `status_card` (contract 2.1.0 §5
    alias) when it carries connector-health information -- either a `run_op`
    preflight failure (`connector_unhealthy` / `connector_not_configured`) or
    an explicit `healthcheck_connector` answer. Returns None otherwise."""
    if w.get("type") != "tool_result":
        return None
    c = w.get("content")
    if not isinstance(c, dict):
        return None

    code = c.get("code")
    if code in _HEALTH_FAIL_CODES:
        connector = c.get("connector") or "connector"
        title = (f"{connector} is not configured"
                 if code == "connector_not_configured"
                 else f"{connector} is unavailable")
        details = [{"label": "Connector", "value": connector}]
        if c.get("health_status"):
            details.append({"label": "Status", "value": c["health_status"]})
        if c.get("version"):
            details.append({"label": "Version", "value": c["version"]})
        sugg = c.get("suggestions") or []
        if sugg:
            details.append({"label": "What to do", "value": sugg[0]})
        return _as_frame(StatusCardFrame, {
            "type": "status_card", "id": f"status-{idx}",
            "variant": "connector_health", "severity": "error",
            "title": title, "summary": c.get("message") or title,
            "details": details})

    # Explicit healthcheck_connector answer → ok/error status_card.
    if tool_name == "healthcheck_connector" and c.get("name"):
        status = c.get("status") or c.get("http_status") or "unknown"
        ok = isinstance(status, str) and status.strip().lower() in _HEALTHY_STATUSES
        connector = c.get("name")
        details = [{"label": "Connector", "value": connector},
                   {"label": "Status", "value": status}]
        if c.get("version"):
            details.append({"label": "Version", "value": c["version"]})
        return _as_frame(StatusCardFrame, {
            "type": "status_card", "id": f"status-{idx}",
            "variant": "connector_health",
            "severity": "ok" if ok else "error",
            "title": f"{connector} is {'available' if ok else 'unavailable'}",
            "summary": c.get("message") or f"Healthcheck status: {status}",
            "details": details})
    return None


def _push_card_from_tool_result(w: dict, tool_name: str | None,
                                idx: int) -> dict | None:
    """Map a successful `push_playbook` result into a 'playbook pushed'
    info_card (contract 2.1.0 §5)."""
    if w.get("type") != "tool_result" or tool_name != "push_playbook":
        return None
    c = w.get("content")
    if not isinstance(c, dict) or not c.get("ok") or not c.get("collection_uuid"):
        return None
    wfs = c.get("workflows") or []
    rows = [{"label": "Collection", "value": c.get("collection_name") or "--"},
            {"label": "Workflows", "value": str(len(wfs))},
            {"label": "UUID", "value": c.get("collection_uuid")}]
    action = c.get("action")
    blocks = [{"kind": "rows", "rows": rows}]
    if wfs:
        blocks.append({"kind": "tags",
                       "tags": [w.get("name") for w in wfs if w.get("name")][:8]})
    return _as_frame(InfoCardFrame, {
        "type": "info_card", "id": f"pushed-{idx}", "variant": "playbook_pushed",
        "severity": "ok",
        "title": f"Playbook pushed: {c.get('collection_name') or 'collection'}"
                 + (f" ({action})" if action else ""),
        "blocks": blocks})


def _manual_field_to_widget(f: dict) -> dict:
    """Map one ``_pending_input`` field (``tools_playbook_run._shape_field``) to the
    control the widget's manual_input card renders. ``checkbox``/boolean → a
    ``checkbox``; a field carrying choices → a ``select``; everything else → a
    text input.

    **A `dynamicList` is only sometimes two-level.** FortiSOAR's dynamicList
    renders whatever its (runtime-computed) `options` array holds: a list of
    GROUPS -- ``[{"value": <manager>, "options": [<device>, …]}]`` -- gets the
    dependent manager→device picker, but a FLAT list of scalars gets a single
    dropdown. This mapper used to assume the grouped shape for every
    dynamicList, and a flat one broke twice over on the demo box (2026-08-05,
    "Add Steps to Device from Profile"): the widget drew TWO dropdowns for one
    choice, and since `dynamicListItems` finds no nested `options` on a string
    group, the second select stayed empty, nothing could be picked, and the
    widget's collapse (`[group, item]` only when BOTH are set) submitted `[]`.
    The gate resolved `status: finished` with `input.profile = []` and the run
    failed downstream -- a silent wrong answer, not an error. So switch on the
    SHAPE of the options, not on the declared data type."""
    dt = str(f.get("data_type") or "").lower()
    ftype = str(f.get("type") or "").lower()
    opts = f.get("options")
    opts = opts if isinstance(opts, list) else []
    # Grouped ⇔ at least one entry is a dict with its own nested `options` list.
    grouped = any(isinstance(o, dict) and isinstance(o.get("options"), list)
                  for o in opts)
    out = {"name": f.get("name"),
           "label": f.get("label") or f.get("name"),
           "tooltip": f.get("tooltip")}
    if f.get("required") is not None:
        out["required"] = bool(f.get("required"))
    if dt == "checkbox" or ftype == "boolean":
        out["type"] = "checkbox"
    elif dt == "dynamiclist" and grouped:
        out["type"] = "dynamic_list"
        out["groups"] = opts
    elif opts:
        # Flat choices -- a dynamicList over scalars, or any select/picklist
        # field. The widget's `select` control renders bare strings and
        # `{value,label}` alike, and submits the picked value as a SCALAR,
        # which is what a playbook consuming `…input.<field>` expects.
        out["type"] = "select"
        out["options"] = opts
    else:
        out["type"] = "text"
    return out


def _manual_input_card_from_awaiting(content: dict, preceding_tool_use_id: str | None,
                                     idx: int) -> dict | None:
    """Synthesize a gating ``manual_input`` card from a ``run_playbook`` /
    ``resume_playbook`` result that paused on a form (``code == awaiting_input``
    with an ``awaiting`` block carrying ``fields``). The card truncates the turn
    (stop_reason ``awaiting_manual_input``) so the analyst fills the form in-chat;
    submit rides ``respond_manual_input`` → pyfsr ``answer()``. Returns None when
    the pause isn't the analyst's to answer (``is_analyst_form``: no fields and
    a single button, which the model may press itself)."""
    if not isinstance(content, dict) or content.get("code") != "awaiting_input":
        return None
    form = content.get("awaiting")
    if not isinstance(form, dict):
        return None
    from fsr_playbooks.mcp_server.tools_playbook_run import is_analyst_form
    if not is_analyst_form(form):
        return None
    fields = [f for f in (form.get("fields") or []) if isinstance(f, dict)]
    run_pk = content.get("run_pk") or form.get("run_pk")
    title = form.get("title") or "Playbook input needed"
    desc = form.get("description")
    question = f"{title}\n\n{desc}" if desc else title
    # The gate's deadline, when the step sets one. A manual_input step can carry
    # its own timeout (the ZTPF metadata-source playbook: one minute); once it
    # lapses the run continues down its timeout branch and a later submit can
    # never land. The card said nothing about that, so the analyst read at
    # leisure and the answer arrived after the run had moved on.
    left = form.get("expires_in_seconds")
    if isinstance(left, int) and left > 0:
        question += (f"\n\n⏱ This form expires in about "
                     f"{left // 60}m {left % 60}s -- submit before then or the "
                     "playbook continues without your answer.")
    return _as_frame(ManualInputFrame, {
        "type": "manual_input",
        "id": str(form.get("manual_input_id") or ""),
        "question": question,
        "expires_at": form.get("expires_at"),
        "expires_in_seconds": left,
        "workflow_run_iri": (f"/wf/api/workflows/{run_pk}/" if run_pk else None),
        "fields": [_manual_field_to_widget(f) for f in fields],
        # The gate's own buttons, in the playbook's order. The widget renders
        # one per label and sends the label back, so a choice like "Save
        # Example" or "Save as YAML" reaches the run -- not just the primary.
        "buttons": [{"label": o.get("option"), "primary": bool(o.get("primary"))}
                    for o in (form.get("options") or [])
                    if isinstance(o, dict) and o.get("option")],
        "preceding_tool_use_id": preceding_tool_use_id,
    })


def _run_card_from_tool_result(w: dict, tool_name: str | None,
                               idx: int) -> dict | None:
    """Map a `run_playbook` / `dry_run_playbook` result into a run-result
    info_card with a failed-step table when it didn't finish clean."""
    if w.get("type") != "tool_result" or tool_name not in (
            "run_playbook", "dry_run_playbook"):
        return None
    c = w.get("content")
    if not isinstance(c, dict) or "status" not in c:
        return None
    status = c.get("status")
    ok = bool(c.get("ok")) or status == "finished"
    rows = [{"label": "Status", "value": status,
             "severity": "ok" if ok else "error"}]
    if c.get("task_id"):
        rows.append({"label": "Task ID", "value": str(c.get("task_id"))})
    if c.get("error_message"):
        rows.append({"label": "Error", "value": str(c.get("error_message")),
                     "severity": "error"})
    blocks: list[dict] = [{"kind": "rows", "rows": rows}]
    failed = c.get("failed_steps") or []
    if failed:
        blocks.append({"kind": "table", "columns": ["Step", "Status", "Error"],
                       "rows": [[f.get("name") or "--", f.get("status") or "--",
                                 (f.get("error") or "")[:120]] for f in failed[:8]]})
    return _as_frame(InfoCardFrame, {
        "type": "info_card", "id": f"run-{idx}", "variant": "run_result",
        "severity": "ok" if ok else "error",
        "title": f"Playbook run: {status}", "blocks": blocks})


# IOC / threat-intel enrichment card synthesis lives in wire_enrichment.py.
# _ioc_extract is re-exported for its unit test (operations._ioc_extract).

# Threat-intel sources whose rating the autonomy policy may act on: the ones
# with a purpose-built extractor. The generic fallback scans arbitrary field
# names (a "category" containing "high" rates as malicious) -- good enough for
# a display card, never for an unattended block.
_POLICY_RATED_SOURCES = frozenset({"VirusTotal", "FortiGuard", "AbuseIPDB",
                                   "Shodan", "FortiSIEM"})


def _fsm_reputation(params: dict, data: Any) -> dict | None:
    """FortiSIEM MCP `get_reputation_by_entity`: a list of per-entity records,
    each with `malwareGroups[{verdict, source}]` -- 'Safe (IOC not found)' or
    'Malware ...' per feed (live shape, v1.24). No feed answered = no rating."""
    rows = data if isinstance(data, list) else [data]
    groups = [g for r in rows if isinstance(r, dict)
              for g in (r.get("malwareGroups") or []) if isinstance(g, dict)]
    if not groups:
        return None
    hits = [g for g in groups
            if str(g.get("verdict") or "").strip().lower().startswith("malware")]
    q = params.get("params") if isinstance(params.get("params"), dict) else params
    ind = next((v[0] if isinstance(v, list) and v else v
                for k in ("ip", "domain", "url", "hash")
                if (v := q.get(k))), None)
    if not ind and rows and isinstance(rows[0], dict):
        ind = rows[0].get("ip") or rows[0].get("domain")
    return {"indicator": str(ind or "indicator"), "source": "FortiSIEM",
            "verdict": "malicious" if hits else "clean",
            "score_str": f"{len(hits)}/{len(groups)} feeds",
            "severity": "error" if hits else "ok",
            "tags": sorted({str(g.get("source")) for g in hits if g.get("source")})}


# Readers for the MCP tools a config declares as intel lookups, by product.
_MCP_INTEL_READERS = {("fortisiem", "get_reputation_by_entity"): _fsm_reputation}


def _policy_result_reader(connector: str, op: str, params: dict,
                          result: dict) -> dict | None:
    """What a threat-intel lookup said about its indicator, for the autonomy
    policy's evidence check. None when the source has no dedicated reader.
    A declared MCP intel tool arrives as connector `mcp:<server>`."""
    if connector.startswith("mcp:"):
        try:
            from fsr_playbooks.mcp_server.materializer import turn_intel_mcp_tools
            product = turn_intel_mcp_tools().get(f"mcp_{connector[4:]}__{op}")
        except Exception:  # noqa: BLE001 - older framework
            return None
        reader = _MCP_INTEL_READERS.get((product or "", op))
        if reader is None or not (result or {}).get("ok"):
            return None
        return reader(params if isinstance(params, dict) else {}, result.get("data"))
    rec = _ioc_extract(connector, op, params, result)
    if rec and rec.get("source") in _POLICY_RATED_SOURCES:
        return rec
    return None


try:  # an older framework has no reader hook; the policy then fails closed
    from fsr_playbooks.mcp_server._citation_validator import set_result_reader
    set_result_reader(_policy_result_reader)
except ImportError:
    pass


# Emitted cards that report rather than gate: spliced where they land and never
# the truncation point while a gating card exists (see `_wire_transcript`).
_NON_GATING_EMITTED_CARDS = frozenset({"verdict_card"})

# On an unattended turn (auto-triage, a scheduled hunt) nobody can answer a
# question, so these cards report rather than gate: they stay in the transcript
# where they landed and the turn runs on to its verdict instead of being cut
# off at them and parked on an answer that never comes.
_UNATTENDED_INFORMATIONAL_CARDS = frozenset({"choice_card", "capability_gap"})


def _non_gating_cards(unattended: bool = False) -> frozenset:
    return (_NON_GATING_EMITTED_CARDS | _UNATTENDED_INFORMATIONAL_CARDS
            if unattended else _NON_GATING_EMITTED_CARDS)


def _splice_verdict_cards(wired: list[dict], kinds: frozenset = _NON_GATING_EMITTED_CARDS
                          ) -> "tuple[list[dict], set[int]]":
    """Insert each DELIVERED non-gating card (a verdict; on an unattended turn
    also a choice or capability gap) right after its tool_result. Returns
    the new list and the `id()`s of the inserted cards. A refused or deferred
    emit (`ok: false`, or a guard's `guard_defer`) carries no card."""
    out: list[dict] = []
    inserted: set[int] = set()
    pending: dict[Any, tuple[dict, Any]] = {}
    last_visible = None
    for w in wired:
        out.append(w)
        emitted = _emitted_card(w)
        if emitted is not None and emitted[0] in kinds:
            pending[w.get("id")] = (emitted[1], last_visible, emitted[0])
            continue
        if w.get("type") == "tool_use" and emitted is None:
            last_visible = w.get("id")
        if w.get("type") != "tool_result" or w.get("tool_use_id") not in pending:
            continue
        fields, preceding, ctype = pending.pop(w.get("tool_use_id"))
        content = w.get("content")
        if not isinstance(content, dict) or content.get("ok") is not True \
                or content.get("kind") in ("guard_defer", "guard_redirect"):
            continue
        if isinstance(content.get("card"), dict):
            card = dict(content["card"])
        else:
            payload = {"type": ctype, **fields}
            model = _frame_model_for(ctype)
            card = _as_frame(model, payload) if model is not None else payload
        card.setdefault("preceding_tool_use_id", preceding)
        out.append(card)
        inserted.add(id(card))
    return out, inserted


# Card types that satisfy the "turn produced a deliverable" check. A bare
# non-gating status_card is deliberately NOT here -- a hunt that hit one
# unconfigured connector still owes a findings card.
_DELIVERABLE_CARD_TYPES = frozenset({
    "info_card", "ioc_card", "action_card", "approval_request", "verdict_card",
    "capability_gap", "playbook_offer", "choice_card", "manual_input",
})


def _triage_summary_fallback_card(wired: list[dict]) -> dict | None:
    """Synthesize a terminal `info_card` from the turn's assistant text when a
    native-MCP investigation produced no deliverable card. Returns None unless
    ALL hold: no deliverable-family card already present; >=1 native MCP
    (`mcp_*`) investigation tool_use in the turn; and >=120 chars of assistant
    text (a real verdict, not a one-line ack). Scoped to native MCP so it never
    fires on build turns (analyze_playbook/verify_*) or changes run_op triage
    (which already cards via _ioc_cards)."""
    if any(w.get("type") in _DELIVERABLE_CARD_TYPES for w in wired):
        return None
    used_mcp = any(w.get("type") == "tool_use"
                   and str(w.get("name") or "").startswith("mcp_")
                   for w in wired)
    if not used_mcp:
        return None
    text = "".join(w.get("text") or "" for w in wired
                   if w.get("type") == "text").strip()
    if len(text) < 120:
        return None
    return _as_frame(InfoCardFrame, {
        "type": "info_card", "id": "triage-summary",
        "variant": "triage_summary", "severity": "info",
        "title": "Triage summary",
        "blocks": [{"kind": "text", "text": text}]})


def _approval_to_action_card(w: dict, call_args: dict | None = None) -> dict | None:
    """Render a tier-3 approval_request as an `action_card` so the agent never
    has to choose emit_action_card explicitly, AND so the approval is persisted
    as a dispatchable card.

    EVERY tier-3 tool gets a card, not just `run_op`. This used to bail with
    None for any other tool, which looked like a rendering choice and was
    actually a silent dead-end: a non-run_op approval stayed a legacy
    `approval_request`, `approval_request` is not in `_GATE_TYPES`, so the card
    was never persisted -- and on approve, `_execute_approved_card` found no
    stored card and fell back to merely *telling* the model the analyst had
    approved. Live on .206 an analyst approved `run_playbook("Add Step from
    Action to Device")` and nothing ran at all: no playbook run on the box, no
    tool_result in the transcript, the turn simply ended. The `run_op` family
    was fixed for exactly this class in 2.5.1; every other tier-3 tool
    (run_playbook, resume_playbook, the mcp_* tools) still had the hole.

    `tool` + `args` are what the executor dispatches. `connector`/`operation`
    stay on run_op cards because the widget renders those fields and
    `_action_card_param_metadata` keys off them.
    """
    tool = w.get("tool")
    if tool != "run_op" and tool not in (
            "create_record", "update_record", "delete_record"):
        # Deliberately NOT converted. Measured in the widget (Playwright, both
        # surfaces side by side): the legacy `approval_request` rendering is the
        # BETTER one for a tool-only approval -- it shows a labelled arg table
        # ("Playbook | Add Step from Action to Device", "Follow | true"), a raw-
        # args toggle and a note field. The `action_card` body is driven by
        # connector param metadata, so a tool-only card renders as a bare title
        # chip plus Confirm/Cancel with no arguments visible at all. Converting
        # would have made the approval surface strictly worse.
        #
        # The execution bug that led here is fixed at the OTHER end: the
        # approval_request is now persisted as a card (see the persist
        # chokepoint) and `_execute_approved_card` dispatches it by tool.
        #
        # Record-write tools (create/update/delete_record) ARE converted --
        # #78: their legacy approval_request showed a raw {module, uuid,
        # fields} blob; the action_card shape carries target/changes/
        # context_match so the analyst sees what is changing, not a uuid.
        return None
    # `call_args` is the suspended call's own arguments (its tool_use input).
    # `preview.args` is a DISPLAY copy: the framework cuts strings at 600 chars
    # ("… [+23 chars truncated]") and masks secrets as "***". Live, a template
    # script 623 chars long was approved from a card built on the preview, and
    # the truncated text was written into the record: approving executes the
    # card's args. The preview is only the fallback when no call is found.
    preview = w.get("preview") or {}
    a = call_args if isinstance(call_args, dict) else (preview.get("args") or {})
    if not isinstance(a, dict):
        return None
    card = {
        "type": "action_card",
        # card_id == approval_id so the orphaned SuspendedSession can be
        # cleaned up on resume; resume executes deterministically by card_id.
        "id": w.get("approval_id") or w.get("tool_use_id"),
        "approval_id": w.get("approval_id"),
        "tool": tool,
        # This card IS the suspended call -- nest it under that same tool_use
        # in the widget rather than a separate emit_ step.
        "preceding_tool_use_id": w.get("tool_use_id"),
    }
    if tool == "run_op":
        connector = a.get("connector")
        operation = a.get("op") or a.get("operation")
        params = a.get("params") or {}
        if not (connector and operation):
            return None
        if not isinstance(params, dict):
            params = {}
        card.update({
            "connector": connector,
            "operation": operation,
            "summary": _clean_run_op_summary(
                w.get("summary"), connector, operation),
            "args": params,
            "editable_fields": list(params.keys()),
        })
    elif tool in ("create_record", "update_record", "delete_record"):
        shaped = _shape_record_write_card(tool, a, w)
        if shaped is None:
            return None
        card.update(shaped)
    batch = _shape_card_batch(w.get("batch"))
    if batch:
        card["batch"] = batch
    # The shadow decision dispatch made for this call. The card IS the call,
    # so the decision moves with it; dropped here, a run_op the model called
    # directly reached the Monitor's shadow table with no decision at all.
    if isinstance(w.get("policy"), dict):
        card["policy"] = w["policy"]
    return _as_frame(ActionCardFrame, card)


def _shape_card_batch(batch: Any) -> list[dict]:
    """The approval's batched calls as card rows.

    Each row keeps the raw call (`tool`, `args`) -- that is what
    `_execute_approved_card` dispatches on approve -- plus the same display
    shape the lead call gets (`summary`, and for record writes `target` /
    `changes`), so the analyst reads what every call on the card will do.
    """
    rows: list[dict] = []
    for item in batch or []:
        if not isinstance(item, dict) or not item.get("tool"):
            continue
        tool = str(item["tool"])
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        row: dict[str, Any] = {"tool": tool, "args": args,
                               "tool_use_id": item.get("tool_use_id"),
                               "summary": item.get("summary")}
        if tool in ("create_record", "update_record", "delete_record"):
            shaped = _shape_record_write_card(tool, args, item) or {}
            row["summary"] = row["summary"] or shaped.get("summary")
            for k in ("target", "changes"):
                if shaped.get(k) is not None:
                    row[k] = shaped[k]
        rows.append(row)
    return rows


def _shape_record_write_card(tool: str, args: dict,
                             approval: dict) -> dict | None:
    """Shape a record-write approval as an action_card with target/changes.

    #78: the legacy approval_request showed a raw args blob (``{module, uuid,
    fields}``). The analyst had to read a uuid to know what was about to change.
    This shapes the card so the widget can render:

      * ``target`` -- {module, uuid, name} so the card identifies the record
        instead of showing a bare uuid.  The name is resolved best-effort from
        the turn's entity (same record → "same"; different → "different").
      * ``changes`` -- a per-field diff [{field, before, after}] for updates,
        or [{field, after}] for creates, instead of an args dump.
      * ``context_match`` -- "same" | "different" | "unknown" vs the turn's
        entity, so the widget can show a confirm or a warning.

    For ``delete_record`` the target is the whole point -- no changes list.
    """
    module = args.get("module") or ""
    uuid_ = args.get("uuid") or ""
    fields = args.get("fields") or {}

    if not module:
        return None

    noun = _module_noun(module)
    # `module_label` is what the card prints; `module` and `uuid` stay for the
    # widget's own use and are never shown.
    target: dict[str, Any] = {"module": module, "module_label": noun.capitalize()}
    if uuid_:
        target["uuid"] = uuid_

    context_match = _context_match(module, uuid_)

    current = _current_record(module, uuid_) if tool in ("update_record", "delete_record") else None
    name = _record_name(current)
    if name:
        target["name"] = name
    # The fallback title names the record, never its uuid: "Update alerts/e94dc2dc"
    # was the whole header on a live card. A model summary loses its IRIs too.
    subject = f"{noun}: {name}" if name else f"this {noun}"
    model_summary = _derecord(approval["summary"]) if approval.get("summary") else ""

    if tool == "update_record":
        # Live, a "create a new template" request became an update of the
        # template on screen: rename, new script, new links. The card said only
        # "Update ztpf_templates/7d762bbe" with the new values, so nothing on it
        # showed an existing record being overwritten. With the record's name and
        # each field's current value, it does.
        changes = []
        for k, v in (fields or {}).items():
            row = {"field": k, "after": _display_new_value(v)}
            if isinstance(current, dict):
                row["before"] = _display_value(current.get(k))
            changes.append(row)
        return {
            "summary": model_summary or f"Update {subject}",
            "args": fields,
            "editable_fields": list((fields or {}).keys()),
            "target": target,
            "changes": changes,
            "context_match": context_match,
        }
    if tool == "create_record":
        changes = [{"field": k, "after": _display_new_value(v)} for k, v in (fields or {}).items()]
        return {
            "summary": model_summary or f"Create {'an' if noun[:1] in 'aeiou' else 'a'} {noun}",
            "args": fields,
            "editable_fields": list((fields or {}).keys()),
            "target": target,
            "changes": changes,
            "context_match": context_match,
        }
    if tool == "delete_record":
        return {
            "summary": model_summary or f"Delete {subject}",
            "args": args,
            "editable_fields": [],
            "target": target,
            "context_match": context_match,
        }
    return None


def _always_allow_offer(playbook: str) -> dict | None:
    """``{persona, playbook}`` when the run card can offer "Always allow": the
    turn has a persona and ``playbook`` is not already one it runs without
    asking. The widget shows the extra button only when this is present."""
    if not playbook:
        return None
    try:
        from fsr_soc_triage.profiles import get_active_profile
        prof = get_active_profile()
    except Exception:  # noqa: BLE001
        return None
    if prof is None or not getattr(prof, "module", None) or prof.may_autorun_playbook(playbook):
        return None
    return {"persona": prof.module, "playbook": playbook}
