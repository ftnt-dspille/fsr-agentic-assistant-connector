"""Every harness script starts when run the way people run it.

Unit tests import scripts as `scripts.<name>` with the repo on sys.path, so a
script that imports a sibling before putting the repo root on its own path
passes every test and dies on launch (`live_writes`: "No module named
'scripts'"). This runs each one as a program, from outside the repo.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Scripts that parse arguments before needing a box or credentials.
SCRIPTS = [
    "analyst_sim", "build_run_proof", "chat_sweep", "conv_scenarios", "drive",
    "eval_child_playbook_apply", "eval_s1_create", "eval_s1_refine_saved",
    "eval_s2_modify", "eval_s3_connector", "eval_s5_diagnose",
    "eval_s6_apply_fix", "live_integration", "live_rows", "live_writes",
    "local_conversation", "local_turn", "prompt_loop", "t1_scenarios",
]


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_starts(name, tmp_path):
    env = {**os.environ, "FSRPB_DEV": "1"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / f"{name}.py"), "--help"],
                       cwd=tmp_path, env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, f"{name} failed to start:\n{r.stderr[-1500:]}"
