"""The advertised tool surface is a CACHE PREFIX -- it must hold still.

Anthropic's prompt cache is a prefix match over `tools` then `system`, and
`anthropic_provider` marks the last tool with `cache_control`. So every time
`_advertised_tools` returns a different list, the whole cached prefix -- the
system prompt included -- is discarded and re-billed at write price.

`_advertised_tools` composes five independent layers (MCP materialization,
config scoping, per-page surfacing, persona allowlist, state drops) plus the
read-only-chip withhold. Nothing asserted that the composition is a *function*
of its inputs, and the token counters that would have shown the churn
(`cache_read`) were logged but never gated.

These tests are the gate: same inputs => byte-identical surface => identical
fingerprint. They do NOT assert which tools are advertised (that is
`test_intent_tool_scoping`'s job) -- only that the answer is stable, which is
what caching requires.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

try:
    from fsr_playbooks.llm.cache_prefix import explain_change, prefix_fingerprint
except ImportError:  # pragma: no cover - older pinned wheel
    pytest.skip("framework wheel predates cache_prefix",
                allow_module_level=True)


@pytest.fixture(scope="module")
def ops():
    """Load operations.py as a package member -- it uses relative imports, so
    a plain `import operations` dies at load. Same shape as
    test_list_agent_tools' fixture."""
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
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


_SYS = "system prompt held constant"


def _surface(ops, **kw):
    persona = ops.ResolvedPersona(intent=kw.pop("intent", "triage"), profile=None)
    return ops._advertised_tools({}, persona, **kw)


def test_same_inputs_give_the_same_surface(ops) -> None:
    """The composition must be a function of its inputs, not of call order.

    A worker serves many turns; if the second call differs from the first for
    no stated reason, every session after the first pays cache-write price."""
    a = _surface(ops)
    b = _surface(ops)
    assert prefix_fingerprint(_SYS, a) == prefix_fingerprint(_SYS, b), \
        explain_change(_SYS, a, _SYS, b)


def test_surface_is_stable_across_repeated_calls(ops) -> None:
    """Ten calls, one fingerprint. Catches order flap and lazy-registration
    side effects that only show up after the Nth call."""
    fps = {prefix_fingerprint(_SYS, _surface(ops)) for _ in range(10)}
    assert len(fps) == 1, f"advertised surface flapped across calls: {fps}"


def test_tool_order_is_deterministic(ops) -> None:
    """A reorder invalidates the cache exactly as a set change does, so
    equal-as-sets is not good enough."""
    a = [t["name"] for t in _surface(ops)]
    b = [t["name"] for t in _surface(ops)]
    assert a == b


@pytest.mark.parametrize("intent", ["triage", "build"])
def test_each_intent_is_internally_stable(ops, intent: str) -> None:
    """Whether the two intents SHOULD differ is a design question (Phase 3 says
    no). Whether each is stable with itself is not -- it must be."""
    a = _surface(ops, intent=intent)
    b = _surface(ops, intent=intent)
    assert prefix_fingerprint(_SYS, a) == prefix_fingerprint(_SYS, b), \
        explain_change(_SYS, a, _SYS, b)
