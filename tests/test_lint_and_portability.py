"""Lint and portability guards, run by `make test` with the rest of the suite.

* No tracked .py file under scripts/ or tests/ hardcodes a home directory. A
  home-directory literal ties the repo to one machine. Derive the path from
  `__file__`, an env var with a default, or `Path.home()` instead.
* `ruff check tests scripts` stays at zero findings. The ratchet is strict:
  the baseline is zero, so any new finding fails here.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUARDED_DIRS = ("scripts", "tests")
# Built from parts so this file does not contain the literal it forbids.
HOME_LITERAL = "/" + "Users" + "/"


def _tracked_py(dirname: str) -> list[Path]:
    """Tracked .py files under `dirname`; falls back to a filesystem walk
    when this is not a git checkout (e.g. an sdist)."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", f"{dirname}/*.py"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout
        return [REPO / rel for rel in out.split()]
    except (OSError, subprocess.CalledProcessError):
        return sorted(p for p in (REPO / dirname).rglob("*.py")
                      if "__pycache__" not in p.parts)


def test_no_home_directory_literals_in_scripts_or_tests():
    hits = []
    for dirname in GUARDED_DIRS:
        for path in _tracked_py(dirname):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if HOME_LITERAL in line:
                    hits.append(f"{path.relative_to(REPO).as_posix()}:{lineno}")
    assert not hits, (
        "hardcoded home-directory path(s); use __file__, an env var with a "
        "default, or Path.home():\n  " + "\n  ".join(hits))


def test_ruff_check_tests_and_scripts_is_clean():
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not on PATH (CI pins ruff 0.15.20): the lint ratchet "
                    "did NOT run; install ruff to enforce it")
    proc = subprocess.run(
        [ruff, "check", "--output-format", "concise", "tests", "scripts"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "ruff check tests scripts found new errors (the baseline is zero):\n"
        + proc.stdout[-4000:])
