#!/usr/bin/env python3
"""Provision an EXPERIMENTAL FortiSOAR box with the Agentic Assistant from GitHub.

One command to stand the solution up on a fresh / experimental box:

    1. get the solution-pack zip  (a local --pack, or --from-release off GitHub)
    2. enable PyPI in the box's connector pip.conf, over ssh, so the connector's
       `fsr-playbooks==<pin>` dependency resolves at install time
    3. import the pack through the FortiSOAR Import Wizard (pyfsr, over REST)
    4. publish both widgets (the wizard lands them as drafts)
    5. read the box back and print what it now carries

The box can come two ways:
  * `--box <alias>`: an alias already in the pyfsr registry (scripts/box.sh),
    the same knob every other target uses.
  * no `--box` (or an alias that is not registered): the script ASKS for the
    FortiSOAR URL, username and password (and, for the pip.conf step, the ssh
    host/user) -- so a box that was never set up here can still be provisioned.
    The password is read with getpass and never stored or logged; ssh prompts
    for its own password on the terminal.

This is the ssh-and-REST provisioning path for an EXPERIMENTAL box. It is NOT for
a stable box: a stable box takes code only through `make stable` (Import Wizard,
no ssh), and this script refuses a registered stable alias up front.

What it deliberately leaves out (each needs a choice this script should not make):
  * Advanced Development Features (allow custom connectors + widgets): a one-time
    System Settings toggle; `--require-dev-features` makes the run assert it is on.
  * The LLM connector configuration + key: add it afterward (the run prints the
    next step), since the provider and key are yours to choose.

Examples
    make -C <monorepo> stable BOX=92 ARGS=--build-only      # writes dist/stable/*.zip
    scripts/provision_box.py --box 92 --pack <that>.zip      # registered box
    scripts/provision_box.py --pack <that>.zip              # asks for URL/user/pass
    scripts/provision_box.py --box 92 --from-release latest # off a GitHub release
    scripts/provision_box.py --pack p.zip --dry-run         # plan only, no secrets
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WIDGET_NAMES = ("agenticAssistant", "agenticAssistantMonitor")
CONNECTOR_NAME = "connector-fsr-agentic-assistant"
PACK_REPO = os.environ.get("PACK_REPO", "ftnt-dspille/fsr-agentic-assistant-connector")

BOX_PIP_CONF = "/opt/cyops/configs/integrations/packages/integrations_env/pip.conf"
PYPI_SIMPLE = "https://pypi.org/simple/"

# Add PyPI to the box's connector pip.conf, idempotently. Lifted verbatim from
# scripts/ensure_framework.sh (proven in the ship path): back up, append to an
# existing extra-index-url or add one under [global], preserve a chattr +i flag.
# Reads its two args ($1 conf, $2 url); we prepend `set --` so the whole thing
# travels as ONE remote command string and leaves ssh's stdin free for a password.
PIP_CONF_FIX = r'''
set -e
conf="$1"; url="$2"
if sudo test -f "$conf" && sudo grep -qiE '^[[:space:]]*extra-index-url[[:space:]]*=.*pypi\.org' "$conf"; then
    echo "   $conf already lists PyPI; nothing to change"; exit 0
fi
sudo test -f "$conf" || { echo "   $conf does not exist; creating it"; sudo touch "$conf"; }
was_immutable=0
if sudo lsattr -d "$conf" 2>/dev/null | cut -d' ' -f1 | grep -q i; then was_immutable=1; fi
bak="$conf.bak-$(date +%Y%m%d%H%M%S)"
sudo cp -p "$conf" "$bak"
if [ "$was_immutable" = 1 ]; then
    echo "   $conf is immutable (chattr +i): lifting for the edit, restoring after"
    sudo chattr -i "$conf"
    trap 'rc=$?; if [ $rc -ne 0 ]; then sudo cp -p "$bak" "$conf" 2>/dev/null || true; fi; sudo chattr +i "$conf"' EXIT
fi
tmp="$(mktemp)"
sudo cat "$conf" | awk -v url="$url" '{l[NR]=$0} END{
  e=0; g=0; n=NR
  for(i=1;i<=n;i++){ if(!e && l[i]~/^[ \t]*extra-index-url[ \t]*=/) e=i; if(!g && l[i]~/^[ \t]*\[global\]/) g=i }
  if(e) l[e]=l[e] " " url
  else if(g) l[g]=l[g] "\nextra-index-url= " url
  else { n++; l[n]="[global]\nextra-index-url= " url }
  for(i=1;i<=n;i++) print l[i] }' > "$tmp"
sudo tee "$conf" < "$tmp" > /dev/null
rm -f "$tmp"
echo "   backed up to $bak; added PyPI as an extra index"
'''


def _say(msg: str) -> None:
    print(f">> {msg}", flush=True)


def _ask(label: str, default: str | None = None, secret: bool = False) -> str:
    prompt = f"{label}{f' [{default}]' if default else ''}: "
    val = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    return val or (default or "")


# --- where the box comes from ------------------------------------------------

def resolve_registered_box(alias: str) -> dict | None:
    """Resolve an alias through box.sh. None when the alias is not registered."""
    out = subprocess.run(
        ["bash", str(HERE / "box.sh"), "--need", "env,ssh", "--print"],
        env={**os.environ, "BOX": alias}, capture_output=True, text=True)
    if out.returncode != 0:
        return None
    kv = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    role = subprocess.run(["bash", str(HERE / "box.sh"), "--role", alias],
                          capture_output=True, text=True).stdout.strip() or "unset"
    return {"kind": "registered", "alias": alias, "env": kv.get("BOX_ENV", "-"),
            "ssh_alias": kv.get("BOX_SSH", "-"), "role": role}


def prompt_box(need_ssh: bool, secrets: bool) -> dict:
    """Ask for the FortiSOAR connection (and ssh, for pip.conf) for an unregistered box."""
    _say("no registered box -- enter the FortiSOAR connection")
    base_url = _ask("FortiSOAR URL (https://host[:port])")
    if not base_url:
        raise SystemExit("provision: a FortiSOAR URL is required")
    if "://" not in base_url:
        base_url = "https://" + base_url
    username = _ask("username", default="csadmin")
    password = _ask("password", secret=True) if secrets else ""
    host = urlparse(base_url).hostname or ""
    ssh = None
    if need_ssh:
        _say("ssh is needed to add PyPI to the box pip.conf (--skip-pypi to avoid)")
        ssh = {"host": _ask("ssh host", default=host),
               "user": _ask("ssh user", default="csadmin"),
               "port": _ask("ssh port", default="22")}
    return {"kind": "prompted", "role": "unregistered", "base_url": base_url,
            "username": username, "password": password, "host": host, "ssh": ssh}


def build_client(src: dict):
    from pyfsr import FortiSOAR  # imported lazily so --dry-run needs no pyfsr
    if src["kind"] == "registered":
        return FortiSOAR.from_env_file(src["env"])
    return FortiSOAR(src["base_url"], username=src["username"], password=src["password"],
                     verify_ssl=False, suppress_insecure_warnings=True)


# --- the steps ---------------------------------------------------------------

def download_release_pack(tag: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    sel = [] if tag == "latest" else [tag]
    _say(f"download  : solution-pack asset from {PACK_REPO} release {tag}")
    r = subprocess.run(
        ["gh", "release", "download", *sel, "--repo", PACK_REPO,
         "--pattern", "*.zip", "--dir", str(dest_dir), "--clobber"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            "provision: could not download a pack asset -- "
            f"{r.stderr.strip() or 'no *.zip asset on that release'}.\n"
            "The connector repo must publish the solution-pack zip on its release. "
            "Until then, build one locally and pass --pack.")
    zips = sorted(dest_dir.glob("*.zip"))
    if not zips:
        raise SystemExit("provision: the release carried no .zip asset")
    return zips[-1]


def _ssh_argv(src: dict) -> list[str]:
    """ssh destination argv for this box -- the registry alias, or user@host -p port."""
    if src["kind"] == "registered":
        return ["ssh", "-o", "ConnectTimeout=10", src["ssh_alias"]]
    s = src["ssh"]
    return ["ssh", "-o", "ConnectTimeout=10", "-p", str(s["port"]), f"{s['user']}@{s['host']}"]


def ensure_pypi_in_pip_conf(src: dict) -> None:
    # The fix travels as ONE remote command (set -- args, then the script), so
    # stdin stays free and ssh can prompt for a password when there is no key.
    remote = f"set -- '{BOX_PIP_CONF}' '{PYPI_SIMPLE}'\n{PIP_CONF_FIX}"
    dest = _ssh_argv(src)
    _say(f"pip.conf  : ensuring PyPI is an extra index in {dest[-1]}:{BOX_PIP_CONF}")
    r = subprocess.run([*dest, remote])
    if r.returncode == 255:
        raise SystemExit(f"provision: cannot reach {dest[-1]} over ssh -- fix ssh first")
    if r.returncode != 0:
        raise SystemExit(f"provision: enabling PyPI in pip.conf failed (rc={r.returncode})")


def publish_newest_drafts(client) -> dict:
    """Publish the highest-version draft of each widget and verify it installed.

    Version-map-free, so it works whatever the pack source."""
    def vtuple(v: str) -> tuple:
        return tuple(int(x) if str(x).isdigit() else 0 for x in str(v).split("."))

    installed: dict[str, str] = {}
    for name in WIDGET_NAMES:
        recs = client.widgets.list(name=name)
        if not recs:
            raise SystemExit(f"provision: the pack landed no widget named {name!r}")
        drafts = [r for r in recs if getattr(r, "draft", False) and not getattr(r, "installed", False)]
        target = max(drafts or recs, key=lambda r: vtuple(r.version))
        if getattr(target, "draft", False):
            client.widgets.publish(target.uuid)
            _say(f"published : {name} {target.version}")
        back = client.widgets.list(name=name)
        if not any(str(r.version) == str(target.version) and r.installed for r in back):
            raise SystemExit(f"provision: widget {name} {target.version} did not publish to installed")
        installed[name] = str(target.version)
    return installed


def read_back(client) -> dict:
    conns = {c.name: str(c.version) for c in client.connectors.list_configured(refresh=True)}
    return {"connector": conns.get(CONNECTOR_NAME, "(not installed)")}


# --- orchestration -----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--box", help="registered alias (scripts/box.sh); omit to be asked for URL/user/pass")
    srcg = ap.add_mutually_exclusive_group(required=True)
    srcg.add_argument("--pack", type=Path, help="a solution-pack .zip built locally")
    srcg.add_argument("--from-release", metavar="TAG",
                      help=f"download the pack asset from {PACK_REPO} (a vX.Y.Z tag, or 'latest')")
    ap.add_argument("--skip-pypi", action="store_true",
                    help="do not touch the box pip.conf (and do not ask for ssh)")
    ap.add_argument("--skip-publish", action="store_true", help="import only; leave widgets as drafts")
    ap.add_argument("--require-dev-features", action="store_true",
                    help="fail fast if Advanced Development Features are off")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; prompt no secrets; touch nothing")
    args = ap.parse_args(argv)
    need_ssh = not args.skip_pypi

    # 1. where the box is
    src = resolve_registered_box(args.box) if args.box else None
    if src and src["role"] == "stable":
        raise SystemExit(
            "provision: BOX is a STABLE box. This script uses ssh (pip.conf) and is for an "
            "EXPERIMENTAL box. A stable box takes code only through `make stable` (Import Wizard, "
            "no ssh).")
    if src and src["env"] in ("", "-"):
        raise SystemExit("provision: the registered box has no pyfsr env file; add one or omit --box")
    if src is None:
        if args.box:
            _say(f"'{args.box}' is not a registered box -- asking for its connection")
        src = prompt_box(need_ssh=need_ssh, secrets=not args.dry_run)
    label = src.get("alias") or src.get("base_url")
    _say(f"box       : {label} [role: {src['role']}]")

    # 2. the pack
    work = Path(os.environ.get("PROVISION_DIST") or HERE.parent / "dist" / "provision")
    if args.from_release:
        zip_path = download_release_pack(args.from_release, work)
    else:
        zip_path = args.pack.expanduser().resolve()
        if not zip_path.is_file():
            raise SystemExit(f"provision: --pack {zip_path} does not exist")
    _say(f"pack      : {zip_path.name} ({zip_path.stat().st_size // 1024} KB)")

    if args.dry_run:
        _say("[dry-run] plan:")
        print(json.dumps({
            "box": label, "role": src["role"], "pack": str(zip_path),
            "steps": [
                "skip pip.conf" if args.skip_pypi else f"enable PyPI in {_ssh_argv(src)[-1]}:{BOX_PIP_CONF}",
                "import pack via Import Wizard (install connector)",
                "import only (leave widget drafts)" if args.skip_publish
                else f"publish newest draft of {', '.join(WIDGET_NAMES)}",
                "read connector/widget versions back",
            ],
        }, indent=2))
        return 0

    # 3. enable PyPI (ssh), before the import so the connector's deps resolve
    if need_ssh:
        ensure_pypi_in_pip_conf(src)

    # 4. import
    client = build_client(src)
    if args.require_dev_features:
        try:
            cfg = client.get("/api/3/system_settings")
            if not json.dumps(cfg).lower().count("developmentfeature"):
                _say("WARN: could not confirm Advanced Development Features are on")
        except Exception as e:  # noqa: BLE001
            _say(f"WARN: could not read system settings to check dev features: {e}")

    from stable_install import run_import  # reuse the Import Wizard driver
    _say("import    : uploading the pack through the Import Wizard (a few minutes)")
    run_import(client, zip_path, install_connector=True)

    # 5. publish widgets, read back
    widgets = {} if args.skip_publish else publish_newest_drafts(client)
    back = read_back(client)
    _say("done. box now carries:")
    print(json.dumps({"connector": back["connector"], "widgets": widgets}, indent=2))
    _say("next: add an LLM connector configuration with its key and mark it the default, "
         "then run a chat turn to confirm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
