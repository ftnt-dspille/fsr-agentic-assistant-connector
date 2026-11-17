"""`live_rows` row selection and grading, box-free.

The runner talks to a real box, so what has to hold without one is what it
picks and how it grades: it must never pick a row that writes, and a scrubbed
literal must not fail a live answer that carries the real value.
"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts import live_rows as LR  # noqa: E402


def test_rows_that_write_or_approve_are_never_picked():
    rows, skipped = LR.load_rows("ztpf_author", [])
    picked = {r["name"] for r in rows}
    assert "ztpf_author_test_template" in picked
    assert "ztpf_author_template_like_loopback_vlan" not in picked
    assert any("template_like_loopback_vlan" in s and "writes" in s for s in skipped)
    assert all("world_after" not in r["expect"] and not r.get("resume") for r in rows)


def test_a_scrubbed_literal_is_unchecked_not_failed():
    spec = {"expect": {"called": {"any_of": ["test_template"]},
                       "reports": {"all_of": ["SingleLo2", "192.0.2.2"]}}}
    g = LR.grade(spec, "edit SingleLo2 set ip 10.255.255.2/32", ["test_template"])
    assert g["ok"] is True
    assert g["unchecked"] == ["192.0.2.2"]


def test_a_missing_literal_or_tool_fails():
    spec = {"expect": {"called": {"any_of": ["search_module_records"]},
                       "reports": {"all_of": ["Upgrade Firmware"], "any_of": ["fail"]}}}
    assert LR.grade(spec, "the device looks healthy", ["search_module_records"])["ok"] is False
    assert LR.grade(spec, "Upgrade Firmware step: Fail", ["get_record"])["ok"] is False
    assert LR.grade(spec, "Upgrade Firmware step: Fail", ["search_module_records"])["ok"] is True


def test_called_as_a_plain_list_means_all_of():
    spec = {"expect": {"called": ["get_record", "test_template"]}}
    assert LR.grade(spec, "", ["get_record"])["ok"] is False
    assert LR.grade(spec, "", ["get_record", "test_template"])["ok"] is True


def test_none_of_fails_when_the_answer_says_it():
    spec = {"expect": {"reports": {"none_of": ["deprecated"]}}}
    assert LR.grade(spec, "Run the deprecated playbook", [])["ok"] is False
    assert LR.grade(spec, "Run the current playbook", [])["ok"] is True


# ---- drive: only an answered (or halted) turn is graded --------------------
# A provider failure comes back `ok: true` with `stop_reason: "error"`, and the
# runner used to grade that envelope's text as the model's answer.

_SPEC = {"name": "row", "intent": "triage",
         "mount": {"module": "alerts", "uuid": "u1"},
         "steps": [{"user": "what happened?"}]}


class _FakeFSR:
    def __init__(self, data):
        from types import SimpleNamespace
        self.client = SimpleNamespace(get=lambda path: {"name": "alert"})
        self._data, self._ns = data, SimpleNamespace

    def execute(self, op, params, timeout=None):
        return self._ns(data=self._data, error=None)


def test_an_error_stop_is_not_graded_as_an_answer():
    run = LR.run_row(_FakeFSR({"ok": True, "stop_reason": "error",
                             "transcript": [{"type": "text", "text": "boom"}]}), _SPEC)
    assert run["error"] and run["error"].startswith("provider_error")


def test_an_answer_is_graded():
    run = LR.run_row(_FakeFSR({"ok": True, "stop_reason": "end_turn",
                             "transcript": [{"type": "text", "text": "It was phishing."}]}), _SPEC)
    assert run["error"] is None and run["said"] == "It was phishing."


# ---- a row whose record is not on this box is skipped, not run -------------
# A row recorded against another box mounts a record this box lacks. A 404 at
# mount is not the agent failing, so the row must never reach the agent, and it
# must not count toward "rows clean".

def test_a_row_whose_record_is_absent_is_skipped_not_run(monkeypatch):
    from types import SimpleNamespace

    def _missing(path):
        raise RuntimeError("404 not found")

    box = SimpleNamespace(base="box", config="cfg", client=SimpleNamespace(get=_missing))
    ran: list[str] = []
    monkeypatch.setattr(LR.LiveFSR, "from_env", staticmethod(lambda: box))
    monkeypatch.setattr(LR, "run_row", lambda fsr, spec: ran.append(spec["name"]) or {
        "error": None, "tools": [], "said": "", "secs": 0})
    monkeypatch.setattr(sys, "argv", ["live_rows", "--scenario", "ztpf_author_test_template"])

    assert LR.main() == 2       # nothing was measured: not a pass
    assert ran == []            # and the agent was never asked
