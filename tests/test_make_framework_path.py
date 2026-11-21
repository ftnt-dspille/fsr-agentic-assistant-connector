"""Every make target imports the framework the one resolver names.

The dev venv is shared per machine and carries an editable install of the main
checkout's framework. A worktree or agent tree whose `.fsr_src` names its own
framework therefore graded the SHARED one: an agent tree's known-answer sweep
ran a deleted guard class. The Makefile puts the resolved framework first on
PYTHONPATH; `make framework-path` shows what a target actually imports.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _dev_env_ok() -> bool:
    out = subprocess.run(["bash", "scripts/dev_env.sh", "status"], cwd=ROOT,
                         capture_output=True, text=True)
    return out.stdout.strip() == "ok"


def _resolved() -> str:
    out = subprocess.run(["bash", "scripts/framework_dir.sh", "--soft", str(ROOT)],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.skipif(not _dev_env_ok(), reason="no dev env (make setup); CI installs deps without one")
def test_make_imports_the_resolved_framework():
    want = _resolved()
    assert want, "framework_dir.sh resolved nothing"
    out = subprocess.run(["make", "-s", "framework-path"], cwd=ROOT,
                         capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert out.returncode == 0, out.stderr
    got = out.stdout.strip().splitlines()[-1]
    assert os.path.realpath(got) == os.path.realpath(want), (got, want)
