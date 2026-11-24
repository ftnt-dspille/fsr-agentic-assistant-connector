"""The connector's side of the two front doors.

`make check` and `make ship` forward to the root repo's door (scripts/door.sh in
fsr_all_widgets) and say which repo they are in; the old connector install is
`make ship-install` (the release path), so a bare `make ship` can never bump and
install onto a box. `scripts/changed_tests.sh` is the one test selection that both
the pre-push hook and `make check` read.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# MAKEFLAGS too: promote runs this suite (pre-push) under `make promote REPO=<tree>`,
# and make hands that command-line REPO to every child make, overriding ours.
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "BOX", "FSR", "DRY", "MAKE"))},
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _make(*args, **env):
    return subprocess.run(["make", "-n", "--no-print-directory", *args], cwd=ROOT, capture_output=True, text=True,
                          env={**ENV, "FSR_ALL_WIDGETS": "/root-repo", **env}).stdout


def test_check_forwards_to_the_root_door_naming_this_repo():
    out = _make("check")
    assert f'-C "/root-repo" check DOOR_REPO="{ROOT}"' in out.replace("  ", " ")


def test_ship_forwards_with_the_box_and_dry_run_and_needs_no_box_to_forward():
    out = _make("ship", "BOX=92", "DRY_RUN=1")
    assert f'ship DOOR_REPO="{ROOT}"' in out and "DRY_RUN=1" in out
    assert _make("ship") != ""      # no BOX: the door decides (tooling-only needs none); make does not refuse it


def test_the_old_connector_install_is_ship_install_and_ship_does_not_install():
    install = _make("ship-install", "BOX=92")
    assert "deploy.sh" in install and "--bump" in install
    assert "deploy.sh" not in _make("ship", "BOX=92")


def test_the_release_chain_calls_the_renamed_install():
    chain = (ROOT / "scripts" / "deploy_chain.sh").read_text()
    assert 'ship-install BUMP="$BUMP"' in chain and ' ship BUMP=' not in chain


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "conn"
    (r / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "changed_tests.sh", r / "scripts" / "changed_tests.sh")
    for rel, body in {"connector-fsr-agentic-assistant/mod_a.py": "A = 1\n", "scripts/tool.sh": "echo\n",
                      "tests/test_a.py": "from mod_a import A\n", "tests/test_tool.py": "S = 'scripts/tool.sh'\n",
                      "tests/test_other.py": "X = 1\n"}.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(body)
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], env=ENV, check=True)
    subprocess.run(["git", "-C", str(r), "add", "-A"], env=ENV, check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "base"], env=ENV, check=True)
    return r


def _select(r, files):
    p = subprocess.run(["bash", "scripts/changed_tests.sh", "--files"], cwd=r, input="\n".join(files) + "\n",
                       capture_output=True, text=True, env=ENV)
    assert p.returncode == 0, p.stderr
    return p.stdout.split()


def test_selection_from_stdin_maps_modules_scripts_and_test_files(repo):
    assert _select(repo, ["connector-fsr-agentic-assistant/mod_a.py"]) == ["tests/test_a.py"]
    assert _select(repo, ["scripts/tool.sh"]) == ["tests/test_tool.py"]
    assert _select(repo, ["tests/test_other.py", "connector-fsr-agentic-assistant/mod_a.py"]) == ["tests/test_a.py", "tests/test_other.py"]


def test_selection_prints_nothing_when_nothing_maps(repo):
    assert _select(repo, ["README.md"]) == []
    assert subprocess.run(["bash", "scripts/changed_tests.sh", "--files"], cwd=repo, input="README.md\n",
                          capture_output=True, text=True, env=ENV).stdout == ""   # no blank line: the caller counts lines
