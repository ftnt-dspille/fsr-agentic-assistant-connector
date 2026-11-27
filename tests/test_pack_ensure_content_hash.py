"""scripts/build_agentic_assistant_pack.py `ensure()`: a cached artifact is reused only while its
SOURCE content is unchanged. The artifact name carries only the version, so an edit without a
version bump used to reuse the stale tarball and ship old content.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_agentic_assistant_pack.py"


@pytest.fixture(scope="module")
def pack():
    spec = importlib.util.spec_from_file_location("build_agentic_assistant_pack", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A "build": copy the source file to the artifact and log one line per run.
_BUILD = ("import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2]); "
          "open(sys.argv[3], 'a').write('built\\n')")


def _setup(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "widget.js").write_text("v1 content\n")
    art = tmp_path / "out" / "widget-1.0.0.tgz"
    art.parent.mkdir()
    log = tmp_path / "builds.log"
    cmd = [sys.executable, "-c", _BUILD, str(src_dir / "widget.js"), str(art), str(log)]
    return src_dir, art, log, cmd


def _builds(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def test_same_version_with_changed_source_rebuilds(pack, tmp_path):
    src_dir, art, log, cmd = _setup(tmp_path)
    pack.ensure(art, cmd, tmp_path, "hint", sources=[src_dir])
    assert _builds(log) == 1 and art.read_text() == "v1 content\n"

    # Edit the source WITHOUT a version bump: the artifact name is unchanged.
    (src_dir / "widget.js").write_text("v2 content, edited in place\n")
    pack.ensure(art, cmd, tmp_path, "hint", sources=[src_dir])

    # Red without the fix: the old ensure() reused the v1 tarball (no second build).
    assert _builds(log) == 2
    assert art.read_text() == "v2 content, edited in place\n"


def test_same_version_unchanged_source_reuses_the_artifact(pack, tmp_path):
    src_dir, art, log, cmd = _setup(tmp_path)
    pack.ensure(art, cmd, tmp_path, "hint", sources=[src_dir])
    pack.ensure(art, cmd, tmp_path, "hint", sources=[src_dir])
    assert _builds(log) == 1


def test_unstamped_artifact_is_rebuilt_not_trusted(pack, tmp_path):
    src_dir, art, log, cmd = _setup(tmp_path)
    art.write_text("left over from an unknown build\n")
    pack.ensure(art, cmd, tmp_path, "hint", sources=[src_dir])
    assert _builds(log) == 1
    assert art.read_text() == "v1 content\n"


def test_unhashable_source_rebuilds_every_time(pack, tmp_path):
    src_dir, art, log, cmd = _setup(tmp_path)
    missing = [tmp_path / "gone"]
    pack.ensure(art, cmd, tmp_path, "hint", sources=missing)
    pack.ensure(art, cmd, tmp_path, "hint", sources=missing)
    assert _builds(log) == 2


def test_source_hash_ignores_caches_and_build_output(pack, tmp_path):
    src_dir, _, _, _ = _setup(tmp_path)
    before = pack.source_hash(src_dir)
    (src_dir / "__pycache__").mkdir()
    (src_dir / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (src_dir / "node_modules").mkdir()
    (src_dir / "node_modules" / "dep.js").write_text("x")
    assert pack.source_hash(src_dir) == before
