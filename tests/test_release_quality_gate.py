"""P1: a connector release refuses without a fresh quality stamp for the same commit.
P4: every skip flag on the release path needs a logged reason and is listed in the
release summary.

The quality tiers themselves are slow, so the gate is exercised here against a
throwaway git repo whose Makefile stands in for them (`run` shells out to
`make -C <root> <tier>`); the shell scripts are driven for real.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .test_box import _env, reg  # noqa: F401  (reg is a fixture)

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "quality_gate.py"


def _git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                    "-c", "core.hooksPath=/dev/null", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    """A repo carrying a copy of the gate and a Makefile whose three tiers pass."""
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    shutil.copy(GATE, r / "scripts" / "quality_gate.py")
    (r / "Makefile").write_text(
        "tool-sweep t1-scenarios known-answer:\n\t@echo tier $@ $(STRICT) $(LLM) >> tiers.log\n")
    (r / ".gitignore").write_text("tiers.log\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _gate(repo, cmd, stamps):
    return subprocess.run([sys.executable, str(repo / "scripts" / "quality_gate.py"), cmd],
                          cwd=repo, capture_output=True, text=True,
                          env={**os.environ, "QUALITY_STAMP_DIR": str(stamps)})


def test_check_without_a_stamp_exits_nonzero(repo, tmp_path):
    r = _gate(repo, "check", tmp_path / "stamps")
    assert r.returncode == 1
    assert "REFUSED" in r.stderr and "make quality-gate" in r.stderr


def test_run_stamps_head_and_check_then_passes(repo, tmp_path):
    stamps = tmp_path / "stamps"
    assert _gate(repo, "run", stamps).returncode == 0
    # the three tiers ran with the arguments the release relies on
    log = (repo / "tiers.log").read_text()
    assert "tier tool-sweep 1" in log and "tier t1-scenarios" in log and "LLM=fake".split("=")[1] in log
    assert "frank" in log.splitlines()[2]
    stamp = json.loads(next(stamps.iterdir()).read_text())
    assert stamp["passed"] and set(stamp["tiers"]) == {"tool-sweep", "t1-scenarios", "known-answer"}
    assert all(t["seconds"] >= 0 for t in stamp["tiers"].values())
    assert _gate(repo, "check", stamps).returncode == 0


def test_a_new_commit_invalidates_the_stamp(repo, tmp_path):
    stamps = tmp_path / "stamps"
    assert _gate(repo, "run", stamps).returncode == 0
    (repo / "x.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "next")
    r = _gate(repo, "check", stamps)
    assert r.returncode == 1 and "no quality stamp for commit" in r.stderr


def test_a_dirty_tracked_tree_is_refused_by_check_and_run(repo, tmp_path):
    stamps = tmp_path / "stamps"
    assert _gate(repo, "run", stamps).returncode == 0
    (repo / "Makefile").write_text((repo / "Makefile").read_text() + "# edit\n")
    assert _gate(repo, "check", stamps).returncode == 1
    r = _gate(repo, "run", tmp_path / "stamps2")
    assert r.returncode == 2 and "dirty" in r.stderr
    assert not (tmp_path / "stamps2").exists()


def test_a_failing_tier_writes_no_stamp(repo, tmp_path):
    (repo / "Makefile").write_text(
        "tool-sweep t1-scenarios:\n\t@true\nknown-answer:\n\t@false\n")
    _git(repo, "commit", "-q", "-am", "red tier")
    stamps = tmp_path / "stamps"
    r = _gate(repo, "run", stamps)
    assert r.returncode == 1 and "known-answer failed" in r.stderr
    assert not stamps.exists() or not list(stamps.iterdir())
    assert _gate(repo, "check", stamps).returncode == 1


def test_the_gate_tiers_are_the_ones_the_roadmap_names():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import quality_gate
    finally:
        sys.path.pop(0)
    got = {n: " ".join(a) for n, a in quality_gate.TIERS}
    assert got == {"tool-sweep": "tool-sweep STRICT=1", "t1-scenarios": "t1-scenarios LLM=fake",
                   "known-answer": "known-answer LLM=frank"}
    # every tier is a real make target
    mk = (ROOT / "Makefile").read_text()
    for name in got:
        assert f"\n{name}:" in mk


# -- the release path really calls the gate --------------------------------------

def _sh(script, args, env_extra=None, **kw):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"),
           "VIA_MAKE": "1", "MAKELEVEL": "1",
           # never let a regression start the real pytest gate from inside pytest
           "TEST_PY": "/nonexistent/python", **(env_extra or {})}
    return subprocess.run(["bash", str(ROOT / "scripts" / script), *args],
                          capture_output=True, text=True, env=env, timeout=120, **kw)


def test_deploy_release_install_without_a_stamp_exits_nonzero(tmp_path):
    r = _sh("deploy.sh", ["--no-install", "--bump", "none"],
            {"FSR_RELEASE_INSTALL": "1", "QUALITY_STAMP_DIR": str(tmp_path / "none")})
    assert r.returncode == 1 and "quality-gate REFUSED" in r.stderr
    assert "test gate" not in r.stdout   # refused before the (slow) pytest gate


def test_deploy_chain_release_without_a_stamp_exits_nonzero_before_any_release_step(reg, tmp_path):
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "deploy_chain.sh"), "--fw", "next", "--dry-run"],
        capture_output=True, text=True, timeout=120,
        env=_env(reg, BOX="inline", VIA_MAKE="1", MAKELEVEL="1", QUALITY_STAMP_DIR=str(tmp_path / "none")))
    assert r.returncode == 1 and "quality-gate REFUSED" in r.stderr
    assert "PyPI latest" not in r.stdout   # no network step was reached


def test_a_box_only_deploy_chain_is_not_quality_gated(reg, tmp_path):
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "deploy_chain.sh"), "--dry-run"],
        capture_output=True, text=True, timeout=120,
        env=_env(reg, BOX="inline", VIA_MAKE="1", MAKELEVEL="1", QUALITY_STAMP_DIR=str(tmp_path / "none")))
    assert "quality-gate" not in r.stderr


# -- P4: skips need a reason and are listed --------------------------------------

SKIP_CASES = [  # (deploy.sh args, env flag, reason var, label)
    (["--no-verify"], {}, "SKIP_VERIFY_REASON", "verify"),
    (["--no-warmup"], {}, "SKIP_WARMUP_REASON", "warmup"),
    (["--no-deps"], {}, "SKIP_DEPS_REASON", "deps"),
    (["--skip-version-check"], {}, "SKIP_VERSION_CHECK_REASON", "version-check"),
    (["--no-test"], {}, "SKIP_TESTS_REASON", "tests"),
    ([], {"SKIP_TESTS": "1"}, "SKIP_TESTS_REASON", "tests"),
    ([], {"SKIP_LIVE_SANITY": "1"}, "SKIP_LIVE_SANITY_REASON", "live-sanity"),
    ([], {"FSR_RELEASE_INSTALL": "1", "SKIP_QUALITY_GATE": "1"}, "SKIP_QUALITY_GATE_REASON", "quality-gate"),
]


@pytest.mark.parametrize("args,flag_env,reason_var,label", SKIP_CASES)
def test_a_skip_without_a_reason_is_refused(args, flag_env, reason_var, label):
    r = _sh("deploy.sh", ["--no-install", "--bump", "none", *args], flag_env)
    assert r.returncode == 1, (r.stdout[-300:], r.stderr[-300:])
    assert reason_var in r.stderr and f"'{label}'" in r.stderr


@pytest.mark.parametrize("args,flag_env,reason_var,label", SKIP_CASES)
def test_a_skip_with_a_reason_is_logged_and_listed_in_the_final_summary(args, flag_env, reason_var, label, tmp_path):
    # Stop right after the declarations: a bad --bump (or no pytest python) makes
    # deploy.sh fail before it builds anything, and the summary must still print on
    # that failing exit.
    env = {**flag_env, reason_var: "because of a test",
           "QUALITY_STAMP_DIR": str(tmp_path / "none")}
    if label == "quality-gate":
        env["SKIP_TESTS"] = "1"
        env["SKIP_TESTS_REASON"] = "stop here"
    log_file = tmp_path / "ungated.log"
    env["SHIP_UNGATED_LOG"] = str(log_file)
    r = _sh("deploy.sh", ["--no-install", "--bump", "bogus", *args], env)
    assert r.returncode != 0
    assert "== release summary: GATES SKIPPED ==" in r.stderr, r.stderr[-600:]
    assert f"SKIPPED {label}: because of a test" in r.stderr
    assert f"because of a test\t{label}" in log_file.read_text()


def test_no_skip_prints_no_skip_summary():
    r = _sh("deploy.sh", ["--no-install", "--bump", "none"], {"TEST_PY": "/nonexistent/python"})
    assert "no gates skipped" in r.stderr and "GATES SKIPPED" not in r.stderr


def test_deploy_chain_ci_watch_skip_needs_a_reason(reg, tmp_path):
    env = dict(BOX="inline", VIA_MAKE="1", MAKELEVEL="1", SKIP_CI_WATCH="1")
    r = subprocess.run(["bash", str(ROOT / "scripts" / "deploy_chain.sh"), "--dry-run"],
                       capture_output=True, text=True, timeout=120, env=_env(reg, **env))
    assert r.returncode == 1 and "SKIP_CI_WATCH_REASON" in r.stderr


def test_every_release_path_skip_flag_goes_through_release_skip():
    """Relationship test: a new `--no-*`/SKIP_* flag in deploy.sh must be declared."""
    import re
    body = (ROOT / "scripts" / "deploy.sh").read_text()
    flags = set(re.findall(r"^\s+--(no-[a-z]+|skip-[a-z-]+)\)", body, re.M)) - {"no-install"}
    assert flags == {"no-warmup", "no-verify", "no-test", "no-deps", "skip-version-check"}
    declared = set(re.findall(r"release_skip ([a-z-]+) ", body))
    assert declared >= {"warmup", "verify", "deps", "version-check", "tests", "live-sanity", "quality-gate"}
