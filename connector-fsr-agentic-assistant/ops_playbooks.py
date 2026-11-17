"""playbooks domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
from typing import Any
from .pydantic_models import (
    QueryStoreParams,
    DryRunPlaybookParams, RenderJinjaParams,
    ValidateYamlParams, CompileYamlParams, ResolveYamlParams,
)

from .ops_common import (
    _check_params, _err, _import_fsr_playbooks, _reference_db_path, _require,
    _require_transport, _serialize_errors,
)
from .ops_playbooks_persist import push_playbook



# ---------- compiler pass-throughs ----------------------------------------

def compile_yaml(config: dict, params: dict) -> dict:
    _check_params(CompileYamlParams, params)
    fsr_playbooks = _import_fsr_playbooks()
    yaml_text = _require(params, "yaml")
    result = fsr_playbooks.compile_yaml(yaml_text, _reference_db_path())
    return {
        "ok": result.ok,
        "workflow_json": result.fsr_json if result.ok else None,
        "errors": _serialize_errors(result.errors),
    }


def validate_yaml(config: dict, params: dict) -> dict:
    _check_params(ValidateYamlParams, params)
    fsr_playbooks = _import_fsr_playbooks()
    yaml_text = _require(params, "yaml")
    coll, errs = fsr_playbooks.parse_yaml(yaml_text)
    if errs:
        return {"ok": False, "errors": _serialize_errors(errs)}
    # parse-time was clean; run the validator pass for the deeper checks.
    deep = list(fsr_playbooks.validate(coll)) if coll is not None else []
    # ...then the RESOLVER pass, which owns the per-handler argument checks.
    #
    # Those checks used to live in the parser: any step key it didn't consume
    # was reported as dropped, which is how a create_record whose record body
    # was written under the wrong key got flagged before a push. Phase G hoisted
    # step arguments to the step's top level, so the parser no longer has a
    # "leftover key" concept -- unknown-argument detection moved into the
    # resolver's per-handler normalizers. `validate_yaml` never ran the resolver,
    # so it silently stopped catching that whole class: a create_record with a
    # misnamed record body validated clean and failed only at runtime
    # (`insert_data() takes at least 2 positional arguments`), which is the exact
    # bug the linter was added for.
    if coll is not None:
        from fsr_playbooks.compiler.errors import CompileError, ErrorCode
        from fsr_playbooks.compiler.resolver import Resolver
        try:
            resolver = Resolver(_reference_db_path())
        except Exception as e:  # noqa: BLE001 -- no reference store: keep the
            # structural verdict rather than failing the whole lint. Surfaced,
            # not swallowed, so a missing store can't masquerade as "clean".
            deep.append(CompileError(
                code=ErrorCode.BAD_VALUE,
                message=(f"reference store unavailable, so connector/argument "
                         f"checks did NOT run: {e}"),
                path="", severity="warning"))
        else:
            try:
                deep.extend(resolver.resolve(coll))
            finally:
                resolver.close()
    return {
        "ok": not [d for d in deep
                   if getattr(d, "severity", "error") != "warning"],
        "errors": _serialize_errors(deep),
    }


def resolve_yaml(config: dict, params: dict) -> dict:
    """Parser + resolver only -- returns the IR shape, not FSR JSON."""
    _check_params(ResolveYamlParams, params)
    fsr_playbooks = _import_fsr_playbooks()
    from fsr_playbooks.compiler.resolver import Resolver
    yaml_text = _require(params, "yaml")
    coll, errs = fsr_playbooks.parse_yaml(yaml_text)
    if errs:
        return {"ok": False, "errors": _serialize_errors(errs)}
    if coll is None:
        return {"ok": False, "errors": [{"code": "empty_collection", "message": "no playbooks in source"}]}
    resolver = Resolver(_reference_db_path())
    try:
        resolver.resolve(coll)
    finally:
        resolver.close()
    return {"ok": True, "ir": _ir_to_dict(coll), "errors": []}


def _ir_to_dict(coll) -> dict:
    """Best-effort IR serialization. The IR is a dataclass tree; the
    full shape isn't part of the connector's public contract yet, so we
    project a small subset (playbook + step names/uuids) for now."""
    out = {"playbooks": []}
    for pb in getattr(coll, "playbooks", []) or []:
        out["playbooks"].append({
            "name": getattr(pb, "name", None),
            "uuid": getattr(pb, "uuid", None),
            "steps": [
                {"name": getattr(s, "name", None), "type": getattr(s, "type", None),
                 "uuid": getattr(s, "uuid", None)}
                for s in (getattr(pb, "steps", None) or [])
            ],
        })
    return out


def render_jinja(config: dict, params: dict) -> dict:
    """Render a Jinja template through FortiSOAR's own Jinja editor
    endpoint so platform-custom filters (`| tojson`, `| b64encode`,
    `| yaql`, ...) all resolve. Pure passthrough -- no fsr_playbooks."""
    _check_params(RenderJinjaParams, params)
    template = _require(params, "template")
    context = params.get("context") or {}
    from_pb = params.get("from_pb_execution")

    mr, err = _require_transport(
        "render_jinja requires the FortiSOAR runtime; "
        "integrations.crudhub.make_request is not importable.")
    if err:
        return err

    values: dict[str, Any] = {}
    if from_pb:
        # Pull vars from the prior run. /api/wf/api/workflows/?task_id=...
        # returns a hydra collection; /api/wf/api/workflows/<pk>/ returns
        # the full record. We try task_id first, fall back to treating
        # the input as a pk.
        try:
            if from_pb.isdigit():
                run = mr(f"/api/wf/api/workflows/{from_pb}/", "GET")
            else:
                listing = mr(
                    "/api/wf/api/workflows/"
                    f"?task_id={from_pb}&parent_wf__isnull=True&limit=1",
                    "GET",
                )
                members = (listing or {}).get("hydra:member") or []
                run = members[0] if members else None
        except Exception as e:
            return _err("from_pb_lookup_failed", f"{e}", from_pb_execution=from_pb)
        if not run:
            return _err("from_pb_not_found",
                        f"no workflow execution matched {from_pb!r}")
        run_vars = (run.get("result") or {}).get("vars") or run.get("vars") or {}
        if run_vars:
            values["vars"] = run_vars
    # Caller-supplied context wins; merge `vars` deeply so a partial
    # override doesn't wipe steps from the run.
    for k, v in (context or {}).items():
        if k == "vars" and isinstance(v, dict) and isinstance(values.get("vars"), dict):
            values["vars"] = {**values["vars"], **v}
        else:
            values[k] = v

    try:
        from connectors.cyops_utilities.crudhub import make_cyops_request  # type: ignore
        resp = make_cyops_request("/api/wf/api/jinja-editor/?format=json", "POST",
                                  body={"template": template, "values": values})
    except Exception as e:
        return _err("jinja_editor_failed", f"{e}")

    # FSR returns either a dict {result: ...} or a raw value. Normalize.
    if isinstance(resp, dict):
        for k in ("result", "output", "rendered", "value"):
            if k in resp:
                return {"ok": True, "output": resp[k]}
        return {"ok": True, "output": resp}
    return {"ok": True, "output": resp}


def query_store(config: dict, params: dict) -> dict:
    """Run a read-only SQL query against the connector's reference store
    (fsr_reference.db) and return rows as a list of dicts. SELECT only --
    any statement that isn't a SELECT is rejected."""
    _check_params(QueryStoreParams, params)
    sql = (_require(params, "sql") or "").strip()
    if not sql.upper().startswith("SELECT"):
        return _err("not_select", "Only SELECT statements are allowed.")
    limit = min(int(params.get("limit") or 200), 1000)
    db_path = _reference_db_path()
    import sqlite3
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql)
            rows = [dict(r) for r in cur.fetchmany(limit)]
            col_names = [d[0] for d in cur.description] if cur.description else []
        return {"ok": True, "rows": rows, "count": len(rows),
                "columns": col_names, "db_path": str(db_path)}
    except sqlite3.OperationalError as e:
        return _err("sql_error", str(e), db_path=str(db_path))


def dry_run_playbook(config: dict, params: dict) -> dict:
    """Push a compiled WorkflowCollection, trigger one of its
    playbooks, poll until it stops progressing on its own, and optionally
    hard-purge the pushed collection. End-to-end smoke check usable from a
    widget.

    "Stops progressing" means terminal OR `awaiting` -- a run parked on a
    manual-input gate is waiting on a human and will never move without one,
    so returning `status="awaiting"` (with `awaiting_steps`) is the honest
    answer, not a timeout."""
    _check_params(DryRunPlaybookParams, params)
    workflow_json = _require(params, "workflow_json")
    playbook_name = params.get("playbook_name")
    inputs = params.get("inputs") or {}
    timeout_s = int(params.get("timeout_s") or 180)
    cleanup = bool(params.get("cleanup", True))
    use_mock_output = bool(params.get("use_mock_output", False))

    mr, err = _require_transport("dry_run_playbook requires the FortiSOAR runtime.")
    if err:
        return err

    # 1. Push via the conflict-safe path (mints a fresh uuid / uniquifies the
    # name on a uniqueness 409 incl. recycle-bin / cross-team clashes) so a
    # dry-run never dies on a name already in use. push_playbook returns the
    # created record's uuid + workflows under `raw`.
    pushed = push_playbook(config, {"workflow_json": workflow_json,
                                    "overwrite": True})
    if not pushed.get("ok"):
        return _err("push_failed", f"{pushed.get('error') or pushed}",
                    stage="push")
    push_resp = pushed.get("raw") or {}
    coll_uuid = pushed.get("collection_uuid") or (push_resp or {}).get("uuid")
    workflows = (push_resp or {}).get("workflows") or []
    if not workflows:
        return _err("push_no_workflows",
                    "push succeeded but no workflows returned; cannot trigger",
                    raw=push_resp)

    # 2. Pick the workflow to trigger.
    wf = None
    if playbook_name:
        for w in workflows:
            if w.get("name") == playbook_name:
                wf = w
                break
        if wf is None:
            return _err("playbook_not_found",
                        f"{playbook_name!r} not in pushed collection; "
                        f"available: {[w.get('name') for w in workflows]}")
    else:
        wf = workflows[0]
    wf_uuid = wf.get("uuid")
    if not wf_uuid:
        return _err("missing_wf_uuid", "pushed workflow has no uuid", wf=wf)

    # 3. Trigger.
    try:
        trig = mr(f"/api/triggers/1/notrigger/{wf_uuid}", "POST",
                  body={"input": {}, "request": {"data": inputs},
                        "useMockOutput": use_mock_output, "globalMock": False})
    except Exception as e:
        return _err("trigger_failed", f"{e}", stage="trigger",
                    collection_uuid=coll_uuid)
    task_id = (trig or {}).get("task_id")
    if not task_id:
        return _err("no_task_id",
                    "trigger returned no task_id; cannot poll",
                    raw=trig, collection_uuid=coll_uuid)

    # 4. Poll.
    #
    # `awaiting` is a STOPPING state here even though the run is not over. A
    # playbook that parks on a manual-input gate stays awaiting until a human
    # answers it, so polling on is guaranteed to burn the full timeout and then
    # report `status=timeout` -- i.e. the harness reads "the playbook doesn't
    # run" when what actually happened is the gate worked. Every trace-built
    # containment playbook now carries such a gate (insert_containment_confirm),
    # so this is the common case, not an edge one. We return promptly with
    # `status="awaiting"` and let the caller decide: answer the gate (see
    # pyfsr's manual_input.pending_for_run/resume) or treat reaching it as the
    # proof it wanted. `ok` stays False -- only `finished` is a completed run.
    import time
    terminal = {"finished", "failed", "terminated", "skipped",
                "finished_with_error", "rejected"}
    stopping = terminal | {"awaiting"}
    deadline = time.monotonic() + timeout_s
    record: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            listing = mr(
                "/api/wf/api/workflows/?format=json&limit=1&ordering=-modified"
                f"&task_id={task_id}&parent_wf__isnull=True",
                "GET",
            )
        except Exception:
            listing = None
        members = (listing or {}).get("hydra:member") or []
        if members and members[0].get("status") in stopping:
            record = members[0]
            break
        time.sleep(2)

    cleaned = False
    if cleanup and coll_uuid:
        # Hard-purge the collection. The platform exposes a hard-delete
        # via the standard DELETE with the purge query flag -- see
        # fsr_purge_blast_radius memory: be precise about scope.
        try:
            # NO trailing slash before the query (trailing slash 404s on
            # /api/3); hardDelete=true skips the recycle bin so the unique
            # collection name isn't reserved. Verified live.
            mr(f"/api/3/workflow_collections/{coll_uuid}?hardDelete=true",
               "DELETE")
            cleaned = True
        except Exception:
            cleaned = False

    if record is None:
        return {"ok": False, "status": "timeout", "task_id": task_id,
                "wf_uuid": wf_uuid, "collection_uuid": coll_uuid,
                "cleaned_up": cleaned,
                "error_message": f"timeout after {timeout_s}s"}

    status = record.get("status", "unknown")
    wf_pk = (record.get("@id") or "").rstrip("/").rsplit("/", 1)[-1]
    out: dict[str, Any] = {
        "ok": status == "finished",
        "status": status,
        "task_id": task_id,
        "wf_uuid": wf_uuid,
        "wf_pk": wf_pk,
        "collection_uuid": coll_uuid,
        "cleaned_up": cleaned,
    }
    if status == "awaiting":
        # Not a failure: name the step(s) holding the run so the caller can
        # assert WHICH gate it parked on (and find it to answer).
        out["awaiting_steps"] = [s.get("name") for s in (record.get("steps") or [])
                                 if s.get("status") == "awaiting"]
    elif status != "finished":
        top = record.get("result") or {}
        out["error_message"] = (
            (top.get("Error message") if isinstance(top, dict) else None)
            or record.get("errorMessage") or record.get("error"))
        failed: list[dict[str, Any]] = []
        for s in record.get("steps") or []:
            if s.get("status") in ("failed", "finished_with_error", "terminated"):
                res = s.get("result") or {}
                err = None
                if isinstance(res, dict):
                    err = res.get("Error message") or res.get("error") or res.get("message")
                    if err is None:
                        err = json.dumps(res)[:300]
                else:
                    err = str(res)[:300]
                failed.append({"name": s.get("name"),
                               "status": s.get("status"), "error": err})
        out["failed_steps"] = failed
    return out
