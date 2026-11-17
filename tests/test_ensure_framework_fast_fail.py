"""ensure_framework.sh preflight: fail fast on states that waiting cannot fix.

A fake `ssh` on PATH plays the box, so no box is needed. A box whose pip index
does not carry fsr-playbooks at all (Fortinet's deps mirror without the PyPI
extra index) used to burn the whole --wait retrying, then blame a release race.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ensure_framework.sh"


def _run(tmp_path, ssh_body: str, wait: int = 60, extra_env=None, record_sleeps: bool = False):
    if record_sleeps:
        # A shim `sleep` that logs and returns at once: lets a test assert the
        # script did not wait an INTERVAL, with no dependence on wall time.
        shim = tmp_path / "sleep"
        shim.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{tmp_path}/sleeps.log"\n')
        shim.chmod(0o755)
    fake = tmp_path / "ssh"
    fake.write_text("#!/usr/bin/env bash\n" + ssh_body)
    fake.chmod(0o755)
    sudo = tmp_path / "sudo"  # the box's `sudo -u nginx ...` runs locally in the fake
    # The fake box models chattr +i with a "<file>.imm" marker and a "<file>.failedit"
    # marker that makes any write to the file fail.
    sudo.write_text(r'''#!/usr/bin/env bash
[ "$1" = -u ] && shift 2
case "$1" in
  lsattr) f="${@: -1}"; if [ -e "$f.imm" ]; then echo "----i---------e------- $f"; else echo "--------------e------- $f"; fi; exit 0 ;;
  chattr) f="${@: -1}"; case "$2" in -i) rm -f "$f.imm" ;; +i) touch "$f.imm" ;; esac; exit 0 ;;
  tee) f="${@: -1}"; if [ -e "$f.imm" ] || [ -e "$f.failedit" ]; then echo "tee: $f: Operation not permitted" >&2; exit 1; fi ;;
esac
exec "$@"
''')
    sudo.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", **(extra_env or {})}
    t0 = time.monotonic()
    p = subprocess.run(["bash", str(SCRIPT), "--mode", "preflight", "--ssh-host", "box",
                        "--pin", "9.9.9", "--wait", str(wait)],
                       capture_output=True, text=True, env=env, timeout=120)
    return p, time.monotonic() - t0


def test_index_without_the_package_fails_fast_with_the_fix(tmp_path):
    body = r'''case "$*" in
  *"pip config list"*) echo "global.index-url='https://mirror.example/simple/'" ;;
  *) echo "ERROR: No matching distribution found for fsr-playbooks" ;;
esac
'''
    p, secs = _run(tmp_path, body, extra_env={"NO_FIX_PIP": "1"})
    assert p.returncode == 1
    assert secs < 10, f"retried for {secs:.0f}s instead of failing fast"
    assert "auto-fix skipped" in p.stderr
    assert "does not carry fsr-playbooks" in p.stderr
    assert "https://mirror.example/simple/" in p.stderr
    assert "extra-index-url= https://pypi.org/simple/" in p.stderr


def test_unreachable_box_fails_fast(tmp_path):
    p, secs = _run(tmp_path, 'echo "ssh: connect to host box: Connection refused" >&2; exit 255\n')
    assert p.returncode == 1
    assert secs < 10, f"retried for {secs:.0f}s instead of failing fast"
    assert "cannot reach box over ssh" in p.stderr


def test_pin_visible_passes(tmp_path):
    p, _ = _run(tmp_path, 'echo "fsr-playbooks (9.9.9)"; echo "Available versions: 9.9.9, 9.9.8"\n')
    assert p.returncode == 0, p.stderr
    assert "OK: fsr-playbooks==9.9.9" in p.stdout


def test_stale_index_still_waits_for_the_release(tmp_path):
    """The one state retrying CAN fix keeps the old release-race path."""
    p, _ = _run(tmp_path, 'echo "fsr-playbooks (9.9.8)"; echo "Available versions: 9.9.8"\n', wait=0)
    assert p.returncode == 1
    assert "index currently tops out at 9.9.8" in p.stderr


# --- auto-fix of the box's pip.conf -----------------------------------------

_CONF_DIR = "/opt/cyops/configs/integrations/packages/integrations_env"


def _stateful_box(tmp_path, conf_text: str):
    """Fake box: the pip index only carries the package once pip.conf lists PyPI.

    The repair script really runs (locally, against a temp pip.conf), so the
    assertions below are about the file it writes, not about a canned reply."""
    venv = tmp_path / "venv"
    venv.mkdir()
    conf = venv / "pip.conf"
    conf.write_text(conf_text)
    body = f"""cmd="${{@: -1}}"
cmd="$(sed "s#{_CONF_DIR}#{venv}#g" <<<"$cmd")"
case "$cmd" in
  *"bash -s"*) eval "$cmd" ;;
  *"pip config list"*) grep -i index-url "{conf}" ;;
  *) echo probe >> "{venv}/probes.log"
     if grep -qi 'pypi.org' "{conf}"; then echo "fsr-playbooks (9.9.9)"; else echo "ERROR: No matching distribution"; fi ;;
esac
"""
    return conf, body


def test_auto_fix_adds_pypi_backs_up_and_reprobes(tmp_path):
    original = "[global]\nindex-url= https://mirror.example/simple/\ntimeout= 30\n"
    conf, body = _stateful_box(tmp_path, original)
    p, _ = _run(tmp_path, body, record_sleeps=True)
    assert p.returncode == 0, p.stderr + p.stdout
    # Behaviour, not wall time: the always-ensure step repairs pip.conf BEFORE
    # the first probe, so the index is probed exactly once (it already sees the
    # package) and the script never sleeps an INTERVAL.
    probes = (conf.parent / "probes.log").read_text().split()
    assert len(probes) == 1, f"expected a single probe after the fix; got {len(probes)}"
    assert not (tmp_path / "sleeps.log").exists(), "slept an INTERVAL: re-probe should be immediate"
    text = conf.read_text()
    assert "index-url= https://mirror.example/simple/" in text  # existing index kept
    assert text.count("extra-index-url= https://pypi.org/simple/") == 1
    assert text.index("extra-index-url") > text.index("[global]")
    backups = list(conf.parent.glob("pip.conf.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert "+extra-index-url= https://pypi.org/simple/" in p.stdout  # change is printed
    assert "OK: fsr-playbooks==9.9.9" in p.stdout


def test_auto_fix_is_idempotent_when_pypi_already_listed(tmp_path):
    original = "[global]\nindex-url= https://mirror.example/simple/\nextra-index-url= https://pypi.org/simple/\n"
    conf, body = _stateful_box(tmp_path, original)
    # force the probe to keep failing so the fix path is reached while the file
    # already has the line: it must not be touched or duplicated
    body = body.replace("grep -qi 'pypi.org'", "test -f /nonexistent")
    p, _ = _run(tmp_path, body)
    assert p.returncode == 1
    assert "nothing to change" in p.stdout
    assert conf.read_text() == original
    assert not list(conf.parent.glob("pip.conf.bak-*"))


def test_auto_fix_appends_to_existing_extra_index(tmp_path):
    conf, body = _stateful_box(tmp_path, "[global]\nindex-url= https://m/simple/\nextra-index-url= https://other.example/simple/\n")
    p, _ = _run(tmp_path, body)
    assert p.returncode == 0, p.stderr
    text = conf.read_text()
    assert "extra-index-url= https://other.example/simple/ https://pypi.org/simple/" in text
    assert text.count("extra-index-url") == 1


def test_auto_fix_opt_out_leaves_the_file_alone(tmp_path):
    original = "[global]\nindex-url= https://mirror.example/simple/\n"
    conf, body = _stateful_box(tmp_path, original)
    p, secs = _run(tmp_path, body, extra_env={"NO_FIX_PIP": "1"})
    assert p.returncode == 1
    assert secs < 10
    assert conf.read_text() == original
    assert not list(conf.parent.glob("pip.conf.bak-*"))


# --- always-ensure + immutable pip.conf -------------------------------------

def _probe_ok_box(tmp_path, conf_text, marks=()):
    """Index already carries the pin, so only the always-ensure step touches the file."""
    conf, body = _stateful_box(tmp_path, conf_text)
    body = body.replace("grep -qi 'pypi.org' \"", "true \"")  # probe always passes
    for m in marks:
        (conf.parent / f"pip.conf.{m}").write_text("")
    return conf, body


def test_ensure_runs_even_when_the_probe_would_pass(tmp_path):
    conf, body = _probe_ok_box(tmp_path, "[global]\nindex-url= https://m/simple/\n")
    p, _ = _run(tmp_path, body)
    assert p.returncode == 0, p.stderr
    assert "extra-index-url= https://pypi.org/simple/" in conf.read_text()


def test_immutable_conf_is_edited_and_flag_restored(tmp_path):
    original = "[global]\nindex-url= https://m/simple/\n"
    conf, body = _probe_ok_box(tmp_path, original, marks=("imm",))
    p, _ = _run(tmp_path, body)
    assert p.returncode == 0, p.stderr + p.stdout
    assert conf.read_text().count("extra-index-url= https://pypi.org/simple/") == 1
    assert (conf.parent / "pip.conf.imm").exists(), "chattr +i must be restored"
    assert "immutable" in p.stdout and "restored" in p.stdout
    backups = list(conf.parent.glob("pip.conf.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == original


def test_flag_restored_and_file_intact_when_the_edit_fails(tmp_path):
    original = "[global]\nindex-url= https://m/simple/\n"
    conf, body = _probe_ok_box(tmp_path, original, marks=("imm", "failedit"))
    p, _ = _run(tmp_path, body)
    assert (conf.parent / "pip.conf.imm").exists(), "chattr +i must be restored after a failed edit"
    assert conf.read_text() == original
    assert "could not ensure PyPI" in p.stderr


def test_immutable_conf_already_listing_pypi_is_untouched(tmp_path):
    original = "[global]\nextra-index-url= https://pypi.org/simple/\n"
    conf, body = _probe_ok_box(tmp_path, original, marks=("imm",))
    p, _ = _run(tmp_path, body)
    assert p.returncode == 0, p.stderr
    assert "nothing to change" in p.stdout
    assert conf.read_text() == original
    assert (conf.parent / "pip.conf.imm").exists()
    assert not list(conf.parent.glob("pip.conf.bak-*"))

