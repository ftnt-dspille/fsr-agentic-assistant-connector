"""Track C1 — per-quick-action build-prompt tailoring.

The 5 build quick-action chips send a `quick_action` tag the build system
prompt branches on. These tests pin the connector seam:

- `_resolve_system_prompt("build", ..., quick_action=<key>)` appends the
  `# Active quick-action` directive naming the key (build-only).
- A build turn with no `quick_action` gets the plain build prompt (no directive).
- A triage turn ignores the tag entirely (no directive) — the build slice is
  what carries the playbook-analysis tools the modes reference.
- `ChatTurnParams` accepts the typed `quick_action` field (Track C1 contract;
  per the pydantic strict-typing goal, a typed field, not extra-passthrough).

The build branch of `_resolve_system_prompt` returns before touching storage
(case_state is None), so the build cases pass a trivial mock; the triage case
uses entity=None, which short-circuits to the static triage prompt before any
storage access — so no preflight mock is needed.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn_qa")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn_qa"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn_qa.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn_qa.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn_qa.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _NoopStorage:
    """The build branch never reads storage; a stand-in is enough."""


def test_build_quick_action_appends_directive(operations):
    system, case_state = operations._resolve_system_prompt(
        "build", None, _NoopStorage(), "sess1", 1, quick_action="add_step")
    # The directive's characteristic sentence (only present when appended —
    # the static prompt merely *mentions* the marker name in prose).
    assert "The analyst opened this turn with the" in system
    assert '"add_step"' in system
    # The directive points at the prompt's modes section (framework-shipped).
    assert "Quick-action modes" in system
    assert case_state is None  # build carries no case state


def test_build_quick_action_works_for_all_five_chips(operations):
    for key in ("explain", "add_step", "find_issues",
                "add_error_handling", "optimize"):
        system, _ = operations._resolve_system_prompt(
            "build", None, _NoopStorage(), "sess1", 1, quick_action=key)
        assert f'"{key}"' in system, f"directive missing key {key}"


def test_build_without_quick_action_has_no_directive(operations):
    system, _ = operations._resolve_system_prompt(
        "build", None, _NoopStorage(), "sess1", 1)
    # No directive sentence appended on a plain build turn.
    assert "The analyst opened this turn with the" not in system
    # The static build prompt (incl. the modes section) still loads.
    assert "# Quick-action modes" in system


def test_triage_ignores_quick_action(operations):
    # entity=None short-circuits triage to the static prompt (no preflight,
    # no storage access) — and the directive is build-only, so it's absent
    # even though a quick_action tag is present.
    system, _ = operations._resolve_system_prompt(
        "triage", None, _NoopStorage(), "sess1", 1, quick_action="iocs")
    assert "The analyst opened this turn with the" not in system


# --- pydantic contract (ChatTurnParams.quick_action) -------------------

sys.path.insert(0, str(ROOT))
import pydantic_models as M  # noqa: E402


def test_chat_turn_params_accepts_quick_action():
    p = M.ChatTurnParams(session_id="s", quick_action="explain")
    assert p.quick_action == "explain"


def test_chat_turn_params_quick_action_defaults_none():
    assert M.ChatTurnParams(session_id="s").quick_action is None
