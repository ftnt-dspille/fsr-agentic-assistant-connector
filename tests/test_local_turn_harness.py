"""PL -- local turn harness CI gate (Persona Spine Unification, phase PL).

Deterministic, network-free assertions on the REAL spine driven fully
in-process by scripts/local_turn.py with the fake provider. A wrong tool
slice or a dropped prompt fragment fails HERE, before any model call -- the
fast-fail backbone the whole plan iterates against.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import local_turn as lt  # noqa: E402


def _turn(**kw):
    kw.setdefault("messages", [{"role": "user", "content": "go"}])
    kw.setdefault("llm", "fake")
    return lt.local_turn(**kw)


def test_triage_no_persona_full_slice():
    """Built-in triage (no persona) exposes the hunt/enrich/containment
    surface and a substantial system prompt -- driven in-process, no box."""
    res = _turn(intent="triage", module="incidents", persona="none")
    assert res.tools, "fake provider should have captured the tool slice"
    names = set(res.tools)
    # Hunt + containment tools present; Phase 3 full-surface means the
    # authoring tools ride along (the disposition prompt keeps focus).
    assert {"get_record", "search_module_records", "run_op", "find"} <= names
    assert {"validate_yaml", "verify_playbook"} <= names
    assert len(res.system or "") > 5000, "triage prompt should be substantial"


def test_build_no_persona_advertises_only_the_playbook_tools():
    """A build turn uses the playbook tools: dispatch refuses the
    investigation surface there (not_a_playbook_tool), so it is not advertised
    either -- showing it cost a refused round per call."""
    res = _turn(intent="build", module=None, persona="none")
    names = set(res.tools or [])
    assert "run_op" not in names and "get_record" not in names
    assert {"find", "get_op_schema", "verify_playbook"} <= names
    assert "test_template" not in names  # persona-allowlist-only survives


def test_persona_fixture_narrows_slice_and_swaps_prompt():
    """A Key Store persona (served from the local fixture) overrides BOTH the
    tool slice (allowlist) and the system prompt -- the whole point of the
    persona path, proven without a SOAR."""
    res = _turn(intent="triage", module="ztpf_templates", persona="fixture")
    assert res.tools == ["get_record", "search_module_records", "test_template"]  # + persona-implied
    # Persona prompt, not the triage monolith. Assert the PROPERTY (the monolith
    # is gone) rather than a byte ceiling: the prompt legitimately grows when a
    # cross-cutting block is added -- the GROUND-TRUTH RULES appended to every
    # prompt already pushed this past a hardcoded 500, which said nothing about
    # whether the monolith leaked back in.
    assert "scoped assistant" in (res.system or "")
    assert "find_containment_actions" not in (res.system or "")
    # A quarter, not a tenth: the triage prompt itself was cut by more than half
    # (W3 prompt diet), so the same persona prompt is a larger share of it.
    assert len(res.system or "") < len(
        _turn(intent="triage", module="incidents", persona="none").system) / 4


def test_persona_custom_allowlist_is_honored():
    """An arbitrary persona allowlist is applied verbatim (subset of shipped
    tools)."""
    fixture = {
        "hydra:member": [
            lt._keys_member(
                "alerts", prompt="Alert desk persona.",
                tools_allow=["get_record", "run_op", "emit_action_card"]),
        ],
        "hydra:totalItems": 1,
    }
    res = _turn(intent="triage", module="alerts", persona="fixture",
                persona_fixture=fixture)
    # `emit_action_card` is retired behind the emit_card union; an allowlist
    # authored against the old surface must still grant the capability.
    assert set(res.tools or []) == {"get_record", "run_op", "emit_card", "test_template"}
    assert "Alert desk persona." in (res.system or "")


def test_no_network_in_fake_mode(no_inet_socket):
    """The cassette is the only 'client'; a write attempt would raise. Confirm
    reads are served locally and no live client leaks in."""
    # Start cold: source availability is memoized for a minute, so whether the
    # turn reads the connector inventory used to depend on test order.
    from fsr_soc_triage.source_availability import forget
    forget()
    res = _turn(intent="triage", module="incidents", persona="fixture")
    assert res.cassette_gets, "persona lookup should have hit the cassette"
    assert no_inet_socket == [], f"offline turn dialled out: {no_inet_socket}"
    # Every read is cassette-served or on the work list: the persona and
    # skills lookups -- the assistant_personas module (P0), the Key Store, the
    # assistant_skills house rules -- and the connector inventory the
    # source-availability check reads. Anything else must be an unserved miss,
    # which is a recorded gap, not a live client.
    _PERSONA_READS = ("/api/3/keys", "/api/3/assistant_personas", "/api/3/assistant_skills",
                      "/api/integration/connector_details")
    unexpected = [g for g in res.cassette_gets
                  if not any(p in g for p in _PERSONA_READS)
                  and g not in res.cassette_misses]
    assert unexpected == [], unexpected


def _env(res):
    """The chat_turn envelope dict local_turn hands back on `.transcript`."""
    return res.transcript


def _wire(env):
    """The chat_turn envelope's wire transcript (list of card/event dicts)."""
    return (env or {}).get("transcript") or []


def test_build_turn_narrating_yaml_lands_on_a_deploy_offer():
    """§2.4 end-to-end, in-process: the intent-aware fake drives a BUILD turn
    that verifies + narrates its YAML in a ```yaml fence but never calls
    emit_playbook_offer. The connector's _salvage_build_offer must synthesize the
    deploy card, so the envelope lands on `awaiting_playbook_offer` with a
    playbook_offer carrying `final_yaml` -- the dead-end §2.4 closes. Box-free."""
    env = _env(_turn(intent="build", module=None, persona="none",
                     messages=[{"role": "user", "content": "design a block-IP playbook"}]))
    assert env["stop_reason"] == "awaiting_playbook_offer", env["stop_reason"]
    card = _wire(env)[-1]
    assert card["type"] == "playbook_offer"
    assert card.get("final_yaml"), "salvaged offer must carry the YAML to deploy"
    assert "Block malicious IP" in card["final_yaml"]
    # Nested under the visible verify_playbook call the fake made.
    assert card.get("preceding_tool_use_id")


def test_open_playbook_edit_lands_on_an_enhancement_offer_not_a_playbook_offer():
    """Editing an OPEN playbook (entity.playbook_yaml present) must never
    synthesize a `playbook_offer` -- that compiles a DUPLICATE and leaves the
    open playbook untouched.

    It must not end on a bare fence either. That was the old contract ("the raw
    fence is correct, Save updates in place"), true on the designer page and
    false in chat, where a fence is text with no Save attached. A live turn
    ("fix the step name j, call it get joke") authored a correct rename, called
    only verify_enhancement, persisted NOTHING, and told the analyst the rename
    was done. The turn now halts on an enhancement_offer carrying the verified
    bytes."""
    env = _env(_turn(intent="build", module="workflows", persona="none",
                     entity={"module": "workflows",
                             # The fake model's YAML adds "Block source" to this
                             # (a stub here would read as deleting the playbook).
                             "playbook_yaml": ("playbooks:\n  - name: Block malicious IP\n"
                                               "    steps:\n      - name: Start\n"
                                               "        type: start\n        module: alerts\n")},
                     messages=[{"role": "user", "content": "tweak the open playbook"}]))
    assert env["stop_reason"] == "awaiting_enhancement_offer", env["stop_reason"]
    wired = _wire(env)
    assert not any(w.get("type") == "playbook_offer" for w in wired), \
        "an offer on an open playbook would compile a duplicate"
    card = wired[-1]
    assert card["type"] == "enhancement_offer"
    assert card.get("verified_id") and card.get("final_yaml")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# --------------------------------------------------------------------------
# Model resolution must be immune to mid-run os.environ mutation.
#
# A tool-using turn calls `fsr_soc_triage.tools_agent._load_repo_env_once()`,
# which setdefaults the framework repo's `.env` into os.environ so the MCP
# server can find credentials. That file also carries FRANK_MODEL/OPENAI_MODEL,
# so the FIRST turn in a process rewrote the model every later turn resolved.
#
# Observed: turn 1 succeeded on the right model, then every subsequent turn in
# the process 403'd ("Provider 'gb200' is not allowed for this virtual key").
# A multi-scenario harness read that as "the model fell apart after scenario 1"
# -- a manufactured model defect. local_turn now snapshots at import.
# --------------------------------------------------------------------------


def test_model_is_snapshotted_at_import_not_read_per_call(monkeypatch):
    """Poison os.environ the way a turn does; resolution must not follow it."""
    monkeypatch.setenv("FRANK_MODEL", "gb200/glm-5.2-fp8")   # the stale id
    monkeypatch.setenv("OPENAI_MODEL", "gb200/gpt-oss-120b")
    # The snapshot was taken at import, before any poisoning could happen.
    assert lt._ENV_MODELS_AT_IMPORT.get("FRANK_MODEL") != "gb200/glm-5.2-fp8"
    assert lt._llm_env().get("FRANK_MODEL") != "gb200/glm-5.2-fp8"


class _ConfigCaptured(Exception):
    """Raised by the chat_turn spy once the harness has built its config."""


def _frank_config_the_turn_would_use(monkeypatch) -> dict:
    """Run the harness's frank path up to the connector call and return the
    config it hands chat_turn. The spy stops the turn before any model call."""
    import importlib
    ops = importlib.import_module("connector_fsr_agentic_assistant.operations")
    seen = {}

    def _spy(config, params):
        seen["config"] = config
        raise _ConfigCaptured

    monkeypatch.setattr(ops, "chat_turn", _spy)
    monkeypatch.setenv("FRANK_BASE_URL", "https://gateway.example.test/v1")
    monkeypatch.setenv("FRANK_API_KEY", "frank-test-key")
    with pytest.raises(_ConfigCaptured):
        lt.local_turn(messages=[{"role": "user", "content": "triage this"}],
                      llm="frank")
    return seen["config"]


def test_no_live_env_read_in_model_resolution(monkeypatch):
    """A FRANK_MODEL that a previous turn leaked into the process env must not
    change the model this turn runs on: the config resolves from the import-time
    snapshot, never from a fresh os.environ read (which is what regressed)."""
    stale = "gb200/glm-5.2-fp8"
    monkeypatch.setenv("FRANK_MODEL", stale)
    monkeypatch.setenv("OPENAI_MODEL", "gb200/gpt-oss-120b")
    config = _frank_config_the_turn_would_use(monkeypatch)
    # The snapshot (or the resolver's default when none was exported at
    # import), in the resolver's pick order.
    from fsr_playbooks.harness.llm import DEFAULT_FRANK_MODEL
    snap = lt._ENV_MODELS_AT_IMPORT
    assert config["openai_model"] == (snap.get("FRANK_MODEL")
                                      or snap.get("OPENAI_MODEL")
                                      or DEFAULT_FRANK_MODEL)


def test_the_local_harness_opts_out_of_persona_models(monkeypatch):
    """Offline evals stay on the harness's chosen model: a persona's stronger
    model must not be swapped in on the frank/openai path."""
    config = _frank_config_the_turn_would_use(monkeypatch)
    assert config["persona_models"] is False


# --------------------------------------------------------------------------
# Unserved cassette reads (test-realism plan, Phase 1.1).
#
# The cassette's failure mode is SILENCE: an unmatched GET returns the same
# empty hydra body a legitimately-empty search returns, so "the fixture is
# missing" and "there is genuinely nothing there" are indistinguishable in the
# response. That is the vacuous-grader defect one layer down -- a triage row
# can grade clean having asked for data nobody answered. The only place that
# knows the difference is the rule-matching loop, so it records it there.
# --------------------------------------------------------------------------


def test_cassette_records_which_gets_matched_no_rule():
    s = lt._CassetteSession([("/api/3/alerts", {"hydra:member": [{"a": 1}]})])
    s.get("https://box/api/3/alerts?$limit=1")
    s.get("https://box/api/3/incidents/abc")
    assert s.gets == ["https://box/api/3/alerts?$limit=1",
                      "https://box/api/3/incidents/abc"]
    assert s.misses == ["https://box/api/3/incidents/abc"]


def test_a_served_but_empty_read_is_not_a_miss():
    """The distinction the whole mechanism exists for: a rule that answers with
    an empty result is SERVED. Counting it as a miss would put fixtures we
    already wrote back on the capture work list."""
    s = lt._CassetteSession([("/api/3/alerts", {"hydra:member": [],
                                                "hydra:totalItems": 0})])
    s.get("https://box/api/3/alerts?$limit=1")
    assert s.misses == []


def test_turn_result_exposes_misses():
    res = _turn(module="alerts", intent="triage")
    assert isinstance(res.cassette_misses, list)
    assert set(res.cassette_misses) <= set(res.cassette_gets)


# ---- cassette connector inventory ----------------------------------------
@pytest.fixture
def canned_inventory():
    """Apply the cassette inventory patch inside a patch scope, undone after."""
    from fsr_playbooks.llm.tools import REGISTRY
    with lt.patch_scope() as scope:
        lt._patch_configured_connectors_for_cassette(scope)
        yield REGISTRY


def test_cassette_box_says_an_absent_connector_is_not_configured(canned_inventory):
    """An offline scenario can only test "this box lacks X" if the box says so.
    Before the inventory, every listing hit the discard port and the agent
    could not tell an absent connector from an unreachable API."""
    listing = canned_inventory["list_configured_connectors"].fn()
    names = {c["name"] for c in listing["configured"]}
    assert "crowd-strike-falcon" not in names
    health = canned_inventory["connector_health"].fn("crowd-strike-falcon")
    assert health["ok"] is False
    assert health["code"] == "connector_not_configured"


def test_cassette_box_offers_real_containment(canned_inventory):
    """The consolidated `find` reads the listing as a module global, so this
    also proves the patch reaches it rather than only the registry entry."""
    from fsr_playbooks.mcp_server.tools_find import find
    r = find(kind="action", action_type="containment", target_type="host")
    connectors = {a["connector"] for a in r["actions"]}
    assert connectors & {"fortinet-fortiedr", "fortigate-firewall"}, r
    assert "crowd-strike-falcon" not in connectors


# --- modules every box has answer "none found", not 404 ------------------------
# build_plain_request_no_record + every known-answer world: GET incidents 404'd,
# and the agent concluded the incidents module does not exist.
def test_an_unanswered_incidents_collection_is_empty_not_missing():
    s = lt._CassetteSession([])
    r = s.get("https://box/api/3/incidents?$limit=10&$search=1.2.3.4")
    assert r.status_code == 200 and r.json()["hydra:member"] == []
    assert s.misses == []


def test_a_record_get_on_incidents_is_still_a_miss():
    s = lt._CassetteSession([])
    s.get("https://box/api/3/incidents/abc?$relationships=true")
    assert s.misses == ["https://box/api/3/incidents/abc?$relationships=true"]


def test_a_rule_still_wins_over_the_empty_default():
    s = lt._CassetteSession([("/api/3/incidents", {"hydra:member": [{"x": 1}]})])
    assert s.get("https://box/api/3/incidents?$limit=1").json()["hydra:member"] == [{"x": 1}]


# --- hygiene plan G5: the fake harness polluted the operator's store --------
# 5,000+ `verify_playbook(yaml=...)` rows from `pl-*` sessions sat in
# ~/.connector_fsr_agentic_assistant.db: the fake scripted a wrong argument name,
# took the build branch on every turn (it keyed on the retired
# `emit_action_card`), and wrote into the default store.

def test_a_fake_triage_turn_takes_the_triage_branch():
    env = _env(_turn(intent="triage", module="incidents", persona="none"))
    assert env["stop_reason"] == "end_turn", env["stop_reason"]
    assert "spine assembled" in json.dumps(env)
    assert not [c for c in _wire(env) if c.get("type") == "playbook_offer"]


def test_the_fake_verify_call_matches_the_tool_s_contract():
    from fsr_playbooks.llm import arg_gate
    from fsr_playbooks.llm.tools import REGISTRY
    spec = REGISTRY.get("verify_playbook")
    assert arg_gate.check("verify_playbook", spec.input_schema, spec.fn,
                          {"yaml_text": lt._FAKE_BUILD_YAML}) is None
    # ...and the old spelling is exactly what the gate now refuses.
    assert arg_gate.check("verify_playbook", spec.input_schema, spec.fn,
                          {"yaml": lt._FAKE_BUILD_YAML}) is not None
    import inspect
    src = inspect.getsource(lt._install_fake_provider)
    assert '"yaml_text": _FAKE_BUILD_YAML' in src


def test_fake_runs_never_write_the_operator_store(monkeypatch, tmp_path):
    monkeypatch.delenv("FSR_PLAYBOOK_BUILDER_DB", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with lt.patch_scope() as scope:
        lt._isolate_fake_session_store(scope)
        store = os.environ["FSR_PLAYBOOK_BUILDER_DB"]
    assert "pl-fake-store-" in store
    assert not store.startswith(str(tmp_path))


def test_an_explicit_store_wins(monkeypatch, tmp_path):
    pinned = str(tmp_path / "mine.db")
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", pinned)
    with lt.patch_scope() as scope:
        lt._isolate_fake_session_store(scope)
        assert os.environ["FSR_PLAYBOOK_BUILDER_DB"] == pinned


def test_a_gateway_turn_without_an_endpoint_is_refused_not_defaulted(monkeypatch):
    """Non-fake LLMs resolve through the shared resolver: a missing endpoint is
    a refusal, never a silent fallback to some other host."""
    for k in ("FRANK_BASE_URL", "OPENAI_ENDPOINT", "FRANK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="needs a base URL"):
        lt.resolve_llm_for_turn("frank")
