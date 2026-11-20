"""The pre-push decides how much to run from what is pushed (scripts/hooks/pre-push).

- A branch push runs the tests for what it changed: its changed test files,
  every test that imports a module it changed, every test that names a script
  it changed. CI runs the full suite before main moves.
- A main push skips the suite only when the required CI is green on that EXACT
  commit (a fast-forward of a CI-green head). A merge commit CI never saw, or a
  pending run, runs the full suite.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "FSR", "PREPUSH"))},
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
ZERO = "0" * 40


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


def _write(repo, rel, body):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "conn"
    (r / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "hooks" / "pre-push", r / "scripts" / "hooks" / "pre-push")
    _write(r, ".github/required-checks", "test (bundled framework)\n")
    _write(r, "connector-fsr-agentic-assistant/shared_mod.py", "X = 1\n")
    _write(r, "connector-fsr-agentic-assistant/other_mod.py", "Y = 1\n")
    _write(r, "scripts/foo.sh", "echo foo\n")
    _write(r, "tests/test_uses_shared.py", "from shared_mod import X\n")
    _write(r, "connector-fsr-agentic-assistant/tests/test_pkg.py", "from pkg.shared_mod import X\n")
    _write(r, "tests/test_uses_shared_attr.py", "from pkg import shared_mod\n")
    _write(r, "tests/test_other.py", "from other_mod import Y\n")
    _write(r, "tests/test_foo_script.py", "SCRIPT = 'scripts/foo.sh'\n")
    _git(tmp_path, "init", "-q", "-b", "main", str(r))
    _git(r, "add", "-A"); _git(r, "commit", "-q", "-m", "base")
    _git(r, "remote", "add", "origin", "https://github.com/o/conn.git")
    _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(r, "checkout", "-q", "-b", "feat/x")
    bin_ = tmp_path / "bin"; bin_.mkdir()
    return r, bin_


def _gh(bin_, body):
    (bin_ / "gh").write_text(f"#!/bin/sh\n{body}\n"); (bin_ / "gh").chmod(0o755)


def _scope(r, bin_, ref):
    sha = _git(r, "rev-parse", "HEAD")
    out = subprocess.run(["bash", str(r / "scripts" / "hooks" / "pre-push"), "origin", "x"], cwd=r,
                         input=f"refs/heads/{ref} {sha} refs/heads/{ref} {ZERO}\n", capture_output=True, text=True,
                         env={**ENV, "PATH": f"{bin_}:{os.environ['PATH']}", "PREPUSH_SCOPE_ONLY": "1",
                              "TEST_PY": shutil.which("python3"), "FSR_SRC": str(r)})
    assert out.returncode == 0, out.stderr
    scope = next(l for l in out.stdout.splitlines() if "scope=" in l)
    sel = sorted(l.split(" ", 1)[1] for l in out.stdout.splitlines() if l.startswith("selected ") and len(l) > 9)
    return scope, sel


def test_a_shared_module_change_selects_every_test_that_imports_it(repo):
    r, b = repo
    _write(r, "connector-fsr-agentic-assistant/shared_mod.py", "X = 2\n"); _git(r, "commit", "-qam", "m")
    scope, sel = _scope(r, b, "feat/x")
    assert "scope=changed" in scope
    assert sel == ["connector-fsr-agentic-assistant/tests/test_pkg.py", "tests/test_uses_shared.py",
                   "tests/test_uses_shared_attr.py"]


def test_a_script_change_selects_the_tests_that_name_it(repo):
    r, b = repo
    _write(r, "scripts/foo.sh", "echo bar\n"); _git(r, "commit", "-qam", "s")
    assert _scope(r, b, "feat/x")[1] == ["tests/test_foo_script.py"]


def test_a_changed_test_file_selects_itself(repo):
    r, b = repo
    _write(r, "tests/test_other.py", "from other_mod import Y\nZ = 1\n"); _git(r, "commit", "-qam", "t")
    assert _scope(r, b, "feat/x")[1] == ["tests/test_other.py"]


def test_main_skips_only_when_ci_is_green_on_that_exact_commit(repo):
    r, b = repo
    _gh(b, "printf 'test (bundled framework)\\tcompleted\\tsuccess\\n'")
    assert "scope=none" in _scope(r, b, "main")[0]


def test_a_merge_commit_ci_never_saw_runs_the_full_suite(repo):
    r, b = repo
    # main moved since the branch was cut; promote builds a merge commit.
    _write(r, "tests/test_other.py", "from other_mod import Y\nW = 1\n"); _git(r, "commit", "-qam", "branch")
    _git(r, "checkout", "-q", "main"); _write(r, "scripts/foo.sh", "echo main moved\n"); _git(r, "commit", "-qam", "main")
    _git(r, "merge", "-q", "--no-ff", "--no-edit", "feat/x")
    _gh(b, "echo 'gh: No commit found for SHA (HTTP 422)' >&2; exit 1")
    scope, _ = _scope(r, b, "main")
    assert "scope=full" in scope


def test_a_pending_or_failed_run_on_main_runs_the_full_suite(repo):
    r, b = repo
    for body in ("printf 'test (bundled framework)\\tin_progress\\t\\n'",
                 "printf 'test (bundled framework)\\tcompleted\\tfailure\\n'"):
        _gh(b, body)
        assert "scope=full" in _scope(r, b, "main")[0]


def test_a_push_lands_in_the_lap_ledger_and_warns_over_budget(repo, tmp_path):
    r, b = repo
    _write(r, "README.md", "x\n"); _git(r, "add", "-A"); _git(r, "commit", "-qm", "doc")
    ledger = tmp_path / "laps.tsv"
    (b / "ruff").write_text("#!/bin/sh\nexit 0\n"); (b / "ruff").chmod(0o755)   # lint is not under test
    sha = _git(r, "rev-parse", "HEAD")
    out = subprocess.run(["bash", str(r / "scripts" / "hooks" / "pre-push"), "origin", "x"], cwd=r,
                         input=f"refs/heads/feat/x {sha} refs/heads/feat/x {ZERO}\n", capture_output=True, text=True,
                         env={**ENV, "PATH": f"{b}:{os.environ['PATH']}", "TEST_PY": sys.executable,
                              "FSR_SRC": str(r), "FSR_LAP_LEDGER": str(ledger), "PREPUSH_BUDGET": "-1"})
    assert out.returncode == 0, out.stderr
    assert "WARNING took" in out.stderr
    row = ledger.read_text().strip().split("\t")
    assert row[1] == "pre-push changed" and row[4] == "-1" and row[5] == "conn"


def _push(r, b, tmp_path, **env):
    (b / "ruff").write_text("#!/bin/sh\nexit 0\n"); (b / "ruff").chmod(0o755)
    sha = _git(r, "rev-parse", "HEAD")
    return subprocess.run(["bash", str(r / "scripts" / "hooks" / "pre-push"), "origin", "x"], cwd=r,
                          input=f"refs/heads/feat/x {sha} refs/heads/feat/x {ZERO}\n", capture_output=True, text=True,
                          env={**ENV, "PATH": f"{b}:{os.environ['PATH']}", "TEST_PY": sys.executable, "FSR_SRC": str(r),
                               "FSR_LAP_LEDGER": str(tmp_path / "laps.tsv"), **env})


def test_shell_heavy_tests_are_left_to_ci_and_named(repo, tmp_path):
    r, b = repo
    _write(r, "conftest.py", 'SHELL_HEAVY_MODULES = frozenset({"tests/test_foo_script.py"})\n')
    _write(r, "tests/test_foo_script.py", "SCRIPT = 'scripts/foo.sh'\n\ndef test_ran():\n    open('RAN', 'w').write('x')\n")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "fixture"); _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(r, "scripts/foo.sh", "echo changed\n"); _git(r, "commit", "-qam", "s")
    out = _push(r, b, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "left to CI (shell-heavy, PREPUSH_SHELL=1 runs them): tests/test_foo_script.py" in out.stdout
    assert not (r / "RAN").exists()
    out = _push(r, b, tmp_path, PREPUSH_SHELL="1")
    assert out.returncode == 0, out.stdout + out.stderr
    assert (r / "RAN").exists() and "left to CI" not in out.stdout


def test_a_slow_module_not_on_the_shell_list_is_named(repo, tmp_path):
    r, b = repo
    _write(r, "conftest.py", "SHELL_HEAVY_MODULES = frozenset()\n")
    _write(r, "tests/test_foo_script.py", "import time\nSCRIPT = 'scripts/foo.sh'\n\ndef test_slow():\n    time.sleep(0.8)\n")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "fixture"); _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(r, "scripts/foo.sh", "echo changed\n"); _git(r, "commit", "-qam", "s")
    out = _push(r, b, tmp_path, PREPUSH_SLOW_MODULE="0.5")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "tests/test_foo_script.py took" in out.stdout and "consider marking it shell-heavy" in out.stdout
    # On the list (or fast enough), no hint.
    out = _push(r, b, tmp_path, PREPUSH_SLOW_MODULE="30")
    assert "consider marking" not in out.stdout
