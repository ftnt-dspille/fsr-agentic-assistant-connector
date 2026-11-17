#!/usr/bin/env python3
"""Release quality gate: the box-free quality tiers, stamped per commit.

A connector release (`make deploy FW=...`, any FSR_RELEASE_INSTALL install) must
not go out on a green pytest suite alone: the suite grades code shape, not what
the agent does. These three tiers grade behaviour with no box and no paid model:

  tool-sweep     `make tool-sweep STRICT=1`     every tool, contract + cases
  t1-scenarios   `make t1-scenarios LLM=fake`   behavioural invariants, fake model
  known-answer   `make known-answer LLM=frank`  verdicts vs recorded evidence

    quality_gate.py run     run the tiers on a CLEAN tree; on all-pass write the
                            stamp for HEAD (a failing tier writes nothing)
    quality_gate.py check   exit 0 only when a stamp for the current HEAD exists
                            and the tracked tree is clean; non-zero otherwise
    quality_gate.py show    print the stamp for HEAD, if any

The stamp is keyed by the commit sha, so any new commit invalidates it, and it is
refused for a dirty tree (the tiers would have graded code that is not the
commit). It lives under the git common dir, so it is shared by worktrees and
never tracked. Bypass only through the logged skip path (release_skips.sh,
SKIP_QUALITY_GATE + SKIP_QUALITY_GATE_REASON).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (name, make target + args). Order is cheapest first so a red tier fails fast.
TIERS: list[tuple[str, list[str]]] = [
    ("tool-sweep", ["tool-sweep", "STRICT=1"]),
    ("t1-scenarios", ["t1-scenarios", "LLM=fake"]),
    ("known-answer", ["known-answer", "LLM=frank"]),
]


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd or ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def head_sha(root: Path = ROOT) -> str:
    return _git("rev-parse", "HEAD", cwd=root)


def tree_dirty(root: Path = ROOT) -> bool:
    """Tracked changes only: untracked scratch files do not change graded code."""
    return bool(_git("status", "--porcelain", "-uno", cwd=root))


def stamp_dir(root: Path = ROOT) -> Path:
    override = os.environ.get("QUALITY_STAMP_DIR")
    if override:
        return Path(override)
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root)
    return Path(common) / "quality-stamps"


def stamp_path(sha: str, root: Path = ROOT) -> Path:
    return stamp_dir(root) / f"{sha}.json"


def check(root: Path = ROOT) -> tuple[bool, str]:
    """(ok, reason). ok only for a stamp written by an all-pass run of THIS commit."""
    try:
        sha = head_sha(root)
        dirty = tree_dirty(root)
    except RuntimeError as e:
        return False, f"cannot read git state: {e}"
    if dirty:
        return False, ("the tracked tree has uncommitted changes, so no stamp can vouch "
                       "for it. Commit, then run: make quality-gate")
    p = stamp_path(sha, root)
    if not p.is_file():
        return False, (f"no quality stamp for commit {sha[:12]}. Run: make quality-gate "
                       "(tool-sweep strict, t1-scenarios on the fake model, known-answer)")
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return False, f"quality stamp {p} is unreadable; re-run: make quality-gate"
    if data.get("sha") != sha or not data.get("passed"):
        return False, f"quality stamp {p} does not record a pass for {sha[:12]}"
    missing = [n for n, _ in TIERS if n not in (data.get("tiers") or {})]
    if missing:
        return False, f"quality stamp lacks tier(s) {', '.join(missing)}; re-run: make quality-gate"
    return True, f"quality stamp ok for {sha[:12]} ({data.get('finished_at')})"


def run(root: Path = ROOT) -> int:
    if tree_dirty(root):
        print("quality-gate: refusing to run on a dirty tracked tree; the stamp is keyed by "
              "commit and would vouch for code that is not the commit. Commit first.",
              file=sys.stderr)
        return 2
    sha = head_sha(root)
    tiers: dict[str, dict] = {}
    for name, margs in TIERS:
        print(f"\n>> quality tier: {name} (make {' '.join(margs)})", flush=True)
        t0 = time.monotonic()
        rc = subprocess.run(["make", "-C", str(root), *margs]).returncode
        secs = round(time.monotonic() - t0, 1)
        print(f">> {name}: {'PASS' if rc == 0 else 'FAIL'} in {secs}s", flush=True)
        if rc != 0:
            print(f"quality-gate: {name} failed; no stamp written.", file=sys.stderr)
            return 1
        tiers[name] = {"seconds": secs, "make": " ".join(margs)}
    if head_sha(root) != sha or tree_dirty(root):
        print("quality-gate: the tree moved while the tiers ran; no stamp written.", file=sys.stderr)
        return 1
    d = stamp_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    stamp_path(sha, root).write_text(json.dumps({
        "sha": sha, "passed": True, "tiers": tiers,
        "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2) + "\n")
    print(f"\n>> quality stamp written for {sha[:12]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "check", "show"])
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run()
    if a.cmd == "check":
        ok, why = check()
        print(("quality-gate: " if ok else "quality-gate REFUSED: ") + why, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    p = stamp_path(head_sha())
    print(p.read_text() if p.is_file() else "no stamp for HEAD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
