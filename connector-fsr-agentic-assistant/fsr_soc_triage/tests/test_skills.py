"""Skills = learned house rules stored as `assistant_skills` records.

These tests are offline — a stubbed ``get_with_reauth`` — and pin the fail-open
posture (a missing module or bad body yields no skills, never an error) plus the
scope/priority/enabled semantics the prompt injection relies on.
"""
from __future__ import annotations

from fsr_soc_triage import skills as sk


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Client:
    base_url = "https://box"


def _hydra(members):
    return _Resp(200, {"hydra:member": members})


def _rec(instruction, *, scope=None, enabled=True, priority=0, trigger="", uuid="u"):
    r = {"instruction": instruction, "enabled": enabled, "priority": priority,
         "trigger": trigger, "uuid": uuid}
    if scope is not None:
        r[sk.F_SCOPE] = scope
    return r


# ── parse_skill_record ──────────────────────────────────────────────────────
def test_parse_requires_instruction():
    assert sk.parse_skill_record({"trigger": "x"}) is None
    assert sk.parse_skill_record({"instruction": "   "}) is None
    assert sk.parse_skill_record("not a dict") is None


def test_parse_reads_fields():
    s = sk.parse_skill_record(_rec("do the thing", scope="ztpf_devices",
                                    priority=5, trigger="thing"))
    assert s is not None
    assert s.instruction == "do the thing"
    assert s.scope_module == "ztpf_devices"
    assert s.priority == 5 and s.trigger == "thing"


def test_enabled_coercion_from_picklist_string():
    assert sk.parse_skill_record(_rec("x", enabled="No")).enabled is False
    assert sk.parse_skill_record(_rec("x", enabled="false")).enabled is False
    assert sk.parse_skill_record(_rec("x", enabled="Yes")).enabled is True
    # absent → enabled (fail-open to applied)
    assert sk.parse_skill_record({"instruction": "x"}).enabled is True


def test_enabled_from_picklist_wrapper():
    s = sk.parse_skill_record({"instruction": "x",
                               "enabled": {"itemValue": "false"}})
    assert s.enabled is False


def test_blank_scope_is_global():
    assert sk.parse_skill_record(_rec("x", scope="  ")).scope_module is None


# ── load_skills ─────────────────────────────────────────────────────────────
def test_load_filters_by_scope_and_globals():
    resp = _hydra([
        _rec("device rule", scope="ztpf_devices", uuid="a"),
        _rec("other rule", scope="ztpf_templates", uuid="b"),
        _rec("global rule", scope=None, uuid="c"),
    ])
    out = sk.load_skills(_Client(), "ztpf_devices",
                         get_with_reauth=lambda c, u: resp)
    got = {s.instruction for s in out}
    assert got == {"device rule", "global rule"}, \
        "a skill applies when scoped to the module OR global; another module's is excluded"


def test_load_drops_disabled():
    resp = _hydra([_rec("on", uuid="a"), _rec("off", enabled=False, uuid="b")])
    out = sk.load_skills(_Client(), "ztpf_devices",
                         get_with_reauth=lambda c, u: resp)
    assert [s.instruction for s in out] == ["on"]


def test_load_sorts_by_priority_desc():
    resp = _hydra([
        _rec("low", priority=1, uuid="a"),
        _rec("high", priority=9, uuid="b"),
        _rec("mid", priority=5, uuid="c"),
    ])
    out = sk.load_skills(_Client(), "ztpf_devices",
                         get_with_reauth=lambda c, u: resp)
    assert [s.instruction for s in out] == ["high", "mid", "low"]


def test_load_fail_open_on_404():
    resp = _Resp(404, {})
    assert sk.load_skills(_Client(), "ztpf_devices",
                          get_with_reauth=lambda c, u: resp) == []


def test_load_fail_open_on_transport_error():
    def boom(c, u):
        raise RuntimeError("network down")
    assert sk.load_skills(_Client(), "ztpf_devices", get_with_reauth=boom) == []


def test_load_no_client_is_empty():
    assert sk.load_skills(None, "ztpf_devices",
                          get_with_reauth=lambda c, u: _hydra([])) == []


def test_load_url_targets_the_skill_module():
    seen = {}

    def capture(c, u):
        seen["url"] = u
        return _hydra([])
    sk.load_skills(_Client(), "ztpf_devices", get_with_reauth=capture)
    assert "/api/3/assistant_skills" in seen["url"]


# ── render_skills_block ─────────────────────────────────────────────────────
def test_render_empty_is_blank():
    assert sk.render_skills_block([], "ztpf_devices") == ""


def test_render_lists_instructions_and_marks_override():
    block = sk.render_skills_block(
        [sk.Skill(instruction="hide deprecated playbooks", trigger="pb filter")],
        "ztpf_devices")
    assert "Learned house rules" in block
    assert "hide deprecated playbooks" in block
    assert "OVERRIDE" in block
    assert "(pb filter)" in block


def test_skills_for_prompt_is_empty_on_failure():
    def boom(c, u):
        raise RuntimeError("x")
    assert sk.skills_for_prompt(_Client(), "ztpf_devices", get_with_reauth=boom) == ""
