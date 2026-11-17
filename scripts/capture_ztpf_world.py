#!/usr/bin/env python3
"""Capture a scrubbed ZTPF world from a live box into a fixture-box bundle.

Real analyst questions ("what has run on this device", "compare the get system
status steps", "which devices has this action run on") can only be graded
against a world that has the records they ask about. This reads one device's
full history plus the authoring modules and the ZTPF record-action playbooks,
READ-ONLY, and writes a bundle `fixture_box` serves.

    .venv/bin/python scripts/capture_ztpf_world.py --env <path/to/.env> \\
        --device <device uuid> --out scripts/eval_fixtures/ztpf/<name>.json

Scrubbing, because the bundle ships in the repo: people fields are dropped,
IPv4 addresses map to documentation ranges, serial numbers and manager
hostnames are replaced, e-mail addresses are replaced, and example/output blobs
that carry encrypted secrets or are too large to be a useful fixture are cut.
Nothing about the box itself (host, credentials, capture date) is written.
Personas are NOT read from the box: they come from `--personas-from` (an
existing bundle), so a box without the persona module still yields a world.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

AUTHORING = ["ztpf_automation_profiles", "ztpf_automation_profile_steps",
             "ztpf_automation_actions", "ztpf_templates", "ztpf_metadata_sources"]
DROP_KEYS = {"createUser", "modifyUser", "@context", "adminRPCPassword",
             "adminRPCUser", "connectorConfig", "sourceData"}
# Values kept verbatim (never reduced to a relation stub); every value is still
# size-capped and secret-checked by `_cut`.
BLOB_KEYS = {"exampleJinjaVars", "exampleOutputJSON", "exampleOutputText",
             "exampleOutputTextHTML", "exampleOutputTextMarkdown", "exampleOutput",
             "exampleScriptRendered", "outputTemplateJSON", "outputSourceData",
             "outputJSON", "jinjaVars", "stepARGS", "outputText",
             "outputMarkdown", "script"}
BLOB_MAX = 6000
STUB_KEYS = ("@id", "@type", "name", "uuid", "itemValue", "stepSequence",
             "stepNumber", "color")


def _stub(v: Any) -> Any:
    if isinstance(v, dict) and "@id" in v:
        return {k: v[k] for k in STUB_KEYS if k in v}
    if isinstance(v, list):
        return [_stub(x) for x in v]
    return v


_SECRET = re.compile(r'"ENC"|(?i:passw(or)?d|secret|api[_-]?key|token)\W{0,4}[:=]\s*["\']?[^\s"\',]{3,}'
                     r'|(?i:"(old-)?passw(or)?d"\s*:\s*\[)')


def _cut(v: Any) -> Any:
    """Every value, not a named list of fields: device dumps turn up in fields
    nobody thought to name (`exampleOutput`, `jinjaVars`), and a secret in an
    un-listed field ships just the same."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    s = v if isinstance(v, str) else json.dumps(v)
    if _SECRET.search(s):
        return None
    if len(s) > BLOB_MAX:
        return (s[:BLOB_MAX] + "\n...[truncated in fixture]") if isinstance(v, str) else None
    return v


def _flat(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if k in DROP_KEYS:
            continue
        v = _stub(v) if k not in BLOB_KEYS else v
        out[k] = v if (isinstance(v, dict) and "@id" in v) else _cut(v)
    return out


_DOC = ("192.0.2.", "198.51.100.", "203.0.113.")


class Scrubber:
    def __init__(self, redact: dict[str, str] | None = None) -> None:
        # Box-specific words (an admin account named after a person) the
        # caller names on the command line, so none is written here.
        self.redact = redact or {}
        self.ips: dict[str, str] = {}
        self.serials: dict[str, str] = {}
        self.hosts: dict[str, str] = {}

    def _ip(self, mo: re.Match) -> str:
        a = mo.group(0)
        if a.startswith(("0.", "255.") + _DOC) or a == "127.0.0.1":
            return a   # already a documentation address: never remap twice
        if a not in self.ips:
            n = len(self.ips)
            pool = _DOC[min(n // 250, 2)]
            self.ips[a] = f"{pool}{n % 250 + 1}"
        return self.ips[a]

    def text(self, t: str) -> str:
        # Digit lookarounds, not `\b`: this runs over JSON text, where a
        # newline is the two characters `\n`, so an address at the start of a
        # line reads `n10.1.2.3` and has no word boundary. `\b` let every
        # routing-table row through.
        t = re.sub(r"(?<![\d.])\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?!\d)", self._ip, t)
        t = re.sub(r"(?<![A-Z0-9.-])FMG-\d+\.\d+\b",
                   lambda m: self.hosts.setdefault(m.group(0), f"FMG-{len(self.hosts) + 1:02d}"), t)
        t = re.sub(r"(?<![A-Z0-9])(FGVM|FMG|FAZ|FGT|FG)[A-Z0-9]{2,4}\d{6,}(?!\d)",
                   lambda m: self.serials.setdefault(
                       m.group(0), f"{m.group(1)}SERIAL{len(self.serials) + 1:04d}"), t)
        for word, repl in self.redact.items():
            t = re.sub(re.escape(word), repl, t, flags=re.I)
        return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "analyst@example.com", t)


_VERIFY: Any = True   # set from the env file's FSR_VERIFY_SSL in main()


def _members(session: Any, base: str, path: str) -> list[dict]:
    r = session.get(base + path, verify=_VERIFY)
    r.raise_for_status()
    return r.json().get("hydra:member") or []


def _query(session: Any, base: str, module: str, filters: list, limit: int = 2000) -> list[dict]:
    r = session.post(f"{base}/api/query/{module}?$limit={limit}&$relationships=true",
                     json={"logic": "AND", "filters": filters}, verify=_VERIFY)
    r.raise_for_status()
    return r.json().get("hydra:member") or []


def _ztpf_playbooks(session: Any, base: str) -> list[dict]:
    """Active + inactive playbooks whose trigger step offers them on a ztpf_*
    module, reduced to what discovery reads (name, uuid, isActive, the start
    step's resources/conditions)."""
    out = []
    for w in _members(session, base, "/api/3/workflows?$limit=1000&$relationships=true"):
        start = None
        for st in w.get("steps") or []:
            args = (st or {}).get("arguments") or {}
            res = args.get("resources") or []
            if isinstance(res, list) and any(str(x).startswith("ztpf_") for x in res):
                start = {"stepType": {"name": "cybersponse.action"},
                         "arguments": {k: args.get(k) for k in (
                             "resources", "singleRecordExecution",
                             "displayConditions", "step_variables") if k in args}}
                break
        if start:
            out.append({"name": w.get("name"), "uuid": w.get("uuid"),
                        "description": w.get("description"),
                        "isActive": bool(w.get("isActive")), "steps": [start]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", required=True, help="box .env (gitignored)")
    ap.add_argument("--device", required=True, help="ztpf_devices uuid to capture")
    ap.add_argument("--out", required=True)
    ap.add_argument("--redact", action="append", default=[], metavar="WORD=REPL",
                    help="replace a box-specific word everywhere (repeatable)")
    ap.add_argument("--personas-from", default="ztpf_device_surface",
                    help="bundle whose personas the world reuses")
    a = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pyfsr import FortiSOAR

    from scripts.fixture_box import load_bundle
    global _VERIFY
    c = FortiSOAR.from_env_file(a.env)
    s, b = c.session, c.base_url
    # The box's own TLS setting from its env file -- a lab appliance with a
    # self-signed cert sets FSR_VERIFY_SSL=false there; nothing here overrides it.
    _VERIFY = getattr(c, "verify_ssl", True)

    modules: dict[str, list[dict]] = {}
    dev_iri = f"/api/3/ztpf_devices/{a.device}"
    modules["ztpf_devices"] = [_flat(r) for r in
                               _members(s, b, "/api/3/ztpf_devices?$limit=200&$relationships=true")]
    steps = _query(s, b, "ztpf_device_automation_steps",
                   [{"field": "ztpfDevices", "operator": "eq", "value": dev_iri}])
    modules["ztpf_device_automation_steps"] = [_flat(r) for r in steps]
    group_ids = sorted({(g if isinstance(g, str) else (g or {}).get("@id"))
                        for r in steps for g in ([r.get("ztpfRunGroups")]
                                                  if not isinstance(r.get("ztpfRunGroups"), list)
                                                  else r.get("ztpfRunGroups")) if g} - {None})
    groups = []
    for iri in group_ids:
        r = s.get(f"{b}{iri}?$relationships=true", verify=_VERIFY)
        if r.status_code == 200:
            groups.append(_flat(r.json()))
    modules["ztpf_run_groups"] = groups
    for m in AUTHORING:
        modules[m] = [_flat(r) for r in
                      _members(s, b, f"/api/3/{m}?$limit=500&$relationships=true")]
    for m in modules.values():   # reverse lists go stale once a world is written to
        for r in m:
            for k in ("ztpfAutomationProfileSteps", "ztpfDeviceAutomationSteps",
                      "ztpfManagerAutomationProfileRule"):
                r.pop(k, None)

    sc = Scrubber(dict(r.split("=", 1) for r in a.redact if "=" in r))
    modules = json.loads(sc.text(json.dumps(modules)))
    playbooks = json.loads(sc.text(json.dumps(_ztpf_playbooks(s, b))))
    personas = load_bundle(a.personas_from).get("personas") or {}

    bundle = {
        "_README": [
            "Scrubbed capture of a live FortiSOAR 8.0 ZTPF world (scripts/capture_ztpf_world.py):",
            "one device's full step + run-group history, every authoring record, and the",
            "ztpf record-action playbooks. People, addresses, serials, manager hostnames and",
            "e-mails are substituted; secret-bearing or oversized blobs are cut.",
            f"Personas come from the {a.personas_from} bundle (the live persona set).",
            "writable: approved writes apply in memory so a multi-step arc can be graded."],
        "writable": True, "skills": [], "personas": personas,
        "playbooks": playbooks, "modules": modules,
    }
    Path(a.out).write_text(json.dumps(bundle, indent=1) + "\n")
    print(f"wrote {a.out}: " + ", ".join(f"{k}={len(v)}" for k, v in modules.items())
          + f", playbooks={len(playbooks)}; remapped ips={len(sc.ips)} "
          f"serials={len(sc.serials)} hosts={len(sc.hosts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
