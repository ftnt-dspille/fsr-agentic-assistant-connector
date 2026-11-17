"""scripts/prune_remote_py.sh: the dev push's delete pass removes ONLY stale .py.

Runs under the PATH bash (`/usr/bin/env bash`) -- the one the dev pushes and
the script's shebang resolve; macOS's /bin/bash 3.2 is not a target.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prune_remote_py.sh"
pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="no rsync")


@pytest.fixture
def bash():
    return shutil.which("bash") or "bash"


def _tree(root: Path, files: list[str]) -> None:
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")


def _run(bash, src, dst, *extra, env=None):
    return subprocess.run([bash, str(SCRIPT), f"{src}/", "-", f"{dst}/", *extra],
                          capture_output=True, text=True, env=env)


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_prunes_stale_py_and_nothing_else(tmp_path, bash):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _tree(src, ["operations.py", "tests/test_kept.py", "info.json"])
    _tree(dst, ["operations.py", "tests/test_kept.py", "info.json",
                "tests/test_deleted.py", "old_module.py",      # stale code
                "storage.db", "wheels/pkg.whl", "notes.md",    # never code
                "wheels/vendored.py",                          # excluded dir
                "__pycache__/old_module.cpython-311.pyc"])
    r = _run(bash, src, dst, "--exclude", "wheels/")
    assert r.returncode == 0, r.stderr
    assert _files(dst) == {"operations.py", "tests/test_kept.py", "info.json",
                           "storage.db", "wheels/pkg.whl", "notes.md",
                           "wheels/vendored.py",
                           "__pycache__/old_module.cpython-311.pyc"}
    assert "old_module.py" in r.stdout and "tests/test_deleted.py" in r.stdout


def test_over_the_cap_deletes_nothing(tmp_path, bash):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _tree(src, ["keep.py"])
    _tree(dst, ["keep.py"] + [f"stale{i}.py" for i in range(5)])
    r = _run(bash, src, dst, env={**os.environ, "PRUNE_MAX": "3"})
    assert r.returncode == 1 and "deleted NOTHING" in r.stderr
    assert len(_files(dst)) == 6


def test_dry_run_lists_without_deleting(tmp_path, bash):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _tree(src, ["keep.py"])
    _tree(dst, ["keep.py", "stale.py"])
    r = _run(bash, src, dst, env={**os.environ, "PRUNE_DRY": "1"})
    assert r.returncode == 0 and "stale.py" in r.stdout
    assert (dst / "stale.py").exists()


def test_a_source_with_no_code_is_refused(tmp_path, bash):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    _tree(dst, ["operations.py"])
    r = _run(bash, src, dst)
    assert r.returncode == 1 and "refusing" in r.stderr
    assert (dst / "operations.py").exists()


def test_nothing_stale_is_silent(tmp_path, bash):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _tree(src, ["a.py"])
    _tree(dst, ["a.py", "storage.db"])
    r = _run(bash, src, dst)
    assert r.returncode == 0 and r.stdout == ""
