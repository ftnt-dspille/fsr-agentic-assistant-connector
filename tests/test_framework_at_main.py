"""Gates test the framework that ships (its remote main), not the shared checkout.

Live: a connector push was refused by 2 failures that came from another
session's framework branch checked out in the shared framework checkout; the
same tests passed on framework main, which is what the connector ships with.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "framework_at_main.sh"
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "FSR"))},
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
ENV.pop("PYTHONPATH", None)


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


def _world(tmp: Path):
    conn = tmp / "connector"
    (conn / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "framework_dir.sh", conn / "scripts" / "framework_dir.sh")
    shutil.copy(LIB, conn / "scripts" / "framework_at_main.sh")
    fw = tmp / "framework"                       # <root>/../framework: the layout the resolver reads
    (fw / "fsr_playbooks").mkdir(parents=True)
    (fw / "fsr_playbooks" / "__init__.py").write_text("WHICH = 'old main'\n")
    bare = tmp / "fw.git"
    _git(tmp, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(fw, "init", "-q", "-b", "main")
    _git(fw, "add", "-A"); _git(fw, "commit", "-q", "-m", "one")
    _git(fw, "remote", "add", "github", str(bare)); _git(fw, "push", "-q", "-u", "github", "main")
    (fw / "fsr_playbooks" / "__init__.py").write_text("WHICH = 'promoted main'\n")
    _git(fw, "commit", "-qam", "two"); _git(fw, "push", "-q", "github", "main")
    remote_main = _git(fw, "rev-parse", "HEAD")
    _git(fw, "reset", "-q", "--hard", "HEAD~1")
    _git(fw, "checkout", "-q", "-b", "someone/else")
    (fw / "fsr_playbooks" / "__init__.py").write_text("WHICH = 'someone else WIP'\n")
    return conn, fw, remote_main


def _run(conn, body, tmp):
    return subprocess.run(["bash", "-c", f'source "{conn}/scripts/framework_at_main.sh"; {body}'],
                          cwd=conn, env={**ENV, "TMPDIR": str(tmp)}, capture_output=True, text=True)


def test_the_tree_is_the_remote_main_and_imports_from_there(tmp_path):
    conn, fw, remote_main = _world(tmp_path)
    r = _run(conn, f'framework_at_main "{conn}" || exit 3; '
                   'git -C "$FSRPB_FRAMEWORK_DIR" rev-parse HEAD; '
                   'python3 -c "import fsr_playbooks as f; print(f.WHICH)"', tmp_path)
    assert r.returncode == 0, r.stderr
    head, which = r.stdout.strip().splitlines()[-2:]
    assert head == remote_main and which == "promoted main"
    # The shared checkout keeps its branch and its WIP.
    assert _git(fw, "branch", "--show-current") == "someone/else"
    assert "someone else WIP" in (fw / "fsr_playbooks" / "__init__.py").read_text()


def test_the_tree_is_removed_on_exit(tmp_path):
    conn, fw, _ = _world(tmp_path)
    r = _run(conn, f'framework_at_main "{conn}" && echo "DIR=$FSRPB_FRAMEWORK_DIR"', tmp_path)
    d = r.stdout.split("DIR=")[1].strip()
    assert not Path(d).exists()
    assert len(_git(fw, "worktree", "list").splitlines()) == 1


def test_no_readable_remote_changes_nothing(tmp_path):
    conn, fw, _ = _world(tmp_path)
    _git(fw, "remote", "remove", "github")
    r = _run(conn, f'framework_at_main "{conn}"; echo "rc=$? dir=${{FSRPB_FRAMEWORK_DIR:-unset}}"', tmp_path)
    assert "rc=1 dir=unset" in r.stdout


def test_pre_push_uses_it_unless_a_framework_is_named():
    src = (ROOT / "scripts" / "hooks" / "pre-push").read_text()
    gate = src.index('framework_at_main "$ROOT"')
    assert src.index('-z "${FSR_SRC:-}"') < gate < src.index('CACHE_SH="$ROOT/scripts/hooks/test_pass_cache.sh"')
