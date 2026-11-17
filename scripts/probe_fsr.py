#!/usr/bin/env python3
"""Probe a LIVE FortiSOAR instance and capture golden responses.

Every endpoint the connector touches is exercised against the real API
and the raw JSON saved under tests/golden/. tests/test_fsr_contract.py
then asserts the connector's required key-paths still exist — so an FSR
upgrade that moves a field fails the suite instead of the widget.

ENV (required):
  FSR_BASE_URL   e.g. https://fsr-1   (no trailing /api)
  FSR_API_TOKEN  appliance/user API token  -- OR --
  FSR_USER + FSR_PASS  username/password (probe will /auth/authenticate)

ENV (optional):
  FSR_AUTH_HEADER  full Authorization header value, overrides scheme guess
  FSR_AUTH_SCHEME  "Bearer" (default) | "API-KEY" | "" (raw token)
  FSR_VERIFY_TLS   "1" to verify TLS (default off; appliances self-sign)

USAGE:
  python scripts/probe_fsr.py --phase readonly     # GETs + jinja render
  python scripts/probe_fsr.py --phase lifecycle    # push/trigger/resume/purge
  python scripts/probe_fsr.py --phase all          # both (default)

The lifecycle phase pushes a throwaway collection ("__probe_<epoch>"),
triggers it, drives the manual_input gate, then HARD-PURGES the
collection it created (by uuid only — never a broad delete; see the
fsr_purge_blast_radius memory). Pass --no-cleanup to leave it for
inspection.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("probe_fsr.py needs `requests` (pip install requests)")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
GOLDEN = REPO / "tests" / "golden"
PKG_DIR = REPO / "connector-fsr-agentic-assistant"


def re_has_port(url: str) -> bool:
    """True if the URL's host already specifies a :port."""
    import re
    # strip scheme, then look for :digits before a / or end.
    host = re.sub(r"^[a-z]+://", "", url, flags=re.I)
    return bool(re.search(r":\d+(/|$)", host))


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class FSR:
    def __init__(self) -> None:
        base = os.environ.get("FSR_BASE_URL", "").rstrip("/")
        if not base:
            sys.exit("set FSR_BASE_URL (e.g. https://fsr-1)")
        # Append FSR_PORT if the URL doesn't already carry one.
        port = os.environ.get("FSR_PORT")
        if port and not re_has_port(base):
            base = f"{base}:{port}"
        self.base = base
        # FSR_VERIFY_TLS (this script) or FSR_VERIFY_SSL (project .env);
        # accept 1/true/yes. Default: verify OFF (appliances self-sign).
        v = (os.environ.get("FSR_VERIFY_TLS")
             or os.environ.get("FSR_VERIFY_SSL") or "").lower()
        self.verify = v in ("1", "true", "yes")
        if not self.verify:
            import urllib3
            urllib3.disable_warnings()
        self.s = requests.Session()
        self.s.headers["Authorization"] = self._auth_header()
        self.s.headers["Content-Type"] = "application/json"

    def _auth_header(self) -> str:
        override = os.environ.get("FSR_AUTH_HEADER")
        if override:
            return override
        token = os.environ.get("FSR_API_TOKEN")
        if token:
            scheme = os.environ.get("FSR_AUTH_SCHEME", "Bearer")
            return f"{scheme} {token}".strip()
        user = os.environ.get("FSR_USER") or os.environ.get("FSR_USERNAME")
        pw = os.environ.get("FSR_PASS") or os.environ.get("FSR_PASSWORD")
        if user and pw:
            # FortiSOAR /auth/authenticate accepts {credentials:{loginid,
            # password}} and returns {token: <jwt>}.
            r = requests.post(f"{self.base}/auth/authenticate",
                              json={"credentials": {"loginid": user,
                                                    "password": pw}},
                              verify=self.verify, timeout=30)
            if not r.ok:
                sys.exit(f"auth failed {r.status_code}: {r.text[:300]}")
            tok = r.json().get("token")
            if not tok:
                sys.exit(f"auth ok but no token in {r.json()!r}")
            return f"Bearer {tok}"
        sys.exit("set FSR_API_TOKEN or FSR_USER(NAME)+FSR_PASS(WORD)")

    def req(self, path: str, method: str = "GET", **kw):
        url = path if path.startswith("http") else f"{self.base}{path}"
        r = self.s.request(method, url, verify=self.verify, timeout=60, **kw)
        try:
            body = r.json()
        except ValueError:
            body = {"__nonjson__": r.text[:2000]}
        return r.status_code, body


def save(name: str, body) -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    (GOLDEN / f"{name}.json").write_text(json.dumps(body, indent=2, default=str))
    print(f"  saved tests/golden/{name}.json")


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------

def phase_readonly(fsr: FSR) -> None:
    print("[readonly]")
    # Connector catalog: custom envelope at /api/integration/connectors/
    # (NOT /api/3/connectors/). Verified live.
    code, body = fsr.req("/api/integration/connectors/?page_size=1", "GET")
    print(f"  connectors GET -> {code}")
    save("connectors_list", body)

    code, body = fsr.req("/api/wf/api/jinja-editor/", "POST",
                         json={"template": "{{ 1 + 1 }} {{ vars.x | upper }}",
                               "values": {"vars": {"x": "hi"}}})
    print(f"  jinja-editor POST -> {code}")
    save("jinja_editor", body)

    # Pending manual inputs (POST; GET returns 405). Verified live. Shows
    # the id/step_id/type/workflow the canonical resume PUT needs.
    code, body = fsr.req("/api/wf/api/manual-wf-input/list_wfinput/",
                         "POST", json={})
    print(f"  manual-wf-input list POST -> {code}")
    save("manual_input_list", body)


# Throwaway playbook with an INPUT-based manual_input gate that captures a
# free-text token, then a set_variable that echoes that token into the
# run's final vars. Because the token is supplied at RESUME time and shows
# up in the finished run's result, we can prove we resumed+inputted the
# RIGHT run by checking each run's final `final_token` equals the distinct
# token we passed it. Self-contained (no dependency on fsr-playbook-framework).
_PROBE_YAML = """\
collection: __PROBE_COLLECTION__
description: Throwaway probe playbook (safe to purge).
visible: true

playbooks:
  - name: Probe Manual Input
    is_active: true
    description: Pause on an input gate; echo the submitted token to vars.
    steps:
      - name: start
        type: start
        next: Approve probe

      - name: Approve probe
        type: manual_input
        options:
          - display: submit
            primary: true
            next: Echo token
          - display: cancel
            next: Cancelled
        arguments:
          title: Probe gate
          description: Enter a token, then click submit (NOT cancel).
          inputs:
            - name: probe_token
              label: Token
              kind: text

      - name: Echo token
        type: set_variable
        vars:
          final_token: "{{ vars.steps['Approve probe'].input.probe_token }}"

      - name: Cancelled
        type: set_variable
        vars:
          final_token: CANCELLED
"""


def _compile_probe_collection(coll_name: str) -> dict:
    """Compile examples/manual_input_then_act.yaml to a WorkflowCollection
    JSON via the connector's own compiler (same path as the compile_yaml
    op), renamed to a throwaway collection."""
    # fsr_playbooks lives either as a top-level install or vendored under the
    # connector package; add both candidate roots.
    sys.path.insert(0, str(PKG_DIR))
    sys.path.insert(0, str(REPO))
    import fsr_playbooks  # type: ignore

    # Reference DB: env override, else the fsr-playbook-framework store layout,
    # else the vendored sibling.
    db = os.environ.get("FSR_REFERENCE_DB")
    if not db:
        root = Path(fsr_playbooks.__file__).resolve().parent
        for cand in (root.parent / "store" / "fsr_reference.db",
                     root / "reference" / "fsr_reference.db"):
            if cand.exists():
                db = str(cand)
                break

    src = _PROBE_YAML.replace("__PROBE_COLLECTION__", coll_name)
    result = fsr_playbooks.compile_yaml(src, db)
    if not getattr(result, "ok", False):
        errs = [getattr(e, "to_dict", lambda: e)() for e in (result.errors or [])]
        raise RuntimeError(f"compile failed: {errs}")
    return result.fsr_json


_AWAITING = {"awaiting"}
_TERMINAL = {"finished", "failed", "terminated", "finished_with_error",
             "rejected"}


def phase_lifecycle(fsr: FSR, cleanup: bool) -> None:
    """Bulletproof cross-talk proof: stand up N independent runs, each
    paused on its own input gate, resume them by ID (reverse order, each
    with a DISTINCT token), then prove every run's finished result echoes
    exactly the token WE handed THAT run — never another run's."""
    print("[lifecycle] multi-run manual-input cross-talk proof")
    n = int(os.environ.get("PROBE_RUNS") or 3)
    epoch = int(os.environ.get("PROBE_EPOCH") or time.time())

    # 1. Create + trigger N runs, each its own collection + distinct token.
    runs: list[dict] = []
    for i in range(n):
        coll_name = f"__probe_{epoch}_{i}"
        token = f"tok-{epoch}-{i}"
        try:
            wj = _compile_probe_collection(coll_name)
        except Exception as e:
            print(f"  !! compile failed: {e}")
            return
        collection = (wj.get("data") or [wj])[0]
        c, push = fsr.req("/api/3/workflow_collections", "POST", json=collection)
        if i == 0:
            save("workflows_push", push)
        wfs = (push or {}).get("workflows") or []
        if not wfs:
            print(f"  !! run {i}: push returned no workflows ({c})")
            continue
        coll_uuid = (push or {}).get("uuid")
        wf_uuid = wfs[0].get("uuid")
        c, trig = fsr.req(f"/api/triggers/1/notrigger/{wf_uuid}", "POST",
                          json={"input": {}, "request": {"data": {}},
                                "useMockOutput": False, "globalMock": False})
        if i == 0:
            save("trigger", trig)
        task_id = (trig or {}).get("task_id")
        # task_id is the anchor: poll filters by it, so run_pk is provably
        # the run for THIS trigger. Keep both for traceability.
        run_pk = _poll_run(fsr, task_id, until=_AWAITING | _TERMINAL)
        print(f"  run {i}: coll={coll_uuid} task_id={task_id} "
              f"run_pk={run_pk} token={token}")
        runs.append({"i": i, "coll_uuid": coll_uuid, "task_id": task_id,
                     "run_pk": run_pk, "token": token})

    # 2. Resume in REVERSE order (stresses correlation): each strictly by
    #    id, with its own distinct token.
    for r in reversed(runs):
        r["resumed"] = (_resume_by_id(fsr, r["run_pk"], r["token"],
                                      save_golden=(r["i"] == 0))
                        if r["run_pk"] else False)

    # 3. Verify each run's finished result echoes ITS OWN token.
    results = []
    for r in runs:
        got = _final_token(fsr, r["run_pk"]) if r["run_pk"] else None
        passed = got is not None and got == r["token"]
        results.append({"i": r["i"], "task_id": r.get("task_id"),
                        "run_pk": r["run_pk"], "token": r["token"],
                        "got": got, "passed": passed})
        print(f"  verify run {r['i']} (pk {r['run_pk']}): "
              f"expected {r['token']!r} got {got!r} -> "
              f"{'PASS' if passed else 'FAIL'}")

    all_pass = bool(results) and all(x["passed"] for x in results)
    save("lifecycle_verification", {"runs": results, "all_pass": all_pass})
    n_pass = sum(x["passed"] for x in results)
    print(f"  >>> CROSS-TALK PROOF: {n_pass}/{len(results)} runs returned "
          f"their own token; all_pass={all_pass}")

    # 4. CLEANUP — purge each collection we created (NO trailing slash;
    #    hardDelete skips the recycle bin — see fsr_recycle_bin_behavior).
    if cleanup:
        for r in runs:
            if r["coll_uuid"]:
                c, _ = fsr.req(
                    f"/api/3/workflow_collections/{r['coll_uuid']}?hardDelete=true",
                    "DELETE")
                print(f"  purge {r['coll_uuid']} -> {c}")


def _poll_run(fsr: FSR, task_id, until: set, timeout_s: int = 120):
    """Poll our run (by task_id) until its status is in `until`. Saves the
    last listing as the workflow_poll golden. Returns the run's integer pk
    (or None)."""
    deadline = time.time() + timeout_s
    run_pk = None
    while time.time() < deadline:
        _, listing = fsr.req(
            "/api/wf/api/workflows/?format=json&limit=1&ordering=-modified"
            f"&task_id={task_id}&parent_wf__isnull=True", "GET")
        members = (listing or {}).get("hydra:member") or []
        if members:
            run = members[0]
            save("workflow_poll", listing)
            run_pk = (run.get("@id") or "").rstrip("/").rsplit("/", 1)[-1]
            st = run.get("status")
            if st in until:
                print(f"  poll run {run_pk} -> {st!r}")
                return run_pk
        time.sleep(2)
    print(f"  poll run {run_pk} -> TIMEOUT")
    return run_pk


def _run_pending_step_ids(fsr: FSR, run_pk: str, save_golden: bool) -> set:
    """The execution-step ids of THIS run that are currently awaiting. We
    take BOTH the numeric `id` and the int parsed from the step `@id`
    (`/wf/api/steps/<int>/`) since the field present varies by build."""
    _, detail = fsr.req(
        f"/api/wf/api/workflows/{run_pk}/?format=json&step_detail=true", "GET")
    detail = detail if isinstance(detail, dict) else {}
    if save_golden:
        save("workflow_detail", detail)
    ids: set = set()
    for s in detail.get("steps") or []:
        if s.get("status") not in ("awaiting", "processing", "incipient"):
            continue
        if isinstance(s.get("id"), int):
            ids.add(s["id"])
        tail = str(s.get("@id") or "").rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            ids.add(int(tail))
    return ids


def _awaiting_input_for_run(fsr: FSR, run_pk: str, save_golden: bool,
                            retries: int = 15, delay: float = 2.0):
    """Identify the pending manual-input belonging to THIS run, strictly by
    id: run's awaiting execution-step ids ∩ list_wfinput member.step_id
    (`workflow` is an encrypted token, so step_id is the only honest link).
    Retries because the manual-input record can lag behind status=awaiting.
    Returns the target enriched with response_mapping, or None."""
    for attempt in range(retries):
        pending_step_ids = _run_pending_step_ids(fsr, run_pk, save_golden)
        _, listing = fsr.req(
            "/api/wf/api/manual-wf-input/list_wfinput/?format=json&limit=200",
            "POST", json={})
        if save_golden:
            save("manual_input_list", listing)
        items = (listing or {}).get("hydra:member") or []
        target = next((it for it in items
                       if it.get("step_id") in pending_step_ids), None)
        if target is not None:
            # The list omits response_mapping (buttons); retrieve_wfinput has it.
            _, rr = fsr.req(
                f"/api/wf/api/manual-wf-input/{target['id']}/retrieve_wfinput/"
                "?format=json", "POST", json={})
            if isinstance(rr, dict):
                target = {**target, **rr}
            return target
        time.sleep(delay)
    print(f"  !! run {run_pk}: no pending input matched our step ids "
          f"after {retries} tries — NOT resuming")
    return None


def _resume_by_id(fsr: FSR, run_pk: str, token: str,
                  save_golden: bool = False) -> bool:
    """Resume THIS run's gate by id, choosing the `submit` button (NOT
    cancel) and submitting `probe_token=token`. The chosen option is
    conveyed via its `step_iri` in the wfinput_resume body — picking the
    wrong button routes to a different step and the token never lands."""
    target = _awaiting_input_for_run(fsr, run_pk, save_golden)
    if target is None:
        return False

    options = (target.get("response_mapping") or {}).get("options") or []
    chosen = next((o for o in options if o.get("option") == "submit"), None)
    if chosen is None:
        print(f"  !! run {run_pk}: no 'submit' option among "
              f"{[o.get('option') for o in options]}")
        return False

    body = {
        "input": {"probe_token": token},
        "step_iri": chosen.get("step_iri"),
        "step_id": target.get("step_id"),
        "manual_input_id": int(target.get("id")),
    }
    code, resp = fsr.req(
        f"/api/wf/api/workflows/{run_pk}/wfinput_resume/?format=json",
        "POST", json=body)
    print(f"  resume run {run_pk} input#{target.get('id')} "
          f"option='submit' token={token!r} -> {code}")
    if save_golden:
        save("manual_input_resume", {
            "method": "POST",
            "endpoint": f"/api/wf/api/workflows/{run_pk}/wfinput_resume/",
            "request_body": body, "status": code, "response": resp})
    return code in (200, 201, 204)


def _final_token(fsr: FSR, run_pk: str, timeout_s: int = 60):
    """Poll the run to terminal and return its final `env.final_token` (the
    value the Echo-token set_variable captured from the submitted input)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _, d = fsr.req(f"/api/wf/api/workflows/{run_pk}/?format=json", "GET")
        d = d if isinstance(d, dict) else {}
        if d.get("status") in _TERMINAL:
            save("workflow_final", d)
            return (d.get("env") or {}).get("final_token")
        time.sleep(2)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["readonly", "lifecycle", "all"],
                    default="all")
    ap.add_argument("--no-cleanup", action="store_true")
    args = ap.parse_args()

    fsr = FSR()
    print(f"probing {fsr.base}  (TLS verify={fsr.verify})")
    if args.phase in ("readonly", "all"):
        phase_readonly(fsr)
    if args.phase in ("lifecycle", "all"):
        phase_lifecycle(fsr, cleanup=not args.no_cleanup)
    print("done.")


if __name__ == "__main__":
    main()
