#!/usr/bin/env python3
"""Mutation check: is each named fix guarded by the test that names it?

A fix nobody's test would notice being undone is a coverage hole. For each
entry in the manifest (default ``tests/mutants/manifest.yaml``) this runner:

  1. copies the repo (or the ``repo:`` checkout) into a temp directory;
  2. runs the named tests on the unmodified copy and requires them green;
  3. applies the revert (an exact, unique search/replace) to the COPY only;
  4. runs the named tests again and expects the named test to go red (or to
     skip, for entries that declare ``expect: skipped``);
  5. restores the copy and re-runs the named tests, requiring green.

A mutant that stays green is a SURVIVOR: a coverage hole, reported and fatal.
A HEAD that is red or skipped is fatal too, because then the mutant result
means nothing. The working tree is never written; the temp copy is removed in
a ``finally``, so an interrupt leaves no residue.

    make mutants                       # every entry
    make mutants ARGS="--only ID"      # one entry (repeatable)
    python scripts/mutation_check.py --list
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "tests" / "mutants" / "manifest.yaml"

# Top-level directories that are build output or a sibling venv, never source.
_TOP_EXCLUDE = {".git", ".venv", ".venv-ci", "build", "dist", "node_modules",
                "web", ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
_ANYWHERE_EXCLUDE = {"__pycache__", ".pytest_cache"}

# Written into a temp dir and loaded with `-p`: records one outcome per node.
_PLUGIN_SRC = '''\
import json
import os

_seen = {}


def pytest_runtest_logreport(report):
    cur = _seen.get(report.nodeid, "passed")
    if report.failed:
        cur = "failed"
    elif report.skipped and cur != "failed":
        cur = "skipped"
    _seen[report.nodeid] = cur


def pytest_sessionfinish(session):
    with open(os.environ["MUTANT_REPORT"], "w") as fh:
        json.dump(_seen, fh)
'''


class ManifestError(Exception):
    """The manifest names something that is not there. Never a mutant result."""


@dataclass
class Mutant:
    id: str
    tests: list[str]
    file: Optional[str] = None     # None for an env-only entry
    old: Optional[str] = None      # exact text to find (must occur once)
    new: str = ""
    repo: str = "connector"
    expect: str = "failed"         # the outcome that counts as caught
    env: dict[str, str] = field(default_factory=dict)  # env for the MUTANT run only
    fix: str = ""


@dataclass
class Result:
    mutant: Mutant
    head_before: dict[str, str]    # node -> outcome on the unmodified copy
    mutated: dict[str, str]        # node -> outcome with the revert applied
    head_after: dict[str, str]     # node -> outcome after the copy is restored
    detail: str = ""               # pytest output tail, when a run could not grade

    @property
    def head_green(self) -> bool:
        return all(v == "passed" for v in {**self.head_before, **self.head_after}.values())

    @property
    def caught(self) -> bool:
        return any(v == self.mutant.expect for v in self.mutated.values())

    @property
    def status(self) -> str:
        if not self.head_green:
            return "HEAD-RED"
        return "CAUGHT" if self.caught else "SURVIVED"


def load_manifest(path: Path) -> list[Mutant]:
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("mutants") or []
    out, seen = [], set()
    for e in entries:
        if "id" not in e or "tests" not in e:
            raise ManifestError(f"entry needs `id` and `tests`: {e!r:.120}")
        if e["id"] in seen:
            raise ManifestError(f"duplicate mutant id {e['id']!r}")
        seen.add(e["id"])
        env = {str(k): str(v) for k, v in (e.get("env") or {}).items()}
        # A text revert needs file+old+new; an env-only entry models a
        # configuration fix (e.g. a Makefile export) and needs none of them.
        if "old" in e or "file" in e:
            if "file" not in e or "old" not in e or "new" not in e:
                raise ManifestError(f"{e['id']}: a text revert needs file, old and new")
            if not e["old"]:
                raise ManifestError(f"{e['id']}: empty `old`")
        elif not env:
            raise ManifestError(f"{e['id']}: needs a text revert or an env override")
        expect = e.get("expect", "failed")
        if expect not in ("failed", "skipped"):
            raise ManifestError(f"{e['id']}: expect must be failed|skipped, not {expect!r}")
        out.append(Mutant(
            id=e["id"], tests=list(e["tests"]), file=e.get("file"), old=e.get("old"),
            new=e.get("new") or "", repo=e.get("repo", "connector"), expect=expect,
            env=env, fix=e.get("fix", "")))
    return out


def resolve_repo(name: str, connector_root: Path) -> Path:
    if name == "connector":
        return connector_root
    if name == "framework":
        p = Path(os.environ.get(
            "FSRPB_FRAMEWORK",
            str(Path.home() / "PycharmProjects" / "fsr-playbook-framework")))
        if not (p / "fsr_playbooks").is_dir():
            raise ManifestError(
                f"framework checkout not found at {p} (set FSRPB_FRAMEWORK)")
        return p
    raise ManifestError(f"unknown repo {name!r} (connector|framework)")


def _ignore_top_level(src: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = set(n for n in names if n in _ANYWHERE_EXCLUDE)
        if Path(directory) == src:
            skip |= set(n for n in names if n in _TOP_EXCLUDE)
        return skip
    return ignore


def copy_repo(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=True, ignore=_ignore_top_level(src))


def run_nodes(repo_dir: Path, nodes: list[str], env_extra: dict[str, str],
              plugin_dir: Path, report: Path) -> dict[str, str]:
    """Run exactly `nodes` in `repo_dir`; return {nodeid: passed|failed|skipped}."""
    # No .pyc: a revert and its restore can be the same size and the same
    # second, and a stale bytecode cache would then grade the wrong source.
    env = {**os.environ, **env_extra, "FSRPB_DEV": "1", "MUTANT_REPORT": str(report),
           "PYTHONDONTWRITEBYTECODE": "1"}
    # The copy goes first so a framework copy shadows the editable install.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(plugin_dir), str(repo_dir), os.environ.get("PYTHONPATH", "")) if p)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           "-p", "_mutant_report", *nodes]
    # A report left by an earlier run must never answer for this one.
    report.unlink(missing_ok=True)
    log = report.with_suffix(".log")
    with log.open("w") as fh:
        subprocess.run(cmd, cwd=repo_dir, env=env, timeout=900,
                       stdout=fh, stderr=subprocess.STDOUT)
    if not report.exists():
        # pytest died before sessionfinish (collection crash, bad node id).
        return {}
    return json.loads(report.read_text())


def outcomes_for(names: list[str], seen: dict[str, str]) -> dict[str, str]:
    """Map each named test to its outcome. A name selects every parametrisation."""
    out: dict[str, str] = {}
    for name in names:
        hits = {k: v for k, v in seen.items() if k == name or k.startswith(name + "[")}
        if not hits:
            out[name] = "not-collected"
        elif "failed" in hits.values():
            out[name] = "failed"
        elif "skipped" in hits.values():
            out[name] = "skipped"
        else:
            out[name] = "passed"
    return out


def apply_revert(target: Path, m: Mutant) -> None:
    text = target.read_text()
    n = text.count(m.old)
    if n != 1:
        raise ManifestError(f"{m.id}: `old` occurs {n} times in {m.file} (need exactly 1); "
                            "the manifest is stale against HEAD")
    target.write_text(text.replace(m.old, m.new, 1))


def _log_tail(report: Path, lines: int = 15) -> str:
    log = report.with_suffix(".log")
    if not log.exists():
        return ""
    return "\n".join(log.read_text().splitlines()[-lines:])


def _run_one(copy: Path, m: Mutant, plugin_dir: Path, workdir: Path) -> Result:
    report = workdir / f"report-{m.id}.json"
    before = outcomes_for(m.tests, run_nodes(copy, m.tests, {}, plugin_dir, report))
    if not all(v == "passed" for v in before.values()):
        # Red or skipped on HEAD: a mutant result would mean nothing. Do not revert.
        return Result(m, head_before=before, mutated={}, head_after={},
                      detail=_log_tail(report))
    mutated: dict[str, str] = {}
    if m.file is None:
        mutated = outcomes_for(m.tests, run_nodes(copy, m.tests, m.env, plugin_dir, report))
    else:
        target = copy / m.file
        if not target.is_file():
            raise ManifestError(f"{m.id}: {m.file} does not exist in {m.repo}")
        original = target.read_text()
        try:
            apply_revert(target, m)
            mutated = outcomes_for(m.tests, run_nodes(copy, m.tests, m.env, plugin_dir, report))
        finally:
            target.write_text(original)
    after = outcomes_for(m.tests, run_nodes(copy, m.tests, {}, plugin_dir, report))
    detail = _log_tail(report) if "not-collected" in mutated.values() else ""
    return Result(m, head_before=before, mutated=mutated, head_after=after, detail=detail)


def run(manifest: Path, connector_root: Optional[Path] = None,
        only: Optional[list[str]] = None,
        repos: Optional[dict[str, Path]] = None) -> list[Result]:
    # The manifest lives at <repo>/tests/mutants/, so that repo is the default
    # `connector` checkout: a manifest always tests the tree it sits in.
    connector_root = connector_root or Path(manifest).resolve().parent.parent.parent
    mutants = load_manifest(manifest)
    if only:
        known = {m.id for m in mutants}
        unknown = [o for o in only if o not in known]
        if unknown:
            raise ManifestError(f"unknown mutant id(s): {', '.join(unknown)}")
        mutants = [m for m in mutants if m.id in only]
    repos = dict(repos or {})
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="mutants-") as tmp:
        tmpdir = Path(tmp)
        plugin_dir = tmpdir / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "_mutant_report.py").write_text(_PLUGIN_SRC)
        copies: dict[str, Path] = {}
        for m in mutants:
            if m.repo in copies:
                continue
            src = repos.get(m.repo) or resolve_repo(m.repo, connector_root)
            dst = tmpdir / f"copy-{m.repo}"
            copy_repo(src, dst)
            copies[m.repo] = dst
        for m in mutants:
            results.append(_run_one(copies[m.repo], m, plugin_dir, tmpdir))
    return results


def _print_table(results: list[Result]) -> None:
    print(f"{'MUTANT':<34} {'REPO':<10} {'EXPECT':<8} {'MUTATED':<22} {'HEAD':<8} RESULT")
    for r in results:
        m = r.mutant
        mut = ",".join(sorted(set(r.mutated.values()))) or "-"
        head = ",".join(sorted(set({**r.head_before, **r.head_after}.values())))
        print(f"{m.id:<34} {m.repo:<10} {m.expect:<8} {mut:<22} {head:<8} {r.status}")
        for node, outcome in r.mutated.items():
            if outcome == "not-collected":
                print(f"    not collected: {node}")
        if r.status == "SURVIVED":
            print(f"    coverage hole: reverting `{m.fix or m.id}` left {m.tests} green")
        if r.status == "HEAD-RED":
            print(f"    HEAD outcome for {m.tests}: before={r.head_before} after={r.head_after}")
        if r.detail and r.status != "CAUGHT":
            print("    pytest output tail:\n" + "\n".join(f"      {ln}" for ln in r.detail.splitlines()))
    caught = sum(r.status == "CAUGHT" for r in results)
    survived = [r.mutant.id for r in results if r.status == "SURVIVED"]
    bad_head = [r.mutant.id for r in results if r.status == "HEAD-RED"]
    print(f"\n{caught} caught, {len(survived)} survived, {len(bad_head)} HEAD-red"
          f" of {len(results)} mutants")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--only", action="append", default=[], help="run one mutant id (repeatable)")
    ap.add_argument("--list", action="store_true", help="list the manifest and exit")
    args = ap.parse_args(argv)
    try:
        if args.list:
            for m in load_manifest(args.manifest):
                print(f"{m.id:<34} {m.repo:<10} {m.file}  -> {', '.join(m.tests)}")
            return 0
        results = run(args.manifest, only=args.only or None)
    except ManifestError as exc:
        print(f"mutation_check: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("mutation_check: interrupted; temp copies removed, sources untouched",
              file=sys.stderr)
        return 130
    _print_table(results)
    ok = all(r.status == "CAUGHT" for r in results) and bool(results)
    return 0 if ok else 1


def _on_term(signum, frame):  # make SIGTERM unwind through the finally blocks
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_term)
    sys.exit(main())
