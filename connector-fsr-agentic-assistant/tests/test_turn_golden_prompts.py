"""Golden system-prompt snapshots for every turn assembly path (Phase 2 Step 0).

Characterization only: nothing here changes production code. Each scenario
drives the REAL `chat_turn` or `_resume_conversation` with the agent loop
stubbed, captures what the loop was handed (the assembled system prompt, the
advertised tool names, the turn tags and max_tool_turns) and compares it to a
committed snapshot under tests/golden/prompts/.

The snapshots are the byte-stability contract for the prompt that the provider
caches as its (tools + system) prefix. A refactor that reorders or re-words a
suffix shows up here as a visible diff. To accept an intentional change:

    UPDATE_GOLDEN=1 make test-unit   # or the scoped pytest run

and review the snapshot diff line by line. A missing snapshot is a failure,
never a silent write.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN_DIR = ROOT / "tests" / "golden" / "prompts"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"

SESSION_PREFIX = "golden-"
PINNED_TODAY = "2026-10-21 (Wednesday)"


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


class _Result:
    transcript = []
    tags = {}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


def _tool_name(tool) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name"))
    return str(getattr(tool, "name", tool))


def _snapshot(kw: dict) -> dict:
    """The part of a run_agent_turn call the golden files pin."""
    return {
        "system": kw["system"],
        "tools": sorted(_tool_name(t) for t in (kw.get("tools") or [])),
        "tags": kw.get("tags"),
        "max_tool_turns": kw.get("max_tool_turns"),
    }


def _framework_off_pin() -> str | None:
    """Why the loaded fsr_playbooks is not the release requirements.txt pins, or None.

    The goldens track the PINNED framework (that is what ships and what CI installs). A dev venv imports the
    framework as an editable checkout, which moves ahead of the pin on every framework merge and drifts the
    prompt (tool descriptions come from the framework). Only a git checkout can be off the pin: a wheel is the
    pin, so CI and the box always compare."""
    import subprocess
    import fsr_playbooks
    pin = None
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        if line.strip().startswith("fsr-playbooks=="):
            pin = line.split("==", 1)[1].split("#")[0].strip()
    src = Path(fsr_playbooks.__file__).resolve().parent
    git = lambda *a: subprocess.run(["git", "-C", str(src), *a], capture_output=True, text=True)  # noqa: E731
    if not pin or git("rev-parse", "--git-dir").returncode != 0:
        return None
    tags = git("tag", "--points-at", "HEAD").stdout.split()
    if f"v{pin}" not in tags:
        return f"the framework checkout at {src.parent} is not at the pinned release v{pin}"
    if git("status", "--porcelain", "--", ".").stdout.strip():
        return f"the framework checkout at {src.parent} has uncommitted changes on top of v{pin}"
    return None


def _check_golden(name: str, actual: dict) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    text = json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
        return
    assert path.exists(), (
        f"no golden snapshot {path}; run once with UPDATE_GOLDEN=1 and commit it")
    expected = path.read_text(encoding="ascii")
    if text != expected:
        off_pin = _framework_off_pin()
        if off_pin:
            pytest.skip(f"golden prompt drift, but {off_pin}: the goldens track the pin. "
                        "Check out the pinned tag (or install the wheel) to compare.")
    assert text == expected, (
        f"golden prompt drift for {name}: the assembled turn changed. "
        f"Review the diff, then regenerate with UPDATE_GOLDEN=1 if intended.")


@pytest.fixture
def harness(operations, tmp_path, monkeypatch):
    """Patch the agent loop, the storage singleton and the provider; return a
    driver that runs one scenario and hands back the captured call."""
    import fsr_playbooks.llm.run_turn as run_turn
    ops_chat = sys.modules["_fsrpb_conn.ops_chat"]
    ops_turn_state = sys.modules["_fsrpb_conn.ops_turn_state"]
    storage_mod = sys.modules["_fsrpb_conn.storage"]

    # The turn-context block states today's date; pin it so the snapshots do
    # not drift at midnight UTC.
    monkeypatch.setenv("FSRPB_TODAY", PINNED_TODAY)
    store = storage_mod.Storage(tmp_path / "golden.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda *a, **k: False)
    monkeypatch.setattr(ops_chat, "_build_provider",
                        lambda config, gateway: object())
    monkeypatch.setattr(sys.modules["_fsrpb_conn.turn_sink"], "log_llm_activity",
                        lambda config, rec: None)

    captured: list[dict] = []

    async def _fake_run_agent_turn(**kw):
        captured.append(_snapshot(kw))
        return _Result()

    async def _fake_resume_agent_turn(**kw):
        captured.append({"resumed": True})
        return _Result()

    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_run_agent_turn)
    monkeypatch.setattr(run_turn, "resume_agent_turn", _fake_resume_agent_turn)
    ops_turn_state._PROFILE_CACHE.clear()
    ops_turn_state._PROFILE_NEG_TS.clear()
    ops_turn_state._PROFILE_DEGRADED_TS.clear()

    class _Harness:
        pass

    h = _Harness()
    h.operations = operations
    h.ops_chat = ops_chat
    h.ops_turn_state = ops_turn_state
    h.store = store
    h.monkeypatch = monkeypatch
    h.captured = captured

    def chat(session: str, **params):
        body = {"session_id": SESSION_PREFIX + session,
                "messages": [{"role": "user", "content": params.pop("text", "triage this")}]}
        body.update(params)
        operations.chat_turn({}, body)
        assert len(captured) == 1, "chat_turn did not reach the agent loop once"
        return captured[-1]

    def resume(session: str, **kwargs):
        sid = SESSION_PREFIX + session
        store.save_conversation(sid, [{"role": "user", "content": "hi"}])
        operations._resume_conversation(
            {}, {"session_id": sid, "card_id": kwargs.pop("card_id", None),
                 "choice_id": kwargs.pop("choice_id", None),
                 "value": kwargs.pop("value", None)},
            kwargs.pop("decision"), store, provider=object(),
            user_msg=kwargs.pop("user_msg", None),
            resume_tag=kwargs.pop("resume_tag", None),
            turn_directive=kwargs.pop("turn_directive", None))
        assert len(captured) == 1, "resume did not reach the agent loop once"
        return captured[-1]

    h.chat = chat
    h.resume = resume
    return h


# --- chat_turn scenarios -------------------------------------------------

def test_golden_chat_triage_ungrounded(harness):
    snap = harness.chat("triage-ungrounded", intent="triage")
    _check_golden("chat__triage-ungrounded", snap)


def test_golden_chat_triage_grounded_cached_casestate(harness):
    from fsr_playbooks.agent import case_state as cs
    key = "alerts:alert-1"
    state = cs.CaseState().for_record(key)
    state.grounding = cs.Grounding(
        scenario_id="golden_scenario",
        system_prompt="CACHED GROUNDED TRIAGE PROMPT.",
        indicators={"ips": ["198.51.100.7"]},
        computed_at=time.time(),
    )
    harness.store.save_case_state(SESSION_PREFIX + "triage-cached", state.to_dict())
    snap = harness.chat("triage-cached", intent="triage",
                        entity={"module": "alerts", "uuid": "alert-1",
                                "fields": {"severity": "high"}})
    _check_golden("chat__triage-grounded-cached-casestate", snap)


def test_golden_chat_build_quick_action(harness):
    snap = harness.chat("build-qa", intent="build", quick_action="add_step",
                        text="add an error handler after step 2")
    _check_golden("chat__build-quick-action", snap)


def test_golden_chat_persona(harness, monkeypatch):
    from fsr_soc_triage.profiles import Profile
    persona = Profile(module="alerts", prompt="PERSONA PROMPT FOR ALERTS.",
                      may_write=["alerts"], tools_allow=["get_record"])
    monkeypatch.setattr("fsr_playbooks.mcp_server._shared._live_client",
                        lambda: object())
    monkeypatch.setattr("fsr_soc_triage.profiles.resolve_profile_status",
                        lambda client, module: (persona, True))
    snap = harness.chat("persona", intent="triage",
                        entity={"module": "alerts"})
    _check_golden("chat__persona", snap)


def test_golden_chat_persona_degraded(harness):
    key = (SESSION_PREFIX + "persona-degraded", "alerts")
    harness.ops_turn_state._PROFILE_DEGRADED_TS[key] = (
        harness.ops_turn_state._profile_clock())
    snap = harness.chat("persona-degraded", intent="triage",
                        entity={"module": "alerts"})
    _check_golden("chat__persona-degraded", snap)


def test_golden_chat_read_only_chip(harness):
    snap = harness.chat("read-only", intent="build", quick_action="explain",
                        read_only=True,
                        text="explain what this playbook does")
    _check_golden("chat__read-only-chip", snap)


def test_golden_chat_triage_unattended(harness):
    snap = harness.chat("unattended", intent="triage",
                        actor={"system": True, "name": "auto-triage"})
    assert snap["tags"].get("unattended") is True
    _check_golden("chat__triage-unattended", snap)


# --- _resume_conversation scenarios -------------------------------------

def test_golden_resume_conversational_reject(harness):
    snap = harness.resume("resume-reject", decision="reject",
                          user_msg="No, do not run that. Try a narrower query.")
    _check_golden("resume__conversational-reject", snap)


def test_golden_resume_choice(harness):
    harness.store.set_session_intent(SESSION_PREFIX + "resume-choice", "triage")
    snap = harness.resume("resume-choice", decision="choice",
                          choice_id="scope_pick", value="all_alerts")
    _check_golden("resume__choice", snap)


def test_golden_resume_after_exec_followup(harness):
    snap = harness.resume("resume-exec", decision="approve",
                          user_msg="The approved playbook ran. Summarise the outcome.")
    _check_golden("resume__after-exec-followup", snap)


def test_golden_resume_budget_deliver(harness):
    snap = harness.resume("resume-budget", decision="choice",
                          choice_id="_budget_ask", value="deliver")
    assert snap["tools"] == [], "budget deliver must strip the tool list"
    _check_golden("resume__budget-deliver", snap)


def test_golden_resume_persona(harness):
    from fsr_soc_triage.profiles import Profile
    sid = SESSION_PREFIX + "resume-persona"
    persona = Profile(module="alerts", prompt="PERSONA PROMPT FOR ALERTS.",
                      may_write=["alerts"], tools_allow=["get_record"])
    harness.store.set_session_module(sid, "alerts")
    harness.ops_turn_state._store_turn_profile(harness.store, sid, persona)
    snap = harness.resume("resume-persona", decision="approve",
                          user_msg="Continue with the approved update.")
    assert snap["system"].startswith("PERSONA PROMPT FOR ALERTS."), (
        "the stored persona prompt must be authoritative on resume")
    _check_golden("resume__persona", snap)


# --- the prompt is rendered for the slice the turn advertises (A1.5) -------

def _named(text: str, name: str) -> bool:
    import re
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                     text) is not None


def test_guided_build_prompt_names_no_tool_the_slice_dropped(harness):
    """A Guided create drops the free-text authoring tools; the system prompt
    the loop is handed must not tell the model to call them (it did, and every
    such call was refused). Asserts the relationship: dropped = freeform slice
    minus guided slice, each absent from the guided prompt."""
    from fsr_playbooks.llm.intents import FREEFORM_AUTHORING_TOOLS
    snap = harness.chat("build-guided", intent="build", create_strategy="guided",
                        text="build a playbook that blocks an IP on the firewall")
    dropped = set(FREEFORM_AUTHORING_TOOLS) - set(snap["tools"])
    assert dropped, "guided slice dropped nothing; the check would be vacuous"
    assert "<!--" not in snap["system"], "unrendered slice block reached the model"
    leaked = sorted(n for n in dropped if _named(snap["system"], n))
    assert not leaked, f"guided prompt still names dropped tools: {leaked}"


def test_freeform_build_prompt_keeps_the_authoring_tools(harness):
    snap = harness.chat("build-freeform", intent="build",
                        create_strategy="freeform",
                        text="build a playbook that blocks an IP on the firewall")
    assert "<!--" not in snap["system"]
    assert "get_step_type" in snap["tools"]
    assert _named(snap["system"], "get_step_type")
