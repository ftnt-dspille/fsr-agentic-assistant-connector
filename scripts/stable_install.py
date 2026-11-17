#!/usr/bin/env python3
"""Install the promoted mains on a STABLE box through the FortiSOAR Import Wizard.

This is the body of `make stable BOX=<alias>` (the root Makefile runs the guard in
scripts/stable_guard.py first, then this). It never uses ssh, rsync or PyPI for the
box: one solution-pack zip goes up through the same import API the Import Wizard
drives (pyfsr `import_config.import_file`), and the appliance installs the bundled
connector and widget tarballs itself.

Built from the checkouts at their main HEAD, into <conn>/dist/stable/:
  framework   fsr_playbooks wheel built from packaging/fsr_playbooks (hatch-vcs: the
              version carries the commit, g<sha>); bundled inside the connector tgz
  connector   scripts/build.sh with FSR_PLAYBOOKS_WHEEL=<that wheel>: the platform's
              pip installs it from wheels/, so the box needs no PyPI for the framework
  widgets     agenticAssistant + agenticAssistantMonitor, packed fresh by the harness
              packager (never a cached tgz: a same-version rebuild would be skipped)
  pack        the Agentic Assistant solution pack (build_agentic_assistant_pack.py),
              validated by pyfsr's export checker before it is uploaded

Installs EXACTLY the repo's info.json versions. The importer's auto-bump is off (it
re-versions a box that already has the version on every run). Per artifact (connector,
each widget): a version not on the box is installed; the same version at the same head
as the install record is skipped; the same version at a different head is REFUSED with a
"bump the version (BUMP=patch), promote, retry" message; a newer version on the box is
refused. The record (dist/stable/installed-<box>.json) holds per-artifact version + head.

Environment (set by the Makefile):
  BOX_ENV        the box's env file (pyfsr registry; resolved by scripts/box.sh)
  BOX            the alias (for the install record)
  MONOREPO       fsr_all_widgets main checkout     FRAMEWORK  fsr-playbook-framework checkout
  CONN_REPO      this repo's main checkout (the connector and pack builder)
  DIST_STABLE    output dir (default <conn>/dist/stable)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_agentic_assistant_pack as pack  # noqa: E402  (the pack builder is the one assembler)
from pyfsr.api.import_config import connector_flags  # noqa: E402
from stable_guard import WIDGET_NAMES  # noqa: E402

DASHBOARD_TITLE = pack.DASHBOARD_TITLE


def _say(msg: str) -> None:
    print(f"stable: {msg}", flush=True)


def _pip_python() -> str:
    """An interpreter with pip: the dev venv has none, and wheel builds need pip."""
    for c in ("python3", "python3.12", "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
        if shutil.which(c) and subprocess.run([c, "-m", "pip", "--version"], capture_output=True).returncode == 0:
            return shutil.which(c) or c
    raise SystemExit("stable: no pip-capable python3 to build the framework wheel")


def build_framework_wheel(framework: Path, out: Path, sha: str) -> tuple[Path, str]:
    """The fsr_playbooks wheel from the framework's main checkout (packaging/fsr_playbooks)."""
    wheel_dir = out / "framework"
    shutil.rmtree(wheel_dir, ignore_errors=True)
    wheel_dir.mkdir(parents=True)
    subprocess.run([_pip_python(), "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(wheel_dir),
                    str(framework / "packaging" / "fsr_playbooks")], check=True)
    wheels = sorted(wheel_dir.glob("fsr_playbooks-*-py3-none-any.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"stable: expected one fsr_playbooks wheel, found {[w.name for w in wheels]}")
    wheel = wheels[0]
    ver = wheel.name[len("fsr_playbooks-"):].split("-py3-none-any")[0]
    # hatch-vcs stamps the commit into the version (+g<sha>): the wheel must be the framework HEAD.
    if sha[:7] not in ver:
        raise SystemExit(f"stable: framework wheel {ver} does not carry HEAD {sha[:9]}; the checkout moved")
    return wheel, ver


def build_connector(conn: Path, wheel: Path, out: Path) -> Path:
    conn_out = out / "connector"
    shutil.rmtree(conn_out, ignore_errors=True)
    conn_out.mkdir(parents=True)
    env = {**os.environ, "FSR_PLAYBOOKS_WHEEL": str(wheel), "FSR_TGZ_DIR": str(conn_out)}
    subprocess.run(["bash", str(conn / "scripts" / "build.sh")], cwd=conn, env=env, check=True)
    tgzs = sorted(conn_out.glob("connector-fsr-agentic-assistant-*.tgz"))
    if len(tgzs) != 1:
        raise SystemExit(f"stable: expected one connector tarball, found {[t.name for t in tgzs]}")
    return tgzs[0]


def build_widgets(monorepo: Path, out: Path, widgets_src: Path) -> dict[str, Path]:
    """Pack both widgets fresh through the harness packager (no server, no cached tgz)."""
    wid_out = out / "widgets"
    shutil.rmtree(wid_out, ignore_errors=True)
    wid_out.mkdir(parents=True)
    harness = monorepo / "fortisoar-widget-harness"
    js = ("const p=require(process.argv[1]);"
          "p.packageWidget(process.argv[2],process.argv[3])"
          ".then(r=>console.log(JSON.stringify({archivePath:r.archivePath,version:r.version,name:r.widgetName})))"
          ".catch(e=>{console.error(e.message);process.exit(1)})")
    tgzs: dict[str, Path] = {}
    for name in WIDGET_NAMES:
        wdir = widgets_src / name / "widget"
        r = subprocess.run(["node", "-e", js, str(harness / "packager.js"), str(wdir), str(wid_out)],
                           cwd=harness, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"stable: packing {name} failed: {r.stderr.strip()[-400:]}")
        info = json.loads(r.stdout.strip().splitlines()[-1])
        tgzs[name] = Path(info["archivePath"])
        _say(f"widget    : {info['name']} {info['version']} (packed fresh)")
    return tgzs


def build_pack(conn_tgz: Path, widget_tgz: dict[str, Path], fw_ver: str, out: Path, widgets_src: Path) -> Path:
    zip_out = out / "agentic-assistant-stable.zip"
    argv = ["-o", str(zip_out), "--no-bundle", "--connector-tgz", str(conn_tgz),
            "--framework-version", fw_ver, "--widgets-src", str(widgets_src)]
    for name, path in widget_tgz.items():
        argv += ["--widget-tgz", f"{name}={path}"]
    rc = pack.main(argv)
    if rc != 0 or not zip_out.is_file():
        raise SystemExit("stable: pack build failed")
    return zip_out


def expected_versions(conn: Path, widgets_src: Path) -> dict:
    """What the package carries, from the checkouts' own info.json files."""
    conn_info = json.loads((conn / "connector-fsr-agentic-assistant" / "info.json").read_text())
    widgets = {}
    for name in WIDGET_NAMES:
        info = json.loads((widgets_src / name / "widget" / "info.json").read_text())
        widgets[name] = info["version"]
    return {"connector": {"name": conn_info["name"], "version": conn_info["version"]},
            "widgets": widgets}


def record_path(out: Path, box: str) -> Path:
    return out / f"installed-{box}.json"


def _vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def content_keys(conn: Path, widgets_src: Path, fw_ver: str, widget_names) -> dict[str, str]:
    """What each artifact actually packages, keyed on content rather than repo HEAD.

    connector = the git tree of connector-fsr-agentic-assistant/ plus the bundled framework
    wheel version (the packaged connector carries both); widget = the git tree of widget/.
    A commit that changes only scripts or docs leaves these keys unchanged.
    """
    def tree(repo: Path, sub: str) -> str:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", f"HEAD:{sub}"],
                           capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            raise SystemExit(f"stable: cannot read the packaged tree {repo}:{sub} ({r.stderr.strip()})")
        return r.stdout.strip()

    keys = {"connector-fsr-agentic-assistant": f"{tree(conn, 'connector-fsr-agentic-assistant')}+fw{fw_ver}"}
    for w in widget_names:
        keys[w] = tree(widgets_src / w, "widget")
    return keys


def plan_install(client, state: dict, content: dict, expect: dict) -> dict:
    """Per artifact (connector, each widget): install, skip, or refuse.

    The stable install installs EXACTLY the repo's info.json versions. The importer's
    auto-bump is off, because it moves a box that already has the version to the next
    patch on every run. Each artifact is keyed on its packaged content (content_keys).
      - version not on the box                       -> install
      - same version, same content as recorded        -> skip (already there)
      - same version, different content (or no record) -> refuse: bump the version (BUMP=patch)
      - box holds a NEWER version                     -> refuse (the importer would skip the older tarball)
    """
    rec = (state or {}).get("artifacts", {})
    actions: dict[str, str] = {}
    cname = expect["connector"]["name"]
    arts = [("connector", cname, expect["connector"]["version"], content[cname])]
    arts += [("widget", w, v, content[w]) for w, v in expect["widgets"].items()]
    for kind, name, ver, key in arts:
        if kind == "connector":
            on_box = [str(c.version) for c in client.connectors.list_configured(refresh=True) if c.name == name]
        else:
            on_box = [str(r.version) for r in client.widgets.list(name=name)]
        mine = rec.get(name, {})
        if ver in on_box:
            if mine.get("version") == ver and mine.get("content") == key:
                actions[name] = "skip"
                continue
            raise SystemExit(
                f"stable: {name} {ver} is already on the box with different packaged content "
                f"(recorded {(mine.get('content') or 'none')[:12]}, repo {key[:12]}). The installer never "
                f"reinstalls a version. Bump it (BUMP=patch in that repo), promote, and retry.")
        newer = [v for v in on_box if _vtuple(v) > _vtuple(ver)]
        if newer:
            raise SystemExit(f"stable: the box holds {name} {max(newer, key=_vtuple)}, newer than the "
                             f"repo's {ver}; the importer would skip the older tarball. Bump the repo past it.")
        actions[name] = "install"
    return actions

def run_import(client, zip_path: Path, install_connector: bool) -> dict:
    """The Import Wizard, driven through pyfsr: upload, options, trigger, wait, verify.

    bump_existing is OFF: the stable install installs exactly the repo's versions (see
    plan_install). The connector is forced to install only when this run carries a new
    connector version (an existing connector is marked includeInstall=false by the importer).
    """
    def _opts(o):
        return connector_flags(o, include_install=True) if install_connector else o
    job = client.import_config.import_file(
        str(zip_path), wait=True, bump_existing=False, retire_superseded=False,
        verify=True, timeout=1200, settle=True, settle_timeout=600, modify_options=_opts)
    status = str(getattr(job, "status", "") or "")
    if status.strip().lower() != "import complete":
        raise SystemExit(f"stable: import ended in status {status!r}: {getattr(job, 'errorMessage', '')}")
    return {"status": status}

def publish_and_retire(client, expect: dict, bumps: list[dict]) -> None:
    """The wizard lands widgets as drafts. Publish the expected version of each widget,
    then retire the other installed versions that no dashboard still names."""
    bumped_to = {b["name"]: b["to"] for b in bumps}
    for name, ver in expect["widgets"].items():
        want = bumped_to.get(name, ver)
        recs = client.widgets.list(name=name)
        for r in recs:
            if str(r.version) == want and r.draft and not r.installed:
                client.widgets.publish(r.uuid)
                _say(f"published : {name} {want}")
        recs = client.widgets.list(name=name)
        if not any(str(r.version) == want and r.installed for r in recs):
            raise SystemExit(f"stable: widget {name} {want} did not publish to installed")
        dash = client.get("/api/3/dashboard", params={"$limit": 1000})
        rows = dash.get("hydra:member", dash) if isinstance(dash, dict) else dash
        text = json.dumps(rows)
        for r in recs:
            if str(r.version) != want and f"{name}-{r.version}" not in text:
                client.widgets.remove(r.uuid)
                _say(f"retired   : {name} {r.version} (no dashboard names it)")


def installed_state(client, expect: dict, bumps: list[dict]) -> dict:
    """Read the box back: the connector and widget versions it now reports."""
    bumped_to = {b["name"]: b["to"] for b in bumps}
    conn_name = expect["connector"]["name"]
    conns = [str(c.version) for c in client.connectors.list_configured(refresh=True) if c.name == conn_name]
    conn_ver = bumped_to.get(conn_name, expect["connector"]["version"])
    if conn_ver not in conns:
        raise SystemExit(f"stable: connector {conn_name} {conn_ver} is not on the box (on box: {conns})")
    widgets: dict[str, str] = {}
    for name, ver in expect["widgets"].items():
        want = bumped_to.get(name, ver)
        recs = [r for r in client.widgets.list(name=name) if str(r.version) == want]
        if not recs or not recs[0].installed:
            raise SystemExit(f"stable: widget {name} {want} is not installed on the box")
        widgets[name] = want
    dash = client.get("/api/3/dashboard", params={"$limit": 1000})
    rows = dash.get("hydra:member", dash) if isinstance(dash, dict) else dash
    if not any(r.get("displayName") == DASHBOARD_TITLE for r in rows):
        raise SystemExit(f"stable: dashboard {DASHBOARD_TITLE!r} is not on the box")
    return {"connector": conn_ver, "widgets": widgets, "dashboard": DASHBOARD_TITLE}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-only", action="store_true", help="build the package; do not touch the box")
    args = ap.parse_args(argv)

    conn = HERE.parent
    monorepo = Path(os.environ["MONOREPO"])
    widgets_src = Path(os.environ.get("WIDGETS_SRC") or monorepo / "widgets-src")
    framework = Path(os.environ["FRAMEWORK"])
    out = Path(os.environ.get("DIST_STABLE") or conn / "dist" / "stable")
    out.mkdir(parents=True, exist_ok=True)

    def head(p: Path) -> str:
        return subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()

    heads = {"fsr_all_widgets": head(monorepo), "fsr-playbook-framework": head(framework),
             "conn_main": head(conn), **{w: head(widgets_src / w) for w in WIDGET_NAMES}}
    _say("heads     : " + ", ".join(f"{k} {v[:9]}" for k, v in heads.items()))

    wheel, fw_ver = build_framework_wheel(framework, out, heads["fsr-playbook-framework"])
    _say(f"framework : fsr-playbooks {fw_ver} (built from main, bundled)")
    conn_tgz = build_connector(conn, wheel, out)
    _say(f"connector : {conn_tgz.name}")
    widget_tgz = build_widgets(monorepo, out, widgets_src)
    zip_path = build_pack(conn_tgz, widget_tgz, fw_ver, out, widgets_src)
    expect = expected_versions(conn, widgets_src)
    content = content_keys(conn, widgets_src, fw_ver, [w for w in expect["widgets"]])
    _say("content   : " + ", ".join(f"{k} {v[:12]}" for k, v in content.items()))
    _say(f"pack      : {zip_path.name} ({zip_path.stat().st_size // 1024} KB)")
    if args.build_only:
        print(json.dumps({"zip": str(zip_path), "framework": fw_ver, "heads": heads, "expect": expect}, indent=2))
        return 0

    from pyfsr import FortiSOAR  # the box is reached through the registry env file only
    env_file = os.environ["BOX_ENV"]
    box = os.environ.get("BOX", "")
    client = FortiSOAR.from_env_file(env_file)
    rec = record_path(out, box)
    state = json.loads(rec.read_text()) if rec.is_file() else {}
    actions = plan_install(client, state, content, expect)
    for name, act in actions.items():
        _say(f"plan      : {name} -> {act}")
    if all(a == "skip" for a in actions.values()):
        _say("box       : already carries these exact versions at these heads (nothing uploaded)")
    else:
        _say("import    : uploading the pack through the Import Wizard (this takes a few minutes)")
        run_import(client, zip_path, install_connector=actions[expect["connector"]["name"]] == "install")
        publish_and_retire(client, expect, [])
    installed = installed_state(client, expect, [])
    cname = expect["connector"]["name"]
    artifacts = {cname: {"version": expect["connector"]["version"], "content": content[cname],
                         "head": heads["conn_main"]}}
    artifacts.update({w: {"version": v, "content": content[w], "head": heads[w]}
                      for w, v in expect["widgets"].items()})
    rec.write_text(json.dumps({"heads": heads, "framework": fw_ver, "installed": installed,
                               "artifacts": artifacts}, indent=2))
    result = {"skipped": all(a == "skip" for a in actions.values()), "installed": installed,
              "actions": actions}
    inst = result["installed"]
    _say(f"verified  : connector {inst['connector']}; widgets " +
         ", ".join(f"{k} {v}" for k, v in inst["widgets"].items()) + f"; dashboard {inst['dashboard']}")
    print(json.dumps({"box": box, "framework": fw_ver, "heads": heads, **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
