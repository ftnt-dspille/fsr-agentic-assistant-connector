#!/usr/bin/env python3
"""Record a real playbook's FAILED-RUN evidence into a replayable bundle.

The troubleshooting rows ask "this playbook keeps failing -- why?", and the
honest answer lives in the run history: which step failed, with what error,
on what rendered arguments. No record table can answer that, so this records
it. Against a live box (env sourced), it drives the same read tools the
assistant uses -- `list_playbook_runs`, `why_did_playbook_fail`,
`get_run_env` -- through a pyfsr client whose transport captures every
request and response, and writes them as the bundle's `http:` section.
`local_turn` replays that section through a transport adapter, so pyfsr's
code runs unchanged against the box's own bytes. Record once, replay forever.

It also writes the playbook YAML (decompiled from the box) for the row to
mount with `"playbook": "<file>.yaml"`.

Redaction happens on the way in: the box host and the lab management range
are replaced, and the run fails if either survives.

Usage (connector venv, box creds in the env):
  set -a; . <harness>/.env.159; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/record_runs.py \\
      --playbook "Refresh Latest Alert Details" --name run_failed_double_iri
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "connector-fsr-agentic-assistant"))

from scripts.record_world import BUNDLE_DIR, _LAB_RANGE, _redactor  # noqa: E402

FIXTURES = HERE / "eval_fixtures"


class _Recorder:
    """A `requests` adapter that forwards to the real transport and keeps
    (method, path, query, status, json body) for every exchange."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.entries: list[dict] = []

    def send(self, request, **kw):
        resp = self._inner.send(request, **kw)
        u = urlparse(request.url)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if body is not None:
            self.entries.append({
                "method": request.method.upper(), "path": u.path,
                "query": dict(parse_qsl(u.query, keep_blank_values=True)),
                "status": resp.status_code, "body": body})
        return resp

    def close(self):
        self._inner.close()


def _dedupe(entries: list[dict]) -> list[dict]:
    """Last response per (method, path, query) -- a retried read keeps the
    final answer, and replay matches first-wins."""
    out: dict[str, dict] = {}
    for e in entries:
        out[json.dumps([e["method"], e["path"], e["query"]], sort_keys=True)] = e
    return list(out.values())


def _only_this_playbook(entries: list[dict], playbook: str) -> list[dict]:
    """Run-history LIST responses carry every recent run on the box (the
    failed-run provider is not filtered by playbook), with full envs -- MBs
    of other playbooks' data. Keep this playbook's runs only: replay then
    answers "what failed" with this box's truth about THIS playbook, and
    nothing unrelated rides into the repo."""
    for e in entries:
        if not e["path"].rstrip("/").endswith(("/api/wf/api/workflows",
                                               "/api/wf/api/historical-workflows")):
            continue
        body = e.get("body")
        if isinstance(body, dict) and isinstance(body.get("hydra:member"), list):
            keep = [m for m in body["hydra:member"]
                    if isinstance(m, dict) and m.get("name") == playbook]
            body["hydra:member"] = keep
            body["hydra:totalItems"] = len(keep)
    return entries


def _drop_request_headers(obj: Any) -> Any:
    """A run env carries the triggering HTTP request's headers -- the caller's
    address (`x-fs-real-ip`), the box host and port. None of it is run
    evidence, all of it is provenance a public repo must not hold."""
    if isinstance(obj, dict):
        return {k: ({} if k == "headers" and isinstance(v, dict)
                    else _drop_request_headers(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_drop_request_headers(v) for v in obj]
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--playbook", required=True, help="playbook name on the box")
    ap.add_argument("--name", required=True, help="bundle + yaml file stem")
    ap.add_argument("--extends", default="soc_alert_surface")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    bundle_path = BUNDLE_DIR / f"{a.name}.json"
    yaml_path = FIXTURES / f"{a.name}.yaml"
    if (bundle_path.exists() or yaml_path.exists()) and not a.force:
        sys.exit(f"{bundle_path.name} / {yaml_path.name} exist (--force to overwrite)")
    base = os.environ.get("FSR_BASE_URL")
    if not base:
        sys.exit("FSR_BASE_URL not set -- source the box env file first")
    scrub = _redactor(urlparse(base if "//" in base else f"https://{base}").hostname or "")

    from scripts.local_turn import _bootstrap
    _bootstrap()
    import probes._env as penv
    from fsr_playbooks.mcp_server import _shared

    client = penv.get_client()
    if client is None:
        sys.exit("no live client -- check the FSR_* env")
    rec = _Recorder(client.session.get_adapter("https://"))
    client.session.mount("https://", rec)
    penv.get_client = lambda: client
    _shared._LIVE_CLIENT_CACHE["client"] = client

    import fsr_soc_triage.registry  # noqa: F401  -- registers the failed-run provider
    from fsr_playbooks.mcp_server.tools_connector_discovery import (
        get_run_env, list_playbook_runs)
    from fsr_playbooks.mcp_server.tools_recipe import why_did_playbook_fail

    failed = list_playbook_runs(playbook=a.playbook)
    list_playbook_runs(playbook=a.playbook, include_finished=True)
    runs = failed.get("runs") or []
    if not runs:
        sys.exit(f"no failed run of {a.playbook!r} on the box -- run it first")
    diag = why_did_playbook_fail(a.playbook)
    for r in runs[:2]:
        for ident in (r.get("pk"), r.get("task_id")):
            if ident:
                get_run_env(str(ident))

    from cli import _decompile_to_yaml, _fetch_workflow_with_refs  # type: ignore
    coll = _fetch_workflow_with_refs(client, a.playbook)
    if coll is None:
        sys.exit(f"could not pull {a.playbook!r} back from the box")
    playbook_yaml = _decompile_to_yaml(coll, _shared.DB_PATH)

    entries = scrub(_only_this_playbook(_dedupe(rec.entries), a.playbook))
    entries = _drop_request_headers(entries)
    text = json.dumps(entries)
    text = re.sub(r"fortisoar\.example\.com:\d+", "fortisoar.example.com", text)
    entries = json.loads(text)
    leftover = (_LAB_RANGE.search(text + playbook_yaml)
                or re.search(r"x-fs-(?:real-ip|forwarded-for)", text))
    if leftover:
        sys.exit(f"redaction left box/caller detail behind: {leftover.group(0)}")
    bundle = {
        "_README": [
            f"Failed-run evidence for the playbook {a.playbook!r}, recorded from a",
            "live appliance by scripts/record_runs.py: the run history, run envs",
            "and failure projection the diagnosis tools read, replayed through",
            "pyfsr's own client offline. The playbook itself is "
            f"eval_fixtures/{yaml_path.name}.",
        ],
        "extends": a.extends,
        "modules": {},
        "http": entries,
    }
    bundle_path.write_text(json.dumps(bundle, indent=2) + "\n")
    yaml_path.write_text(playbook_yaml)
    print(f"wrote {bundle_path.relative_to(HERE.parent)} ({len(entries)} "
          f"responses) and {yaml_path.relative_to(HERE.parent)}")
    print(f"diagnosis on the box: failing_step={diag.get('failing_step')!r} "
          f"error={str(diag.get('error_message') or '')[:160]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
