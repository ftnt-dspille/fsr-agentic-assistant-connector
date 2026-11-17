"""scripts/dev_env.sh: the ONE resolver for the dev/test python.

A missing or stale env must fail loud with the fix command, a worktree must
resolve to the shared env, and no script may pick a python any other way.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _git(cwd, *a):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True)


def _mini_repo(tmp_path: Path) -> Path:
    """A tiny repo carrying a copy of the resolver and its declared inputs."""
    main = tmp_path / "main"
    (main / "scripts").mkdir(parents=True)
    (main / "connector-x").mkdir()
    for f in ("dev_env.sh", "box.sh"):
        shutil.copy(SCRIPTS / f, main / "scripts" / f)
    (main / "pyproject.toml").write_text("[project]\nname='x'\n")
    (main / "connector-x" / "requirements.txt").write_text("fsr-playbooks==1.0\nrequests\n")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "i")
    return main


def _run(repo: Path, cmd: str, **env):
    e = {k: v for k, v in os.environ.items() if k != "FSR_DEV_VENV"}
    e.update(env)
    return subprocess.run(["bash", str(repo / "scripts" / "dev_env.sh"), cmd],
                          capture_output=True, text=True, env=e)


def _fake_env(venv: Path, stamp: str | None) -> None:
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "python").chmod(0o755)
    if stamp is not None:
        (venv / ".fsr-env-stamp").write_text(stamp + "\n")


def _build(repo: Path, venv: Path) -> None:
    """What `make setup` leaves behind: an interpreter plus the inputs hash."""
    _fake_env(venv, None)
    (venv / ".fsr-env-stamp").write_text(
        subprocess.run(["bash", "-c", f"source {repo}/scripts/dev_env.sh; de_inputs_hash"],
                       capture_output=True, text=True, check=True).stdout)


def test_missing_env_fails_loud_with_the_fix(tmp_path):
    repo = _mini_repo(tmp_path)
    r = _run(repo, "python", FSR_DEV_VENV=str(tmp_path / "nope"))
    assert r.returncode == 3 and r.stdout == ""
    assert "make setup" in r.stderr and str(tmp_path / "nope") in r.stderr
    assert _run(repo, "status", FSR_DEV_VENV=str(tmp_path / "nope")).stdout.strip() == "missing"


def test_unstamped_env_is_not_trusted(tmp_path):
    repo = _mini_repo(tmp_path)
    v = tmp_path / "v"
    _fake_env(v, None)
    r = _run(repo, "python", FSR_DEV_VENV=str(v))
    assert r.returncode == 3 and "FRESH=1 make setup" in r.stderr


def test_built_env_resolves(tmp_path):
    repo = _mini_repo(tmp_path)
    v = tmp_path / "v"
    _build(repo, v)
    r = _run(repo, "python", FSR_DEV_VENV=str(v))
    assert r.returncode == 0 and r.stdout.strip() == f"{v}/bin/python"


def test_changed_requirements_make_the_env_stale(tmp_path):
    repo = _mini_repo(tmp_path)
    v = tmp_path / "v"
    _build(repo, v)
    (repo / "connector-x" / "requirements.txt").write_text("fsr-playbooks==1.0\nrequests\nhttpx2\n")
    r = _run(repo, "python", FSR_DEV_VENV=str(v))
    assert r.returncode == 3 and "stale" in r.stderr and "make setup" in r.stderr


def test_framework_pin_bump_is_not_staleness(tmp_path):
    # the framework is installed editable from the sibling checkout, so its pin line is not an input
    repo = _mini_repo(tmp_path)
    v = tmp_path / "v"
    _build(repo, v)
    (repo / "connector-x" / "requirements.txt").write_text("fsr-playbooks==2.0\nrequests\n")
    assert _run(repo, "status", FSR_DEV_VENV=str(v)).stdout.strip() == "ok"


def test_worktree_resolves_to_the_shared_main_env(tmp_path):
    repo = _mini_repo(tmp_path)
    _build(repo, repo / ".venv")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "b", str(wt))
    r = _run(wt, "python")
    assert r.returncode == 0
    assert Path(r.stdout.strip()).resolve() == (repo / ".venv" / "bin" / "python").resolve()


def test_worktree_never_adopts_a_sibling_worktrees_env(tmp_path):
    # the old probe took any worktree's venv that "happened to import"; now only the declared one counts
    repo = _mini_repo(tmp_path)
    sib, wt = tmp_path / "sib", tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "a", str(sib))
    _git(repo, "worktree", "add", "-q", "-b", "b", str(wt))
    _build(sib, sib / ".venv")
    r = _run(wt, "python")
    assert r.returncode == 3 and "make setup" in r.stderr


def test_box_python_alias_is_the_same_resolver(tmp_path):
    repo = _mini_repo(tmp_path)
    r = subprocess.run(["bash", str(repo / "scripts" / "box.sh"), "--python"], capture_output=True,
                       text=True, env={**os.environ, "FSR_DEV_VENV": str(tmp_path / "nope"),
                                       "PYFSR_PY": ""})
    assert r.returncode != 0 and "make setup" in r.stderr


def test_no_script_picks_python_its_own_way():
    """Every interpreter lookup goes through dev_env.sh (no probing, no PATH/.venv guessing)."""
    banned = [r"box\.sh\"? --venv", r"bx_dev_venv", r"\$ROOT/\.venv/bin/python"]
    offenders = []
    files = [REPO / "Makefile", *SCRIPTS.glob("*.sh"), *(SCRIPTS / "hooks").glob("*")]
    for f in files:
        if f.name == "dev_env.sh" or not f.is_file():
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(re.search(b, line) for b in banned):
                offenders.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
