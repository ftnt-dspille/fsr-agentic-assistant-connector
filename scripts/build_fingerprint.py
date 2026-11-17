"""Content fingerprint of a connector build -- the evidence a version string cannot give.

A ship that lands on a version number the box already has does NOT replace the
package: FortiSOAR keeps the existing `connector-fsr-agentic-assistant_<ver>` dir
and every worker then honestly reports the target version -- from the PREVIOUS
package of that name. `verify_workers.py` passed while the shipped code was not
on the box (card #110). The only evidence it collected was the version string,
which is exactly the field a stale same-named package also satisfies.

So the rollout gate compares a hash of the CODE instead. This module owns the
canonical algorithm; `operations._build_id()` implements the runtime half over
the package dir a worker actually imported, and
`tests/test_build_fingerprint.py` pins the two against each other.

Algorithm (must stay in lockstep with `operations._build_id`):
  * only `*.py` files outside `__pycache__/` and `scratchpad/` -- everything else on-box is platform-owned churn
    (`__pycache__`, install markers, a rewritten storage.db)
  * paths are relative to the package root, so the version-stamped on-box dir
    name (`..._0_5_106/`) and the tarball's plain `connector-fsr-agentic-assistant/`
    normalize to the same key
  * sorted by relpath; each entry hashed as `relpath\\0len\\0bytes`, so neither
    a rename nor a content swap between two files can collide
  * sha256, first 12 hex chars (collision-irrelevant at this scale, and short
    enough to read in deploy output)

A version bump alone does not change the fingerprint -- info.json is not `.py`.
That is deliberate: this answers "is the shipped CODE on the box", and
identical code under a new number is not a rollout failure.
"""
from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path
from typing import Iterable, Tuple

DIGEST_LEN = 12


def _digest(entries: Iterable[Tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for relpath, data in sorted(entries, key=lambda e: e[0]):
        h.update(relpath.encode())
        h.update(b"\0")
        h.update(str(len(data)).encode())
        h.update(b"\0")
        h.update(data)
    return h.hexdigest()[:DIGEST_LEN]


def _keep(relpath: str) -> bool:
    if not relpath.endswith(".py"):
        return False
    parts = relpath.split("/")
    # scratchpad/ is local diagnosis scratch: dev-push and the build exclude it,
    # so it must not be part of "is the shipped code on the box".
    return "__pycache__" not in parts and "scratchpad" not in parts


def _strip_root(name: str) -> str:
    """Drop the leading package-dir component so a tarball's
    `connector-fsr-agentic-assistant/x.py` and an on-box
    `connector-fsr-agentic-assistant_0_5_106/x.py` produce the same key."""
    return name.split("/", 1)[1] if "/" in name else name


def from_dir(root: Path) -> str:
    """Fingerprint an unpacked package directory (the on-box shape)."""
    root = Path(root)
    entries = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if _keep(rel):
            entries.append((rel, p.read_bytes()))
    return _digest(entries)


def from_tarball(path: Path) -> str:
    """Fingerprint the built tarball -- the exact bytes the deploy ships."""
    entries = []
    with tarfile.open(path, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            rel = _strip_root(m.name)
            if not _keep(rel):
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            entries.append((rel, f.read()))
    return _digest(entries)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", help="path to a .tgz build or an unpacked package dir")
    args = ap.parse_args(argv)
    p = Path(args.target)
    print(from_dir(p) if p.is_dir() else from_tarball(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
