"""Put the framework's `tooling/` package on sys.path, once, for every script.

`tooling/` (the eval matrix's IR assertions, chat drive) lives next to
`fsr_playbooks/` in the framework checkout and is not part of the wheel, so the
scripts reach it through the checkout. The shared chat-turn harness is
`fsr_playbooks.harness` and needs none of this: FSRPB_FRAMEWORK_DIR when set, else the
directory holding the imported `fsr_playbooks` (the editable clone).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def framework_root() -> Path:
    """The framework checkout. Same precedence as scripts/framework_dir.sh (the
    shell resolver): FSR_SRC, FSRPB_FRAMEWORK_DIR ("none" ignored), <repo>/.fsr_src,
    <repo>/../framework (monorepo layout); then, python-only, the directory holding
    the imported `fsr_playbooks` (the editable clone)."""
    for var in ("FSR_SRC", "FSRPB_FRAMEWORK_DIR"):
        env = os.environ.get(var)
        if env and env != "none":
            return Path(env)
    repo = Path(__file__).resolve().parents[1]
    pin = repo / ".fsr_src"
    if pin.is_file() and pin.read_text().strip():
        return Path(pin.read_text().splitlines()[0])
    mono = repo.parent / "framework"
    if (mono / "fsr_playbooks").is_dir():
        return mono
    import fsr_playbooks
    return Path(fsr_playbooks.__file__).resolve().parents[1]


def ensure_tooling() -> Path:
    """Make `import tooling...` work; returns the framework root. Raises
    ImportError naming the fix when the framework is an installed wheel."""
    root = framework_root()
    if not (root / "tooling" / "evals").is_dir():
        raise ImportError(
            f"no framework tooling/ under {root}: point FSRPB_FRAMEWORK_DIR at "
            f"a framework checkout (the wheel does not ship tooling/)")
    # `import tooling...` needs the root; `import probes` (a package under
    # tooling/) needs tooling/ itself. Append, don't insert: a wheel install of
    # fsr_playbooks must keep winning over the source clone next to tooling/, so
    # the CI-mirror still tests the pinned wheel, not the checkout.
    for p in (str(root), str(root / "tooling")):
        if p not in sys.path:
            sys.path.append(p)
    return root
