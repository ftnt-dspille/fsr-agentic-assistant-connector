"""pre-push skips a pass only for code that already passed, and only that code.

The cache key must move with every input the suite depends on (connector tree,
framework checkout commit, requirements) and must refuse a dirty tree. The hook
must skip the suite outright for a push that only deletes remote refs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "scripts" / "hooks"
_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def _env(**extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e.update(_ID)
    # The cache key also reads the widgets checkout's HEAD and dirtiness. Pointed at
    # the real one, a commit or edit by anyone working there between `mark` and `has`
    # made the key vanish (dirty) or change, so "a recorded pass is found" failed at
    # random. A path that is no git repo contributes nothing and cannot move.
    e["FSR_ALL_WIDGETS"] = "/nonexistent/fsr_all_widgets"
    e.update(extra)
    return e


def _git(cwd, *a):
    # core.hooksPath=/dev/null: fixture commits are plumbing; the user's global hooks
    # (content scans) would run on each one, seconds apiece.
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *a], cwd=cwd, env=_env(), check=True, capture_output=True)


def _repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    r = tmp_path / name
    for rel, text in files.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "i")
    return r


def _setup(tmp_path):
    conn = _repo(tmp_path, "conn", {
        "pyproject.toml": "[project]\nname='x'\n",
        "connector-fsr-agentic-assistant/requirements.txt": "fsr-playbooks==1.0\n",
        "code.py": "x = 1\n",
    })
    (conn / "scripts" / "hooks").mkdir(parents=True)
    for f in ("test_pass_cache.sh", "pre-push"):
        shutil.copy(HOOKS / f, conn / "scripts" / "hooks" / f)
    (conn / ".gitignore").write_text("scripts/\n")
    _git(conn, "add", ".gitignore")
    _git(conn, "commit", "-q", "-m", "ignore")
    fw = _repo(tmp_path, "fw", {"f.py": "y = 1\n"})
    return conn, fw, tmp_path / "cache"


def _cache(conn, fw, cache, *args):
    return subprocess.run(["bash", str(conn / "scripts/hooks/test_pass_cache.sh"), *args],
                          cwd=conn, capture_output=True, text=True,
                          env=_env(FSRPB_FRAMEWORK_DIR=str(fw), FSR_TEST_PASS_CACHE=str(cache)))


def test_a_recorded_pass_is_found_for_the_same_code(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1
    assert _cache(conn, fw, cache, "mark", "editable").returncode == 0
    assert _cache(conn, fw, cache, "has", "editable").returncode == 0
    assert _cache(conn, fw, cache, "has", "ci-mirror").returncode == 1


def test_any_input_change_invalidates_the_pass(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    _cache(conn, fw, cache, "mark", "editable")

    (conn / "code.py").write_text("x = 2\n")          # dirty tree: never trusted
    assert _cache(conn, fw, cache, "key").returncode == 1
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1
    _git(conn, "commit", "-qam", "change")            # new tree
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1

    _cache(conn, fw, cache, "mark", "editable")
    (fw / "f.py").write_text("y = 2\n")               # framework dirty
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1
    _git(fw, "commit", "-qam", "fw")                  # framework moved
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1

    _cache(conn, fw, cache, "mark", "editable")
    (conn / "connector-fsr-agentic-assistant/requirements.txt").write_text("fsr-playbooks==2.0\n")
    _git(conn, "commit", "-qam", "pin")               # requirements moved
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1


def test_a_delete_only_push_runs_no_suite(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    zero = "0" * 40
    r = subprocess.run(["bash", str(conn / "scripts/hooks/pre-push"), "origin", "x"],
                       cwd=conn, capture_output=True, text=True,
                       input=f"(delete) {zero} refs/heads/old {'1' * 40}\n",
                       env=_env(TEST_PY="/nonexistent/python", FSRPB_FRAMEWORK_DIR=str(fw),
                                FSR_TEST_PASS_CACHE=str(cache)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "only deleting remote refs" in r.stdout
