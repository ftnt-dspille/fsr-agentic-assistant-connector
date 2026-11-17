"""An open designer, offline: Apply lands in a canvas the next turn edits.

The widget's Apply from the designer sends `apply_to: "canvas"`; the connector
runs `update_playbook(stage=True)` -- the pre-write loss guard, a restore point
-- and hands back a `playbook_staged` card the widget loads as unsaved changes.
Its `yaml` then becomes the open playbook the next turn grounds on
(`$scope.playbookYaml`). Live, a driver that skipped that last step hid two
bugs: a second Apply was blocked by the first, and turn 2 edited the SAVED
(empty) copy so every edit failed "no steps yet".

Offline there is no box, so only the box I/O is replaced: the transport, the
read of the live playbook (answered with the CURRENT canvas, compiled), the
snapshot, and the canvas shaping. Everything between -- `_check_prewrite_loss`,
the id graft, the collection pin, the staged card -- is the connector's own.
"""
from __future__ import annotations

import contextlib
import sys
from typing import Any, Callable, Iterator, Optional


def _live_from_yaml(ops, yaml_text: str, workflow_uuid: str) -> Optional[dict]:
    """The canvas as `_read_live_workflow` would return it: the compiled
    workflow, with the `@id`s a GET carries. None when it does not compile --
    an empty designer playbook has nothing live to protect."""
    if not (yaml_text or "").strip():
        return None
    try:
        res = ops.compile_yaml({}, {"yaml": yaml_text})
    except Exception:  # noqa: BLE001
        return None
    if not (isinstance(res, dict) and res.get("ok") and res.get("workflow_json")):
        return None
    coll = ops._unwrap_collection(res["workflow_json"])
    wfs = (coll or {}).get("workflows") or []
    if not wfs or not isinstance(wfs[0], dict):
        return None
    wf = dict(wfs[0])
    wf["uuid"] = workflow_uuid
    wf["@id"] = f"/api/3/workflows/{workflow_uuid}"
    for s in wf.get("steps") or []:
        if isinstance(s, dict) and s.get("uuid"):
            s.setdefault("@id", f"/api/3/workflow_steps/{s['uuid']}")
    return wf


@contextlib.contextmanager
def offline_designer(ops, canvas_yaml: Callable[[], str]) -> Iterator[dict]:
    """Patch the box I/O under `update_playbook` for one Apply.

    `canvas_yaml()` returns what the designer shows right now. Yields a log
    dict: `snapshots` (restore points taken) -- an Apply that staged without
    one would be a connector regression, and the sim checks it."""
    log: dict[str, Any] = {"snapshots": 0, "reads": 0, "created": []}
    saved: dict = {}

    def _no_wire(*_a, **_k):
        raise AssertionError("offline designer: the stage path reached the wire")

    def _read(_mr, workflow_uuid):
        log["reads"] += 1
        live = _live_from_yaml(ops, canvas_yaml(), workflow_uuid)
        if live is None:
            # A brand-new designer playbook: the record exists, its steps don't.
            live = {"uuid": workflow_uuid, "@id": f"/api/3/workflows/{workflow_uuid}",
                    "steps": [], "routes": [], "groups": []}
        return {"ok": True, "workflow": live}

    def _snap(_mr, workflow_uuid, note, live):
        log["snapshots"] += 1
        return {"ok": True, "version_uuid": f"offline-version-{log['snapshots']}"}

    def _canvas(_mr, wf, live):
        return wf, None

    def _create(_mr, wfs, _collection, _note=""):
        # A new child playbook is a SAVE on the box; offline it is recorded,
        # so the sim can check what an Apply would have created.
        made = [{"name": w.get("name"), "uuid": w.get("uuid")} for w in wfs]
        log["created"].extend(made)
        return {"ok": True, "created": made}

    fakes = {
        "_require_transport": lambda *a, **k: (_no_wire, None),
        "_read_live_workflow": _read,
        "_snapshot_playbook": _snap,
        "_canvas_playbook": _canvas,
        "_upsert_new_playbooks": _create,
    }
    # Patch every connector module that binds the name (the facade and each domain module),
    # so whichever module calls the helper sees the fake.
    prefix = ops.__name__.rpartition(".")[0]
    sites = [ops] + [m for k, m in list(sys.modules.items())
                     if m is not None and k.startswith((f"{prefix}." if prefix else "") + "ops_")]
    for m in sites:
        for n, f in fakes.items():
            if n in vars(m):
                saved[(m, n)] = getattr(m, n)
                setattr(m, n, f)
    try:
        yield log
    finally:
        for (m, n), f in saved.items():
            setattr(m, n, f)


def staged_yaml(transcript: list) -> Optional[str]:
    """The `yaml` on the turn's `playbook_staged` card -- what the widget adopts
    as the open playbook. None when there is no staged card."""
    # chat_resume returns the wire envelope ({ok, ..., transcript: [...]}),
    # not the frame list; walk the frames the driver walks.
    from fsr_playbooks.harness.frames import frames
    for f in frames(transcript):
        if isinstance(f, dict) and f.get("type") == "info_card" \
                and f.get("variant") == "playbook_staged":
            y = f.get("yaml")
            return y if isinstance(y, str) else ""
    return None


def designer_mount(yaml_text: str) -> str:
    """A fixture playbook as the designer mount hands it over: compiled, then
    decompiled -- step uuids, slug routes and all. A hand-written fixture has
    no step uuids, so a rename in it can only look like a delete plus an add;
    the real mount carries them (KB 18.9). Falls back to the text as written
    when it does not compile (an empty playbook)."""
    if not (yaml_text or "").strip():
        return yaml_text
    try:
        from fsr_playbooks.compiler import compile_yaml
        from fsr_playbooks.compiler.decompiler import decompile_to_yaml
        from fsr_playbooks.mcp_server._shared import DB_PATH
        res = compile_yaml(yaml_text, DB_PATH)
        if not res.ok:
            return yaml_text
        return decompile_to_yaml(res.fsr_json, DB_PATH)
    except Exception:  # noqa: BLE001 -- the fixture as written is still usable
        return yaml_text
