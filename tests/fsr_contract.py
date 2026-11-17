"""The FortiSOAR API contract the connector depends on.

Source of truth for endpoints + shapes is the curated OpenAPI spec in the
fortisoar-api-docs checkout (build/fortisoar.curated.openapi.yaml +
build/curated_verification.json), which was live-verified against 7.6.5.

Each entry names a probe (one captured live response, saved under
`tests/golden/<probe>.json` by `scripts/probe_fsr.py`) and the exact
response key-paths the connector *reads* in operations.py. If an FSR
upgrade moves or renames one of these, re-probing + running
`test_fsr_contract.py` fails loudly.

`verified`: whether the upstream curated spec marks this endpoint
x-verified-live. UNVERIFIED entries are documented-only — treat their
required/soft paths as best-effort until our own probe captures a golden.
Mismatches on unverified probes are warnings to chase, not proof of
breakage.

Path syntax (dotted, evaluated against the parsed JSON):
  - "a.b.c"        nested dict keys
  - "a.b[].c"      `a.b` is a non-empty list; check key `c` on element 0
  - keys may contain ':' (hydra:member) — kept verbatim

`alt_paths`: list of path-groups where AT LEAST ONE group must fully hold
(for endpoints whose shape legitimately varies).
"""
from __future__ import annotations

CONTRACT: dict[str, dict] = {
    # warmup(): connector list. VERIFIED endpoint, custom (non-hydra)
    # envelope: {status, totalItems, data[], itemsPerPage, nextPage}.
    # NOTE: the connector previously hit /api/3/connectors/ (404) and
    # parsed hydra:member — fixed to this shape.
    "connectors_list": {
        "endpoint": "/api/integration/connectors/?page_size=1",
        "method": "GET",
        "verified": True,
        "required": [
            "data",
            "data[].id",
            "data[].name",
            "data[].version",
        ],
        "soft": [
            "data[].status",
            "data[].configuration",
            "totalItems",
            "nextPage",
        ],
    },

    # render_jinja(): VERIFIED. Reader accepts result/output/rendered/value.
    "jinja_editor": {
        "endpoint": "/api/wf/api/jinja-editor/",
        "method": "POST",
        "verified": True,
        "required": ["result"],
        "soft": ["output", "rendered", "value"],
    },

    # push_playbook()/dry_run_playbook(): strict create is
    # POST /api/3/workflow_collections with the UNWRAPPED collection body
    # (NOT the {type,data} export wrap, and NOT /api/3/workflows/ which
    # 404s). VERIFIED live (201). Response is the created collection
    # record: top-level {uuid} + workflows[].uuid.
    "workflows_push": {
        "endpoint": "POST /api/3/workflow_collections (unwrapped collection)",
        "method": "POST",
        "verified": True,
        "required": ["uuid", "workflows[].uuid"],
        "soft": ["name", "workflows[].name"],
    },

    # dry_run_playbook() trigger. VERIFIED (200). task_id is top-level here;
    # the API guide notes some trigger flavors nest it under data.
    "trigger": {
        "endpoint": "/api/triggers/1/notrigger/<wf_uuid>",
        "method": "POST",
        "verified": True,
        "required": [],
        "alt_paths": [["task_id"], ["data.task_id"]],
    },

    # dry_run_playbook() poll: GET /api/wf/api/workflows/. VERIFIED. Hydra.
    "workflow_poll": {
        "endpoint": "/api/wf/api/workflows/?task_id=<task_id>&parent_wf__isnull=True&limit=1",
        "method": "GET",
        "verified": True,
        "required": [
            "hydra:member",
            "hydra:member[].status",
            "hydra:member[].@id",
        ],
        "soft": ["hydra:member[].steps", "hydra:member[].result"],
    },

    # Pending manual inputs: POST /api/wf/api/manual-wf-input/list_wfinput/.
    # VERIFIED. Each member carries the id + step_id + type + workflow the
    # canonical resume PUT needs.
    "manual_input_list": {
        "endpoint": "/api/wf/api/manual-wf-input/list_wfinput/",
        "method": "POST",
        "verified": True,
        "required": ["hydra:member"],
        "soft": [
            "hydra:member[].id",
            "hydra:member[].step_id",
            "hydra:member[].type",
            "hydra:member[].workflow",
        ],
    },

    # Canonical manual-input resume for `awaiting` runs. VERIFIED live
    # end-to-end on 8.0.0 (run advanced to `finished` afterward):
    #   POST /api/wf/api/workflows/{run_pk}/wfinput_resume/
    #   body {input:<dict>, step_iri:<chosen option's next step>,
    #         step_id, manual_input_id}
    # The chosen button is conveyed via its `step_iri`. Correlate the gate
    # by id: run's awaiting step ids (step_detail=true) ∩ list member
    # step_id. CORRECTION: `PUT /api/wf/api/manual-wf-input/{id}/` returns
    # 200 but does NOT advance the run — it is NOT the resume path. The
    # probe wraps the real response under "response" (carries task_id).
    "manual_input_resume": {
        "endpoint": "POST /api/wf/api/workflows/<run_pk>/wfinput_resume/",
        "method": "POST",
        "verified": True,
        "required": ["response.task_id"],
        "soft": ["response.message"],
    },
}


def split_path(path: str) -> list[tuple[str, bool]]:
    """Parse a dotted path into [(key, is_list_element), ...]. A segment
    ending in "[]" means: value at `key` is a non-empty list; remaining
    path is checked against element 0."""
    out: list[tuple[str, bool]] = []
    for seg in path.split("."):
        if seg.endswith("[]"):
            out.append((seg[:-2], True))
        else:
            out.append((seg, False))
    return out


def path_holds(body, path: str) -> bool:
    """True iff `path` resolves to an existing (non-None) value in `body`."""
    cur = body
    for key, is_list in split_path(path):
        if not isinstance(cur, dict) or key not in cur:
            return False
        cur = cur[key]
        if is_list:
            if not isinstance(cur, list) or not cur:
                return False
            cur = cur[0]
    return cur is not None
