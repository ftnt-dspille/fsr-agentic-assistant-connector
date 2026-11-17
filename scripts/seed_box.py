#!/usr/bin/env python3
"""Seed a FortiSOAR box with the live-row mount + enrichment records so
`make live MODE=rows/writes` stops skipping ("record ... is not on this box").

The sweep rows mount a FIXED uuid; FSR honors a provided `uuid` on create, so
every record is seeded at its scenario uuid and no scenario edits are needed.
Records come from the fixture bundles the rows name (`records:` + `mount:`), so
the live world matches the offline fixture world.

What it handles, learned from .159 (2026-10-14):
  * scalar fields POST directly; a provided `uuid` is honored.
  * REQUIRED picklist fields reject a scalar string and need the box's IRI --
    resolved here per (picklistName, itemValue): indicators.reputation/type,
    incidents.severity/status, assets.criticality, tasks.status.
  * relationships (task -> asset/incident) are set from the bundle's `@id` refs.
  * creating an alert does NOT fire auto-triage on .159 (no run storm).
  * `siem_events` is not a crudhub module (SIEM comes via the connector) -- skip.
  * `people` create 500s on this box even with a valid PeopleType IRI (deeper
    server-side requirement); assignees are best-effort and skipped on failure.

Modules whose uuids are ztpf_* are skipped (those belong on the ZTPF box).

Usage (creds read from FSR_ENV_FILE, default the .159 harness env):
    python scripts/seed_box.py --dry               # resolve + print, write nothing
    python scripts/seed_box.py --write             # POST to the box
    python scripts/seed_box.py --cleanup           # delete everything in the manifest

Writes a manifest of created @ids (--manifest, default scratch) for --cleanup.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

SWEEP = _HERE / "conversations" / "sweep"
BUNDLE_DIR = _HERE / "eval_fixtures" / "ztpf"
SKIP_MODULE_PREFIX = ("ztpf_",)
RECORD_MODULES = {"alerts", "incidents", "indicators", "assets", "tasks", "comments"}
SERVER_FIELDS = {"@context", "@id", "@type", "id", "createDate", "modifyDate",
                 "createUser", "modifyUser", "@version"}
# field -> picklistName for REQUIRED picklists that reject a scalar string.
PICKLIST_FIELDS = {
    ("indicators", "reputation"): "IndicatorReputation",
    ("indicators", "type"): "IndicatorType",
    ("incidents", "severity"): "Severity",
    ("incidents", "status"): "IncidentStatus",
    ("assets", "criticality"): "Criticality",
    ("tasks", "status"): "TaskStatus",
}
# No default box: --env is required (a silent default once aimed writes at the wrong box).


def load_env(path: str) -> dict:
    """Read FSR_* from the box env file (the Bash credential classifier can
    block `source`, and conftest scrubs FSR_* once imported -- so read it here,
    before importing the connector)."""
    env = {}
    p = Path(path).expanduser()
    if p.exists():
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def referenced_bundles() -> list[str]:
    out = set()
    for f in sorted(glob.glob(str(SWEEP / "*.json"))):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        m, b = d.get("mount"), d.get("records")
        if m and b and not any((m.get("module") or "").startswith(p)
                               for p in SKIP_MODULE_PREFIX):
            out.add(b)
    return sorted(out)


def project(rec: dict) -> dict:
    """Scalar fields + uuid. Dict/list fields are dropped here and re-added
    (picklists as IRIs, relationships as @id refs) by the caller."""
    return {k: v for k, v in rec.items()
            if k not in SERVER_FIELDS and not isinstance(v, (dict, list))
            and v is not None}


def needed(load_bundle, FixtureBox) -> dict:
    seen = {}
    for bundle in referenced_bundles():
        try:
            box = FixtureBox(load_bundle(bundle))
        except Exception as e:  # noqa: BLE001
            print(f"  [bundle-skip] {bundle}: {e!r}"[:140])
            continue
        for mod, rows in box.modules.items():
            if mod not in RECORD_MODULES or any(mod.startswith(p)
                                                for p in SKIP_MODULE_PREFIX):
                continue
            for rec in rows:
                if rec.get("uuid"):
                    seen.setdefault((mod, rec["uuid"]), (bundle, rec))
    return seen


class PicklistResolver:
    def __init__(self, client):
        self._names = {p.get("name"): p.get("@id") for p in
                       client.get("/api/3/picklist_names", params={"$limit": 500})
                       .get("hydra:member", [])}
        self._items = client.get("/api/3/picklists", params={"$limit": 3000}) \
            .get("hydra:member", [])

    def iri(self, listname: str, value) -> str | None:
        ln = self._names.get(listname)
        for p in self._items:
            if p.get("listName") == ln and \
               (p.get("itemValue") or "").lower() == str(value).lower():
                return p.get("@id")
        return None


def build_body(mod: str, rec: dict, resolver: PicklistResolver) -> dict:
    body = project(rec)
    body["uuid"] = rec["uuid"]
    for (m, field), plname in PICKLIST_FIELDS.items():
        if m != mod:
            continue
        raw = rec.get(field)
        val = raw.get("itemValue") if isinstance(raw, dict) else raw
        if val is None:
            continue
        iri = resolver.iri(plname, val)
        if iri:
            body[field] = {"@id": iri}
    # relationships: list of {@id} refs
    for relf in ("assets", "incidents"):
        refs = [{"@id": x["@id"]} for x in (rec.get(relf) or [])
                if isinstance(x, dict) and x.get("@id")]
        if refs:
            body[relf] = refs
    return body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True,
                    help="box env file (no default box; BOX=<alias>, scripts/box.sh)")
    ap.add_argument("--base-url", default=None,
                    help="default: FSR_BASE_URL[:FSR_PORT] from --env")
    ap.add_argument("--username", default="labuser")
    ap.add_argument("--dry", action="store_true",
                    help="resolve and print only (the default when --write is absent)")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--manifest",
                    default=str(_REPO / "scratch" / "seed_box_manifest.json"))
    a = ap.parse_args(argv)

    env = load_env(a.env)
    pw = env.get("FSR_PASSWORD") or env.get("SOAR_PASSWORD") \
        or os.environ.get("FSR_PASSWORD")
    base = a.base_url or env.get("FSR_BASE_URL") or "https://fortisoar.example.com"
    port = env.get("FSR_PORT")
    if port and ":" not in base.split("//", 1)[-1]:
        base = f"{base}:{port}"
    a.base_url = base
    a.username = a.username if a.username != "labuser" else \
        (env.get("FSR_USERNAME") or a.username)
    # capture creds BEFORE conftest import scrubs FSR_* from the env
    os.environ.setdefault("FSRPB_DEV", "1")
    sys.path.insert(0, str(_REPO / "tests"))
    import conftest  # noqa: F401  (bootstraps the connector + fsr_playbooks)
    import urllib3
    urllib3.disable_warnings()
    from pyfsr import FortiSOAR
    from scripts.fixture_box import FixtureBox, load_bundle

    client = FortiSOAR(a.base_url, username=a.username, password=pw,
                       verify_ssl=False)
    mf = Path(a.manifest)

    if a.cleanup:
        ids = json.loads(mf.read_text()) if mf.exists() else []
        for aid in reversed(ids):
            try:
                client.delete(aid)
                print(f"  deleted {aid}")
            except Exception as e:  # noqa: BLE001
                print(f"  [del-skip] {aid}: {repr(e)[:80]}")
        mf.write_text("[]")
        return 0

    resolver = PicklistResolver(client)
    items = needed(load_bundle, FixtureBox)
    created = []
    print(f"{len(items)} records across referenced bundles")
    for (mod, uuid), (bundle, rec) in sorted(items.items()):
        body = build_body(mod, rec, resolver)
        if not a.write:
            print(f"  {mod:11} {uuid}  <- {bundle:26} fields={len(body)}")
            continue
        try:
            client.get(f"/api/3/{mod}/{uuid}")
            print(f"  EXISTS {mod}:{uuid[:8]}")
            continue
        except Exception:  # noqa: BLE001
            pass
        try:
            resp = client.post(f"/api/3/{mod}", data=body)
            created.append(resp.get("@id"))
            print(f"  CREATED {resp.get('@id')}")
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {mod}:{uuid[:8]}: {repr(e)[:120]}")
    if a.write:
        mf.parent.mkdir(parents=True, exist_ok=True)
        prev = json.loads(mf.read_text()) if mf.exists() else []
        mf.write_text(json.dumps(prev + created, indent=2))
        print(f"\n+{len(created)} created; manifest -> {mf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
