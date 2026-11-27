"""BOX=pool: ship-time auto-selection over the registry's ship_pool boxes
(scripts/box.sh). The first experimental ship_pool box in FSR_BOX_ALLOW order that is
free (no live holder, or the caller's own) and reachable (its probe answers) is
picked and leased. An explicit BOX=<alias> is unchanged: no pick, no probe.

Temp registry, temp lease dir, stubbed reachability probe; no network.
"""
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOXSH = ROOT / "scripts" / "box.sh"


def _has_env_cli() -> bool:
    r = subprocess.run([sys.executable, "-m", "pyfsr.cli", "instances", "env", "--help"],
                       capture_output=True, text=True)
    return r.returncode == 0 and "--env-file-path" in r.stdout


pytestmark = pytest.mark.skipif(not _has_env_cli(), reason="pyfsr without `instances env` (needs pyfsr main >= instances env)")

ALLOW = "pool-a pool-b stable-x nonpool"


@pytest.fixture()
def reg(tmp_path):
    for n in ("pool-a", "pool-b", "stable-x", "nonpool"):
        (tmp_path / f".env.{n}").write_text(f"FSR_BASE_URL=https://{n}.example.com\nFSR_API_KEY=k\n")
    p = tmp_path / "instances.toml"
    p.write_text(f"""
[instances.pool-a]
env_file = "{tmp_path / '.env.pool-a'}"
role = "experimental"
ship_pool = true
[instances.pool-a.appliance]
host = "pool-a.example.com"
user = "admin"
port = 22
key_path = "/keys/a"

[instances.pool-b]
env_file = "{tmp_path / '.env.pool-b'}"
role = "experimental"
ship_pool = true
[instances.pool-b.appliance]
host = "pool-b.example.com"
user = "admin"
port = 22
key_path = "/keys/b"

[instances.stable-x]
env_file = "{tmp_path / '.env.stable-x'}"
role = "stable"
ship_pool = true
[instances.stable-x.appliance]
host = "stable-x.example.com"
user = "admin"
port = 22
key_path = "/keys/s"

[instances.nonpool]
env_file = "{tmp_path / '.env.nonpool'}"
role = "experimental"
[instances.nonpool.appliance]
host = "nonpool.example.com"
user = "admin"
port = 22
key_path = "/keys/n"
""")
    # Reachability stub: exits 1 (down) for aliases listed in $DOWN, else 0 (up).
    # Every probed alias is appended to $PROBE_LOG so a test can see what was probed.
    stub = tmp_path / "probe.sh"
    stub.write_text('#!/bin/sh\necho "$1" >> "$PROBE_LOG"\ncase " $DOWN " in *" $1 "*) exit 1;; esac\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "leases").mkdir()
    (tmp_path / "probe.log").write_text("")
    return tmp_path


def _env(reg, **kw):
    e = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"),
         "PYFSR_INSTANCES": str(reg / "instances.toml"), "PYFSR_PY": sys.executable,
         "FSR_BOX_ALLOW": ALLOW, "FSR_BOX_CACHE": str(reg / "cache"),
         "FSR_LEASE_DIR": str(reg / "leases"), "FSR_LEASE_HOLDER": "tree:me",
         "FSR_POOL_PROBE_CMD": str(reg / "probe.sh"), "PROBE_LOG": str(reg / "probe.log"),
         "DOWN": ""}
    if os.environ.get("PYTHONPATH"):
        e["PYTHONPATH"] = os.environ["PYTHONPATH"]
    e.update(kw)
    return e


def _lease_file(reg, box, holder, until_offset=1200, since=None, gen=1):
    now = int(time.time())
    (reg / "leases" / box).write_text(f"{holder} {since or now} {now + until_offset} {gen}\n")


def _resolve(reg, box="pool", need="lease", **kw):
    """Run resolve_box exactly as a make target does, with BOX=<box>."""
    env = _env(reg, **kw)
    if box is not None:
        env["BOX"] = box
    return subprocess.run(
        ["bash", "-c", f'source "{BOXSH}"; resolve_box {need}; rc=$?; echo "PICKED=$BOX"; echo "LEASE=$BOX_LEASE"; exit $rc'],
        capture_output=True, text=True, env=env, timeout=120)


def _probed(reg):
    return [l for l in (reg / "probe.log").read_text().splitlines() if l]


def test_a_pool_pick_with_both_free_takes_the_first_in_allowlist_order(reg):
    r = _resolve(reg)
    assert r.returncode == 0, r.stderr
    assert "PICKED=pool-a" in r.stdout and "LEASE=tree:me#1" in r.stdout
    assert (reg / "leases" / "pool-a").read_text().split()[0] == "tree:me"
    assert not (reg / "leases" / "pool-b").exists()


def test_a_box_held_by_another_holder_is_skipped_for_the_next_pool_box(reg):
    _lease_file(reg, "pool-a", "tree:other")
    r = _resolve(reg)
    assert r.returncode == 0, r.stderr
    assert "PICKED=pool-b" in r.stdout
    assert (reg / "leases" / "pool-a").read_text().split()[0] == "tree:other"


def test_all_pool_boxes_held_refuses_and_names_both_holders(reg):
    _lease_file(reg, "pool-a", "tree:other")
    _lease_file(reg, "pool-b", "tree:third")
    r = _resolve(reg)
    assert r.returncode == 2
    assert "PICKED=pool-" not in r.stdout
    assert "tree:other" in r.stderr and "tree:third" in r.stderr
    assert "held by" in r.stderr and "until" in r.stderr
    assert "unreachable" not in r.stderr


def test_a_pool_pick_re_picks_the_box_the_caller_already_holds(reg):
    _lease_file(reg, "pool-b", "tree:me", gen=4)
    r = _resolve(reg)
    assert r.returncode == 0, r.stderr
    assert "PICKED=pool-b" in r.stdout
    assert "LEASE=tree:me#4" in r.stdout
    assert not (reg / "leases" / "pool-a").exists()


def test_stable_and_non_pool_boxes_are_never_picked(reg):
    _lease_file(reg, "pool-a", "tree:other")
    _lease_file(reg, "pool-b", "tree:other")
    r = _resolve(reg)
    assert r.returncode == 2
    assert "PICKED=stable-x" not in r.stdout and "PICKED=nonpool" not in r.stdout
    assert "stable-x" not in _probed(reg) and "nonpool" not in _probed(reg)


def test_an_explicit_alias_is_unchanged_and_never_probes(reg):
    _lease_file(reg, "pool-a", "tree:other")
    held = _resolve(reg, box="pool-a")
    assert held.returncode == 2 and "held by tree:other" in held.stderr
    free = _resolve(reg, box="pool-b", DOWN="pool-b")
    assert free.returncode == 0 and "PICKED=pool-b" in free.stdout
    assert _probed(reg) == []


def test_no_box_still_has_no_default(reg):
    r = _resolve(reg, box=None)
    assert r.returncode == 2
    assert "no target box" in r.stderr and "PICKED=pool" not in r.stdout


def test_a_free_but_unreachable_pool_box_is_skipped_for_a_reachable_one(reg):
    r = _resolve(reg, DOWN="pool-a")
    assert r.returncode == 0, r.stderr
    assert "PICKED=pool-b" in r.stdout
    assert (reg / "leases" / "pool-b").exists() and not (reg / "leases" / "pool-a").exists()


def test_all_pool_boxes_unreachable_refuses_with_host_down_wording(reg):
    r = _resolve(reg, DOWN="pool-a pool-b")
    assert r.returncode == 2
    assert "unreachable (host down)" in r.stderr
    assert "held by" not in r.stderr
    assert not (reg / "leases" / "pool-a").exists() and not (reg / "leases" / "pool-b").exists()


def test_mixed_held_and_unreachable_names_each_reason(reg):
    _lease_file(reg, "pool-a", "tree:other")
    r = _resolve(reg, DOWN="pool-b")
    assert r.returncode == 2
    assert "pool-a: held by tree:other" in r.stderr
    assert "pool-b: unreachable (host down)" in r.stderr


def test_the_probe_can_be_switched_off(reg):
    r = _resolve(reg, DOWN="pool-a pool-b", FSR_POOL_PROBE="0")
    assert r.returncode == 0, r.stderr
    assert "PICKED=pool-a" in r.stdout
    assert _probed(reg) == []


# --- the real reachability probe resolves the ssh target from the appliance -------
# env_file, not only inline host/port. A pool box registered with just an env_file
# (PYFSR_APPLIANCE_HOST/PORT) must probe that host, not read as "host down".
def _probe_src() -> str:
    """The probe python box.sh runs, pulled from the script itself."""
    r = subprocess.run(["bash", "-c", f"source {BOXSH} >/dev/null 2>&1; _bx_pool_probe_py"],
                       capture_output=True, text=True)
    assert r.stdout.strip(), r.stderr
    return r.stdout


def _run_probe(tmp_path, reg_toml: str, alias: str, timeout: str = "3") -> int:
    reg = tmp_path / "instances.toml"
    reg.write_text(reg_toml)
    probe = tmp_path / "probe.py"
    probe.write_text(_probe_src())
    return subprocess.run([sys.executable, "-I", str(probe), str(reg), alias, timeout]).returncode


def test_probe_resolves_host_from_the_appliance_env_file(tmp_path):
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        envf = tmp_path / ".env.pool-envonly.appliance"
        envf.write_text(f"PYFSR_APPLIANCE_HOST=127.0.0.1\nPYFSR_APPLIANCE_PORT={port}\n")
        reg = f"""
[instances.pool-envonly]
role = "experimental"
ship_pool = true
[instances.pool-envonly.appliance]
env_file = "{envf}"
"""
        # the appliance has NO inline host -- only the env_file. Before the fix this
        # exits 3 (reported "unreachable (host down)") though the host is reachable.
        assert _run_probe(tmp_path, reg, "pool-envonly") == 0
    finally:
        srv.close()


def test_probe_exits_3_when_no_host_in_registry_or_env_file(tmp_path):
    reg = """
[instances.pool-nohost]
role = "experimental"
ship_pool = true
[instances.pool-nohost.appliance]
port = 11006
"""
    assert _run_probe(tmp_path, reg, "pool-nohost") == 3


def _nohost_reg(tmp_path):
    """A ship_pool box whose registry entry has no appliance host and no env_file host."""
    (tmp_path / "leases").mkdir()
    (tmp_path / "instances.toml").write_text("""
[instances.pool-nohost]
role = "experimental"
ship_pool = true
[instances.pool-nohost.appliance]
port = 11006
""")
    return tmp_path


def _nohost_env(reg):
    # The REAL probe (no FSR_POOL_PROBE_CMD stub): it exits 3 for a box with no host
    # before opening any socket, so the test needs no network.
    e = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"),
         "PYFSR_INSTANCES": str(reg / "instances.toml"), "PYFSR_PY": sys.executable,
         "FSR_BOX_ALLOW": "pool-nohost", "FSR_BOX_CACHE": str(reg / "cache"),
         "FSR_LEASE_DIR": str(reg / "leases"), "FSR_LEASE_HOLDER": "tree:me"}
    if os.environ.get("PYTHONPATH"):
        e["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return e


def test_a_pool_box_with_no_ssh_host_says_no_profile_not_host_down(tmp_path):
    reg = _nohost_reg(tmp_path)
    env = _nohost_env(reg)
    env["BOX"] = "pool"
    r = subprocess.run(
        ["bash", "-c", f'source "{BOXSH}"; resolve_box lease; exit $?'],
        capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 2, r.stderr
    assert "no ssh profile in the registry" in r.stderr
    assert "host down" not in r.stderr


def test_pool_status_reports_no_ssh_profile_not_host_down(tmp_path):
    reg = _nohost_reg(tmp_path)
    r = subprocess.run(["bash", str(BOXSH), "--pool"], capture_output=True, text=True,
                       env=_nohost_env(reg), timeout=120)
    assert "pool-nohost" in r.stdout and "no ssh profile" in r.stdout, (r.stdout, r.stderr)
    assert "host down" not in r.stdout
