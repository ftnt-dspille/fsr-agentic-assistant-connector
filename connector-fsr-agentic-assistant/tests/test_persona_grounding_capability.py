"""P3 — grounding/preflight/CaseState as a persona-declared capability.

Un-welds the dynamic turn-shaping spine from the literal intent string. The
gate is now ``ResolvedPersona.capabilities.grounding``:

- built-in ``triage`` grounds (unchanged); built-in ``build`` does not.
- a Key Store persona that declares ``capabilities.grounding: true`` earns the
  preflight → CaseState → continuation spine on ANY module — grounding on its
  OWN prompt (the triage scenario prompt never overrides a persona).
- a prompt-only persona (no declaration) stays ungrounded, exactly as before.

Exit criterion: a non-triage persona with ``grounding: true`` gets CaseState
continuation across turns, driven in-process; triage parity stays green (the
byte-exact goldens + test_case_state_wiring cover that).
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


class _Storage:
    """Minimal storage: tracks case-state loads/saves + progress frames."""

    def __init__(self):
        self.case_states = {}
        self.turn_progress = []

    def get_case_state(self, session_id):
        return self.case_states.get(session_id)

    def save_case_state(self, session_id, state):
        self.case_states[session_id] = state

    def append_turn_progress(self, session_id, turn, frame):
        self.turn_progress.append((session_id, turn, frame))


def _persona(operations, *, grounding, prompt="PERSONA PROMPT"):
    from fsr_soc_triage.profiles import Profile, PersonaCapabilities
    caps = PersonaCapabilities(grounding=True) if grounding else None
    return Profile(module="ztpf_templates", prompt=prompt, capabilities=caps)


# ---------------------------------------------------------------- capabilities

def test_builtin_intent_capabilities(operations):
    """Built-in triage grounds; build does not (no persona layered)."""
    RP = operations.ResolvedPersona
    assert RP(intent="triage").capabilities.grounding is True
    assert RP(intent="build").capabilities.grounding is False


def test_prompt_only_persona_does_not_inherit_grounding(operations):
    """A persona that declares no capabilities stays ungrounded even over the
    triage base — grounding is never silently inherited from the built-in."""
    RP = operations.ResolvedPersona
    prof = _persona(operations, grounding=False)
    assert RP(intent="triage", profile=prof).capabilities.grounding is False


def test_declared_grounding_wins(operations):
    """An explicit ``grounding: true`` declaration turns the spine on."""
    RP = operations.ResolvedPersona
    prof = _persona(operations, grounding=True)
    assert RP(intent="triage", profile=prof).capabilities.grounding is True


# --------------------------------------------------------- prompt resolution

def test_prompt_only_persona_returns_prompt_no_preflight(operations, monkeypatch):
    """A prompt-only persona returns its prompt and never runs preflight."""
    calls = []
    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.triage_preflight",
        lambda target, user_message=None, emit=None: calls.append(target),
    )
    storage = _Storage()
    prof = _persona(operations, grounding=False)
    entity = {"module": "ztpf_templates", "uuid": "tmpl-1"}
    system, case_state = operations._resolve_system_prompt(
        "triage", entity, storage, "s1", 1, profile=prof)
    assert system == "PERSONA PROMPT"
    assert case_state is None
    assert calls == []


def test_grounding_persona_grounds_on_own_prompt_and_continues(operations, monkeypatch):
    """Exit criterion: a grounding-capable persona on a non-triage module runs
    preflight (turn 1), grounds on its OWN prompt (not the triage scenario), and
    continues the case across turns (turn 2 skips preflight, appends the
    continuation directive)."""
    calls = []

    def fake_preflight(target, user_message=None, emit=None):
        calls.append(target)
        # A triage scenario prompt the persona must NOT adopt as its own.
        return {"system": "TRIAGE SCENARIO PROMPT",
                "scenario": "generic", "indicators": {"ips": ["1.1.1.1"]}}

    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.triage_preflight", fake_preflight)

    storage = _Storage()
    prof = _persona(operations, grounding=True)
    entity = {"module": "ztpf_templates", "uuid": "tmpl-1"}

    # Turn 1: preflight runs, grounding set to the PERSONA prompt.
    system1, cs1 = operations._resolve_system_prompt(
        "triage", entity, storage, "s2", 1, user_message="help", profile=prof)
    assert calls == ["ztpf_templates:tmpl-1"]
    assert system1 == "PERSONA PROMPT"
    assert "TRIAGE SCENARIO PROMPT" not in system1
    assert cs1.grounding is not None
    assert cs1.grounding.system_prompt == "PERSONA PROMPT"
    assert cs1.grounding.indicators == {"ips": ["1.1.1.1"]}
    storage.save_case_state("s2", cs1.to_dict())

    # Turn 2: same record, cached grounding, no preflight, continuation appended.
    calls.clear()
    system2, cs2 = operations._resolve_system_prompt(
        "triage", entity, storage, "s2", 2, user_message="more", profile=prof)
    assert calls == []
    assert system2.startswith("PERSONA PROMPT")
    assert "ongoing case you have already triaged" in system2


def test_keystore_authored_persona_reaches_grounding_and_scoped_tools(
        operations, monkeypatch):
    """P5 exit criterion, in-process: a persona authored purely as a Key Store
    record (no connector code) — declaring capabilities.grounding + a tool
    allowlist — resolves to a scoped tool set AND grounds across turns."""
    from fsr_soc_triage.profiles import parse_profile_record

    # The record an author would create in the Key Store UI.
    record = {
        "key": "fsr_assistant_profile:ir_runbook",
        "value": "You are the IR Runbook assistant. Ground in the case.",
        "jSONValue": {
            "version": 1,
            "tools": {"allow": ["get_record", "search_module_records"]},
            "capabilities": {"grounding": True},
        },
    }
    profile = parse_profile_record(record, "ir_runbook")
    assert profile is not None

    # Scoped tool set: exactly the allowlist, from the full catalog, no code.
    persona = operations.ResolvedPersona(intent="triage", profile=profile)
    tool_names = {t["name"] for t in operations._tools_for_persona(persona)}
    assert tool_names == {"get_record", "search_module_records", "test_template"}  # + persona-implied

    # Grounding: preflight runs turn 1, continuation carries turn 2.
    calls = []
    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.triage_preflight",
        lambda target, user_message=None, emit=None: (
            calls.append(target) or {"system": "IGNORED SCENARIO",
                                     "scenario": "generic", "indicators": {}}))
    storage = _Storage()
    entity = {"module": "ir_runbook", "uuid": "case-9"}
    s1, cs1 = operations._resolve_system_prompt(
        "triage", entity, storage, "irs", 1, profile=profile)
    assert calls == ["ir_runbook:case-9"]
    assert s1 == "You are the IR Runbook assistant. Ground in the case."
    storage.save_case_state("irs", cs1.to_dict())
    s2, _ = operations._resolve_system_prompt(
        "triage", entity, storage, "irs", 2, profile=profile)
    assert "ongoing case you have already triaged" in s2


def test_grounding_persona_preflight_failure_still_grounds(operations, monkeypatch):
    """When preflight raises for a grounding persona, grounding still gets
    established on the persona prompt so continuation carries to the next turn
    (a built-in triage turn, by contrast, falls back to the static prompt)."""
    def boom(target, user_message=None, emit=None):
        raise RuntimeError("record fetch failed")

    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.triage_preflight", boom)

    storage = _Storage()
    prof = _persona(operations, grounding=True)
    entity = {"module": "ztpf_templates", "uuid": "tmpl-1"}

    system1, cs1 = operations._resolve_system_prompt(
        "triage", entity, storage, "s3", 1, profile=prof)
    assert system1 == "PERSONA PROMPT"
    assert cs1.grounding is not None
    assert cs1.grounding.system_prompt == "PERSONA PROMPT"
    storage.save_case_state("s3", cs1.to_dict())

    system2, cs2 = operations._resolve_system_prompt(
        "triage", entity, storage, "s3", 2, profile=prof)
    assert "ongoing case you have already triaged" in system2
