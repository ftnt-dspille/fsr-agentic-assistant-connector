"""R2: under Claude Code the box deploy/install scripts run only from a make
target. GNU make exports MAKELEVEL to recipes; a direct call has none."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SHELL = ["deploy.sh", "deploy_chain.sh", "ship_dev.sh", "dev_push.sh", "dev_push_deps.sh"]


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "MAKELEVEL", "VIA_MAKE", "MAKEFLAGS")}
    env.update(extra)
    return env


def _run(name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPTS / name), "--help"] if name.endswith(".py") else ["bash", str(SCRIPTS / name), "--help"]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("name", SHELL + ["install_to_fsr.py"])
def test_refused_when_run_directly_under_claude_code(name: str) -> None:
    r = _run(name, _env(CLAUDECODE="1"))
    assert r.returncode != 0
    assert "is run through make" in r.stderr


@pytest.mark.parametrize("name", SHELL + ["install_to_fsr.py"])
@pytest.mark.parametrize("extra", [{}, {"CLAUDECODE": "1", "MAKELEVEL": "1"}, {"CLAUDECODE": "1", "VIA_MAKE": "1"}])
def test_not_refused_for_a_human_or_from_make(name: str, extra: dict[str, str]) -> None:
    r = _run(name, _env(**extra))
    assert "is run through make" not in r.stderr
