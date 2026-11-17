"""playbooks_persist domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any
from connectors.core.connector import ConnectorError
from .pydantic_models import (
    UpdatePlaybookParams, DecompilePlaybookParams,
    WorkflowCollectionsEnvelope,
    PushPlaybookParams,
)

from .ops_common import (
    _check_params, _err, _frame, _import_fsr_playbooks, _log, _reference_db_path, _require,
    _require_transport, _validate_model,
)



# ---------- platform-side operations (stubs in 0.0.1) ---------------------

def _unwrap_collection(workflow_json) -> dict:
    """compile_yaml emits the export wrap {type, data:[collection], ...}.
    Strict create wants the UNWRAPPED collection record. Accept either a
    pre-unwrapped collection or the wrap.

    NOTE: FortiSOAR's /api/integration/execute/ delivers object-typed params
    as JSON STRINGS to the handler (verified live -- pushing a dict
    `workflow_json` arrived stringified, so the unwrap was skipped and the raw
    string POSTed → "null value in column name"). Parse a string first."""
    if isinstance(workflow_json, str):
        try:
            workflow_json = json.loads(workflow_json)
        except (ValueError, TypeError):
            return workflow_json
    if isinstance(workflow_json, dict) and "data" in workflow_json:
        data = workflow_json.get("data") or []
        if data:
            return data[0]
    return workflow_json


def _purge_collection_by_uuid(mr, collection: dict) -> None:
    """Hard-delete an existing collection and its workflows by their
    deterministic uuids so a strict re-create won't 409 on the uniqueness
    constraint.

    Uses SINGLE-ROW deletes -- `DELETE /api/3/{entity}/{uuid}?$hardDelete=true&
    $showDeleted=true` -- which the platform's loopback `make_request` issues
    correctly (URL + query only; no DELETE body, which make_request does NOT
    forward). Three details, each verified live, matter:
      * `$hardDelete=true` (WITH the `$` prefix) -- without `$` the operator is
        ignored and the row is SOFT-deleted into the recycle bin, where it keeps
        reserving its uuid+name and the re-create 409s on the uniqueness
        constraint ("record might be in the recycle bin").
      * `$showDeleted=true` -- lets the delete reach a row already sitting in the
        recycle bin (e.g. left there by an older buggy purge), so a stuck uuid
        self-heals on the next push instead of 409ing forever.
      * The BULK endpoint `/api/3/delete/{entity}` with `{"ids":[...]}` is NOT
        usable here: it needs a DELETE body (dropped by make_request) and 500s
        with NotFoundHttpException if any id is already gone (not idempotent).
    Deleting the collection cascades to its workflows/steps/routes via Doctrine
    onDelete=CASCADE, but we also delete the workflow rows explicitly first to
    clear any partial/orphaned state. Best-effort: a missing row is fine."""
    if not isinstance(collection, dict):
        return
    q = "?$hardDelete=true&$showDeleted=true"
    for w in (collection.get("workflows") or []):
        wu = w.get("uuid") if isinstance(w, dict) else None
        if wu:
            try:
                mr(f"/api/3/workflows/{wu}{q}", "DELETE")
            except Exception:
                pass
    cu = collection.get("uuid")
    if cu:
        try:
            mr(f"/api/3/workflow_collections/{cu}{q}", "DELETE")
        except Exception:
            pass


def _regen_collection_uuids(collection: dict) -> None:
    """Mint fresh uuids for the collection and its workflows so a create can't
    collide with an existing -- or recycle-bin, or not-visible-to-this-user --
    record on the uuid uniqueness constraint. Non-destructive: we make a NEW
    record instead of deleting the incumbent (the safe option). Step/route ids
    inside each workflow are (re)assigned by the platform on create."""
    if not isinstance(collection, dict):
        return
    collection["uuid"] = str(uuid.uuid4())
    for w in collection.get("workflows") or []:
        if isinstance(w, dict):
            w["uuid"] = str(uuid.uuid4())


def _bump_name(name: str) -> str:
    """`Foo` -> `Foo (2)`, `Foo (2)` -> `Foo (3)`. Uniquifies a collection name
    when the platform reports the name is already taken -- including by a record
    in the recycle bin or one owned by another team (invisible to us, so we
    can't purge it). We never create a second record with a name in use."""
    name = (name or "Playbook").strip()
    if name.endswith(")") and "(" in name:
        head, _, tail = name.rpartition("(")
        num = tail[:-1].strip()
        if num.isdigit():
            return f"{head.strip()} ({int(num) + 1})"
    return f"{name} (2)"


def _is_uniqueness_conflict(err: str) -> "tuple[bool, bool]":
    """Classify a push error. Returns (is_uniqueness_conflict, is_on_name).
    FortiSOAR raises a 409 UniqueConstraintViolationException; when the clashing
    field is the collection name the message names `fields (name)`."""
    e = (err or "").lower()
    conflict = ("uniqueconstraint" in e or " 409" in e or "409 " in e
                or "already exists" in e or "recycle bin" in e
                or "uniqueness constraint" in e)
    on_name = "(name)" in e
    return conflict, on_name


def push_playbook(config: dict, params: dict) -> dict:
    """Create a WorkflowCollection via strict create:
    POST /api/3/workflow_collections with the UNWRAPPED collection body.
    (NOT /api/3/workflows/ -- that route 404s -- and NOT the {type,data}
    export wrap, which only imports via the file-upload import_jobs flow.)
    Verified live; returns the created record {uuid,
    workflows[].uuid}. Off-platform returns the crudhub_unavailable stub."""
    _check_params(PushPlaybookParams, params)
    workflow_json = _require(params, "workflow_json")

    make_request, err = _require_transport()
    if err:
        return err

    # Conflict-safe create (the compiler assigns DETERMINISTIC uuid5-of-name
    # uuids, so a re-push or a name already taken elsewhere collides on the
    # platform's uniqueness constraints -- uuid, and collection name, enforced
    # even against soft-deleted records in the recycle bin and records owned by
    # other teams that we can't see or purge).
    #
    # `overwrite=True` (default) first hard-deletes OUR OWN prior copy by its
    # deterministic uuid (incl. the recycle bin) so a re-run replaces cleanly.
    # Then, whatever the platform still rejects on a uniqueness 409, we resolve
    # WITHOUT destroying the incumbent: mint a fresh uuid (the safe option),
    # and if the *name* is the clash, uniquify it (`Foo` -> `Foo (2)`), then
    # retry. We never create a second playbook with a name already in use, and
    # the saved record is always fresh/live -- never resurrected from the bin.
    overwrite = bool(params.get("overwrite", True))
    collection = _unwrap_collection(workflow_json)
    endpoint = "/api/3/workflow_collections"
    if not isinstance(collection, dict):
        return _err("bad_collection", "workflow_json did not unwrap to a "
                    "collection record")

    if overwrite:
        _purge_collection_by_uuid(make_request, collection)

    requested_name = collection.get("name")
    renamed = False
    uuid_regenerated = False
    resp = None
    last_err = None
    attempts_made = 0
    call_succeeded = False
    for attempt in range(6):
        attempts_made = attempt + 1
        try:
            resp = make_request(endpoint, "POST", body=collection)
            call_succeeded = True
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            conflict, on_name = _is_uniqueness_conflict(str(e))
            if not conflict:
                raise ConnectorError(f"push to {endpoint} failed: {e}")
            # Resolve the conflict non-destructively, then retry.
            if on_name:
                collection["name"] = _bump_name(collection.get("name"))
                renamed = True
            _regen_collection_uuids(collection)
            uuid_regenerated = True
    # `call_succeeded` (no exception, whatever the body) is distinct from
    # "resp is falsy": a call that raised on every attempt exhausted the
    # conflict-retry budget (push_conflict_unresolved, below). A call that
    # SUCCEEDED but returned an empty/non-record body (e.g. RBAC drop) is a
    # different failure -- fall through to the created_uuid check, which
    # reports that honestly as push_no_record instead of a bogus "6 attempts"
    # conflict message with last_err=None (found live 2026-07-01: make_request
    # returned None on the first, non-exception attempt).
    if not call_succeeded:
        return _err("push_conflict_unresolved",
                    f"could not create after {attempts_made} attempts: {last_err}",
                    requested_name=requested_name)

    # A 2xx with no created record (empty/non-record body -- e.g. an RBAC drop,
    # a 204, or a crudhub passthrough that didn't echo the row) must NOT report
    # success: the widget treats any result without `ok:false` as "Created
    # playbook in FortiSOAR", so a uuid-less response shows a phantom success
    # with no actual playbook. Fail honestly instead.
    created_uuid = (resp or {}).get("uuid") if isinstance(resp, dict) else None
    if not created_uuid:
        return _err("push_no_record",
                    "create returned no workflow_collection record (no uuid in "
                    "the response) -- the playbook was NOT created. The POST to "
                    f"{endpoint} succeeded at the transport layer but echoed no "
                    "row; check RBAC/team ownership on workflow_collections.",
                    requested_name=requested_name,
                    raw=resp)

    workflows = (resp or {}).get("workflows") or []
    out = {
        "ok": True,
        "collection_uuid": (resp or {}).get("uuid"),
        "collection_name": (resp or {}).get("name") or collection.get("name"),
        "workflow_uuids": [w.get("uuid") for w in workflows],
        "raw": resp,
    }
    # Surface honestly when we had to deviate from what was requested.
    if renamed:
        out["renamed_from"] = requested_name
    if uuid_regenerated:
        out["uuid_regenerated"] = True
    return out


def _workflow_uuid_from_iri(iri: str) -> str | None:
    """Extract the trailing uuid from a workflows IRI
    (``/api/3/workflows/<uuid>`` → ``<uuid>``). None when the IRI isn't a
    workflows record path (so the caller can require an explicit uuid param)."""
    m = re.search(r"/api/3/workflows/([^/?#]+)", str(iri or ""))
    return m.group(1) if m else None


def _canvas_playbook(make_request, wf: dict, live: dict):
    """Shape a grafted workflow for the open designer's canvas, which loads it
    as unsaved changes (PlaybookDesignerCtrl's `designerDetail:entityUpdated`
    path -- the one its undo/redo and Versions restore take).

    The canvas is stricter than the save PUT in one way that matters: it reads
    `stepType.name`, so a step type must be the OBJECT, not the IRI string the
    compiler emits (a bare IRI breaks the designer's trigger detection and its
    own save). Types already on the live playbook are reused; any other is
    read from the box. steps / routes / groups must be arrays. Fields the
    platform manages are dropped -- the widget merges this over the designer's
    own entity, which keeps them.

    Returns (playbook, None) or (None, error envelope)."""
    import copy
    out = copy.deepcopy(wf)
    for k in ("versions", "collection", "modifyUser", "modifyDate", "lastModifyDate"):
        out.pop(k, None)
    # A top-level null would OVERWRITE the designer's own value: the widget
    # merges this with angular.extend over the loaded entity. Live, the compiler's
    # `playbookOrigin: null` replaced the real object and PlaybookDesignerCtrl
    # .init died on `playbookOrigin["@id"]` before building a single step -- the
    # canvas went blank on every staged Apply. Absent keys keep the entity's.
    for k in [k for k, v in out.items() if v is None]:
        out.pop(k)
    known: dict = {}
    for ls in (live.get("steps") or []):
        st = ls.get("stepType") if isinstance(ls, dict) else None
        if isinstance(st, dict) and st.get("@id"):
            known[st["@id"]] = st
    steps = out.get("steps") or []
    if isinstance(steps, dict):
        steps = list(steps.values())
    for step in steps:
        if not isinstance(step, dict):
            continue
        st = step.get("stepType")
        if isinstance(st, str) and st:
            if st not in known:
                try:
                    got = make_request(st, "GET")
                except Exception as e:  # noqa: BLE001
                    got = None
                    why = str(e)
                else:
                    why = "no record came back"
                if not (isinstance(got, dict) and got.get("name")):
                    return None, _err(
                        "step_type_unresolved",
                        f"could not read step type {st} for step "
                        f"{step.get('name')!r} ({why}), so the edit was not "
                        f"loaded into the designer. Nothing was changed.")
                known[st] = got
            step["stepType"] = known[st]
        if isinstance(step.get("arguments"), list) and not step["arguments"]:
            step["arguments"] = {}
    out["steps"] = steps
    for key in ("routes", "groups"):
        val = out.get(key) or []
        out[key] = list(val.values()) if isinstance(val, dict) else list(val)
    return out, None


def _prepare_workflow_for_save(wf: dict) -> dict:
    """Shape a workflow record like the designer's
    ``playbookService.preparePlaybookForSave`` (app.beautified.js) so it is
    accepted by the ``PUT .../workflows/<uuid>?$relationships=true`` save:
      * drop ``versions`` (the platform manages snapshots; sending them back on
        a relationship PUT is rejected),
      * flatten each step's ``stepType`` to its ``@id`` IRI string when it
        arrived as an expanded object (compiled bodies already carry the IRI;
        a GET-modify-PUT round-trip carries the object -- handle both),
      * normalise an empty-array ``arguments`` to ``{}``,
      * stamp ``lastModifyDate``.
    Non-mutating: operates on a deep copy.
    """
    import copy
    import time
    out = copy.deepcopy(wf)
    out.pop("versions", None)
    steps = out.get("steps") or []
    if isinstance(steps, dict):
        steps = list(steps.values())
    for s in steps:
        if not isinstance(s, dict):
            continue
        st = s.get("stepType")
        if isinstance(st, dict) and st.get("@id"):
            s["stepType"] = st["@id"]
        if isinstance(s.get("arguments"), list) and not s["arguments"]:
            s["arguments"] = {}
    out["steps"] = steps
    out["lastModifyDate"] = int(time.time())
    return out


_WORKFLOW_VERSIONS_EP = "/api/3/workflow_versions"
# Fields the platform manages; a snapshot's `json` blob must not carry them
# back (mirrors the editor's `preparePlaybookForOverwrite`).
_VERSION_STRIP_FIELDS = ("versions", "collection", "modifyUser", "modifyDate")
# FortiSOAR caps snapshots at 20 per playbook -- past that the POST is rejected.
# Since the snapshot gate is fail-closed, hitting the cap would wedge editing
# entirely, so we prune our OWN old restore points to make room (never a
# human's).
_MAX_PLAYBOOK_VERSIONS = 20
# Marks a snapshot as one this connector took automatically before an AI edit.
# Load-bearing: pruning only ever deletes snapshots carrying this prefix.
_AI_SNAPSHOT_PREFIX = "ai-pre-edit"


def _members(resp) -> list:
    """Rows out of a collection response (hydra envelope or a bare list)."""
    if isinstance(resp, dict):
        return resp.get("hydra:member") or resp.get("member") or []
    return resp if isinstance(resp, list) else []


def _prepare_version_body(wf: dict) -> dict:
    """Shape a live workflow into a snapshot's ``json`` payload.

    Mirrors pyfsr's ``_prepare_version_body`` / the editor's
    ``preparePlaybookForOverwrite``: drop platform-managed fields and coerce
    dict-keyed ``steps``/``groups`` to list form (the wire shape a snapshot's
    ``json`` and a restore PUT both expect).
    """
    body = {k: v for k, v in wf.items() if k not in _VERSION_STRIP_FIELDS}
    steps = body.get("steps")
    if isinstance(steps, dict):
        body["steps"] = list(steps.values())
    groups = body.get("groups")
    if isinstance(groups, dict):
        body["groups"] = list(groups.values())
    return body


def _prune_ai_snapshots(make_request, workflow_uuid: str) -> None:
    """Make room under the 20-snapshot cap by deleting our oldest auto-snapshots.

    Only ever deletes snapshots whose note carries ``_AI_SNAPSHOT_PREFIX`` -- a
    human's named restore point is never touched, even at the cap. If the cap is
    full of human snapshots we delete nothing and let the POST fail, which the
    caller reports as an actionable ``snapshot_failed``. Best-effort: a prune
    failure is not itself fatal (the POST is the real gate).
    """
    try:
        resp = make_request(
            f"{_WORKFLOW_VERSIONS_EP}?workflow={workflow_uuid}&$limit=100",
            "GET")
    except Exception:  # noqa: BLE001
        return
    rows = _members(resp)
    if len(rows) < _MAX_PLAYBOOK_VERSIONS:
        return
    ours = [r for r in rows
            if isinstance(r, dict)
            and str(r.get("note") or "").startswith(_AI_SNAPSHOT_PREFIX)]
    # Oldest first. The listing is newest-first by modifyDate, but sort
    # explicitly rather than trusting the server's default ordering.
    ours.sort(key=lambda r: r.get("modifyDate") or 0)
    # Delete enough to leave one slot free for the snapshot we're about to take.
    for row in ours[:len(rows) - _MAX_PLAYBOOK_VERSIONS + 1]:
        vid = row.get("uuid") or _version_uuid_from_iri(row.get("@id") or "")
        if not vid:
            continue
        try:
            make_request(f"{_WORKFLOW_VERSIONS_EP}/{vid}", "DELETE")
        except Exception:  # noqa: BLE001
            continue


def _version_uuid_from_iri(iri: str) -> str | None:
    """``/api/3/workflow_versions/<uuid>`` → ``<uuid>``."""
    m = re.search(r"/api/3/workflow_versions/([^/?#]+)", str(iri or ""))
    return m.group(1) if m else None


def _as_list(v) -> list:
    """Steps/routes/groups arrive dict-keyed from some reads, list from others."""
    if isinstance(v, dict):
        return list(v.values())
    return v if isinstance(v, list) else []


def _read_live_workflow(make_request, workflow_uuid: str) -> dict:
    """GET the playbook as it currently stands, relationships expanded.

    One read serves both callers: it is what we freeze into the restore point,
    and it carries the ``@id`` identities ``_graft_live_ids`` needs. The query
    is inlined into the URL, not passed as a params dict, because the
    on-platform crudhub bridge ignores params.
    """
    try:
        live = make_request(
            f"/api/3/workflows/{workflow_uuid}?$relationships=true", "GET")
    except Exception as e:  # noqa: BLE001
        return _err("live_read_failed",
                    f"could not read playbook {workflow_uuid}: {e}")
    if not isinstance(live, dict) or not live.get("uuid"):
        return _err("live_read_failed",
                    f"could not read playbook {workflow_uuid}: no record "
                    f"returned.")
    return {"ok": True, "workflow": live}


def _uuid_from_ref(ref) -> str | None:
    """Read the uuid out of a nested-record reference.

    The same relationship arrives in three shapes depending on who serialised it:
    an IRI string (``/api/3/workflow_steps/<uuid>``, what the compiler emits), an
    expanded dict (a live GET with ``$relationships=true``), or a bare uuid.
    """
    if isinstance(ref, dict):
        return ref.get("uuid") or _uuid_from_ref(ref.get("@id") or "")
    s = str(ref or "").strip()
    if not s:
        return None
    return s.rsplit("/", 1)[-1] or None


def _remap_ref(ref, remap: dict):
    """Repoint a reference at a record whose uuid we realigned to the live one."""
    new = remap.get(_uuid_from_ref(ref))
    if not new:
        return ref
    old = _uuid_from_ref(ref)
    if isinstance(ref, dict):
        out = dict(ref)
        out["uuid"] = new
        if isinstance(out.get("@id"), str):
            out["@id"] = out["@id"].replace(old, new)
        return out
    return str(ref).replace(old, new)


def _match_live_records(compiled_items: list, live_items: list,
                        keyfns: list) -> list:
    """Pair each compiled record with the live record it is a revision OF.

    ``keyfns`` are tried in order, strongest identity first; a live record can be
    claimed only once. A key matching **more than one** unclaimed live record
    pairs nothing rather than guessing -- an ambiguous graft would silently move
    an edit onto the wrong record, which is worse than the create-and-drop it
    falls back to. Returns ``[(compiled_item, live_item_or_None), ...]``.
    """
    claimed: set = set()
    pairs = [[it, None] for it in compiled_items]
    for keyfn in keyfns:
        index: dict = {}
        for r in live_items:
            if not isinstance(r, dict):
                continue
            k = keyfn(r)
            if k is not None:
                index.setdefault(k, []).append(r)
        for pair in pairs:
            item, matched = pair
            if matched is not None or not isinstance(item, dict):
                continue
            k = keyfn(item)
            if k is None:
                continue
            cands = [c for c in index.get(k, []) if id(c) not in claimed]
            if len(cands) == 1:
                claimed.add(id(cands[0]))
                pair[1] = cands[0]
    return [tuple(p) for p in pairs]


def _k_uuid(r: dict):
    return r.get("uuid") or None


def _k_name(r: dict):
    return r.get("name") or None


def _k_route_ends(r: dict):
    """A route's real identity is the pair of steps it connects, not its uuid."""
    src = _uuid_from_ref(r.get("sourceStep"))
    tgt = _uuid_from_ref(r.get("targetStep"))
    return (src, tgt) if src and tgt else None


# Synthetic names used only for the pre-write comparison. The guard joins the
# two sides by NAME, so any name that legitimately differs between live and
# outgoing reads as "this thing was deleted". update_playbook targets ONE
# workflow by IRI -- the two sides are the same workflow by construction -- so
# neither the collection name nor the workflow name may be part of the join.
_PREWRITE_COLLECTION_NAME = "_prewrite"
_PREWRITE_WORKFLOW_NAME = "_prewrite_wf"


def _as_collection_envelope(workflow: dict) -> dict:
    """Wrap a single workflow record as the collection envelope the framework's
    pre-write guard compares.

    Both synthetic names are pinned identical on both sides so neither can be
    the thing that differs. The workflow name matters as much as the collection
    name: with it left free, RENAMING a playbook -- an ordinary edit -- made
    the guard report `collection.workflows[<old name>]` as dropped and refuse
    the whole save, claiming the entire playbook was being deleted.

    Shallow-copies, because the caller's dict is the real outgoing PUT body --
    renaming it here would rename the analyst's playbook.
    """
    wf = dict(workflow)
    wf["name"] = _PREWRITE_WORKFLOW_NAME
    return {"data": [{"name": _PREWRITE_COLLECTION_NAME, "description": "",
                      "workflows": [wf]}]}


def _check_prewrite_loss(live: dict, outgoing: dict,
                         acknowledged: list | None = None) -> dict | None:
    """None when the write is safe; an error envelope when it would delete data.

    Delegates to ``fsr_playbooks.compiler.prewrite`` so the connector and
    ``push_playbook`` refuse on exactly the same semantics -- a second copy of
    this comparison would drift, and the drift would be invisible until it let
    a deletion through.

    If the framework is too old to carry the guard we log and ALLOW. That is
    the one deliberate fail-open here: connector and framework ship on
    independent cadences, and a version skew must not brick every playbook edit
    on the box. The framework pin is what actually enforces the gate.
    """
    try:
        from fsr_playbooks.compiler.prewrite import check_prewrite
    except ImportError:
        _log("update_playbook: framework has no prewrite guard; saving "
             "UNCHECKED. Bump the fsr-playbooks pin to re-arm HARDEN-1.",
             "warning")
        return None

    verdict = check_prewrite(
        _as_collection_envelope(live),
        _as_collection_envelope(outgoing),
        [str(p) for p in (acknowledged or [])],
    )
    if verdict.ok:
        return None
    return _err(
        "would_drop_fields",
        verdict.message,
        dropped=verdict.dropped,
        workflow_uuid=live.get("uuid") or "",
    )


def _open_playbook_yaml(final_yaml: str, created: Any) -> str:
    """`final_yaml` minus the playbooks Apply created, with references to them
    switched from `target: <name>` (resolves only within one YAML) to
    `workflowReference: /api/3/workflows/<uuid>`. Unchanged when none were."""
    made = {str(c.get("name")): str(c.get("uuid")) for c in (created or [])
            if isinstance(c, dict) and c.get("name") and c.get("uuid")}
    if not made:
        return final_yaml
    import yaml as _yaml
    try:
        doc = _yaml.safe_load(final_yaml)
    except Exception:  # noqa: BLE001
        return final_yaml
    pbs = doc.get("playbooks") if isinstance(doc, dict) else None
    if not isinstance(pbs, list):
        return final_yaml
    keep = [p for p in pbs if isinstance(p, dict) and str(p.get("name")) not in made]
    for pb in keep:
        for st in pb.get("steps") or []:
            if not isinstance(st, dict) or st.get("type") != "workflow_reference":
                continue
            for holder in (st, st.get("arguments") if isinstance(st.get("arguments"), dict) else None):
                if holder and str(holder.get("target")) in made:
                    holder["workflowReference"] = f"/api/3/workflows/{made[str(holder.pop('target'))]}"
    doc["playbooks"] = keep
    return _yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _upsert_new_playbooks(make_request, wfs: list, collection: Any,
                          note: str = "") -> dict:
    """Create (or, on a re-Apply, update) the playbooks an edit adds.

    Through pyfsr's `upsert_playbooks` (live-verified create-or-update keyed by
    uuid) on the connector's own transport. Fail closed throughout:

    * a uuid is FREE only on a positive "no such playbook" (an empty list
      query); any lookup error aborts before anything is written;
    * a uuid held by a playbook of another name is refused -- not ours;
    * one of the same name (a re-Apply) is an overwrite: it gets the pre-write
      loss guard with NO acknowledged drops of its own, and a restore point
      first, like the open playbook; it keeps its own collection.
    """
    import copy
    import re as _re
    try:
        from fsr_soc_triage._live_crudhub import CrudhubLiveClient, _attach_pyfsr_apis
        client = CrudhubLiveClient(make_request)
        _attach_pyfsr_apis(client)
        api = getattr(client, "playbooks", None)
        if api is None or not hasattr(api, "upsert_playbooks"):
            # Live: the box had pyfsr 0.16.1 under a `>=0.7.9` pin; the method
            # arrived in 0.23.0 (requirements.txt now pins that floor).
            _log("child playbook create needs pyfsr>=0.23.0 "
                 "(PlaybooksAPI.upsert_playbooks); the installed pyfsr lacks it",
                 "warn")
            return _err("create_unsupported",
                        "this install can't create a new playbook yet -- the "
                        "connector's libraries need updating -- so nothing was "
                        "changed")
    except Exception as e:  # noqa: BLE001
        return _err("create_unsupported", f"cannot create playbooks here: {e}")
    uuid_re = _re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                          r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
    rows = []
    for wf in wfs:
        row = copy.deepcopy(wf)
        uuid = str(row.get("uuid") or "")
        if not uuid_re.fullmatch(uuid):
            return _err("bad_playbook_id",
                        f"cannot create {row.get('name')!r}: its id {uuid!r} is not a "
                        "uuid. Nothing was changed.")
        try:
            found = make_request(f"/api/3/workflows?uuid={uuid}&$limit=1", "GET")
        except Exception as e:  # noqa: BLE001
            return _err("child_lookup_failed",
                        f"could not check whether {row.get('name')!r} already exists "
                        f"({e}); nothing was changed.")
        members = found.get("hydra:member") if isinstance(found, dict) else None
        if not isinstance(members, list):
            return _err("child_lookup_failed",
                        f"could not check whether {row.get('name')!r} already exists "
                        "(unexpected reply); nothing was changed.")
        if members:
            read = _read_live_workflow(make_request, uuid)
            if not read.get("ok"):
                return read
            have = read["workflow"]
            if str(have.get("name")) != str(row.get("name")):
                return _err("playbook_id_taken",
                            f"cannot create {row.get('name')!r}: its id is already "
                            f"used by playbook {have.get('name')!r}. Nothing was changed.")
            guard = _check_prewrite_loss(have, row, [])
            if guard is not None:
                return guard
            snap = _snapshot_playbook(make_request, uuid, note, have)
            if not snap.get("ok"):
                return snap
            # An existing record stays where it is.
            if have.get("collection"):
                coll = have["collection"]
                row["collection"] = (coll.get("@id") or coll.get("uuid")
                                     if isinstance(coll, dict) else coll)
        elif collection:
            row["collection"] = collection
        rows.append(row)
    try:
        api.upsert_playbooks(rows)
    except Exception as e:  # noqa: BLE001
        return _err("create_failed",
                    f"could not create {', '.join(repr(r.get('name')) for r in rows)}: "
                    f"{e}. The open playbook was not changed.")
    return {"ok": True, "created": [{"name": r.get("name"), "uuid": r.get("uuid")}
                                    for r in rows]}


def _graft_live_ids(compiled: dict, live: dict) -> dict:
    """Give the compiled body the platform identities of the records it revises.

    THE reason the relationship PUT 409s. The compiler emits ``uuid`` but no
    ``@id``; the platform decides create-vs-update on ``@id``. So a compiled body
    without ``@id`` asks the platform to INSERT every step -- and if a step's uuid
    already exists anywhere, it refuses:

        UniqueConstraintViolationException: ... uniqueness constraint for the
        fields (uuid) and a record already exists ...

    Grafting each compiled record onto its live counterpart turns those inserts
    into in-place updates -- the same thing the designer's Save sends, and the
    same reason pyfsr's `playbooks.update()` works (its steps come from a GET, so
    they already carry ``@id``).

    **Matching is by identity, not by uuid.** An earlier version paired records by
    uuid alone, on the reasoning that compiled uuids are deterministic so they
    collide exactly with the live ones. That holds for exactly one case -- a
    playbook this compiler created and nobody cloned -- and the build persona's
    whole premise is the other case. Live-probed on 8.0, uuid-only matching gave:

      * a **clone** (same names, regenerated uuids) → the compiled uuids collide
        with the ORIGINAL playbook's steps, nothing grafts, **409**. Duplicate a
        playbook and ask for a tweak → fails every time.
      * a **designer-built** playbook (random uuids) → nothing collides and
        nothing grafts, so the PUT "succeeds" while destroying and recreating
        every step (``kept=0 new=2 dropped=2``). The edit lands; the step records,
        their run history, and any uuid-keyed diff do not survive it.

    So we match on uuid FIRST (an exact identity always wins), then on **name** --
    which is the compiler's own uuid seed, i.e. the thing that made the uuids
    deterministic in the first place. Routes have no meaningful name of their own,
    so they match on the pair of steps they connect.

    Once a compiled record is paired, it takes the live record's ``uuid`` as well
    as its ``@id``, so the live identity survives the edit. That means every
    reference BY uuid -- a route's ``sourceStep``/``targetStep``, a step's
    ``group`` -- is left pointing at the compiled uuid and must be remapped, or
    the cascade rebuilds the graph with dangling edges.

    A compiled record with no live match keeps no ``@id`` and is correctly created
    (a genuinely new step). A live record absent from the body is dropped by the
    cascade (a deleted step). Non-mutating: operates on a deep copy.
    """
    import copy
    out = copy.deepcopy(compiled)
    if live.get("@id"):
        out["@id"] = live["@id"]

    # Pass 1: steps and groups. Realigning their uuids to the live ones is what
    # makes the write an update rather than a rewrite -- and what leaves the
    # references below stale.
    remap: dict = {}
    for key in ("steps", "groups"):
        items = _as_list(out.get(key))
        for item, match in _match_live_records(
                items, _as_list(live.get(key)), [_k_uuid, _k_name]):
            if match is None:
                continue
            if match.get("@id"):
                item["@id"] = match["@id"]
            old, new = item.get("uuid"), match.get("uuid")
            if not new:
                continue
            if old and old != new:
                remap[old] = new
            item["uuid"] = new
        out[key] = items

    # Pass 2: repoint every uuid-bearing reference at the grafted identities.
    # This must happen BEFORE routes are matched -- their identity IS their
    # (source, target) pair, and it is only comparable to the live one once both
    # sides speak in live uuids.
    #
    # The workflow's own `triggerStep` is one of these, and it is the reference
    # that is easiest to forget: it lives on the parent record rather than
    # alongside the steps. Live on 8.0 the platform rejects the whole PUT for it
    # -- `Foreign key violation on field 'triggerstep_uuid' ... is not a valid
    # entry of type 'workflow_steps'` -- because the compiled uuid it names has
    # just been grafted away.
    if out.get("triggerStep"):
        out["triggerStep"] = _remap_ref(out["triggerStep"], remap)
    for item in _as_list(out.get("steps")):
        if isinstance(item, dict) and item.get("group"):
            item["group"] = _remap_ref(item["group"], remap)
    routes = _as_list(out.get("routes"))
    for item in routes:
        if not isinstance(item, dict):
            continue
        for field in ("sourceStep", "targetStep", "group"):
            if item.get(field):
                item[field] = _remap_ref(item[field], remap)

    # Pass 3: routes, now that their endpoints are live uuids.
    for item, match in _match_live_records(
            routes, _as_list(live.get("routes")),
            [_k_uuid, _k_route_ends, _k_name]):
        if match is None:
            continue
        if match.get("@id"):
            item["@id"] = match["@id"]
        if match.get("uuid"):
            item["uuid"] = match["uuid"]
    out["routes"] = routes
    return out


def _snapshot_playbook(make_request, workflow_uuid: str, note: str,
                       live: dict) -> dict:
    """Freeze the playbook AS IT IS NOW, so an AI edit is always recoverable.

    Mirrors the editor's ``saveSnapshot`` (and pyfsr's ``create_version``): take
    the live workflow, strip platform-managed fields, stringify into ``json``,
    and POST ``{note, json, workflow, modifyDate}`` to
    ``/api/3/workflow_versions``. The result is a real entry in the playbook's
    **Versions** tab, so a user can roll back through the product UI without
    knowing an AI was involved.

    ``live`` is the pre-edit record from ``_read_live_workflow`` -- passed in
    rather than re-read so the snapshot cannot drift from the body we graft
    identities onto, and so the freeze is unambiguously of the user's work.

    Returns ``{ok: True, version_uuid, note}``, or an ``_err`` envelope. The
    caller MUST treat a non-ok as fatal: see ``update_playbook``'s fail-closed
    gate for why.

    Quirk (live-verified on 8.0): the server does NOT echo the ``json`` blob back
    on create -- a created snapshot's response has ``json: None``. That is
    expected, not a failure; we key success off the record's uuid. (Reading one
    back needs ``$includeData=true`` or the blob is withheld and the snapshot
    looks empty.)
    """
    import time

    _prune_ai_snapshots(make_request, workflow_uuid)
    body = {
        "note": note,
        "json": json.dumps(_prepare_version_body(live), default=str),
        "workflow": f"/api/3/workflows/{workflow_uuid}",
        "modifyDate": int(time.time()),
    }
    try:
        resp = make_request(_WORKFLOW_VERSIONS_EP, "POST", body=body)
    except Exception as e:  # noqa: BLE001
        return _err("snapshot_failed",
                    f"could not create a restore point for playbook "
                    f"{workflow_uuid}: {e}. The edit was NOT applied. If this "
                    f"playbook has 20 saved versions, delete one from its "
                    f"Versions tab and retry.")
    vid = (resp or {}).get("uuid") if isinstance(resp, dict) else None
    if not vid:
        return _err("snapshot_failed",
                    f"could not create a restore point for playbook "
                    f"{workflow_uuid}: the snapshot POST returned no record. "
                    f"The edit was NOT applied.")
    return {"ok": True, "version_uuid": vid, "note": note}


def _live_collection_envelope(make_request, live: dict) -> dict:
    """Wrap a live workflow into the ``{data: [collection]}`` envelope the
    decompiler takes.

    Mirrors the CLI's ``_fetch_live_collection`` (tooling/cli.py:946), scoped to
    the one playbook the designer has open. ``$relationships=true`` inlines the
    steps/routes, which is the shape ``decompile`` already handles (its
    ``stepType`` branch reads both the export IRI string and the live nested
    dict).

    The collection is needed only for its name/description/visible header. With
    relationships it arrives as a nested dict; otherwise it is an IRI to fetch.
    A collection we cannot read is not fatal -- the header degrades to empty and
    the steps, which are what the assistant edits, are unaffected.
    """
    coll = live.get("collection")
    if isinstance(coll, str) and coll:
        try:
            coll = make_request(coll, "GET")
        except Exception:  # noqa: BLE001
            coll = None
    if not isinstance(coll, dict):
        coll = {}
    header = {
        "name": coll.get("name") or "",
        "description": coll.get("description") or "",
        "visible": bool(coll.get("visible", True)),
        "workflows": [live],
    }
    return _frame(WorkflowCollectionsEnvelope, type="workflow_collections",
                  data=[header])


def decompile_playbook(config: dict, params: dict) -> dict:
    """Read an EXISTING playbook off the box and return it as authored YAML.

    The read counterpart of ``compile_yaml``, and the fix for the gap S2 found:
    **the build persona could not see the playbook it was being asked to edit.**
    Nothing in ``tools_for_intent("build")`` can fetch a live playbook --
    ``get_record``/``search_module_records`` are scoped out of build (C5),
    ``search_playbooks`` queries an offline corpus, and ``analyze_playbook``
    takes ``yaml_text`` only. So every ungrounded designer turn dead-ended
    asking the analyst to paste in the playbook they already had open (0/4 live
    runs). Handed the YAML, the same assistant edited it correctly first try --
    the blocker was the missing read path, not the model and not the prompt.

    The widget calls this when the designer mounts and seeds the result as the
    open playbook's YAML, so it rides every build turn -- and the push button
    then has a base to push even when the assistant returns a partial edit.

    Takes ``workflow_iri`` (/api/3/workflows/<uuid>) or ``workflow_uuid``.
    Returns ``{ok, yaml, workflow_uuid, workflow_iri, name}``. Off-platform
    returns the crudhub_unavailable stub.
    """
    _validate_model(DecompilePlaybookParams, params or {})
    workflow_iri = params.get("workflow_iri") or ""
    workflow_uuid = params.get("workflow_uuid") or ""
    if not workflow_uuid:
        workflow_uuid = _workflow_uuid_from_iri(workflow_iri) or ""
    if not workflow_uuid:
        return _err(
            "missing_workflow_target",
            "decompile_playbook needs a playbook to read: pass workflow_iri "
            "(/api/3/workflows/<uuid>) or workflow_uuid. The widget seeds this "
            "from the open playbook in the designer.",
        )

    make_request, err = _require_transport()
    if err:
        return err

    read = _read_live_workflow(make_request, workflow_uuid)
    if not read.get("ok"):
        return read
    live = read["workflow"]

    _import_fsr_playbooks()  # assert the pinned build before using its compiler
    from fsr_playbooks.compiler.decompiler import decompile_to_yaml
    envelope = _live_collection_envelope(make_request, live)
    try:
        yaml_text = decompile_to_yaml(envelope, _reference_db_path())
    except Exception as e:  # noqa: BLE001
        # A playbook we cannot render is reported, never half-rendered. The
        # widget seeds this straight into the editor's YAML, so a partial
        # decompile that got edited and pushed back would silently truncate the
        # user's playbook. Same fail-closed reasoning as the snapshot gate.
        return _err(
            "decompile_failed",
            f"could not render playbook {workflow_uuid} as YAML: {e}",
            workflow_uuid=workflow_uuid,
        )
    return {
        "ok": True,
        "yaml": yaml_text,
        "workflow_uuid": workflow_uuid,
        "workflow_iri": f"/api/3/workflows/{workflow_uuid}",
        "name": live.get("name") or "",
    }


def update_playbook(config: dict, params: dict) -> dict:
    """Modify an EXISTING playbook in place -- the modify counterpart of
    ``push_playbook`` (which is create-only). Three-pillar plan Track C2.

    **Always snapshots first.** Before the PUT lands, the playbook as it stands
    is frozen into its Versions tab (``_snapshot_playbook``) so the user's work
    is recoverable from the product UI. This gate is FAIL-CLOSED: no restore
    point, no edit. That is deliberate -- the ``$relationships=true`` cascade
    below is what makes the write succeed AND what makes it destructive (it
    replaces the nested step/route/group records), so an unsnapshotted AI edit
    can silently eat a hand-built playbook. ``push_playbook`` needs no snapshot:
    a create has nothing to recover.

    Targets the open playbook's workflow IRI (the widget seeds
    ``/api/3/workflows/<uuid>`` from ``main.playbookDetail``'s ``$state.params.id``).

    This is the designer's own Save, nothing else: GET the live workflow, graft
    its record identities onto the compiled body (``_graft_live_ids``), shape the
    body like ``preparePlaybookForSave`` (strip ``versions``, flatten
    ``stepType`` to its IRI, stamp ``lastModifyDate``), and PUT to
    ``/api/3/workflows/<uuid>?$relationships=true&$versions=true`` -- the call
    app.beautified.js ``ye()`` makes via the ``Modules`` $resource ``update``.
    ``$relationships=true`` cascades the PUT into the nested step/route/group
    records; the ``@id`` graft is what makes that cascade UPDATE them rather than
    try to insert over them (see ``_graft_live_ids`` for the 409 it fixes).

    There is deliberately **no fallback**. An ``import_jobs`` merge_replace used
    to sit behind this, and it was worse than nothing: re-importing the WHOLE
    collection to change one step is a far bigger blast radius than the designer
    ever takes, and live on 8.0 it returned ok while applying nothing -- so a PUT
    failure reached the user as a phantom success. One path, honestly reported.

    Returns ``{ok, workflow_iri, workflow_uuid, collection_uuid, method: "put",
    version_uuid, raw}``. ``version_uuid`` is the pre-edit restore point: the
    snapshot gate is fail-closed, so ok=True implies one exists. Off-platform
    returns the crudhub_unavailable stub.
    """
    _validate_model(UpdatePlaybookParams, params or {})
    workflow_json = _require(params, "workflow_json")
    workflow_iri = params.get("workflow_iri") or ""
    workflow_uuid = params.get("workflow_uuid") or ""
    if not workflow_uuid:
        workflow_uuid = _workflow_uuid_from_iri(workflow_iri) or ""
    if not workflow_uuid:
        return _err(
            "missing_workflow_target",
            "update_playbook needs the existing playbook's identity: pass "
            "workflow_iri (/api/3/workflows/<uuid>) or workflow_uuid. The widget "
            "seeds this from the open playbook in the designer.",
        )

    make_request, err = _require_transport()
    if err:
        return err

    collection = _unwrap_collection(workflow_json)
    if not isinstance(collection, dict):
        return _err("bad_collection", "workflow_json did not unwrap to a "
                    "collection record")
    workflows = collection.get("workflows") or []
    if not workflows or not isinstance(workflows[0], dict):
        return _err("no_workflow", "workflow_json has no workflow to update "
                    "(collection.workflows is empty)")
    # One playbook is updated in place. A collection with a sibling (a parent
    # plus the child it references) used to stage workflows[0] -- which the
    # compiler orders child first -- and report ok: the canvas became the
    # child, the parent was gone, and its reference pointed at a playbook that
    # was never created. Refuse instead of choosing.
    # The exception is a playbook the verified edit ADDS (`create_playbooks`,
    # from the card's playbooks_added): a new reusable child is created beside
    # the open playbook, and exactly one playbook -- the open one -- remains.
    create = {str(n) for n in (params.get("create_playbooks") or []) if n}
    new_wfs = [w for w in workflows if isinstance(w, dict) and str(w.get("name")) in create]
    rest = [w for w in workflows if isinstance(w, dict) and str(w.get("name")) not in create]
    missing = create - {str(w.get("name")) for w in new_wfs}
    if missing:
        return _err("bad_create_playbooks",
                    f"create_playbooks names {sorted(missing)} that are not in this edit")
    if len(rest) != 1:
        names = [str(w.get("name") or "?") for w in workflows if isinstance(w, dict)]
        return _err("multiple_playbooks",
                    f"this edit contains {len(workflows)} playbooks "
                    f"({', '.join(names)}), but only the open playbook can be "
                    "updated. Put the steps in the open playbook, or add the "
                    "new playbook with edit_playbook add_playbook so Apply "
                    "creates it.")
    wf = rest[0]
    # Align the body's uuid to the target so the PUT lands on the EXISTING
    # record -- whether it was created by this compiler (deterministic uuid) or
    # by the designer (random uuid). The URL uuid wins.
    wf["uuid"] = workflow_uuid
    endpoint = f"/api/3/workflows/{workflow_uuid}"

    # ONE read of the live playbook, serving two masters: it is what we freeze
    # into the restore point, and it carries the `@id` identities the PUT needs.
    # Reading once means the snapshot cannot drift from the body we graft.
    read = _read_live_workflow(make_request, workflow_uuid)
    if not read.get("ok"):
        return read
    live = read["workflow"]

    # HARDEN-1: refuse an edit that silently DELETES part of the playbook.
    # The `$relationships=true` PUT below replaces the nested step/route/group
    # records wholesale, so anything the model left out of its YAML is gone --
    # silently, with no error and no visible diff. That has shipped twice
    # (`for_each`, then declared `parameters`). We have the live copy in hand
    # right here, so this is the one place the loss is detectable.
    #
    # A snapshot makes the damage RECOVERABLE; it does not make it VISIBLE.
    # Nobody reads the Versions tab for a diff they were never told about, so
    # this gate runs regardless of the snapshot below.
    guard = _check_prewrite_loss(live, wf, params.get("acknowledged_drops"))
    if guard is not None:
        return guard

    # Freeze the playbook BEFORE touching it. Fail-closed: if we cannot create a
    # restore point we do not edit, because the PUT below is not reversible.
    session_id = params.get("session_id") or ""
    note = f"{_AI_SNAPSHOT_PREFIX} {session_id}".strip()
    snap = _snapshot_playbook(make_request, workflow_uuid, note, live)
    if not snap.get("ok"):
        return snap

    # New playbooks first, in the open playbook's own collection and under
    # their compiled uuids -- the reference step in the open playbook already
    # points at /api/3/workflows/<that uuid>. Before the open playbook is
    # touched: if a child cannot be created, nothing else changes.
    created: list[dict] = []
    if new_wfs:
        coll_ref = live.get("collection")
        if isinstance(coll_ref, dict):
            coll_ref = coll_ref.get("@id") or coll_ref.get("uuid")
        made = _upsert_new_playbooks(make_request, new_wfs, coll_ref, note)
        if not made.get("ok"):
            return made
        created = made["created"]

    # The designer's canvas Save (app.beautified.js `ye()` → the `Modules`
    # $resource `update` PUT) is:
    #   PUT /api/3/workflows/<uuid>?$relationships=true&$versions=true
    # `$relationships=true` cascades the PUT into the nested step/route/group
    # records; `_graft_live_ids` is what makes that cascade UPDATE them instead
    # of trying to insert over them (409, live-diagnosed on 8.0). The query is
    # inlined into the URL (not a params dict) because the on-platform crudhub
    # PUT bridge ignores params.
    body = _prepare_workflow_for_save(_graft_live_ids(wf, live))
    # An in-place edit must never re-point the playbook's collection. The
    # compiled body's `collection` is a deterministic uuid5 of the YAML's
    # collection NAME (emitter `collection|<name>`); when the live playbook
    # sits in a designer-created collection (random uuid), that reference
    # names a record that does not exist and the cascade PUT dies with a
    # ForeignKeyConstraintViolation 500 (live on 8.0: applying a verified
    # enhancement to a hand-made collection left the playbook empty). Pin the
    # body to the collection the playbook actually lives in.
    live_coll = live.get("collection")
    if isinstance(live_coll, dict):
        live_coll = live_coll.get("@id") or live_coll.get("uuid")
    if live_coll:
        body["collection"] = live_coll
    else:
        body.pop("collection", None)
    # Staged: the restore point exists and every guard has passed; hand the
    # playbook back for the designer to load as unsaved changes. The analyst's
    # Save in the designer is the write -- the same PUT as below, made by them.
    if params.get("stage"):
        canvas, cerr = _canvas_playbook(make_request, _graft_live_ids(wf, live), live)
        if cerr is not None:
            return cerr
        return {
            "ok": True,
            "staged": True,
            "workflow_iri": endpoint,
            "workflow_uuid": workflow_uuid,
            "version_uuid": snap["version_uuid"],
            "workflow": canvas,
            "created_playbooks": created,
        }
    put_endpoint = f"{endpoint}?$relationships=true&$versions=true"
    try:
        resp = make_request(put_endpoint, "PUT", body=body)
    except Exception as e:  # noqa: BLE001
        return _err(
            "update_failed",
            f"could not update playbook {workflow_uuid}: {e}. The edit was NOT "
            f"applied; the playbook is unchanged and its pre-edit restore point "
            f"is in the Versions tab.",
            workflow_iri=endpoint,
            workflow_uuid=workflow_uuid,
            version_uuid=snap["version_uuid"],
        )
    # Only an echoed record proves the write landed. Anything else is reported
    # as a failure rather than assumed -- the import_jobs fallback that used to
    # absorb this case returned ok while applying nothing, and a phantom success
    # is worse than an honest error.
    if not (isinstance(resp, dict) and resp.get("uuid") == workflow_uuid):
        return _err(
            "update_failed",
            f"could not update playbook {workflow_uuid}: the PUT returned no "
            f"matching record ({str(resp)[:200] if resp else 'empty'}). Treating "
            f"it as NOT applied. The pre-edit restore point is in the Versions "
            f"tab.",
            workflow_iri=endpoint,
            workflow_uuid=workflow_uuid,
            version_uuid=snap["version_uuid"],
        )
    return {
        "ok": True,
        "workflow_iri": endpoint,
        "workflow_uuid": workflow_uuid,
        "collection_uuid": collection.get("uuid"),
        "method": "put",
        "version_uuid": snap["version_uuid"],
        "created_playbooks": created,
        "raw": resp,
    }
