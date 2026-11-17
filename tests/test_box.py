"""One box registry, no default box (scripts/box.sh over pyfsr's instances.toml).

Every box-reaching make target and script resolves BOX=<alias> through
`pyfsr instances env`: the env file the recipes source and the ssh target both come
from the registry, so an API call can never go to one box while the SSH half goes
to another. The retired knobs (ENV FSR_ENV SHIP_ENV SWEEP_ENV MATRIX_ENV LIVE_ENV
SSH_HOST) get one release of warn-and-map for humans and a refusal for agents.

Temp registry + env files only; no network, never the developer's real registry.
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOXSH = ROOT / "scripts" / "box.sh"


def _has_env_cli() -> bool:
    r = subprocess.run([sys.executable, "-m", "pyfsr.cli", "instances", "env", "--help"],
                       capture_output=True, text=True)
    return r.returncode == 0 and "--env-file-path" in r.stdout


pytestmark = pytest.mark.skipif(not _has_env_cli(), reason="pyfsr without `instances env` (needs pyfsr main >= instances env)")


@pytest.fixture()
def reg(tmp_path):
    for n in ("alpha", "web"):
        (tmp_path / f".env.{n}").write_text(f"FSR_BASE_URL=https://{n}.example.com\nFSR_API_KEY=k\n")
    p = tmp_path / "instances.toml"
    p.write_text(f"""
[instances.alpha]
env_file = "{tmp_path / '.env.alpha'}"
[instances.alpha.appliance]
host = "ssh-alpha.example.com"
user = "admin"
port = 11000
key_path = "/keys/alpha"

[instances.web]
env_file = "{tmp_path / '.env.web'}"

[instances.inline]
base_url = "https://inline.example.com"
[instances.inline.auth]
type = "credentials"
username = "u"
password = "pw"
[instances.inline.appliance]
host = "ssh-inline.example.com"
user = "labuser"
port = 22
insecure_skip_host_key_check = true

[instances.outsider]
env_file = "{tmp_path / '.env.alpha'}"
""")
    return tmp_path


def _env(reg, **kw):
    e = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"),
         "PYFSR_INSTANCES": str(reg / "instances.toml"), "PYFSR_PY": sys.executable,
         "FSR_BOX_ALLOW": "alpha web inline ghost", "FSR_BOX_CACHE": str(reg / "cache")}
    if os.environ.get("PYTHONPATH"):
        e["PYTHONPATH"] = os.environ["PYTHONPATH"]
    e.update(kw)
    return e


def _run(reg, need="env,ssh", **kw):
    return subprocess.run(["bash", str(BOXSH), "--need", need, "--print"],
                          capture_output=True, text=True, env=_env(reg, **kw), timeout=60)


def _kv(out):
    return dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)


def test_box_resolves_env_file_and_ssh_from_the_registry(reg):
    r = _run(reg, BOX="alpha")
    assert r.returncode == 0, r.stderr
    kv = _kv(r.stdout)
    assert kv["BOX"] == "alpha"
    assert kv["BOX_ENV"] == str(reg / ".env.alpha")
    assert kv["BOX_SSH"] == "fsrbox-alpha"
    conf = (reg / "cache" / "ssh" / "alpha.conf").read_text()
    for line in ("Host fsrbox-alpha", "HostName ssh-alpha.example.com", "User admin",
                 "Port 11000", "IdentityFile /keys/alpha", "StrictHostKeyChecking accept-new"):
        assert line in conf, (line, conf)


def test_an_already_sourced_box_env_does_not_hide_the_registry(reg):
    # A recipe that sourced one box's env file (FSR_BASE_URL etc. exported) and then
    # runs a box target must still resolve BOX=<alias>; pyfsr alone would see only
    # an env-built "default" instance.
    home = reg / "home"
    (home / ".pyfsr").mkdir(parents=True)
    (home / ".pyfsr" / "instances.toml").write_text((reg / "instances.toml").read_text())
    e = _env(reg, BOX="alpha", HOME=str(home), FSR_BASE_URL="https://other.example.com",
             FSR_USERNAME="u", FSR_PASSWORD="p")
    del e["PYFSR_INSTANCES"]   # the default search path, as in a real recipe
    r = subprocess.run(["bash", str(BOXSH), "--need", "env", "--print"],
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    assert _kv(r.stdout)["BOX_ENV"] == str(reg / ".env.alpha")


def test_no_box_has_no_default(reg):
    r = _run(reg)
    assert r.returncode == 2 and "no default box" in r.stderr


@pytest.mark.parametrize("box,msg", [
    ("outsider", "not an agent-targetable box"),   # in the registry, not on the list
    ("ghost", "registry has no answer"),          # on the list, not in the registry
])
def test_unknown_or_unlisted_box_is_refused(reg, box, msg):
    r = _run(reg, BOX=box)
    assert r.returncode == 2 and msg in r.stderr, r.stderr


def test_box_without_ssh_profile_is_refused_for_ssh_but_fine_for_env(reg):
    r = _run(reg, BOX="web")
    assert r.returncode == 2 and "no ssh profile" in r.stderr
    r = _run(reg, need="env", BOX="web")
    assert r.returncode == 0 and _kv(r.stdout)["BOX_ENV"] == str(reg / ".env.web")
    r = _run(reg, need="env,ssh?", BOX="web")
    assert r.returncode == 0 and _kv(r.stdout)["BOX_SSH"] == ""


def test_inline_alias_gets_a_private_env_file_and_insecure_ssh(reg):
    r = _run(reg, BOX="inline")
    assert r.returncode == 0, r.stderr
    f = Path(_kv(r.stdout)["BOX_ENV"])
    try:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert "inline.example.com" in f.read_text()
    finally:
        f.unlink(missing_ok=True)
    conf = (reg / "cache" / "ssh" / "inline.conf").read_text()
    assert "StrictHostKeyChecking no" in conf and "UserKnownHostsFile /dev/null" in conf


@pytest.mark.parametrize("knob", ["ENV", "FSR_ENV", "SHIP_ENV", "SWEEP_ENV", "MATRIX_ENV", "LIVE_ENV"])
def test_retired_env_knob_warns_and_maps_for_a_human(reg, knob):
    r = _run(reg, **{knob: ".env.alpha"})
    assert r.returncode == 0, r.stderr
    assert "retired" in r.stderr and "BOX=alpha" in r.stderr
    assert _kv(r.stdout)["BOX"] == "alpha"


@pytest.mark.parametrize("env,msg", [
    ({"ENV": ".env.nope"}, "names no registered box"),
    ({"SSH_HOST": "fsr159"}, "cannot pick a box on its own"),
    ({"BOX": "web", "ENV": ".env.alpha"}, "pass BOX=<alias> alone"),
    ({"ENV": ".env.alpha", "SHIP_ENV": ".env.web"}, "name different boxes"),
])
def test_retired_knob_that_does_not_map_is_refused(reg, env, msg):
    r = _run(reg, need="env", **env)
    assert r.returncode == 2 and msg in r.stderr, r.stderr


@pytest.mark.parametrize("knob", ["ENV", "SHIP_ENV", "SSH_HOST"])
def test_agents_are_refused_any_retired_knob(reg, knob):
    r = _run(reg, BOX="alpha", CLAUDECODE="1", **{knob: ".env.alpha"})
    assert r.returncode == 2 and f"{knob}= is retired; pass BOX=<alias>" in r.stderr


def test_ssh_shim_points_a_box_destination_at_its_generated_config(reg, tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "ssh").write_text('#!/bin/sh\necho "ARGS $*"\n')
    (fake / "ssh").chmod(0o755)
    e = _env(reg, BOX="alpha", PATH=f"{fake}:{os.environ['PATH']}")
    script = f'source "{BOXSH}"; resolve_box ssh >/dev/null 2>&1 || exit 9; ssh fsrbox-alpha hostname; ssh other-host x'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == f"ARGS -F {reg / 'cache' / 'ssh' / 'alpha.conf'} fsrbox-alpha hostname"
    assert lines[1] == "ARGS other-host x"   # a non-box destination passes through untouched


def test_main_checkout_of_a_worktree(tmp_path):
    # Run under a git hook (pre-push runs this suite), git exports GIT_DIR and friends,
    # which would point these commands at the real repo instead of the temp one.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    main = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(main)], check=True, env=env)
    subprocess.run(["git", "-C", str(main), "-c", "user.email=a@b", "-c", "user.name=a",
                    "commit", "-q", "--allow-empty", "-m", "x"], check=True, env=env)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True, env=env)
    r = subprocess.run(["bash", str(BOXSH), "--main-checkout", str(wt)], capture_output=True, text=True, env=env)
    assert Path(r.stdout.strip()).resolve() == main.resolve()


def test_make_targets_resolve_through_box_sh(reg):
    e = _env(reg, BOX="alpha")
    r = subprocess.run(["make", "--no-print-directory", "-C", str(ROOT), "help"],
                       capture_output=True, text=True, env=e, timeout=120)
    assert r.returncode == 0, r.stderr
    assert f"box=alpha  env={reg / '.env.alpha'}" in r.stdout
    r = subprocess.run(["make", "--no-print-directory", "-C", str(ROOT), "-n", "ssh"],
                       capture_output=True, text=True, env=e, timeout=120)
    assert "ssh fsrbox-alpha" in r.stdout, r.stdout + r.stderr


def test_no_script_still_reads_the_retired_registry():
    hits = subprocess.run(["/usr/bin/grep", "-rlE", r"resolve_box\.sh|boxes\.tsv|\$\{?SSH_HOST\b|\$\{?FSR_ENV\b",
                           str(ROOT / "scripts"), str(ROOT / "Makefile")],
                          capture_output=True, text=True).stdout.split()
    hits = [h for h in hits if not h.endswith(("box.sh", ".pyc")) and "__pycache__" not in h]
    assert hits == [], hits


# ---------------------------------------------------------------- box roles
# A registry entry may carry role = "stable" | "experimental". Unreleased code
# (the `push` need token: dev-push, ship-dev, loop, box-only deploy) is refused on a
# stable box before anything is sent; read-only targets and released installs pass.

@pytest.fixture()
def role_reg(reg):
    p = reg / "instances.toml"
    p.write_text(p.read_text().replace(
        '[instances.alpha]\n', '[instances.alpha]\nrole = "stable"\n', 1).replace(
        '[instances.inline]\n', '[instances.inline]\nrole = "experimental"\n', 1))
    return reg


def _box_cli(reg, *args, **kw):
    return subprocess.run(["bash", str(BOXSH), *args], capture_output=True, text=True,
                          env=_env(reg, **kw), timeout=60)


@pytest.mark.parametrize("alias,role", [("alpha", "stable"), ("inline", "experimental"), ("web", "unset")])
def test_role_is_read_from_the_registry(role_reg, alias, role):
    r = _box_cli(role_reg, "--role", alias)
    assert r.returncode == 0 and r.stdout.strip() == role
    assert f"[role: {role}]" in _run(role_reg, need="env", BOX=alias).stderr


def test_unknown_role_value_reads_as_unset(role_reg):
    p = role_reg / "instances.toml"
    p.write_text(p.read_text().replace('role = "stable"', 'role = "production"'))
    assert _box_cli(role_reg, "--role", "alpha").stdout.strip() == "unset"
    assert _run(role_reg, need="env,ssh,push", BOX="alpha").returncode == 0   # unset = as before


def test_push_to_a_stable_box_is_refused(role_reg):
    r = _run(role_reg, need="env,ssh,push", BOX="alpha")
    assert r.returncode == 2
    assert "STABLE box" in r.stderr and "make stable" in r.stderr
    assert "BOX=" not in r.stdout   # nothing was resolved


def test_push_to_experimental_and_unset_boxes_is_allowed(role_reg):
    assert _run(role_reg, need="env,ssh,push", BOX="inline").returncode == 0
    assert _run(role_reg, need="env,push", BOX="web").returncode == 0


def test_read_only_targets_still_reach_a_stable_box(role_reg):
    assert _run(role_reg, need="env,ssh", BOX="alpha").returncode == 0
    assert _run(role_reg, need="env,ssh?", BOX="alpha").returncode == 0


def test_no_env_override_reopens_ssh_to_a_stable_box(role_reg):
    # the retired bypasses: neither the owner override nor a release install unlocks ssh
    for extra in ({"ALLOW_STABLE_PUSH": "1"}, {"FSR_RELEASE_INSTALL": "1"},
                  {"FSR_RELEASE_INSTALL": "1", "CONFIRM_DEPLOY": "1"}):
        r = _run(role_reg, need="env,ssh,push", BOX="alpha", **extra)
        assert r.returncode == 2 and "STABLE box" in r.stderr, (extra, r.stderr[-300:])


def test_stable_install_path_reads_the_box_without_the_push_token(role_reg):
    # `make stable` drives the Import Wizard over REST: env + role, no ssh, no push token
    r = _run(role_reg, need="env", BOX="alpha")
    assert r.returncode == 0 and "[role: stable]" in r.stderr


@pytest.mark.parametrize("script,args", [
    ("dev_push.sh", []), ("ship_dev.sh", []), ("loop.sh", []),
    ("deploy.sh", ["--bump", "none", "--no-test"]), ("deploy_chain.sh", ["--dry-run"]),
])
def test_every_push_script_refuses_a_stable_box_before_doing_anything(role_reg, script, args):
    # the script resolves the box first; a stable box stops it there (rc 2, the role message)
    r = subprocess.run(["bash", str(ROOT / "scripts" / script), *args], capture_output=True, text=True,
                       env=_env(role_reg, BOX="alpha", VIA_MAKE="1", MAKELEVEL="1"), timeout=120)
    assert r.returncode == 2 and "STABLE box" in r.stderr, (r.returncode, r.stderr[-400:])
    assert ">> target box" not in r.stderr   # no banner: it never got as far as a target


def test_deploy_chain_release_path_is_refused_on_a_stable_box(role_reg):
    # a release install (FW=...) used to reach a stable box; now it is refused like any push
    r = subprocess.run(["bash", str(ROOT / "scripts" / "deploy_chain.sh"), "--fw", "next", "--dry-run"],
                       capture_output=True, text=True,
                       env=_env(role_reg, BOX="alpha", VIA_MAKE="1", MAKELEVEL="1",
                                SKIP_QUALITY_GATE="1", SKIP_QUALITY_GATE_REASON="role-resolution test"),
                       timeout=120)
    assert r.returncode == 2 and "STABLE box" in r.stderr
