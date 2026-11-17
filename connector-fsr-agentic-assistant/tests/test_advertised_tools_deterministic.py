"""The advertised tool surface is a prompt-cache prefix, so it must be a pure
function of its inputs: (intent, open playbook, read-only, persona).

Anthropic caches on a prefix match over (tools, system). The surface is composed
from registries and sets, so an import order, a hash seed or a first-call-versus-
later-call difference can reorder it while no input changed, and every turn that
sees the new order pays a cache write. These tests build the same turns twice in
one process, then in fresh child processes under different PYTHONHASHSEED values
and with a different import order, and require one digest across all of them.

They also bound how many distinct surfaces a default deployment produces. Each
distinct surface is a separate cache prefix a worker can be asked to hold.
"""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("fsr_playbooks.llm.turn_plan")
pytest.importorskip("fsr_playbooks.llm.cache_prefix")

# The grid every process builds. Intent and open playbook are the axes the
# turn plan reads; read-only is the chip or scheduled-task flag; the quick
# action covers the read-only chip and a change chip.
_GRID = list(itertools.product(
    ("triage", "build"), (True, False), (False, True), (None, "explain")))

_CHILD = r'''
import hashlib, importlib.util, itertools, json, sys, types
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
if "pre-import" in sys.argv[2:]:
    import fsr_playbooks.mcp_server  # noqa: F401  framework first ...
    import fsr_soc_triage  # noqa: F401  ... then the connector's triage package
for name in ("connectors", "connectors.core", "connectors.core.connector"):
    sys.modules.setdefault(name, types.ModuleType(name))
class ConnectorError(Exception):
    pass
sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
pkg = types.ModuleType("_fsrpb_conn")
pkg.__path__ = [str(root)]
sys.modules["_fsrpb_conn"] = pkg
spec = importlib.util.spec_from_file_location("_fsrpb_conn.operations", root / "operations.py")
ops = importlib.util.module_from_spec(spec)
sys.modules["_fsrpb_conn.operations"] = ops
spec.loader.exec_module(ops)
from fsr_playbooks.llm.session_state import SessionState, bound
from fsr_playbooks.llm.turn_plan import TurnContext, plan_turn
grid = json.loads(sys.argv[-1])
out = {}
for intent, has_open, ro, qa in grid:
    plan = plan_turn(intent, context=TurnContext(has_open_playbook=has_open))
    persona = ops.ResolvedPersona(intent=intent, profile=None)
    with bound(SessionState(read_only=ro, turn_plan=plan,
                            grounded_yaml=("x: 1" if has_open else None))):
        tools = ops._advertised_tools({}, persona, has_open_playbook=has_open,
                                      read_only=ro, quick_action=qa)
    out["|".join(map(str, (intent, has_open, ro, qa)))] = tools
blob = json.dumps(out, sort_keys=False, default=str)
print("DIGEST " + hashlib.sha256(blob.encode("utf-8")).hexdigest())
'''


def _load_ops():
    """Load operations.py as a package member, the way the connector's own
    tests do (it uses relative imports)."""
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for key in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


def _in_process_surfaces(ops) -> dict[str, list]:
    from fsr_playbooks.llm.session_state import SessionState, bound
    from fsr_playbooks.llm.turn_plan import TurnContext, plan_turn

    out: dict[str, list] = {}
    for intent, has_open, ro, qa in _GRID:
        plan = plan_turn(intent, context=TurnContext(has_open_playbook=has_open))
        persona = ops.ResolvedPersona(intent=intent, profile=None)
        with bound(SessionState(read_only=ro, turn_plan=plan,
                                grounded_yaml=("x: 1" if has_open else None))):
            tools = ops._advertised_tools({}, persona, has_open_playbook=has_open,
                                          read_only=ro, quick_action=qa)
        out["|".join(map(str, (intent, has_open, ro, qa)))] = tools
    return out


def _digest(surfaces: dict[str, list]) -> str:
    # Same encoding as the child process, so the digests are comparable.
    blob = json.dumps(surfaces, sort_keys=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def ops():
    return _load_ops()


def _child_digest(seed: str, *extra: str) -> str:
    env = dict(os.environ, PYTHONHASHSEED=seed)
    grid = json.dumps([list(g) for g in _GRID])
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(ROOT), *extra, grid],
        env=env, capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("DIGEST "))
    return line.split(" ", 1)[1]


def test_same_turn_built_twice_in_one_process_is_byte_identical(ops) -> None:
    first = _in_process_surfaces(ops)
    second = _in_process_surfaces(ops)
    assert first.keys() == second.keys()
    diverged = sorted(k for k in first
                      if json.dumps(first[k], default=str)
                      != json.dumps(second[k], default=str))
    assert not diverged, f"the surface changed between two builds of the same turn: {diverged}"


def test_surface_is_identical_across_hash_seeds_and_import_order(ops) -> None:
    """Fresh interpreters, different string-hash seeds, and one that imports the
    framework and connector packages before the operations module. Any set
    iteration or import-order dependence in the composition shows up here."""
    in_proc = _digest(_in_process_surfaces(ops))
    children = {
        "seed=1": _child_digest("1"),
        "seed=2": _child_digest("2"),
        "seed=3, pre-import": _child_digest("3", "pre-import"),
    }
    mismatched = {k: v for k, v in children.items() if v != in_proc}
    assert not mismatched, (
        f"the advertised surface depends on process state: parent={in_proc} "
        f"children={children}")


def test_a_default_deployment_produces_a_bounded_number_of_surfaces(ops) -> None:
    """Intent x open playbook x read-only is six distinct surfaces with no
    persona. A custom persona adds one more per allowlist. The bound is what
    the cache has to hold, so growth past it is a regression to look at."""
    per_turn = _in_process_surfaces(ops)
    variants = {hashlib.sha256(json.dumps(v, default=str).encode()).hexdigest()
                for v in per_turn.values()}
    assert len(variants) <= 6, f"{len(variants)} distinct surfaces for the default persona"
