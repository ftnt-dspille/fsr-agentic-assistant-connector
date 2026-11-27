#!/usr/bin/env python3
"""Assemble the Agentic Assistant solution pack **locally**, from built artifacts.

`export_agentic_assistant_pack.py` asks the appliance to build the pack. That turned
out to be both slow and *wrong*: the box's export engine serves widget tarballs
out of its `/tmp/solutionpacks/` cache, which tracks neither the installed widget
nor local source. A box export produced `agenticAssistant-1.2.47` while
1.2.48 was installed AND 1.2.48 was the source version -- a silently stale pack.

This builds the same zip offline from artifacts we already have (any that is
missing is built first, without bumping a version):

    connector : <repo>/dist/connector-fsr-agentic-assistant-<version>.tgz
    widgets   : <harness>/widget-packages/<name>-<version>.tgz
    dashboard : scripts/pack_assets/<title>.json  (the only box-authored piece;
                refresh it with --refresh-dashboard --env <path>)

Versions are read from each component's own info.json, so the pack can only ever
describe what it actually carries.

INSTALL IT WITH THE IMPORT WIZARD. FortiSOAR installs bundled connector and
widget tgz files only on a non-solution-pack import (cyops-api WidgetConfig /
ConnectorConfig: `install_mode == 'tgz' && $importType != 'SolutionPack
Import'`), and every Content Hub upload is typed 'SolutionPack Import'. So a
Content Hub upload registers the pack and its dashboard and installs neither
the connector nor the widgets. The Import Wizard installs both (widgets land as
drafts to publish). `info.json` still carries an identity (`name` + `version`
+ `type: solutionpack`) so the pack is named and versioned. Without that identity the
zip is a plain configuration export -- importable, but never registered as a
pack. Its install notes ride in `prerequisite` (scripts/pack_assets/
prerequisite.md): files under the export root that no content category claims,
a docs/ folder for instance, are ignored by the importer. The finished zip is
checked with pyfsr's own export validator and refused on any error.

Next to the pack it writes a release bundle (`agentic-assistant-release-<v>.zip`):
the pack, a README (scripts/pack_assets/README.md, versions filled in) and the
user and admin guides.

    python scripts/build_agentic_assistant_pack.py
    python scripts/build_agentic_assistant_pack.py -o dist/pack.zip
    python scripts/build_agentic_assistant_pack.py --refresh-dashboard --env ../.env.159
    make pack                      # the same, from the connector repo
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid as _uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONNECTOR_DIR = REPO / "connector-fsr-agentic-assistant"
DIST = REPO / "dist"
ASSETS = REPO / "scripts" / "pack_assets"
# What the connector tarball is built from: a cached tarball is reused only while these are unchanged.
CONNECTOR_SOURCES = [
    CONNECTOR_DIR, REPO / "scripts" / "build.sh", REPO / "scripts" / "fetch_pinned_wheel.sh",
]

# Resolved relative to this repo so the script works from any cwd.
HARNESS_PACKAGES = Path(
    "~/WebstormProjects/fsr_all_widgets/fortisoar-widget-harness/widget-packages"
).expanduser()
WIDGET_SRC = Path("~/WebstormProjects/fsr_all_widgets/widgets-src").expanduser()
HARNESS = HARNESS_PACKAGES.parent
GUIDES = WIDGET_SRC / "agenticAssistant" / "docs"

WIDGET_NAMES = ["agenticAssistant", "agenticAssistantMonitor"]
DASHBOARD_TITLE = "Agentic Assistant Monitor"
DASHBOARD_ENDPOINT = "/api/3/dashboard"  # singular; the plural form 404s
PACK_LABEL = "Agentic Assistant"
# The pack's identity. PERMANENT: Content Hub matches a later version to this
# name to upgrade in place, so renaming it orphans every installed copy.
PACK_NAME = "agenticAssistant"
# The PACK's version, independent of the connector's: bump it whenever the pack
# ships different contents (a connector, widget or dashboard change).
PACK_VERSION = "1.1.0"
PACK_PUBLISHER = "Fortinet CSE"
PACK_CATEGORY = ["Investigation", "Threat Hunting and Search", "Utilities"]
PACK_DESCRIPTION = (
    "An AI analyst built into FortiSOAR. It investigates alerts with cited "
    "verdicts, stages containment for an analyst to approve, and turns an "
    "investigation into a playbook that runs without it. Ships the SOC "
    "Assistant connector, the assistant and Monitor widgets, and the SOC "
    "Assistant Monitor dashboard.")


def widget_info(name: str) -> dict:
    p = WIDGET_SRC / name / "widget" / "info.json"
    if not p.is_file():
        raise SystemExit(f"no widget info.json at {p}")
    return json.loads(p.read_text())


def require(path: Path, hint: str) -> Path:
    if not path.is_file():
        raise SystemExit(f"missing artifact: {path}\n  build it first: {hint}")
    return path


# Never part of a source hash: build output and caches that change without a source edit.
_SKIP_PARTS = {"__pycache__", "node_modules", ".git", "dist", "build", ".DS_Store"}


def source_hash(*roots: Path) -> str | None:
    """sha256 over the relative path and bytes of every file under each root (a root may be a
    single file). None when a root is missing: the caller then cannot vouch for a cached
    artifact and rebuilds."""
    h = hashlib.sha256()
    for root in roots:
        if not root.exists():
            return None
        files = [root] if root.is_file() else sorted(
            p for p in root.rglob("*") if p.is_file()
            and not (_SKIP_PARTS & set(p.relative_to(root).parts)) and p.suffix != ".pyc")
        for p in files:
            rel = p.name if root.is_file() else str(p.relative_to(root))
            h.update(rel.encode() + b"\0")
            h.update(p.read_bytes() + b"\0")
    return h.hexdigest()


def ensure(path: Path, cmd: list[str], cwd: Path, hint: str,
           sources: list[Path] = ()) -> Path:
    """The artifact, built first when it is missing or when its sources changed since it was
    built. Reuse is keyed on the CONTENT of `sources`, not the version string: the artifact
    name carries only the version, so an edit without a bump used to reuse a stale tarball.
    Each build leaves a `<artifact>.srchash` stamp; no stamp, a different hash, or a source
    tree that cannot be hashed all mean rebuild. Neither build bumps a version, so the pack
    still carries exactly what info.json names."""
    want = source_hash(*sources) if sources else None
    stamp = path.with_name(path.name + ".srchash")
    if path.is_file() and want is not None and stamp.is_file() \
            and stamp.read_text().strip() == want:
        return path
    if path.is_file():
        print(f"stale     : {path.name} (sources changed or unstamped), rebuilding")
    else:
        print(f"build     : {' '.join(cmd)}   (no {path.name})")
    subprocess.run(cmd, cwd=cwd, check=True)
    require(path, hint)
    if want is not None:
        stamp.write_text(want + "\n")
    return path


def framework_pin() -> str:
    m = re.search(r"^fsr-playbooks==(\S+)", (CONNECTOR_DIR / "requirements.txt").read_text(), re.M)
    return m.group(1) if m else "?"


def validate(out: Path) -> None:
    """pyfsr's structural check of the finished zip: refuse on any error, and
    on a bundle that is not a solution pack (it would never register as one)."""
    from pyfsr.exports import Export, ExportKind
    arc = Export.open(out)
    findings = arc.problems()
    for f in findings:
        print(f"check     : {f}")
    errors = [f for f in findings if f.is_error]
    if errors or arc.kind is not ExportKind.SOLUTION_PACK:
        out.unlink()
        raise SystemExit(f"REFUSING: {out.name} is not a valid solution pack "
                         f"({len(errors)} error(s), kind={arc.kind.value})")


def bundle(pack: Path, version: str, fill: dict) -> Path:
    """The handout: the pack, a README with this pack's versions, the guides."""
    name = f"agentic-assistant-release-{version}"
    stage = DIST / ".bundlestage" / name
    if stage.parent.exists():
        shutil.rmtree(stage.parent)
    (stage / "docs").mkdir(parents=True)
    shutil.copy2(pack, stage / pack.name)
    readme = (ASSETS / "README.md").read_text()
    for k, v in fill.items():
        readme = readme.replace("{" + k + "}", str(v))
    (stage / "README.md").write_text(readme)
    for doc in ("USER_GUIDE.md", "AGENTIC_ASSISTANT_GUIDE.md"):
        shutil.copy2(GUIDES / doc, stage / "docs" / doc)
    shutil.copytree(GUIDES / "images", stage / "docs" / "images")
    for doc in ("PERSONA_AUTHORING.md", "SKILLS_AUTHORING.md"):
        shutil.copy2(CONNECTOR_DIR / "docs" / doc, stage / "docs" / doc)
    shutil.copy2(CONNECTOR_DIR / "release_notes.md", stage / "RELEASE_NOTES.md")
    out = DIST / f"{name}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(stage.parent))
    shutil.rmtree(stage.parent)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--version", default=None,
                    help=f"pack version (default: {PACK_VERSION})")
    ap.add_argument("--refresh-dashboard", action="store_true",
                    help="re-fetch the dashboard json from a box (needs --env)")
    ap.add_argument("--env", default=None)
    ap.add_argument("--no-bundle", action="store_true",
                    help="write only the pack, not the release bundle beside it")
    ap.add_argument("--allow-dashboard-drift", action="store_true",
                    help="package anyway when a dashboard pins a version the pack "
                         "does not carry (it will render blank)")
    ap.add_argument("--connector-tgz", default=None,
                    help="use this connector tarball instead of dist/ (make stable: a build "
                         "that bundles the framework wheel)")
    ap.add_argument("--widget-tgz", action="append", default=[], metavar="NAME=PATH",
                    help="use this widget tarball for NAME instead of the harness package dir "
                         "(repeatable; make stable packs both widgets fresh)")
    ap.add_argument("--widgets-src", default=None,
                    help="the widget source tree (default: the main checkout's widgets-src); "
                         "versions come from its info.json files")
    ap.add_argument("--framework-version", default=None,
                    help="the framework version the pack carries (default: the requirements pin)")
    args = ap.parse_args(argv)
    global WIDGET_SRC
    if args.widgets_src:
        WIDGET_SRC = Path(args.widgets_src)
    widget_tgz = {}
    for spec in args.widget_tgz:
        name, _, path = spec.partition("=")
        if name not in WIDGET_NAMES or not Path(path).is_file():
            raise SystemExit(f"--widget-tgz {spec!r}: want NAME=PATH with NAME one of {WIDGET_NAMES}")
        widget_tgz[name] = Path(path)

    dash_path = ASSETS / f"{DASHBOARD_TITLE}.json"
    if args.refresh_dashboard:
        if not args.env:
            raise SystemExit("--refresh-dashboard needs --env")
        import warnings
        warnings.filterwarnings("ignore")
        from pyfsr import FortiSOAR
        client = FortiSOAR.from_env_file(args.env)
        resp = client.get(DASHBOARD_ENDPOINT, params={"$limit": 200})
        rows = resp.get("hydra:member", resp) if isinstance(resp, dict) else resp
        match = [r for r in rows if r.get("displayName") == DASHBOARD_TITLE]
        if not match:
            raise SystemExit(f"dashboard {DASHBOARD_TITLE!r} not found on that box")
        full = client.get(f"{DASHBOARD_ENDPOINT}/{match[0]['uuid']}")
        ASSETS.mkdir(parents=True, exist_ok=True)
        dash_path.write_text(json.dumps(full, indent=4))
        print(f"refreshed {dash_path}")

    if not dash_path.is_file():
        raise SystemExit(f"no dashboard asset at {dash_path}; "
                         f"run once with --refresh-dashboard --env <path>")
    dash = json.loads(dash_path.read_text())

    conn = json.loads((CONNECTOR_DIR / "info.json").read_text())
    conn_ver = conn["version"]
    if args.connector_tgz:
        conn_tgz = require(Path(args.connector_tgz), "scripts/build.sh")
    else:
        conn_tgz = ensure(DIST / f"connector-fsr-agentic-assistant-{conn_ver}.tgz",
                          ["bash", "scripts/build.sh"], REPO, "bash scripts/build.sh",
                          sources=CONNECTOR_SOURCES)

    widgets = []
    for name in WIDGET_NAMES:
        ver = widget_info(name)["version"]
        if name in widget_tgz:
            tgz = widget_tgz[name]
        else:
            tgz = ensure(HARNESS_PACKAGES / f"{name}-{ver}.tgz",
                         ["node", "scripts/widget.js", "pack", name], HARNESS,
                         f"node scripts/widget.js pack {name}   (in the harness)",
                         sources=[WIDGET_SRC / name, HARNESS / "scripts"])
        widgets.append({"name": name, "version": ver, "tgz": tgz,
                        "title": widget_info(name).get("title", name)})

    # A dashboard pin is `<name>-<version>`, so it goes stale on every widget
    # bump: the exported asset pinned agenticAssistantMonitor-1.0.4 long after 1.0.9
    # shipped. Repin by NAME to the version this pack actually carries, so the
    # pack is self-consistent by construction and cannot drift again. Only a
    # pin whose *name* we do not ship is unfixable -- that one still refuses.
    by_name = {w["name"]: w["version"] for w in widgets}
    unfixable = []
    for row in (dash.get("config") or {}).get("rows") or []:
        for col in row.get("columns") or []:
            for w in col.get("widgets") or []:
                pin = w.get("type") or ""
                if not pin:
                    continue
                name = pin.rsplit("-", 1)[0]
                if name not in by_name:
                    unfixable.append(pin)
                    continue
                want = f"{name}-{by_name[name]}"
                if pin != want:
                    print(f"repin     : {pin} -> {want}")
                    w["type"] = want
    if unfixable:
        msg = (f"\ndashboard {DASHBOARD_TITLE!r} renders {unfixable}, which this "
               f"pack does not carry.\nIt would import cleanly and then render "
               f"blank.")
        if not args.allow_dashboard_drift:
            raise SystemExit("REFUSING:" + msg + "\nAdd the widget, or pass "
                             "--allow-dashboard-drift to package anyway.")
        print("WARNING:" + msg, file=sys.stderr)

    version = args.version or PACK_VERSION
    out = Path(args.output or (DIST / f"agentic-assistant-pack-{version}.zip"))
    out.parent.mkdir(parents=True, exist_ok=True)

    root = f"export_{_uuid.uuid4()}"
    stage = DIST / ".packstage"
    if stage.exists():
        shutil.rmtree(stage)
    base = stage / root
    (base / "dashboards").mkdir(parents=True)
    (base / "connectors").mkdir(parents=True)
    (base / "widgets").mkdir(parents=True)

    # Write, don't copy: `dash` carries the repinned widget versions above.
    (base / "dashboards" / f"{DASHBOARD_TITLE}.json").write_text(
        json.dumps(dash, indent=4))

    # The platform names the connector tarball `<name>_<version>.tgz` (underscore);
    # our local build dir uses a hyphen. `installer_path` would stay consistent
    # either way, but match the platform's convention rather than rely on the
    # importer reading only that field.
    conn_arcname = f"{conn['name']}_{conn_ver}.tgz"
    shutil.copy2(conn_tgz, base / "connectors" / conn_arcname)
    (base / "connectors" / "data.json").write_text(json.dumps([{
        "name": conn["name"],
        "label": conn["label"],
        "category": [conn["category"]] if isinstance(conn["category"], str)
                    else conn["category"],
        "description": conn.get("description", ""),
        "publisher": conn.get("publisher", "Internal"),
        "version": conn_ver,
        "operation_roles": [],
        "configurations": [],
        "dataImports": [],
        "install_mode": "tgz",
        "installer_path": conn_arcname,
    }], indent=4))

    wdata = []
    for w in widgets:
        shutil.copy2(w["tgz"], base / "widgets" / w["tgz"].name)
        wdata.append({
            "name": w["name"], "version": w["version"], "title": w["title"],
            "install_mode": "tgz", "installer_path": w["tgz"].name,
        })
    (base / "widgets" / "data.json").write_text(json.dumps(wdata, indent=4))

    framework = args.framework_version or framework_pin()
    (base / "info.json").write_text(json.dumps({
        "name": PACK_NAME,
        "version": version,
        "type": "solutionpack",
        "local": True,
        "label": PACK_LABEL,
        "publisher": PACK_PUBLISHER,
        "description": PACK_DESCRIPTION,
        "prerequisite": (ASSETS / "prerequisite.md").read_text().replace(
            "{framework}", framework),
        "help": None,
        "category": PACK_CATEGORY,
        "supportInfo": None,
        "iconLarge": None,
        "dependencies": [],
        "postInstallConfig": {"enabled": False},
        "fsrMinCompatibility": "8.0.0",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contents": {
            "dashboards": [{"name": DASHBOARD_TITLE}],
            "connectors": [{"name": conn["label"], "apiName": conn["name"],
                            "version": conn_ver}],
            # `name` is the widget's TITLE here (the platform's own exports
            # write {apiName, name}); a `title` key is not read, and the
            # importer recorded the pack's widget list as empty -- live on a
            # clean 8.0, the pack imported and no widget was installed.
            "widgets": [{"apiName": w["name"], "name": w["title"]}
                        for w in widgets],
        },
    }, indent=4))

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(base.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(stage))
    shutil.rmtree(stage)
    validate(out)

    print(f"pack      : {PACK_NAME} {version} (solution pack)")
    print(f"connector : {conn['label']} {conn_ver}")
    for w in widgets:
        print(f"widget    : {w['name']} {w['version']}")
    print(f"dashboard : {DASHBOARD_TITLE}")
    print(f"framework : fsr-playbooks {framework}")
    print(f"\nwrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    if not args.no_bundle:
        b = bundle(out, version, {
            "version": version, "name": PACK_NAME, "connector": conn_ver,
            "framework": framework, **{w["name"]: w["version"] for w in widgets}})
        print(f"wrote {b}  ({b.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
