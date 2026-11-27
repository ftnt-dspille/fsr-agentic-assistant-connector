"""A transient assistant_skills read failure must not strip the house rules for the whole
process life. `_resolve_profile` used to cache the persona it got back even when the skills
read had failed (the rules were silently missing from the cached profile). Now only a
successful skills application (or a 404 "no skills module") is cached, so a later turn
re-resolves and applies the rules.
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

RULE = "RULE-ALWAYS-LIST-ACTIVE-ONLY"


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
    for name in ("_PROFILE_CACHE", "_PROFILE_NEG_TS", "_PROFILE_DEGRADED_TS"):
        getattr(operations, name).clear()
    yield
    for name in ("_PROFILE_CACHE", "_PROFILE_NEG_TS", "_PROFILE_DEGRADED_TS"):
        getattr(operations, name).clear()


class _Prof:
    """Stands in for the persona Profile: only .prompt and model_copy are used here."""

    def __init__(self, prompt: str):
        self.prompt = prompt

    def model_copy(self, update):
        return _Prof(update["prompt"])


class _Resp:
    def __init__(self, status: int, members=None):
        self.status_code = status
        self._members = members or []

    def json(self):
        return {"hydra:member": self._members}


class _Client:
    base_url = "https://box.example"


def _wire(monkeypatch, skills_responses):
    """Fake the live client, the persona read (always found) and the skills GET. Each skills
    GET pops the next entry: an Exception is raised, a _Resp is returned."""
    calls = {"skills": 0}

    def fake_get(client, url, *a, **kw):
        calls["skills"] += 1
        item = skills_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("fsr_playbooks.mcp_server._shared._live_client", lambda: _Client())
    monkeypatch.setattr("fsr_playbooks.mcp_server._shared.live_get_with_reauth", fake_get)
    # tools_triage binds _get_with_reauth at import time; load_skills falls back to that name.
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_triage._get_with_reauth", fake_get)
    monkeypatch.setattr("fsr_soc_triage.profiles.resolve_profile_status",
                        lambda client, module: (_Prof("BASE PERSONA"), True))
    return calls


_RULE_RECORD = {"instruction": RULE, "scopeModule": "alerts", "enabled": True,
                "priority": 1, "trigger": "list"}


def test_transient_skills_failure_is_retried_and_the_rule_applies_on_the_next_turn(
        operations, monkeypatch):
    calls = _wire(monkeypatch, [
        ConnectionError("transient: skills read dropped"),   # turn 1: read fails
        _Resp(200, [_RULE_RECORD]),                          # turn 2: read succeeds
    ])

    first = operations._resolve_profile("alerts", "sess-retry")
    assert first is not None and RULE not in first.prompt   # this turn runs without it
    assert ("sess-retry", "alerts") not in operations._PROFILE_CACHE  # and nothing is cached

    second = operations._resolve_profile("alerts", "sess-retry")
    # Red without the fix: the stripped persona from turn 1 was cached, so the rule stays gone.
    assert RULE in second.prompt
    assert calls["skills"] == 2


def test_a_successful_skills_application_is_cached(operations, monkeypatch):
    calls = _wire(monkeypatch, [_Resp(200, [_RULE_RECORD])])
    first = operations._resolve_profile("alerts", "sess-ok")
    second = operations._resolve_profile("alerts", "sess-ok")
    assert RULE in first.prompt and RULE in second.prompt
    assert calls["skills"] == 1            # cached: no second skills read
    assert ("sess-ok", "alerts") in operations._PROFILE_CACHE


def test_no_skills_module_404_is_definitive_and_cached(operations, monkeypatch):
    calls = _wire(monkeypatch, [_Resp(404)])
    first = operations._resolve_profile("alerts", "sess-404")
    second = operations._resolve_profile("alerts", "sess-404")
    assert first.prompt == "BASE PERSONA" == second.prompt
    assert calls["skills"] == 1            # a 404 is "no skills module", cached like any success
