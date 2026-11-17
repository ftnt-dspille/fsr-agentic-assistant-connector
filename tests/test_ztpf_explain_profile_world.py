"""`ztpf_real_explain_profile` grades depth: did the agent go past each step's
action to the step's RUNS, where the results live?

A live "Explain this profile" answer listed each action's name and IRI, said
"all stepARGS blank" and stopped -- while step 4's latest runs were failing.
Two things made it stop, and these pin both, so the row cannot quietly go
vacuous on a re-capture:

- the world must actually HOLD runs for every action the mounted profile uses
  (the first capture held none of them: it took one device's history, and these
  steps ran later), or "never read the runs" is unanswerable either way;
- the profiles persona must say where a step's results live. It used to route
  every action through `ztpfTemplates`, which only 2 of 25 live actions have,
  so on the rest the model had nowhere further to go.
"""
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from scripts.fixture_box import load_bundle  # noqa: E402

ROW = _REPO / "scripts/conversations/sweep/ztpf_real_explain_profile.json"


def _iri(v):
    return v if isinstance(v, str) else (v or {}).get("@id", "")


def _world():
    row = json.loads(ROW.read_text())
    return row, load_bundle(row["records"])


def test_every_action_of_the_mounted_profile_has_runs_in_the_world():
    row, world = _world()
    m = world["modules"]
    profile = f"/api/3/ztpf_automation_profiles/{row['mount']['uuid']}"
    steps = [s for s in m["ztpf_automation_profile_steps"]
             if _iri(s.get("ztpfAutomationProfiles")) == profile]
    assert len(steps) >= 2, "the mounted profile lost its steps"
    actions = {_iri(s.get("ztpfAutomationActions")) for s in steps}
    have = {a.get("@id") for a in m["ztpf_automation_actions"]}
    assert actions <= have, f"steps run actions the world lacks: {actions - have}"
    runs = {}
    for r in m["ztpf_device_automation_steps"]:
        runs.setdefault(_iri(r.get("ztpfAutomationActions")), []).append(r)
    missing = [a for a in actions if not runs.get(a)]
    assert not missing, f"no runs in the world for {missing}"
    # The row's discriminating fact: a run whose result says it failed.
    msgs = " ".join(str(r.get("outputStatusMessage") or "")
                    for a in actions for r in runs[a]).lower()
    assert "failed to parse" in msgs


def test_profiles_persona_points_past_the_action_to_its_runs():
    row, world = _world()
    persona = world["personas"]["ztpf_automation_profiles"]["value"]
    assert "ztpf_device_automation_steps" in persona
    assert "outputStatusMessage" in persona


def test_quick_action_row_asks_what_the_button_sends():
    """The quick-action row copies the persona's prompt; an edit to one
    without the other would grade a button nobody can press."""
    _, world = _world()
    ui = world["personas"]["ztpf_automation_profiles"]["jSONValue"]["ui"]
    sent = next(q["prompt"] for q in ui["quickActions"] if q["key"] == "explain")
    row = json.loads((ROW.parent / "ztpf_explain_profile_quick_action.json").read_text())
    assert row["steps"][0]["user"] == sent


def test_profiles_persona_offers_list_page_cards():
    # The drawer over the profiles LIST shows ui.listQuickActions (the widget
    # whitelists listQuickActions/listLabel/listNoun/recordNoun). Each card
    # needs the {key,label,sub,prompt} shape runQuickAction sends.
    bundle = json.loads((_REPO / "scripts/eval_fixtures/ztpf/ztpf_world_live.json").read_text())
    ui = bundle["personas"]["ztpf_automation_profiles"]["jSONValue"]["ui"]
    cards = ui["listQuickActions"]
    assert [c["key"] for c in cards] == ["new_profile", "failing_profiles", "recent_runs"]
    for c in cards:
        assert all(isinstance(c.get(k), str) and c[k] for k in ("key", "label", "sub", "prompt"))
    assert ui["recordNoun"] == "profile" and ui["listNoun"] == "profiles"
