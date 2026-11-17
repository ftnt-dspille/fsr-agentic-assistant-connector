"""The suite runs inside git hooks (pre-push), which export GIT_DIR and friends.

A test that shells out to git in a tmp dir must never reach the repo the hook
belongs to. Run a tmp-repo commit under a decoy GIT_DIR and assert the decoy is
untouched: red if conftest stops scrubbing the variables.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def test_hook_git_vars_cannot_redirect_a_tmp_repo_commit(tmp_path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    env = {**os.environ, **_GIT_ID}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=decoy, env=env, check=True)
    (decoy / "keep.txt").write_text("keep\n")
    subprocess.run(["git", "add", "-A"], cwd=decoy, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "decoy"], cwd=decoy, env=env, check=True)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=decoy, env=env,
                            capture_output=True, text=True, check=True).stdout

    # A child pytest that loads the repo conftest, with the hook's variables
    # pointing at the decoy, commits in its own tmp repo.
    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent("""
        import os, subprocess
        def test_commit(tmp_path):
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
            subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True)
            (tmp_path / "f").write_text("x")
            subprocess.run(["git", "add", "-A"], cwd=tmp_path, env=env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "probe"], cwd=tmp_path, env=env, check=True)
    """))
    child_env = {**os.environ, **_GIT_ID, "PYTHONPATH": str(ROOT), "GIT_DIR": str(decoy / ".git"),
                 "GIT_WORK_TREE": str(decoy), "GIT_INDEX_FILE": str(decoy / ".git" / "index")}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
                        "-p", "no:xdist", "--rootdir", str(ROOT), "-c", str(ROOT / "pyproject.toml"),
                        "-p", "conftest", str(probe)],
                       cwd=ROOT, env=child_env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

    env.pop("GIT_DIR", None)
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=decoy, env=env,
                           capture_output=True, text=True, check=True).stdout
    assert after == before, "a tmp-repo commit landed in the hook's repo"
    assert (decoy / "keep.txt").exists()
