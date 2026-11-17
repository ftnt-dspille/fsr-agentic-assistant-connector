"""Phase 2.4 — unify the build-completion path.

A free-form NEW-playbook authoring turn that *narrates* its finished YAML
(prose + a ```yaml fence) instead of calling `emit_playbook_offer` used to
dead-end: the analyst got a design and no Deploy button. `_salvage_build_offer`
is the deterministic backstop — it synthesizes the same `playbook_offer` card
the tool would have emitted and appends it, turning `end_turn` into
`awaiting_playbook_offer`. These tests pin that behavior plus its exemptions
(triage intent, open-playbook edit, already-carded turns)."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def ops():
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


# A playbook that passes verify_playbook -- the offer verifies the bytes it
# carries, so salvage can only ever surface a playbook that clears the gate.
_YAML = (
    "playbooks:\n"
    "  - name: Block malicious IP\n"
    "    steps:\n"
    "      - name: Start\n"
    "        type: start\n"
    "        module: alerts\n"
    "        next: Block source\n"
    "      - name: Block source\n"
    "        type: connector\n"
    "        connector: fortigate-firewall\n"
    "        operation: block_ip\n"
)

# The same playbook before the edit: Start only. `_YAML` adds "Block source" to
# it -- a real edit. (A stub like `playbooks: [{name: Existing}]` is not one:
# replacing it deletes the open playbook, and the salvage now verifies.)
_OPEN_PB = (
    "playbooks:\n"
    "  - name: Block malicious IP\n"
    "    steps:\n"
    "      - name: Start\n"
    "        type: start\n"
    "        module: alerts\n"
)

# A card-less build turn: a visible verify call, then the model just stopped.
_PROSE_TURN = [
    {"type": "tool_use", "id": "tu-verify", "name": "verify_playbook"},
    {"type": "tool_result", "tool_use_id": "tu-verify",
     "content": {"ok": True}},
    {"type": "text", "text": "Here's the finished playbook. ```yaml ...```"},
]


def _salvage(ops, transcript, **kw):
    defaults = dict(intent="build", entity=None, last_assistant_yaml=_YAML,
                    session_id="sess-1", turn_idx=3)
    defaults.update(kw)
    return ops._salvage_build_offer(transcript, **defaults)


def test_prose_build_turn_gets_a_deploy_offer(ops):
    out = _salvage(ops, list(_PROSE_TURN))
    assert out is not None
    card = out[-1]
    assert card["type"] == "playbook_offer"
    # Carries the final YAML so accept compiles + pushes deterministically.
    assert card["final_yaml"] == _YAML
    assert card["id"] == "offer-sess-1-3"
    # Nested under the last visible (non-emit_) call — the verify step.
    assert card["preceding_tool_use_id"] == "tu-verify"
    # The salvaged card is terminal → the turn now lands on the awaiting reason.
    assert ops._envelope_stop_reason(out, "end_turn") == "awaiting_playbook_offer"


def test_display_summary_parsed_from_yaml(ops):
    out = _salvage(ops, list(_PROSE_TURN))
    labels = [s.get("label") for s in out[-1].get("ops_summary", [])]
    assert "Block source" in labels


def test_yaml_that_fails_verify_is_not_salvaged(ops):
    """Live: the model listed blocking problems in prose and stopped; the
    backstop then offered that YAML anyway, and the saved playbook ran only its
    first step. A step with no route in never runs -- no Deploy button."""
    orphaned = _YAML.replace("        next: Block source\n", "")
    assert _salvage(ops, list(_PROSE_TURN), last_assistant_yaml=orphaned) is None


def test_start_end_draft_is_not_salvaged(ops):
    """Live: "I want to create a new playbook." got a Save card for a start ->
    end draft. Nothing to save means no Deploy button -- the turn asks."""
    empty = ("playbooks:\n  - name: test1\n    steps:\n"
             "      - {name: Start, type: start, next: End}\n"
             "      - {name: End, type: end}\n")
    assert _salvage(ops, list(_PROSE_TURN), last_assistant_yaml=empty) is None


def test_a_choice_card_is_left_alone(ops):
    """The model asked through a choice card: the analyst answers next."""
    asked = list(_PROSE_TURN) + [{"type": "choice_card", "id": "q1"}]
    assert _salvage(ops, asked) is None


def test_triage_intent_is_not_salvaged(ops):
    assert _salvage(ops, list(_PROSE_TURN), intent="triage") is None


def test_open_playbook_edit_gets_an_enhancement_offer_not_a_playbook_offer(ops):
    """Editing an OPEN playbook must never produce a `playbook_offer` (that
    would compile a DUPLICATE and leave the open one untouched) -- but it must
    not dead-end either.

    The old contract was "exempt, never salvage", on the reasoning that a raw
    YAML fence is fine because the analyst's Save pushes it over the open
    record. That holds on the designer page and is false in chat, where a fence
    is text with no Save attached. Live repro: "fix the step name j, call it
    get joke" authored a correct rename, called only `verify_enhancement`,
    persisted nothing, and closed with "I renamed the step ... and updated all
    references". The playbook was unchanged. The analyst now gets the
    enhancement_offer's Apply, which writes through update_playbook -- carrying
    the bytes verify_enhancement passed, not a retyped snippet."""
    entity = {"playbook_yaml": _OPEN_PB}
    out = _salvage(ops, list(_PROSE_TURN), entity=entity)
    assert out is not None, "an open-playbook edit turn must not dead-end"
    card = out[-1]
    assert card["type"] == "enhancement_offer"
    assert card.get("verified_id"), "the card must carry verified bytes"
    assert card["final_yaml"].strip() == _YAML.strip()
    assert not any(w.get("type") == "playbook_offer" for w in out), \
        "an offer here would compile a duplicate playbook"


def test_open_playbook_edit_with_no_actual_change_is_not_carded(ops):
    """A turn that only DESCRIBED an edit without altering the document gets no
    Apply button -- an identical before/after is a fake one."""
    entity = {"playbook_yaml": _YAML}
    assert _salvage(ops, list(_PROSE_TURN), entity=entity) is None


def test_open_playbook_edit_that_already_carded_is_left_alone(ops):
    """The model carded the edit itself -- don't double-card."""
    entity = {"playbook_yaml": "playbooks:\n  - name: Existing\n"}
    carded = list(_PROSE_TURN) + [{"type": "enhancement_offer", "id": "e1"}]
    assert _salvage(ops, carded, entity=entity) is None


def test_no_yaml_anywhere_is_not_salvaged(ops):
    # No prose YAML AND no YAML in any tool call → nothing to salvage.
    bare = [
        {"type": "tool_use", "id": "tu-1", "name": "get_step_type"},
        {"type": "tool_result", "tool_use_id": "tu-1", "content": "info"},
        {"type": "text", "content": "I need more info."},
    ]
    assert _salvage(ops, bare, last_assistant_yaml=None) is None
    assert _salvage(ops, bare, last_assistant_yaml="   ") is None


def test_yaml_recovered_from_verify_playbook_call(ops):
    """When the model ends with prose (no YAML fence) but DID feed YAML
    into verify_playbook / validate_yaml, salvage recovers it from the
    tool call and synthesizes the offer (#1).
    """
    transcript = [
        {"type": "tool_use", "id": "tu-verify", "name": "verify_playbook",
         "input": {"yaml_text": _YAML}},
        {"type": "tool_result", "tool_use_id": "tu-verify",
         "content": {"ok": False, "ready_to_push": False}},
        {"type": "text",
         "content": "SendGrid is not configured. Which connector do you have?"},
    ]
    out = _salvage(ops, transcript, last_assistant_yaml=None)
    assert out is not None
    card = out[-1]
    assert card["type"] == "playbook_offer"
    assert card["final_yaml"] == _YAML
    assert card["preceding_tool_use_id"] == "tu-verify"


def test_yaml_recovered_from_validate_yaml_call(ops):
    """Same recovery when the YAML is in a validate_yaml call (no
    verify_playbook ran)."""
    transcript = [
        {"type": "tool_use", "id": "tu-validate", "name": "validate_yaml",
         "input": {"yaml_text": _YAML}},
        {"type": "tool_result", "tool_use_id": "tu-validate",
         "content": {"ok": True}},
        {"type": "text", "content": "I need to check which connectors are available."},
    ]
    out = _salvage(ops, transcript, last_assistant_yaml=None)
    assert out is not None
    card = out[-1]
    assert card["type"] == "playbook_offer"
    assert card["final_yaml"] == _YAML


def test_last_yaml_carrier_wins(ops):
    """When multiple tool calls carry YAML, the most recent one is used."""
    yaml_v1 = _YAML.replace("Block malicious IP", "V1")
    yaml_v2 = _YAML.replace("Block malicious IP", "V2")
    transcript = [
        {"type": "tool_use", "id": "tu-v1", "name": "validate_yaml",
         "input": {"yaml_text": yaml_v1}},
        {"type": "tool_result", "tool_use_id": "tu-v1", "content": {"ok": True}},
        {"type": "tool_use", "id": "tu-v2", "name": "verify_playbook",
         "input": {"yaml_text": yaml_v2}},
        {"type": "tool_result", "tool_use_id": "tu-v2",
         "content": {"ok": False}},
        {"type": "text", "content": "Need to reconfigure."},
    ]
    out = _salvage(ops, transcript, last_assistant_yaml=None)
    assert out is not None
    assert out[-1]["final_yaml"] == yaml_v2


def test_already_carded_turn_is_left_alone(ops):
    # The model DID offer (transcript already terminates in a gating card):
    # salvage must not double-append.
    carded = list(_PROSE_TURN) + [{"type": "playbook_offer", "id": "x"}]
    assert _salvage(ops, carded) is None


def test_errored_turn_is_left_alone(ops):
    errored = list(_PROSE_TURN) + [{"type": "error", "message": "boom"}]
    assert _salvage(ops, errored) is None


_ELIDED = "collection: 00-Workshop\n... (truncated) ... }'\n    next: do_we_want\n"


def test_model_elided_yaml_never_becomes_an_apply_button(ops):
    """The Apply button WRITES. A live 0.5.117 rename turn passed
    verify_enhancement a document the MODEL had elided itself
    (`... (truncated) ...`), and verify_enhancement duly "verified" the stub.
    Offering that as a patch would one-click-truncate the analyst's playbook."""
    assert not ops._is_whole_playbook_yaml(_ELIDED)
    assert not ops._is_whole_playbook_yaml("")
    assert not ops._is_whole_playbook_yaml("playbooks: []")
    assert not ops._is_whole_playbook_yaml("playbooks:\n  - name: X\n")  # no steps
    assert ops._is_whole_playbook_yaml(_YAML), "a real document must pass"


def test_elided_enhancement_diff_is_not_used_as_the_patch(ops):
    transcript = [
        {"type": "tool_use", "id": "tu-ve", "name": "verify_enhancement",
         "input": {"before_yaml": _ELIDED, "after_yaml": _ELIDED}},
        {"type": "tool_result", "tool_use_id": "tu-ve", "content": {"ok": True}},
        {"type": "text", "content": "I renamed the step."},
    ]
    assert ops._enhancement_diff(transcript) is None
    # ...and with no usable YAML anywhere, the turn is left alone rather than
    # offered a patch that would truncate the playbook.
    out = _salvage(ops, transcript, entity={"playbook_yaml": _YAML},
                   last_assistant_yaml=_ELIDED)
    assert out is None


def test_whole_enhancement_diff_is_used_as_the_patch(ops):
    # A value edit. (Renaming the playbook reads to verify as the open one
    # disappearing, and an unasked step rename is `step_renamed_silently`.)
    after = _YAML.replace("        module: alerts\n",
                          "        module: alerts\n        button_label: Block it\n")
    transcript = [
        {"type": "tool_use", "id": "tu-ve", "name": "verify_enhancement",
         "input": {"before_yaml": _YAML, "after_yaml": after}},
        {"type": "tool_result", "tool_use_id": "tu-ve", "content": {"ok": True}},
        {"type": "text", "content": "Renamed."},
    ]
    out = _salvage(ops, transcript, entity={"playbook_yaml": "playbooks:\n  - name: Other\n    steps: [{name: s}]\n"},
                   last_assistant_yaml=None)
    assert out is not None
    card = out[-1]
    assert card["type"] == "enhancement_offer"
    # The model's own verify_enhancement pair, not the mounted entity.
    assert card["final_yaml"].strip() == after.strip()


def test_read_only_turn_is_never_salvaged(ops):
    """An explain / find-issues turn runs read_only=True. Even if it printed a
    full, verifying YAML fence, it must not be turned into an offer: the analyst
    would get an Apply button for a change they never requested."""
    out = _salvage(ops, list(_PROSE_TURN), read_only=True)
    assert out is None
    # And with an open playbook (the enhancement path) it is likewise refused.
    out2 = _salvage(ops, list(_PROSE_TURN), read_only=True,
                    entity={"playbook_yaml": _OPEN_PB})
    assert out2 is None
