"""The offline designer runs the connector's REAL staged Apply.

Only box I/O is stubbed (scripts/offline_designer.py), so what this proves is
the connector's own: a verified edit stages with a restore point and hands
back its YAML, and the pre-write loss guard still refuses an edit that drops a
step the canvas has.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for p in (_SCRIPTS, _SCRIPTS.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from offline_designer import offline_designer, staged_yaml  # noqa: E402

CANVAS = """collection: SOC Response
playbooks:
- name: test
  steps:
  - {name: Start, type: start, module: alerts, next: Note}
  - {name: Note, type: set_variable, vars: {a: "1"}}
"""
EDITED = CANVAS.replace('vars: {a: "1"}}', 'vars: {a: "1"}, next: More}') + \
    '  - {name: More, type: set_variable, vars: {b: "2"}}\n'
DROPPED = """collection: SOC Response
playbooks:
- name: test
  steps:
  - {name: Start, type: start, module: alerts}
"""
UUID = "00000000-0000-0000-0000-00000000abcd"


@pytest.fixture(scope="module")
def ops():
    import local_turn
    local_turn._bootstrap()
    from connector_fsr_agentic_assistant import operations
    return operations


class _Store:
    def __init__(self, card):
        self.card = card

    def get_card(self, sid, cid):
        return self.card

    def __getattr__(self, name):          # every other storage call is a no-op
        return lambda *a, **k: None


def _accept(ops, final_yaml):
    card = {"type": "enhancement_offer", "id": "e1", "final_yaml": final_yaml,
            "steps_added": ["More"], "steps_modified": []}
    with offline_designer(ops, lambda: CANVAS) as log:
        res = ops._resume_enhancement_offer_accept(
            {}, {"session_id": "s", "offer_id": "e1", "apply_to": "canvas",
                 "workflow_iri": f"/api/3/workflows/{UUID}", "workflow_uuid": UUID},
            _Store(card))
    return res, log


def test_a_verified_edit_stages_with_a_restore_point_and_its_yaml(ops):
    res, log = _accept(ops, EDITED)
    assert res["tags"]["code"] == "staged", res
    assert log["snapshots"] == 1
    assert staged_yaml(res["transcript"]) == EDITED


def test_the_real_loss_guard_still_refuses_a_silent_drop(ops):
    res, log = _accept(ops, DROPPED)
    assert res["tags"].get("code") != "staged", res["tags"]
    assert staged_yaml(res["transcript"]) is None
    assert log["snapshots"] == 0          # refused before the restore point


def test_staged_yaml_reads_the_wire_envelope_chat_resume_returns():
    """chat_resume returns {ok, ..., transcript: [...]}, not the frame list.
    Reading only a list missed every staged card, and the simulated analyst
    was told its canvas was empty after two Applies that had landed."""
    card = {"type": "info_card", "variant": "playbook_staged", "yaml": EDITED}
    assert staged_yaml({"ok": True, "transcript": [card]}) == EDITED
    assert staged_yaml([card]) == EDITED
    assert staged_yaml({"ok": True, "transcript": []}) is None


def test_a_requested_rename_on_a_designer_mounted_playbook_applies(ops):
    """Live (analyst sim, a real shipped playbook): 'rename the manual task to
    Approve IPv4 Block' was refused by the pre-write guard on every Apply --
    it identified steps by name, so a rename read as a deletion. With the
    designer's mount (step uuids) the same edit stages."""
    from fsr_playbooks.mcp_server._shared import reset_grounded_yaml, set_grounded_yaml
    from fsr_playbooks.mcp_server.tools_enhancement import edit_playbook
    from offline_designer import designer_mount
    canvas = designer_mount((_SCRIPTS / "eval_fixtures" /
                             "real_manual_input_block_ipv4.yaml").read_text())
    tok = set_grounded_yaml(canvas)
    try:
        res = edit_playbook([{"op": "rename_step",
                              "name": "Manual Task to Block IPv4 Addresses",
                              "to": "Approve IPv4 Block"}],
                            user_message="rename the manual task to 'Approve IPv4 Block'")
    finally:
        reset_grounded_yaml(tok)
    after = res.get("after_yaml")
    assert after and "Approve IPv4 Block" in after, (res.get("code"), res.get("message"))
    card = {"type": "enhancement_offer", "id": "e1", "final_yaml": after,
            "steps_added": [], "steps_modified": ["Approve IPv4 Block"]}
    with offline_designer(ops, lambda: canvas):
        out = ops._resume_enhancement_offer_accept(
            {}, {"session_id": "s", "offer_id": "e1", "apply_to": "canvas",
                 "workflow_iri": f"/api/3/workflows/{UUID}", "workflow_uuid": UUID},
            _Store(card))
    assert out["tags"]["code"] == "staged", [f.get("text") for f in out["transcript"]]
