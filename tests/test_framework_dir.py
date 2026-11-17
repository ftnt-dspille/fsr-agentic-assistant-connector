"""One resolver for the framework checkout: scripts/framework_dir.sh (shell) and
scripts/framework_tooling.framework_root (python) agree on precedence."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SH = ROOT / "scripts" / "framework_dir.sh"


def _fw(path: Path) -> Path:
    (path / "fsr_playbooks").mkdir(parents=True)
    return path


def _run(repo: Path, env_extra=None, *args):
    env = {k: v for k, v in os.environ.items()
           if k not in ("FSR_SRC", "FSRPB_FRAMEWORK_DIR") and not k.startswith("GIT_")}
    env.update(env_extra or {})
    return subprocess.run(["bash", str(SH), *args, str(repo)], capture_output=True,
                          text=True, env=env, timeout=60)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "conn"
    (r / "scripts").mkdir(parents=True)
    # box.sh --main-checkout needs a git repo; reuse the real script.
    (r / "scripts" / "box.sh").write_text((ROOT / "scripts" / "box.sh").read_text())
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def test_explicit_fsr_src_wins(repo, tmp_path):
    a, b = _fw(tmp_path / "a"), _fw(tmp_path / "b")
    (repo / ".fsr_src").write_text(str(b))
    r = _run(repo, {"FSR_SRC": str(a), "FSRPB_FRAMEWORK_DIR": str(b)})
    assert Path(r.stdout.strip()).resolve() == a.resolve()


def test_legacy_env_then_pin_file_then_monorepo(repo, tmp_path):
    legacy, pin = _fw(tmp_path / "legacy"), _fw(tmp_path / "pin")
    mono = _fw(tmp_path / "framework")  # <root>/../framework
    (repo / ".fsr_src").write_text(str(pin) + "\n")
    assert Path(_run(repo, {"FSRPB_FRAMEWORK_DIR": str(legacy)}).stdout.strip()).resolve() == legacy.resolve()
    assert Path(_run(repo).stdout.strip()).resolve() == pin.resolve()
    (repo / ".fsr_src").unlink()
    assert Path(_run(repo).stdout.strip()).resolve() == mono.resolve()


def test_none_is_ignored_and_a_bad_explicit_value_is_an_error(repo, tmp_path):
    mono = _fw(tmp_path / "framework")
    assert Path(_run(repo, {"FSRPB_FRAMEWORK_DIR": "none"}).stdout.strip()).resolve() == mono.resolve()
    r = _run(repo, {"FSR_SRC": str(tmp_path / "missing")})
    assert r.returncode == 1 and "FSR_SRC" in r.stderr


def test_nothing_found_is_loud_unless_soft(repo):
    r = _run(repo)
    assert r.returncode == 1 and "no framework checkout found" in r.stderr
    assert _run(repo, None, "--soft").returncode == 0


def test_python_resolver_follows_the_same_order(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import framework_tooling
    monkeypatch.delenv("FSRPB_FRAMEWORK_DIR", raising=False)
    monkeypatch.setenv("FSR_SRC", str(tmp_path / "x"))
    assert framework_tooling.framework_root() == tmp_path / "x"
    monkeypatch.delenv("FSR_SRC")
    monkeypatch.setenv("FSRPB_FRAMEWORK_DIR", str(tmp_path / "y"))
    assert framework_tooling.framework_root() == tmp_path / "y"
    monkeypatch.setenv("FSRPB_FRAMEWORK_DIR", "none")
    # falls through to the pin file / monorepo layout / the imported clone
    assert framework_tooling.framework_root() != tmp_path / "y"


def test_no_script_rederives_the_framework_path():
    """The sibling-path derivation lives in framework_dir.sh only."""
    for name in ("deploy.sh", "deploy_chain.sh", "ship_dev.sh", "loop.sh"):
        src = (ROOT / "scripts" / name).read_text()
        assert "../../fsr-playbook-framework" not in src, name
        assert "framework_dir.sh" in src, name


def test_stable_mode_ignores_session_overrides(repo, tmp_path):
    mono = _fw(tmp_path / "framework")
    other = _fw(tmp_path / "other")
    (repo / ".fsr_src").write_text(str(other))
    r = _run(repo, {"FSR_SRC": str(other)}, "--stable")
    assert Path(r.stdout.strip()).resolve() == mono.resolve()
