"""The rollout gate's content fingerprint must be able to fail (card #110).

Two implementations of one algorithm: `scripts/build_fingerprint.py` hashes
what the deploy is about to ship (a tarball), `operations._build_id` hashes
what a worker actually imported (its package dir). If they ever drift, the gate
either fails every green ship or -- far worse -- silently agrees on nothing and
goes back to being a check whose pass and fail states are indistinguishable.

So these pin: the two agree on the same tree, a one-byte code change moves the
hash, and the fields that legitimately differ between the tarball and the
on-box dir (the version-stamped root, `__pycache__`, a rewritten `info.json`)
do not.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "connector-fsr-agentic-assistant"


def _fp():
    spec = importlib.util.spec_from_file_location(
        "build_fingerprint", ROOT / "scripts" / "build_fingerprint.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path) -> Path:
    """A miniature package: two .py files, one non-.py, one __pycache__."""
    root = tmp_path / "connector-fsr-agentic-assistant"
    (root / "sub").mkdir(parents=True)
    (root / "__pycache__").mkdir()
    (root / "operations.py").write_text("def health_check():\n    return 1\n")
    (root / "sub" / "helper.py").write_text("X = 2\n")
    (root / "info.json").write_text('{"version": "0.5.106"}')
    (root / "__pycache__" / "operations.cpython-311.pyc").write_bytes(b"\x00stale")
    return root


def _tar(root: Path, dest: Path, *, arcname: str | None = None) -> Path:
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(root, arcname=arcname or root.name)
    return dest


def _runtime_build_id(root: Path) -> str:
    """Run `operations._build_id`'s algorithm against `root`.

    operations.py cannot be imported outside FortiSOAR, so exercise the real
    source by copying it in as the module under test would see it -- the copy
    below extracts the function body's contract, and
    `test_runtime_impl_matches_source` guards the copy against drift.
    """
    import hashlib
    h = hashlib.sha256()
    entries = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if "__pycache__" in rel.split("/") or "scratchpad" in rel.split("/"):
            continue
        entries.append((rel, p.read_bytes()))
    for rel, data in sorted(entries, key=lambda e: e[0]):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(str(len(data)).encode())
        h.update(b"\0")
        h.update(data)
    return h.hexdigest()[:12]


def test_tarball_and_dir_agree(tmp_path):
    fp = _fp()
    root = _tree(tmp_path)
    tgz = _tar(root, tmp_path / "b.tgz")
    assert fp.from_dir(root) == fp.from_tarball(tgz)


def test_runtime_and_build_side_agree(tmp_path):
    """The two implementations must produce the same digest, or the gate is
    a no-op dressed as a check."""
    fp = _fp()
    root = _tree(tmp_path)
    assert _runtime_build_id(root) == fp.from_dir(root)


def test_version_stamped_root_does_not_change_the_hash(tmp_path):
    """On-box the package dir is `..._0_5_106`; in the tarball it is plain.
    If the root name leaked into the hash, every ship would fail."""
    fp = _fp()
    root = _tree(tmp_path)
    tgz = _tar(root, tmp_path / "b.tgz",
               arcname="connector-fsr-agentic-assistant_0_5_106")
    assert fp.from_tarball(tgz) == fp.from_dir(root)


def test_a_one_byte_code_change_moves_the_hash(tmp_path):
    """The mutation proof. Without this the gate cannot fail."""
    fp = _fp()
    root = _tree(tmp_path)
    before = fp.from_dir(root)
    (root / "operations.py").write_text("def health_check():\n    return 2\n")
    assert fp.from_dir(root) != before


def test_a_moved_file_moves_the_hash(tmp_path):
    """Content alone is not identity: relpaths are hashed too, so shuffling
    the same bytes between files is a different build."""
    fp = _fp()
    root = _tree(tmp_path)
    before = fp.from_dir(root)
    (root / "sub" / "helper.py").rename(root / "sub" / "renamed.py")
    assert fp.from_dir(root) != before


def test_platform_owned_churn_does_not_move_the_hash(tmp_path):
    """`__pycache__`, a rewritten info.json and a touched storage.db are all
    things the platform changes on-box. None is a code change."""
    fp = _fp()
    root = _tree(tmp_path)
    before = fp.from_dir(root)
    (root / "info.json").write_text('{"version": "0.5.107"}')
    (root / "storage.db").write_bytes(b"rewritten")
    (root / "__pycache__" / "operations.cpython-311.pyc").write_bytes(b"\x00fresh")
    assert fp.from_dir(root) == before


def test_runtime_impl_matches_the_script_on_the_real_tree():
    """The connector's own `_build_id` (what a worker reports) must agree with
    the build script's algorithm on the real package. Drift in either one is a
    different digest, so the deploy gate would reject every worker."""
    import ops_health
    assert ops_health._build_id() == _fp().from_dir(PKG)


def test_real_package_fingerprint_is_computable():
    """Not a shape check: the algorithm must actually run over the real tree
    and produce a stable digest twice."""
    fp = _fp()
    a = fp.from_dir(PKG)
    b = fp.from_dir(PKG)
    assert a == b and len(a) == 12


def test_cli_prints_a_digest(tmp_path):
    root = _tree(tmp_path)
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_fingerprint.py"), str(root)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert out == _fp().from_dir(root)
    shutil.rmtree(root)


def test_deploy_passes_the_fingerprint_to_the_gate():
    """A fingerprint nothing calls is not a gate."""
    body = (ROOT / "scripts" / "deploy.sh").read_text()
    line = [ln for ln in body.splitlines() if "verify_workers.py" in ln
            and not ln.strip().startswith("#")]
    assert line, "deploy.sh no longer invokes verify_workers.py"
    assert any("--tarball" in ln or "--build-id" in ln
               for ln in body.splitlines()
               if "verify_workers.py" in ln or "--tarball" in ln), (
        "deploy.sh calls verify_workers.py without a build fingerprint -- the "
        "gate is back to comparing only the version string (card #110).")


def test_health_check_reports_build_id():
    """The health check a deploy gate polls must carry the content digest, or
    verify_workers compares against None on every worker."""
    import ops_health
    ident = ops_health._worker_identity()
    assert ident["build_id"] == ops_health._build_id()
    assert len(ident["build_id"]) == 12


def test_scratchpad_is_not_part_of_the_fingerprint(tmp_path):
    """dev-push never syncs scratchpad/, so counting it made every push against a
    tree with a scratch .py unverifiable (local id != the id the box computes)."""
    bf = _fp()
    a = tmp_path / "pkg"
    (a / "scratchpad").mkdir(parents=True)
    (a / "x.py").write_text("x = 1\n")
    before = bf.from_dir(a)
    (a / "scratchpad" / "probe.py").write_text("print('local only')\n")
    assert bf.from_dir(a) == before
    assert _runtime_build_id(a) == bf.from_dir(a)  # the worker's half agrees
