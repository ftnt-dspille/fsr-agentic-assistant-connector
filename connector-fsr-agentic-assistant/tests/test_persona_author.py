"""persona_author: the drafting engine behind the persona builder.

These pin the decisions that make a drafted persona trustworthy:
  * the drafter is grounded in the module's REAL shape (field names + relation
    targets), and NO raw record value ever reaches the prompt (hygiene);
  * a drafted record is linted with the tool catalog, so a hallucinated tool
    name is an error the approver sees, not a silently narrowed persona;
  * a junk reply yields no record rather than a broken one;
  * the save-time summary states the allowlist and write scope in plain words.
"""
from __future__ import annotations

import json

from fsr_soc_triage import persona_author as pa


class _FakeProvider:
    """Matches provider.stream(system=, messages=, tools=, tags=, max_tool_turns=),
    yielding the canned reply as two TextEvents (exercises accumulation)."""

    def __init__(self, reply: str, capture: dict | None = None):
        self._reply, self.capture = reply, capture

    async def stream(self, *, system, messages, tools, tags, max_tool_turns):
        from fsr_playbooks.llm.provider import TextEvent
        if self.capture is not None:
            self.capture["system"] = system
            self.capture["user"] = messages[0].content
            self.capture["tags"] = tags
        half = len(self._reply) // 2
        yield TextEvent(text=self._reply[:half])
        yield TextEvent(text=self._reply[half:])


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def get(self, path, params=None):
        return {"hydra:member": self._rows}


SECRET = "SuperSecretHostname-do-not-leak.local"
ROWS = [
    {"@id": "/api/3/alerts/u1", "name": "Alert one", "severity": "High",
     "sourceIp": SECRET,
     "incident": {"@id": "/api/3/incidents/i1", "name": "Inc"}},
    {"@id": "/api/3/alerts/u2", "name": "Alert two", "status": "Open",
     "assets": [{"@id": "/api/3/assets/a1"}]},
]


def _good_reply(tools):
    return "Here is the persona:\n" + json.dumps({
        "value": "You are the Alert Triage assistant for the alerts module. "
                 "You investigate; you do not author playbooks. Ground in the "
                 "alert on screen, read its linked incident and assets, then "
                 "report with the evidence.",
        "jSONValue": {
            "label": "Alert Triage",
            "tools": {"allow": tools},
            "may_write": [],
            "capabilities": {"grounding": True},
            "ui": {"label": "Alert Triage", "greeting": "Investigating {name}",
                   "placeholder": "Ask me about this alert"},
        },
    })


def test_gather_knowledge_derives_fields_and_relations_without_leaking_values():
    k = pa.gather_module_knowledge(_FakeClient(ROWS), "alerts")
    assert k["module"] == "alerts"
    # union of record keys, @-metadata excluded
    assert set(k["fields"]) == {"name", "severity", "sourceIp", "incident",
                                "status", "assets"}
    # relations resolved to their target module, one level into a list
    assert k["relations"] == {"incident": "incidents", "assets": "assets"}
    # hygiene: no raw record value is carried anywhere in the knowledge
    assert SECRET not in json.dumps(k)


def test_draft_persona_parses_and_validates_clean():
    known = {"get_record", "search_module_records"}
    prov = _FakeProvider(_good_reply(["get_record", "search_module_records"]))
    rec, issues = pa.draft_persona(prov, "alerts", "triage alerts",
                                   knowledge={"module": "alerts", "fields": ["name"],
                                              "relations": {}},
                                   known_tools=known)
    assert rec is not None and rec["value"].startswith("You are the Alert Triage")
    assert rec["jSONValue"]["tools"]["allow"] == ["get_record", "search_module_records"]
    assert [i for i in issues if i.level == "error"] == []


def test_draft_persona_lint_flags_a_hallucinated_tool():
    known = {"get_record"}  # catalog does NOT contain the invented tool
    prov = _FakeProvider(_good_reply(["get_record", "make_coffee"]))
    rec, issues = pa.draft_persona(prov, "alerts", "triage alerts",
                                   knowledge={"module": "alerts", "fields": [], "relations": {}},
                                   known_tools=known)
    errs = [i for i in issues if i.level == "error" and i.field == "tools.allow"]
    assert errs and "make_coffee" in errs[0].message


def test_draft_persona_returns_none_on_junk():
    prov = _FakeProvider("sorry, I cannot do that")
    rec, issues = pa.draft_persona(prov, "alerts", "x",
                                   knowledge={"module": "alerts", "fields": [], "relations": {}},
                                   known_tools=set())
    assert rec is None and issues == []


def test_user_brief_carries_knowledge_and_catalog_but_not_raw_values():
    cap: dict = {}
    prov = _FakeProvider(_good_reply(["get_record"]), capture=cap)
    knowledge = pa.gather_module_knowledge(_FakeClient(ROWS), "alerts")
    pa.draft_persona(prov, "alerts", "triage alerts", knowledge=knowledge,
                     known_tools={"get_record", "search_module_records"})
    brief = cap["user"]
    assert "sourceIp" in brief and "incident links to module incidents" in brief
    assert "get_record" in brief and "search_module_records" in brief
    assert cap["tags"] == {"intent": "persona_author"}
    # the field NAMES are in the brief; a raw value must never be
    assert SECRET not in brief


def test_summarize_states_allowlist_and_write_scope():
    rec = {"value": "p", "jSONValue": {"label": "Author",
           "tools": {"allow": ["get_record", "create_record"]},
           "may_write": ["ztpf_templates"], "capabilities": {"grounding": False}}}
    s = pa.summarize(rec)
    assert "get_record" in s and "create_record" in s
    assert "ztpf_templates" in s and "grounding): no" in s


def test_key_for_uses_the_profile_prefix():
    assert pa.key_for("alerts") == "fsr_assistant_profile:alerts"


# --- draft_persona_update: fold a taught instruction into an existing persona ---

def _update_reply(prompt_text, tools):
    return "updated:\n" + json.dumps({
        "value": prompt_text,
        "jSONValue": {"label": "Alert Triage", "tools": {"allow": tools},
                      "may_write": [], "ui": {"label": "Alert Triage"}},
    })


EXISTING = {
    "value": "You triage alerts. Investigate and cite evidence.",
    "jSONValue": {"label": "Alert Triage", "tools": {"allow": ["get_record"]},
                  "may_write": [], "ui": {"label": "Alert Triage"}},
}


def test_update_folds_the_instruction_and_validates_clean():
    known = {"get_record", "search_module_records"}
    folded = ("You triage alerts. Investigate and cite evidence. "
              "ALWAYS check prior cases before deciding a verdict.")
    prov = _FakeProvider(_update_reply(folded, ["get_record", "search_module_records"]))
    rec, issues = pa.draft_persona_update(
        prov, "alerts", EXISTING, "Always check prior cases before you decide a verdict.",
        knowledge={"module": "alerts", "fields": ["name"], "relations": {}},
        known_tools=known)
    assert rec is not None
    assert "prior cases" in rec["value"].lower()
    assert "investigate and cite evidence" in rec["value"].lower()  # kept the old guidance
    assert [i for i in issues if i.level == "error"] == []


def test_update_with_no_existing_persona_creates_one():
    prov = _FakeProvider(_update_reply("You triage alerts, checking prior cases first.",
                                       ["get_record"]))
    rec, issues = pa.draft_persona_update(
        prov, "alerts", None, "Check prior cases first.",
        knowledge={"module": "alerts", "fields": [], "relations": {}},
        known_tools={"get_record"})
    assert rec is not None and "prior cases" in rec["value"].lower()


def test_update_brief_carries_the_instruction_and_current_guide():
    cap = {}
    prov = _FakeProvider(_update_reply("x check prior cases", ["get_record"]), capture=cap)
    pa.draft_persona_update(prov, "alerts", EXISTING, "Check prior cases first.",
                            knowledge={"module": "alerts", "fields": [], "relations": {}},
                            known_tools={"get_record"})
    brief = cap["user"]
    assert "Check prior cases first." in brief
    assert "Investigate and cite evidence" in brief  # the current guide is shown
    assert cap["tags"] == {"intent": "persona_update"}


def test_update_lint_flags_a_hallucinated_tool():
    prov = _FakeProvider(_update_reply("x", ["get_record", "make_coffee"]))
    rec, issues = pa.draft_persona_update(
        prov, "alerts", EXISTING, "do x",
        knowledge={"module": "alerts", "fields": [], "relations": {}},
        known_tools={"get_record"})
    errs = [i for i in issues if i.level == "error" and i.field == "tools.allow"]
    assert errs and "make_coffee" in errs[0].message
