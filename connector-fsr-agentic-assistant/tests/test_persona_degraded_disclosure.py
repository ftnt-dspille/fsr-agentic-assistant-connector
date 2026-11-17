"""Phase 2 item 4 -- fail CLOSED on load-bearing persona state.

A transient Key Store read failure used to strip the module's persona
SILENTLY: the turn ran as built-in triage/build, writes got refused
nondeterministically, and nothing anywhere said why ("sometimes it just
acts dumb"). `_resolve_profile` now records the transient miss and
`_profile_resolution_degraded` exposes it to the turn, which discloses
the degradation in the prompt and the activity feed.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_caches(operations):
    operations._PROFILE_CACHE.clear()
    operations._PROFILE_NEG_TS.clear()
    operations._PROFILE_DEGRADED_TS.clear()
    yield
    operations._PROFILE_CACHE.clear()
    operations._PROFILE_NEG_TS.clear()
    operations._PROFILE_DEGRADED_TS.clear()


def test_transient_miss_marks_turn_degraded(operations, monkeypatch):
    # No live client => transient (definitive=False) => degraded, uncached.
    monkeypatch.setattr(
        "fsr_playbooks.mcp_server._shared._live_client", lambda: None)
    assert operations._resolve_profile("alerts", "s1") is None
    assert operations._profile_resolution_degraded("alerts", "s1")
    # Transient: nothing cached, so the next turn retries.
    assert ("s1", "alerts") not in operations._PROFILE_CACHE


def test_definitive_no_match_is_not_degraded(operations, monkeypatch):
    class _Client:
        pass
    monkeypatch.setattr(
        "fsr_playbooks.mcp_server._shared._live_client", lambda: _Client())
    monkeypatch.setattr(
        "fsr_soc_triage.profiles.resolve_profile_status",
        lambda client, module: (None, True))
    assert operations._resolve_profile("alerts", "s2") is None
    assert not operations._profile_resolution_degraded("alerts", "s2")
    # Definitive no-match caches (negative TTL) as before.
    assert ("s2", "alerts") in operations._PROFILE_CACHE


def test_definitive_resolution_clears_degraded(operations, monkeypatch):
    operations._PROFILE_DEGRADED_TS[("s3", "alerts")] = operations._profile_clock()

    class _Client:
        pass
    monkeypatch.setattr(
        "fsr_playbooks.mcp_server._shared._live_client", lambda: _Client())
    monkeypatch.setattr(
        "fsr_soc_triage.profiles.resolve_profile_status",
        lambda client, module: (None, True))
    operations._resolve_profile("alerts", "s3")
    assert not operations._profile_resolution_degraded("alerts", "s3")


def test_stale_marker_expires(operations):
    operations._PROFILE_DEGRADED_TS[("s4", "alerts")] = (
        operations._profile_clock() - operations._PROFILE_DEGRADED_FRESH_S - 1)
    assert not operations._profile_resolution_degraded("alerts", "s4")


# The disclosure reaching the model is pinned behaviourally in
# tests/test_chat_operations.py::test_degraded_persona_is_disclosed_to_the_model.
