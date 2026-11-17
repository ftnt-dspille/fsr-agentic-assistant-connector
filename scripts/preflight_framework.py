#!/usr/bin/env python3
"""Build-time guard: verify the PINNED fsr_playbooks wheel actually provides
every symbol the connector imports from it.

This is the guard for the class of outage that shipped in connector 0.4.43:
`fsr_soc_triage/registry.py` began `from fsr_playbooks.llm.intents import
TRIAGE_ONLY_TOOLS`, but the pinned wheel (0.4.19) predated that symbol, so the
whole `fsr_soc_triage` package failed to import on-box and took the crudhub
bridge down with it (every live op → crudhub_unavailable). A missing framework
symbol must fail the BUILD, not the box.

Static + dependency-free: it does NOT install or import anything. It
  1. reads the `fsr-playbooks==<ver>` pin from requirements.txt,
  2. finds the materialized wheel in wheels/ (build.sh downloads it there),
  3. scans the connector package for `from fsr_playbooks[.<mod>] import <names>`,
  4. asserts each imported name is DEFINED in the corresponding module inside the
     wheel (a top-level `name =` / `def name` / `class name` / `__all__` entry).

Exit non-zero (with a clear message) on any missing symbol or a missing wheel.
Run: `python3 scripts/preflight_framework.py [--wheels DIR] [--pkg DIR]`
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "connector-fsr-agentic-assistant"


def _pin(req: Path) -> str:
    for line in req.read_text().splitlines():
        m = re.match(r"^fsr-playbooks==(\S+)", line.strip())
        if m:
            return m.group(1)
    sys.exit("preflight: no `fsr-playbooks==` pin in requirements.txt")


def _find_wheel(wheels: Path, ver: str) -> Path | None:
    if not wheels.is_dir():
        return None
    hits = list(wheels.glob(f"fsr_playbooks-{ver}-*.whl"))
    return hits[0] if hits else None


def _wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as z:
        return set(z.namelist())


def _wheel_module_src(wheel: Path, dotted: str, names: set[str]) -> str | None:
    """Return the source of `fsr_playbooks.<dotted>` from the wheel, or None."""
    rel = "fsr_playbooks/" + dotted.replace(".", "/") if dotted else "fsr_playbooks"
    for c in (f"{rel}.py", f"{rel}/__init__.py"):
        if c in names:
            with zipfile.ZipFile(wheel) as z:
                return z.read(c).decode("utf-8", "replace")
    return None


def _is_submodule(names: set[str], sub: str, name: str) -> bool:
    """True if `name` is itself a module/subpackage of fsr_playbooks.<sub> in the
    wheel (i.e. `from fsr_playbooks.<sub> import <name>` imports a module)."""
    base = "fsr_playbooks/" + (f"{sub}/" if sub else "")
    rel = f"{base}{name}"
    return f"{rel}.py" in names or f"{rel}/__init__.py" in names


def _collect(body: list, out: set[str]) -> None:
    """Collect module-scope bound names from a statement list, descending into
    control-flow blocks (try/if/with/for/while) -- which can conditionally bind a
    module-level name (e.g. `try: mcp = _make_mcp() except: mcp = _Fallback()`) --
    but NOT into def/class bodies (those introduce a new scope)."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.Try, ast.If, ast.With, ast.For, ast.While,
                               ast.AsyncWith, ast.AsyncFor)):
            for attr in ("body", "orelse", "finalbody", "handlers"):
                block = getattr(node, attr, None) or []
                if attr == "handlers":
                    for h in block:
                        _collect(h.body, out)
                else:
                    _collect(block, out)


def _defined_names(src: str) -> set[str]:
    """Module-scope names bound in a module: assignments (incl. conditional/
    try-guarded), def/class, imports, and everything listed in __all__."""
    out: set[str] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    _collect(tree.body, out)
    # __all__ entries (covers re-exports the connector may rely on)
    m = re.search(r"__all__\s*=\s*\[([^\]]*)\]", src)
    if m:
        out |= set(re.findall(r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]", m.group(1)))
    return out


def _collect_required_imports(body: list, out: set[tuple[str, str]],
                               guarded: bool = False) -> None:
    """Walk statements collecting (submodule, name) pairs from ImportFrom
    nodes.  Imports inside a try/except are OPTIONAL (fail-open) and are
    NOT collected as required."""
    for node in body:
        if isinstance(node, ast.Try) and not guarded:
            # Imports inside try/except are optional -- skip them
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Imports inside function bodies are lazy/optional -- skip
            continue
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                if mod == "fsr_playbooks" or mod.startswith("fsr_playbooks."):
                    sub = mod[len("fsr_playbooks"):].lstrip(".")
                    for a in node.names:
                        if a.name != "*":
                            out.add((sub, a.name))
        elif isinstance(node, (ast.If, ast.With, ast.For, ast.While,
                               ast.AsyncWith, ast.AsyncFor)):
            for attr in ("body", "orelse"):
                block = getattr(node, attr, None) or []
                _collect_required_imports(block, out, guarded)


def _connector_imports(pkg: Path) -> dict[str, set[str]]:
    """Map fsr_playbooks submodule (dotted, '' = top pkg) -> set of imported
    names, across every .py under the connector package.

    Only module-scope imports NOT inside a try/except are required.  The
    connector uses ``try: from fsr_playbooks.x import y; except ImportError``
    to degrade gracefully when an older wheel lacks a symbol; flagging those
    would block every ship on a symbol that is optional by design."""
    wanted: dict[str, set[str]] = {}
    for py in pkg.rglob("*.py"):
        if "/tests/" in py.as_posix():
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        req: set[tuple[str, str]] = set()
        _collect_required_imports(tree.body, req)
        for sub, name in req:
            wanted.setdefault(sub, set()).add(name)
    return wanted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheels", default=str(PKG / "wheels"))
    ap.add_argument("--pkg", default=str(PKG))
    ap.add_argument("--req", default=str(PKG / "requirements.txt"))
    ap.add_argument("--version", default=None,
                    help="validate this version instead of the requirements pin "
                         "(used by `make bump-framework` to check BEFORE writing "
                         "the pin)")
    args = ap.parse_args()

    ver = args.version or _pin(Path(args.req))
    wheel = _find_wheel(Path(args.wheels), ver)
    if wheel is None:
        print(f"preflight: no fsr_playbooks-{ver} wheel in {args.wheels} -- "
              f"cannot verify the pin provides the connector's imports. "
              f"(build.sh must materialize it; set ALLOW_OFFLINE_BUILD=1 only "
              f"if you accept an unverified bundle.)", file=sys.stderr)
        return 2

    wanted = _connector_imports(Path(args.pkg))
    wheel_names = _wheel_names(wheel)
    missing: list[str] = []
    for sub, names in sorted(wanted.items()):
        src = _wheel_module_src(wheel, sub, wheel_names)
        modlabel = "fsr_playbooks" + (f".{sub}" if sub else "")
        if src is None:
            missing.append(f"{modlabel} (module absent from wheel) -> {sorted(names)}")
            continue
        have = _defined_names(src)
        for n in sorted(names):
            # a name resolves if it's defined/re-exported in the module OR is
            # itself a submodule of it (`from fsr_playbooks.agent import case_state`).
            if n not in have and not _is_submodule(wheel_names, sub, n):
                missing.append(f"{modlabel}.{n}")

    if missing:
        print(f"preflight FAIL: fsr-playbooks=={ver} does not provide symbols the "
              f"connector imports:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("Bump the fsr-playbooks pin to a release that includes them "
              "(and publish it first -- see RELEASING.md in the framework repo).",
              file=sys.stderr)
        return 1

    total = sum(len(v) for v in wanted.values())
    print(f"preflight OK: fsr-playbooks=={ver} provides all {total} connector-"
          f"imported symbol(s) across {len(wanted)} module(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
