"""The connector's tools are held to ONE argument contract: the input_schema
each advertises, built from its typed signature, checked by the framework's
dispatch gate.

A second pydantic copy of every signature (`arg_validation.TOOL_ARG_MODELS`)
used to re-check the call inside the tool. Copies drift: the parity test that
policed them asserted both pydantic layers accepted `relationships` as a name
list while the schema gate, which runs first, refused it. Replayed over 34,336
recorded calls the copy refused 2 calls the schema passed (`siem_search
by="id"`, a list-of-lists `filters`); both gaps are now closed in the signature
itself, and these tests pin them through the real dispatch stages.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from fsr_playbooks.llm import tools as T  # noqa: E402

from fsr_soc_triage.registry import register_triage_tools  # noqa: E402

register_triage_tools()


def _stages(name, args):
    """What dispatch's argument stages say (None = the call would proceed)."""
    c = T._Call(name=name, spec=T.REGISTRY[name], args=dict(args), internal=False,
                session_id=None, approved_by=None, summary=None)
    out = T._coerce_args(c) or T._check_args(c)
    return out, c.args


# --- the shapes the agent emits that must pass --------------------------------

CANONICAL = [
    ("get_record", {"module": "ztpf_devices", "uuid": "u", "relationships": True}),
    ("get_record", {"module": "ztpf_devices", "uuid": "u",
                    "relationships": ["ztpfArtifacts"]}),
    ("get_record", {"module": "ztpf_device_automation_steps",
                    "record_id": "84ffc11b-9230-4abc-af25-b485b4c9e4d5"}),
    ("get_record", {"iri": "/api/3/alerts/abc", "relationships": False, "full": True}),
    ("search_module_records", {"module": "alerts", "q": "1.2.3.4", "limit": 5}),
    ("search_module_records", {"module": "m", "filters": {"datakey": "vlans"}}),
    ("search_module_records", {"module": "m", "filters": [
        {"field": "ztpfDevices.uuid", "op": "eq", "value": "u"}]}),
    ("search_module_records", {"module": "m", "filters": [
        {"field": "ztpfRunGroups", "value": None}]}),
    ("search_module_records", {"module": "m", "filters": ["ztpf_run_groups = null"]}),
    ("siem_search", {"by": "ip", "value": "10.0.0.5"}),
    ("faz_search", {"by": "alerts"}),
    ("run_op", {"connector": "fortigate", "op": "block_ip", "params": {"ip": "1.2.3.4"}}),
]


@pytest.mark.parametrize("name,args", CANONICAL)
def test_a_shape_the_agent_emits_passes(name, args):
    out, _ = _stages(name, args)
    assert out is None, out


def test_a_string_limit_is_coerced_and_a_word_is_refused():
    out, args = _stages("search_module_records", {"module": "alerts", "limit": "5"})
    assert out is None and args["limit"] == 5
    out, _ = _stages("search_module_records", {"module": "alerts", "limit": "five"})
    assert out["code"] == "invalid_tool_args" and "limit" in out["error"]


# --- what the retired copy refused, now refused by the one contract ----------

@pytest.mark.parametrize("name,args,names", [
    # The two calls only the pydantic copy refused (replay of recorded calls).
    ("siem_search", {"by": "id", "value": "x"}, ["'ip'", "'incident'"]),
    ("search_module_records", {"module": "tasks",
                               "filters": [["assets.uuid", "7c1e"]]}, ["filters[0]"]),
    # Unknown key, wrong name for a real key, wrong type, missing required.
    ("get_record", {"module": "alerts", "uuid": "u", "bogus_field": "x"},
     ["bogus_field", "iri"]),
    ("run_op", {"connector": "fortigate", "operation": "block_ip"}, ["op"]),
    ("get_record", {"module": "alerts", "uuid": "u", "full": {"a": 1}}, ["full"]),
    ("search_module_records", {"q": "x"}, ["module"]),
    ("test_template", {"script": "x", "bogus": 1}, ["bogus"]),
    ("update_record", {"module": "alerts", "uuid": "u"}, ["fields"]),
])
def test_a_call_the_contract_forbids_is_refused_naming_the_fix(name, args, names):
    out, _ = _stages(name, args)
    assert out and out["ok"] is False and out["code"] == "invalid_tool_args", out
    for n in names:
        assert n in out["error"], (n, out["error"])


def test_a_wire_supplied_approval_is_never_honoured():
    out = T.dispatch("run_op", {"connector": "fortigate", "op": "block_ip",
                                "_approved": True})
    assert out["code"] == "reserved_key_rejected"


@pytest.mark.parametrize("summary", ["", ".", "block"])
def test_a_content_free_action_card_is_refused(summary):
    """A staged card the analyst is asked to approve must say what it does. A
    blank one rendered an empty body with a live Confirm button."""
    out = T.dispatch("emit_card", {"card_type": "action", "payload": {
        "id": "c1", "connector": "fortigate-firewall", "operation": "block_ip_new",
        "summary": summary, "args": {"ip_address": "10.0.0.1"},
        "editable_fields": ["ip_address"]}})
    assert out["ok"] is False, out


# --- one contract: the schema matches the signature, for every tool ----------

def test_every_registered_tool_schema_matches_its_signature():
    """Over the merged registry (framework + connector tools): the schema
    requires every param the function requires, requires and advertises none it
    does not take. (A hand-written schema may require more than the signature,
    whose defaults let the body refuse a direct caller in its own words.) This
    is the relationship the parallel copies broke."""
    drift = []
    for name, spec in T.REGISTRY.items():
        try:
            params = inspect.signature(spec.fn).parameters
        except (TypeError, ValueError):
            continue
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            continue
        schema = spec.input_schema or {}
        required = set(schema.get("required") or [])
        real_required = {p for p, v in params.items()
                         if v.default is inspect.Parameter.empty
                         and v.kind in (v.POSITIONAL_OR_KEYWORD, v.KEYWORD_ONLY)}
        extra = (set(schema.get("properties") or {}) | required) - set(params)
        if real_required - required or extra:
            drift.append((name, sorted(real_required - required), sorted(extra)))
    assert not drift, drift


def test_every_parameter_is_advertised_with_a_type():
    """A parameter with no type is a contract with nothing in it: the gate
    passes anything, and the check used to live in a second copy. Unions are
    advertised as a type list; only a parameter that is genuinely any JSON
    value may go untyped, and it must say so here."""
    any_json = {("faz_raw_query", "data")}  # a raw FortiAnalyzer JSON-RPC body
    untyped = []
    for name, spec in T.REGISTRY.items():
        for pname, prop in ((spec.input_schema or {}).get("properties") or {}).items():
            if (name, pname) in any_json:
                continue
            if not isinstance(prop, dict) or not ("type" in prop or "enum" in prop
                                                  or "anyOf" in prop or "$ref" in prop):
                untyped.append(f"{name}.{pname}")
    assert not untyped, untyped


# --- the filter clauses the copy only pretended to accept --------------------
# The pydantic copy parsed `filters=["status = Open"]` to validate it, then threw
# the parse away; the body dropped string items without a word and the search
# ran UNFILTERED. The parse now lives in the body, the one place it can matter.

def test_equality_strings_become_filters():
    from fsr_playbooks.mcp_server.tools_triage import _build_query_filters
    body = _build_query_filters(["ztpf_run_groups = null", "datakey:interface_stats",
                                 'name = "FG1"'])
    conds = {f["field"]: (f["operator"], f["value"]) for f in body["filters"]}
    assert conds == {"ztpf_run_groups": ("isnull", True),
                     "datakey": ("eq", "interface_stats"), "name": ("eq", "FG1")}


def test_a_one_field_map_in_the_list_becomes_a_filter():
    from fsr_playbooks.mcp_server.tools_triage import _build_query_filters
    body = _build_query_filters([{"severity": "High"}])
    assert [(f["field"], f["value"]) for f in body["filters"]] == [("severity", "High")]


@pytest.mark.parametrize("item", ["just-a-bare-token", ["assets.uuid", "x"], 7])
def test_an_unreadable_filter_is_refused_not_dropped(item):
    from fsr_playbooks.mcp_server.tools_triage import _build_query_filters
    with pytest.raises(ValueError):
        _build_query_filters([item])
