"""`live_writes` grading, box-free.

The runner writes to a real box; what must hold without one is how it judges
a write. Above all: a value saved shorter than the call sent it is a failure.
That is the defect this tier was built for (cards executed the 600-character
preview of a template script) and it shipped past every offline tier.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts import live_oracles as LO  # noqa: E402
from scripts import live_writes as LW  # noqa: E402

SCRIPT = "config system interface\n" + "".join(f'edit "port{i}"\nnext\n' for i in range(80)) + "end"


def test_a_script_saved_cut_short_fails():
    call = {"module": "ztpf_templates", "fields": {"name": "T", "script": SCRIPT}}
    saved = {"name": "T", "script": SCRIPT[:600]}
    probs = LW.write_problems(call, saved, None)
    assert len(probs) == 1 and probs[0].startswith("script: sent")
    assert f"({len(SCRIPT)} chars)" in probs[0]


def test_a_write_that_holds_what_was_sent_passes():
    call = {"fields": {"name": "T", "script": SCRIPT, "type": "Text",
                       "ztpfMetadataSources": ["/api/3/ztpf_metadata_sources/7a12bb5f-aaaa"]}}
    saved = {"name": "T", "script": SCRIPT,
             "type": {"@id": "/api/3/picklists/1234", "itemValue": "Text"},
             "ztpfMetadataSources": [{"@id": "/api/3/ztpf_metadata_sources/7a12bb5f-aaaa",
                                      "name": "DeviceDB System Interfaces - VLAN"}]}
    assert LW.write_problems(call, saved, None) == []


def test_a_link_list_missing_one_fails():
    call = {"fields": {"ztpfMetadataSources": ["/api/3/ztpf_metadata_sources/a",
                                               "/api/3/ztpf_metadata_sources/b"]}}
    saved = {"ztpfMetadataSources": [{"@id": "/api/3/ztpf_metadata_sources/a"}]}
    assert LW.write_problems(call, saved, None)


def test_an_update_that_changes_a_field_it_did_not_name_fails():
    before = {"name": "Src", "datakey": "old", "script": "{}", "modifyDate": 1}
    call = {"uuid": "u", "fields": {"datakey": "ha_statistics"}}
    saved = {"name": "Src", "datakey": "ha_statistics", "script": "", "modifyDate": 2}
    probs = LW.write_problems(call, saved, before)
    assert probs == ["script: not in the call, but changed from '{}' to ''"]


def test_updates_only_land_on_records_the_row_may_change():
    named = [{"module": "ztpf_metadata_sources", "uuid": "8f58c58e"},
             {"module": "ztpf_automation_profile_steps", "any": True}]
    assert LW._matches_named({"uuid": "8f58c58e-d11f"}, "ztpf_metadata_sources", named)
    assert not LW._matches_named({"uuid": "7d762bbe-580d"}, "ztpf_metadata_sources", named)
    assert LW._matches_named({"uuid": "x"}, "ztpf_automation_profile_steps", named)
    assert not LW._matches_named({"uuid": "x"}, "ztpf_templates", [])


def test_rows_are_the_writing_ones_and_the_live_only_ones():
    rows, _ = LW.load_rows(None, [])
    names = {r["name"] for r in rows}
    assert "ztpf_author_template_like_loopback_vlan" in names
    assert "ztpf_author_render_interface_descriptions" in names
    assert "ztpf_author_test_template" not in names, "a read-only row has nothing to read back"


def test_mounted_update_rows_say_so():
    """A row whose ask is to change the open record must allow it, or the
    runner rejects the very update the row asks for."""
    rows, _ = LW.load_rows(None, ["ztpf_author_metadata_update_two_fields"])
    assert rows and rows[0].get("may_change_mounted") is True


def test_the_oracle_is_plain_python_over_device_data():
    ints = [{"name": "port2", "ip": ["0.0.0.0", "0.0.0.0"]},
            {"name": "port1", "ip": ["10.0.0.1", "255.255.255.0"], "alias": "wan",
             "allowaccess": ["ping", "https"]},
            {"name": "lan", "ip": ["10.1.0.1", "255.255.255.0"], "allowaccess": ""}]
    assert LO.physical_interface_descriptions(ints, "DEV") == [
        "config system interface",
        'edit "lan"', 'set description "lan on DEV"', "next",
        'edit "port1"', 'set description "wan on DEV"', "set allowaccess ping https", "next",
        "end"]


def test_rendered_text_is_read_from_its_heading():
    q = "Template Rendered\n\nTemplate Results\n```text\nconfig system interface\n\n  end\n```"
    assert LO.lines(LO.code_block_after(q, "Template Results")) == ["config system interface", "end"]


def test_a_one_value_tag_saved_as_a_list_is_the_same_value():
    assert LW.same_value("Firmware", ["Firmware"])
    assert not LW.same_value("Firmware", ["Firmware", "Upgrade"])


class _FakeFSR:
    """chat_resume answers a batched card with two executed writes."""
    def __init__(self):
        self.client = self

    def get(self, path, params=None):
        uid = path.rsplit("/", 1)[-1]
        return {"uuid": uid, "name": uid, "stepSequence": {"s1": 2, "s2": 1}[uid]}

    def execute(self, op, params, timeout=0):
        frames = [{"type": "tool_result", "tool_use_id": f"cardexec_{i}",
                   "content": {"ok": True, "action": "update", "module": "steps", "uuid": u}}
                  for i, u in enumerate(("s1", "s2"))]
        frames.append({"type": "tool_result", "tool_use_id": "model-1",
                       "content": {"ok": False, "code": "not_found", "module": "steps"}})
        return type("R", (), {"data": {"ok": True, "transcript": frames}})()


def test_every_write_a_batched_card_runs_is_read_back():
    arc = LW.Arc.__new__(LW.Arc)
    arc.fsr, arc.log, arc.problems, arc.writes = _FakeFSR(), lambda s: None, [], []
    arc.created, arc.restore, arc.sid = [], [], "s"
    arc.transport = LW._ArcTransport(arc.fsr, "s", arc.problems)
    arc.may_change = [{"module": "steps", "any": True}]
    arc.calls = {"t1": {"name": "update_record", "input": {"module": "steps", "uuid": "s1",
                                                          "fields": {"stepSequence": 2}}},
                 "t2": {"name": "update_record", "input": {"module": "steps", "uuid": "s2",
                                                          "fields": {"stepSequence": 1}}}}
    card = {"id": "c", "tool": "update_record", "preceding_tool_use_id": "t1",
            "target": {"module": "steps", "uuid": "s1"},
            "batch": [{"tool": "update_record", "args": {"module": "steps", "uuid": "s2",
                                                         "fields": {"stepSequence": 1}}}]}
    arc._action(card)
    assert [w["uuid"] for w in arc.writes] == ["s1", "s2"]
    assert arc.problems == []


def test_links_sent_in_the_read_tools_shape_match_their_stubs():
    sent = [{"iri": "/api/3/ztpf_metadata_sources/a"}, {"iri": "/api/3/ztpf_metadata_sources/b"}]
    saved = [{"@id": "/api/3/ztpf_metadata_sources/b", "name": "B"},
             {"@id": "/api/3/ztpf_metadata_sources/a", "name": "A"}]
    assert LW.same_value(sent, saved)
    assert LW.same_value(["VLAN", "testnew"], ["testnew", "VLAN"])
    assert not LW.same_value(["VLAN"], ["testnew"])


class _BoxAfterAPlaybook:
    """A step a playbook renumbered: no tool_use names this write."""
    def __init__(self):
        self.client, self.puts = self, []

    def get(self, path, params=None):
        return {"uuid": "s1", "name": "Pre-Upgrade Routing Table", "stepSequence": 1,
                "stepARGS": "", "modifyDate": 9}

    def put(self, path, data):
        self.puts.append((path, data))

    def delete(self, path):
        raise AssertionError("nothing was created")


def test_cleanup_puts_back_what_a_playbook_changed():
    arc = LW.Arc.__new__(LW.Arc)
    arc.fsr, arc.log, arc.created, arc.restore = _BoxAfterAPlaybook(), lambda s: None, [], []
    arc.snapshot = [("steps", {"uuid": "s1", "name": "Pre-Upgrade Routing Table",
                               "stepSequence": 2, "stepARGS": "", "modifyDate": 1})]
    arc.cleanup()
    assert arc.fsr.puts == [("/api/3/steps/s1", {"stepSequence": 2})]


class _ScriptedBox:
    """A box whose chat_turn halts on an approval and whose chat_resume finishes."""
    def __init__(self):
        from types import SimpleNamespace
        self._ns = SimpleNamespace
        self.client = SimpleNamespace(get=lambda path, params=None: {"uuid": "s1", "name": "Step"})
        self.sent = []

    def execute(self, op, params, timeout=0):
        self.sent.append((op, dict(params)))
        if op == "chat_turn":
            data = {"ok": True, "stop_reason": "approval_required", "transcript": [
                {"type": "tool_use", "id": "t1", "name": "run_playbook", "input": {}},
                {"type": "approval_request", "approval_id": "ap1", "tool": "run_playbook"}]}
        else:
            data = {"ok": True, "stop_reason": "end_turn",
                    "transcript": [{"type": "text", "text": "Done."}]}
        return self._ns(data=data, error=None)


def test_the_arc_answers_an_approval_through_the_shared_driver():
    """The arc's turn loop is drive(): a halted turn is approved, the approved
    envelope is recorded, and its reply is what the arc reports as said."""
    box = _ScriptedBox()
    spec = {"name": "approve-one", "intent": "triage",
            "mount": {"module": "steps", "uuid": "s1"}, "steps": [{"user": "run it"}]}
    arc = LW.Arc(box, spec, None, lambda s: None)
    arc.run()
    resumes = [p for op, p in box.sent if op == "chat_resume"]
    assert resumes and resumes[0]["approval_id"] == "ap1" and resumes[0]["decision"] == "approve"
    assert arc.tools == ["run_playbook"]
    assert arc.said == "Done."
    assert arc.problems == []
