"""The create-method toggle narrows the build tool slice.

A `guided` create strategy removes the free-text YAML authoring surface from the
advertised build slice so `compose_playbook` (plan->compiler) is the only path
to a deliverable -- the slice IS the policy. `freeform` (and an unresolved/None
value) leave the slice whole. The strategy is applied in `_advertised_tools`,
gated to the build intent (triage never authors YAML).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fsr_playbooks") is None,
    reason="fsr_playbooks framework not importable in this env",
)


@pytest.fixture(scope="module")
def ops():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_cs")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_cs"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_cs.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_cs.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_cs.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


def _slice(ops, intent, strategy):
    return {t["name"] for t in ops._advertised_tools(
        {}, ops.ResolvedPersona(intent=intent), include_mcp=False,
        create_strategy=strategy)}


def _freeform_authoring():
    from fsr_playbooks.llm.intents import FREEFORM_AUTHORING_TOOLS
    return set(FREEFORM_AUTHORING_TOOLS)


def test_guided_build_drops_free_text_authoring_keeps_compose(ops):
    free = _slice(ops, "build", "freeform")
    # Precondition: the free-text authoring tools and compose are present when
    # Freeform (else the guided assertion proves nothing).
    assert _freeform_authoring() & free, "free-text authoring tools absent in freeform build"
    assert "compose_playbook" in free

    guided = _slice(ops, "build", "guided")
    assert not (_freeform_authoring() & guided), (
        "guided build still advertises free-text YAML authoring tools -- the "
        "model can fall back to typing raw YAML")
    assert "compose_playbook" in guided, "guided build dropped the only authoring path"


def test_freeform_and_none_leave_the_build_slice_whole(ops):
    free = _slice(ops, "build", "freeform")
    assert _slice(ops, "build", None) == free
    assert _slice(ops, "build", "auto") == free  # unresolved auto = no-op here


def test_guided_does_not_touch_triage(ops):
    # Triage never authors YAML; a guided strategy must not alter its slice.
    assert _slice(ops, "triage", "guided") == _slice(ops, "triage", "freeform")
