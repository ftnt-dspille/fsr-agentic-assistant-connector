"""Closure: the connector's offer-salvage backstops record a firing.

`_salvage_build_offer` and `_salvage_enhancement_offer` card a turn the model
left uncarded. That is a guard in every sense that matters (it hides the prompt
or tool bug that made it necessary), but it lives in the connector, after the
framework's guard list was snapshotted. Each salvage therefore hands back its
transcript tagged with a mechanism (`ops_offers._salvaged`), and the chat path
puts it on `tags.guards_fired` next to the framework's.

Closed three ways, none a grep count:
* every `_salvage_*` function in ops_offers is known here, so a third salvage
  fails until it is classified;
* every non-None return of one goes through `_salvaged(<registered mechanism>, ...)`
  or another salvage function, never a bare list;
* both paths, driven for real, come back tagged, and the envelope writer
  carries the tag.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from connector_fsr_agentic_assistant import ops_offers as O

SRC = Path(O.__file__)

#: The salvage functions and the mechanism each records.
SALVAGES = {
    "_salvage_build_offer": O.SALVAGE_BUILD_OFFER,
    "_salvage_enhancement_offer": O.SALVAGE_ENHANCEMENT_OFFER,
}

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
_OPEN_PB = (
    "playbooks:\n"
    "  - name: Block malicious IP\n"
    "    steps:\n"
    "      - name: Start\n"
    "        type: start\n"
    "        module: alerts\n"
)
_PROSE_TURN = [
    {"type": "tool_use", "id": "tu-verify", "name": "verify_playbook"},
    {"type": "tool_result", "tool_use_id": "tu-verify", "content": {"ok": True}},
    {"type": "text", "text": "Here is the finished playbook."},
]


def _functions() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(SRC.read_text())
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_every_salvage_function_is_classified():
    found = {n for n in _functions() if n.startswith("_salvage_")}
    assert found == set(SALVAGES), (
        f"ops_offers has salvage functions {sorted(found)}; this test knows "
        f"{sorted(SALVAGES)}. A new salvage cards a turn the model left uncarded: "
        f"give it a mechanism in ops_offers.SALVAGE_MECHANISMS and a row here.")
    assert set(SALVAGES.values()) == set(O.SALVAGE_MECHANISMS)


@pytest.mark.parametrize("name", sorted(SALVAGES))
def test_every_non_none_return_is_tagged(name):
    fn = _functions()[name]
    bad = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Return) or n.value is None:
            continue
        v = n.value
        if isinstance(v, ast.Constant) and v.value is None:
            continue
        ok = (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
              and (v.func.id.startswith("_salvage_")
                   or (v.func.id == "_salvaged" and v.args
                       and isinstance(v.args[0], ast.Name)
                       and v.args[0].id in {"SALVAGE_BUILD_OFFER",
                                            "SALVAGE_ENHANCEMENT_OFFER"})))
        if not ok:
            bad.append(n.lineno)
    assert not bad, (f"{name} returns an untagged transcript at line(s) {bad}: "
                     f"return _salvaged(<mechanism>, wire_transcript + [card]) "
                     f"so the firing is recorded")


def test_a_new_card_salvage_is_recorded_as_build():
    out = O._salvage_build_offer(
        list(_PROSE_TURN), intent="build", entity=None,
        last_assistant_yaml=_YAML, session_id="s", turn_idx=1)
    assert out is not None and out[-1]["type"] == "playbook_offer"
    assert out.mechanism == O.SALVAGE_BUILD_OFFER


def test_an_open_playbook_edit_salvage_is_recorded_as_enhancement():
    out = O._salvage_build_offer(
        list(_PROSE_TURN), intent="build", entity={"playbook_yaml": _OPEN_PB},
        last_assistant_yaml=_YAML, session_id="s", turn_idx=1)
    assert out is not None and out[-1]["type"] == "enhancement_offer"
    assert out.mechanism == O.SALVAGE_ENHANCEMENT_OFFER


def test_a_salvaged_transcript_is_still_a_plain_list_to_every_reader():
    out = O._salvaged(O.SALVAGE_BUILD_OFFER, [{"type": "text"}])
    assert out == [{"type": "text"}] and out[0]["type"] == "text"
    assert list(out) == [{"type": "text"}]


def test_a_turn_that_needs_no_salvage_records_nothing():
    assert O._salvage_build_offer(
        list(_PROSE_TURN), intent="triage", entity=None,
        last_assistant_yaml=_YAML, session_id="s", turn_idx=1) is None


def test_the_chat_path_hands_the_mechanism_to_the_envelope():
    """The glue: ops_chat reads `.mechanism` off the salvaged transcript and
    passes it to `_with_guard_fires`. Source-level, because the chat path needs
    a full turn to reach: it must not drop the tag."""
    chat = (SRC.parent / "ops_chat.py").read_text()
    assert 'getattr(_salvaged, "mechanism", "")' in chat
    assert "_with_guard_fires(tags, result, _connector_fires)" in chat
