#!/usr/bin/env python3
"""The gate in front of `make stable BOX=<alias>`: the only way code reaches a stable box.

A stable box (the customer demo) takes code through the Import Wizard, never ssh.
This guard refuses the install unless every condition below holds:

  1. BOX is a registry alias with role = "stable" (scripts/box.sh --role).
  2. Each source repo is clean on the inputs that go into the package: no tracked
     changes and no untracked files there. The inputs come from the root repo's
     .github/package-inputs (the same file make promote classifies by): a repo's
     `ship` paths plus its `extra` paths. A stray untracked directory elsewhere in
     the repo is someone's WIP, not a package input.
  3. Each source repo's HEAD equals the remote's main (`git ls-remote`): either a
     checkout on main, or (`--promoted-source`) a detached tree that `make stable`
     created at the promoted commit. Nothing unpushed or unpromoted reaches a
     customer box. `make stable` builds from detached trees at each remote's main,
     so the shared checkouts (often on someone else's branch) are never read.
  4. Each HEAD has a VERIFIED, clean live-sanity stamp taken on an EXPERIMENTAL
     box (the stamp's `box` alias must read role = "experimental"). A stable box
     never proves its own code; the proof is the experimental run of that commit.
     A HEAD with no stamp at all is also accepted when the newest ancestor that
     has such a stamp (looked for in the last 200 commits) is separated from HEAD
     only by tooling paths, classified as make promote classifies them: the root
     repo's package-inputs `ship` lines when the repo has some, else the repo's
     .github/tooling-paths read from its remote main. A tooling-only promote has
     no box stamp (CI is its proof), so it must not need one here. The report
     names the ancestor used.

Nothing here touches a box. The install is scripts/stable_install.py.

CLI (run by the root Makefile through `make stable`):
    python3 scripts/stable_guard.py --box 159 --monorepo DIR --framework DIR --conn DIR
Exit 0 when every check passes; 1 with the failures listed; 2 on a usage error.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

WIDGET_NAMES = ("agenticAssistant", "agenticAssistantMonitor")
BOX_SH = Path(__file__).resolve().parent / "box.sh"


@dataclass(frozen=True)
class RepoSpec:
    name: str                       # the stamp directory name (the main checkout's basename)
    path: Path                      # the main checkout
    inputs: tuple[str, ...] = ()    # pathspecs that go into the package; () = the whole repo
    ship: tuple[str, ...] = ()      # the `ship` lines alone (what lands on a box); ("." ,) = the whole repo


@dataclass
class Report:
    heads: dict[str, str] = field(default_factory=dict)     # repo name -> HEAD sha
    notes: list[str] = field(default_factory=list)          # e.g. "used stamped ancestor ..."
    problems: list[str] = field(default_factory=list)
    unproven: list[str] = field(default_factory=list)       # repos whose only problem is a missing/foreign stamp (re-provable)

    @property
    def ok(self) -> bool:
        return not self.problems


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)


PACKAGE_INPUTS = Path(".github") / "package-inputs"


def load_package_inputs(monorepo: Path) -> dict[str, tuple[str, ...]]:
    """repo name -> the pathspecs that must be clean: its `ship` plus `extra` lines of the
    root repo's .github/package-inputs. `.` (the whole repo) becomes () = no pathspec.
    Raises OSError when the file is missing; a repo with no line at all is
    repo_specs's problem (it refuses)."""
    f = monorepo / PACKAGE_INPUTS
    out: dict[str, list[str]] = {}
    for raw in f.read_text().splitlines():
        parts = raw.split("#", 1)[0].split()
        if len(parts) == 3 and parts[1] in ("ship", "extra"):
            out.setdefault(parts[0], []).append(parts[2])
    return {k: () if "." in v else tuple(v) for k, v in out.items()}


def load_ship_inputs(monorepo: Path) -> dict[str, tuple[str, ...]]:
    """repo name -> its `ship` lines alone (`.` kept as the whole repo)."""
    out: dict[str, list[str]] = {}
    for raw in (monorepo / PACKAGE_INPUTS).read_text().splitlines():
        parts = raw.split("#", 1)[0].split()
        if len(parts) == 3 and parts[1] == "ship":
            out.setdefault(parts[0], []).append(parts[2])
    return {k: tuple(v) for k, v in out.items()}


def repo_specs(monorepo: Path, framework: Path, conn: Path) -> list[RepoSpec]:
    """The five source repos a stable package is built from. What each must be clean on is
    not listed here: it is the one definition in the root repo's .github/package-inputs."""
    inputs = load_package_inputs(monorepo)
    ships = load_ship_inputs(monorepo)
    paths = {"fsr_all_widgets": monorepo, "fsr-playbook-framework": framework, "conn_main": conn}
    for w in WIDGET_NAMES:
        paths[w] = monorepo / "widgets-src" / w
    missing = [n for n in paths if n not in inputs]
    if missing:
        raise KeyError(f"{monorepo / PACKAGE_INPUTS} has no line for: {', '.join(missing)}")
    return [RepoSpec(n, p, inputs[n], ships.get(n, ())) for n, p in paths.items()]


def check_clean(spec: RepoSpec) -> list[str]:
    args = ["status", "--porcelain", "--untracked-files=all"]
    if spec.inputs:
        args += ["--", *spec.inputs]
    r = _git(spec.path, *args)
    if r.returncode != 0:
        return [f"{spec.name}: git status failed ({r.stderr.strip() or 'no output'})"]
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        return []
    shown = "; ".join(ln.strip() for ln in lines[:4]) + (" ..." if len(lines) > 4 else "")
    return [f"{spec.name}: not clean on the package inputs ({shown}). Commit or promote it first."]


def head_of(spec: RepoSpec, *, detached_ok: bool = False) -> tuple[str | None, list[str]]:
    """The HEAD sha when the checkout is on main, else (None, problems).

    `detached_ok`: a detached HEAD is accepted too. The caller then relies on
    check_remote_main, which every head goes through, to prove it is the
    promoted commit; a checkout on another branch is still refused."""
    r = _git(spec.path, "rev-parse", "--verify", "-q", "HEAD")
    head = r.stdout.strip()
    if r.returncode != 0 or not head:
        return None, [f"{spec.name}: no HEAD"]
    branch = _git(spec.path, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if detached_ok and not branch:
        return head, []
    if branch != "main":
        return None, [f"{spec.name}: checked out on '{branch or '?'}', not main. Only main ships."]
    return head, []


def upstream_remote(spec: RepoSpec) -> str | None:
    """The remote that main tracks (its upstream), never a hardcoded name."""
    r = _git(spec.path, "for-each-ref", "--format=%(upstream:remotename)", "refs/heads/main")
    return r.stdout.strip() or None


def check_remote_main(spec: RepoSpec, head: str) -> list[str]:
    """HEAD must equal the main of the remote that main tracks (its upstream)."""
    remote = upstream_remote(spec)
    if not remote:
        return [f"{spec.name}: main tracks no remote (no upstream). Set one, then push main."]
    r = _git(spec.path, "ls-remote", remote, "refs/heads/main")
    if r.returncode != 0:
        return [f"{spec.name}: cannot read upstream '{remote}' main ({r.stderr.strip() or 'no answer'})"]
    remote_sha = (r.stdout.split() or [""])[0]
    if remote_sha != head:
        return [f"{spec.name}: HEAD {head[:9]} is not the upstream '{remote}' main "
                f"({remote_sha[:9] or 'none'}). Push main first."]
    return []


def _stamp_files(stamp_dir: Path, repo: str, sha: str) -> list[Path]:
    d = stamp_dir / repo
    out = []
    if (d / f"{sha}.json").is_file():
        out.append(d / f"{sha}.json")
    return out


def stamp_name(path: Path) -> str:
    """The stamp directory: the basename of the git common dir, as promote and
    live-sanity name it (conn_main's common dir is the fsr-playbook-builder repo)."""
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if not common:
        return path.name
    c = Path(common)
    return c.parent.name if c.name == ".git" else c.name


TOOLING_PATHS = ".github/tooling-paths"
WALK_LIMIT = 200   # how many commits back to look for a stamped ancestor


def _stamp_problem(spec: RepoSpec, sha: str, stamp_dir: Path,
                   role_of: Callable[[str], str]) -> tuple[bool, list[str]]:
    """(ok, reasons) for the stamp files of `sha`: ok only for a VERIFIED, clean stamp
    whose box reads role = experimental. No stamp file at all gives ([], reasons empty)."""
    reasons: list[str] = []
    for f in _stamp_files(stamp_dir, stamp_name(spec.path), sha):
        try:
            st = json.loads(f.read_text())
        except (OSError, ValueError) as exc:
            reasons.append(f"unreadable stamp {f.name} ({exc})")
            continue
        box = str(st.get("box") or "")
        if st.get("verdict") != "VERIFIED":
            reasons.append(f"stamp on box {box or '?'} reads {st.get('verdict')!r}, not VERIFIED")
        elif st.get("dirty") is not False:
            reasons.append(f"stamp on box {box or '?'} was taken on a dirty tree")
        elif st.get("sha") != sha:
            reasons.append("stamp names a different commit")
        else:
            role = role_of(box)
            if role == "experimental":
                return True, []
            reasons.append(f"VERIFIED stamp is from box {box or '?'} (role {role}); "
                           f"a stable box never proves its own code")
    return False, reasons


def _tooling_globs(spec: RepoSpec) -> list[str] | None:
    """The repo's .github/tooling-paths from its remote main (never the working tree), as
    scripts/promote.sh reads it. None when it cannot be read."""
    remote = upstream_remote(spec)
    if not remote:
        return None
    r = _git(spec.path, "show", f"refs/remotes/{remote}/main:{TOOLING_PATHS}")
    if r.returncode != 0:
        return None
    globs = [ln.split("#", 1)[0].strip() for ln in r.stdout.splitlines()]
    return [g for g in globs if g]


def shipped_paths(spec: RepoSpec, base: str, head: str) -> tuple[list[str], str]:
    """(paths changed between base and head that still need a box stamp, why-not when the
    classification itself failed). Same rule as scripts/promote.sh: with `ship` lines a path
    is shipped when it is under one of them (or under .github/); without, every path must match
    a glob of the remote main's .github/tooling-paths. An empty diff counts as shipped."""
    r = _git(spec.path, "diff", "--name-status", "-M", base, head)
    if r.returncode != 0:
        return [], f"git diff failed ({r.stderr.strip() or 'no output'})"
    changed: list[str] = []
    for line in r.stdout.splitlines():
        changed += [p for p in line.split("\t")[1:] if p]
    if not changed:
        return ["(empty diff)"], ""
    if spec.ship:
        def is_ship(p: str) -> bool:
            if p.startswith(".github/"):
                return True
            return any(i == "." or p == i or p.startswith(i + "/") for i in spec.ship)
        return [p for p in changed if is_ship(p)], ""
    globs = _tooling_globs(spec)
    if globs is None:
        return [], f"no {TOOLING_PATHS} on the remote main"
    return [p for p in changed if not any(fnmatch.fnmatchcase(p, g) for g in globs)], ""


def stamped_ancestor(spec: RepoSpec, head: str, stamp_dir: Path,
                     role_of: Callable[[str], str]) -> str | None:
    """The newest ancestor of HEAD (within WALK_LIMIT commits) with a valid stamp."""
    r = _git(spec.path, "rev-list", f"--max-count={WALK_LIMIT + 1}", head)
    for sha in r.stdout.split()[1:]:
        if _stamp_problem(spec, sha, stamp_dir, role_of)[0]:
            return sha
    return None


def reprove_hint(name: str) -> str:
    """The exact commands that give a head a VERIFIED stamp from an experimental box (the door path's own steps).
    `make stable` runs them itself when it can lease an experimental box (PROVE_BOX=<alias> pins one)."""
    exp = "BOX=<experimental>"
    if name in WIDGET_NAMES:
        how = f"make widget-push WIDGET={name} {exp}"
    elif name == "fsr_all_widgets":
        how = f"make doctor {exp} SANITY=1 STAMP=<root checkout>"
    else:
        how = f"make dev-push {exp} (connector checkout, FSR_SRC=<framework checkout>; stamps both)"
    return (f"Re-prove it on an experimental box with `{how}` (what make ship does; make stable does it for you when it can "
            f"lease one, or pin PROVE_BOX=<alias>) and retry.")


def check_stamp(spec: RepoSpec, head: str, stamp_dir: Path,
                role_of: Callable[[str], str], notes: list[str] | None = None) -> list[str]:
    """A VERIFIED, clean stamp for HEAD whose box reads role = experimental. A HEAD with no
    stamp file at all is also accepted when only tooling paths lie between it and the newest
    stamped ancestor (a tooling-only promote has no box stamp; CI proved it)."""
    ok, reasons = _stamp_problem(spec, head, stamp_dir, role_of)
    if ok:
        return []
    fix = reprove_hint(spec.name)
    if not reasons:
        anc = stamped_ancestor(spec, head, stamp_dir, role_of)
        if anc is None:
            reasons.append(f"no live-sanity stamp, and none on its last {WALK_LIMIT} ancestors")
        else:
            shipped, err = shipped_paths(spec, anc, head)
            if err:
                reasons.append(f"no live-sanity stamp; cannot classify the change since stamped "
                               f"ancestor {anc[:9]} ({err})")
            elif shipped:
                shown = ", ".join(shipped[:4]) + (" ..." if len(shipped) > 4 else "")
                reasons.append(f"no live-sanity stamp, and shipped path(s) changed since stamped "
                               f"ancestor {anc[:9]} ({shown})")
            else:
                if notes is not None:
                    notes.append(f"{spec.name}@{head[:9]}: no stamp of its own; only tooling paths "
                                 f"changed since the VERIFIED stamp on ancestor {anc[:9]}")
                return []
    return [f"{spec.name}@{head[:9]}: {reasons[0]}. {fix}"]


def box_role(alias: str) -> str:
    """The registry role of a box alias through scripts/box.sh (the one resolver)."""
    r = subprocess.run(["bash", str(BOX_SH), "--role", alias], capture_output=True, text=True)
    return r.stdout.strip() or "unset"


def guard(box: str, specs: list[RepoSpec], stamp_dir: Path,
          role_of: Callable[[str], str] = box_role, *, detached_ok: bool = False) -> Report:
    rep = Report()
    if not box:
        rep.problems.append("no BOX=<alias>. There is no default box.")
        return rep
    role = role_of(box)
    if role != "stable":
        rep.problems.append(f"BOX={box} has role '{role}', not stable. make stable installs only on a "
                            f"stable box; an experimental box takes code with make push/loop.")
    for spec in specs:
        rep.problems += check_clean(spec)
        head, why = head_of(spec, detached_ok=detached_ok)
        rep.problems += why
        if head is None:
            continue
        rep.heads[spec.name] = head
        rep.problems += check_remote_main(spec, head)
        stamp_problems = check_stamp(spec, head, stamp_dir, role_of, rep.notes)
        if stamp_problems:
            rep.unproven.append(spec.name)
        rep.problems += stamp_problems
    return rep


def default_stamp_dir() -> Path:
    return Path(os.environ.get("FSR_LIVE_SANITY_DIR") or Path.home() / ".cache" / "fsr-live-sanity")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--box", default=os.environ.get("BOX", ""))
    ap.add_argument("--monorepo", required=True, type=Path)
    ap.add_argument("--framework", required=True, type=Path)
    ap.add_argument("--conn", required=True, type=Path)
    ap.add_argument("--stamp-dir", type=Path, default=None)
    ap.add_argument("--json", action="store_true",
                    help="print one JSON object on stdout: {ok, problems, unproven} (unproven = repos whose stamp can be re-proved)")
    ap.add_argument("--promoted-source", action="store_true",
                    help="the repos are detached trees at the promoted commits (make stable)")
    args = ap.parse_args(argv)
    try:
        specs = repo_specs(args.monorepo, args.framework, args.conn)
    except (OSError, KeyError) as exc:
        print(f"make stable: cannot read the package inputs: {exc}", file=sys.stderr)
        return 2
    rep = guard(args.box, specs,
                args.stamp_dir or default_stamp_dir(), detached_ok=args.promoted_source)
    if args.json:
        print(json.dumps({"ok": rep.ok, "problems": rep.problems, "unproven": rep.unproven, "heads": rep.heads}))
        return 0 if rep.ok else 1
    if not rep.ok:
        print("make stable: REFUSED", file=sys.stderr)
        for p in rep.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    for name, head in rep.heads.items():
        print(f"stable guard: {name} {head[:9]} clean, on remote main, VERIFIED on an experimental box")
    for note in rep.notes:
        print(f"stable guard: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
