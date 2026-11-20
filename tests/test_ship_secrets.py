"""Ship secrets resolve before the bump, from a worktree too.

A ship from a git worktree bumped the version, installed it on the box, and only
then aborted: the Anthropic key and the Frank env were looked up beside the
worktree, where the gitignored files the main checkout carries do not exist.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "ship_secrets.sh"
GIT_ENV = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
           "GIT_CONFIG_VALUE_0": "/dev/null"}


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=GIT_ENV)


def _resolve(root, fw, box_env="", *, keychain=None, tmp):
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    sec = bindir / "security"
    sec.write_text("#!/bin/sh\n" + (f"echo {keychain}\n" if keychain else "exit 44\n"))
    sec.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "FRANK_ENV")}
    env["PATH"] = f"{bindir}:{env['PATH']}"
    out = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; ship_secrets_resolve "$1" "$2" "$3"; '
                       'echo "KEY=${ANTHROPIC_API_KEY:-}"; echo "FRANK=${FRANK_ENV:-}"',
         "_", str(root), str(fw), str(box_env)],
        capture_output=True, text=True, env=env, check=True).stdout
    vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and line[:4] in ("KEY=", "FRAN"))
    return vals.get("KEY", ""), vals.get("FRANK", ""), out


def _repo_with_worktree(tmp):
    main = tmp / "code" / "fw"
    main.mkdir(parents=True)
    _git(main, "init", "-q", "-b", "main")
    _git(main, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
    wt = tmp / "scratch" / "deep" / "wt_fw"  # its ../.. is not the main checkout's
    _git(main, "worktree", "add", "-q", str(wt), "-b", "feat")
    return main, wt


def test_a_worktree_finds_the_key_in_its_main_checkout(tmp_path):
    main, wt = _repo_with_worktree(tmp_path)
    (main / ".env").write_text('ANTHROPIC_API_KEY="from-main"\n')
    key, _, out = _resolve(tmp_path, wt, tmp=tmp_path)
    assert key == "from-main"
    assert "from-main" not in out.split("KEY=")[0], "the key must never be printed"


def test_the_worktree_env_wins_over_the_main_checkout(tmp_path):
    main, wt = _repo_with_worktree(tmp_path)
    (main / ".env").write_text("ANTHROPIC_API_KEY=from-main\n")
    (wt / ".env").write_text("ANTHROPIC_API_KEY=from-wt\n")
    assert _resolve(tmp_path, wt, tmp=tmp_path)[0] == "from-wt"


def test_the_keychain_is_the_last_resort(tmp_path):
    _, wt = _repo_with_worktree(tmp_path)
    assert _resolve(tmp_path, wt, keychain="from-keychain", tmp=tmp_path)[0] == "from-keychain"
    box = tmp_path / ".env.box"
    box.write_text("export ANTHROPIC_API_KEY=from-box\n")
    assert _resolve(tmp_path, wt, box, keychain="from-keychain", tmp=tmp_path)[0] == "from-box"


def test_no_key_anywhere_resolves_to_nothing(tmp_path):
    _, wt = _repo_with_worktree(tmp_path)
    assert _resolve(tmp_path, wt, tmp=tmp_path)[0] == ""


def test_a_worktree_finds_frank_beside_its_main_checkout(tmp_path):
    main, wt = _repo_with_worktree(tmp_path)
    frank = tmp_path / "Miscellaneous" / "frank"
    frank.mkdir(parents=True)
    (frank / ".env").write_text("OPENAI_API_KEY=x\n")
    # <root>/../../Miscellaneous: only the main checkout (tmp/code/fw) reaches tmp/Miscellaneous.
    assert _resolve(wt, wt, tmp=tmp_path)[1] == str(frank / ".env")


def test_deploy_resolves_and_refuses_before_the_bump():
    src = (ROOT / "scripts" / "deploy.sh").read_text()
    secrets = src.index('ship_secrets_resolve "$ROOT"')
    refuse = src.index("ABORT before the bump: no ANTHROPIC_API_KEY")
    bump = src.index('echo ">> bump version $CUR')
    assert secrets < refuse < bump
