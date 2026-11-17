"""test_template: render + lint a ztpf_templates Jinja body.

No box -- a fake `_shared._live_client()` + captured GET. Invariants:
  - script path: lints + renders an inline body against inline context.
  - undefined var ⇒ render_error + has_errors (StrictUndefined).
  - syntax error ⇒ a severity=error finding + has_errors.
  - uuid path: reads the record, pulls `script` + `exampleJinjaVars` (incl. the
    JSON-string form) off it.
  - Registration: lands in REGISTRY at tier 1 and in TRIAGE_ONLY_TOOLS.
"""
from __future__ import annotations

from fsr_soc_triage import tools_ztpf


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self):
        return self._payload


class _Client:
    base_url = ""


def _patch_client(monkeypatch, capture, resp):
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: _Client())

    def fake_request(method, client, url, **kwargs):
        capture.append((method, url, kwargs))
        return resp

    monkeypatch.setattr(_shared, "live_request_with_reauth", fake_request)


# --- script path (no box read) ----------------------------------------------

def test_clean_render():
    out = tools_ztpf.test_template(
        script="hostname {{ input.name | upper }}", context={"input": {"name": "fgt"}})
    assert out["ok"] is True
    assert out["rendered"] == "hostname FGT"
    assert out["render_error"] is None
    assert out["has_errors"] is False
    assert out["findings"] == []
    # Off-box (no crudhub) the fallback offline renderer runs.
    assert out["render_via"] == "offline"


def test_do_extension_template_lints_clean():
    # `{% do %}` is a valid FSR runtime construct -- it must NOT be a syntax error.
    script = ("{%- set m = {'id': 0} -%}"
              "{%- for i in items -%}{%- do m.update({'id': i}) -%}{%- endfor -%}"
              "id={{ m.id }}")
    out = tools_ztpf.test_template(script=script, context={"items": [1, 2, 3]})
    assert out["ok"] is True
    assert not any(f.get("code") == "jinja_syntax_error" for f in out["findings"])
    assert out["render_error"] is None
    assert out["has_errors"] is False
    assert out["rendered"] == "id=3"


def test_undefined_var_is_render_error():
    out = tools_ztpf.test_template(script="{{ input.missing }}", context={"input": {}})
    assert out["ok"] is True
    assert out["rendered"] is None
    assert out["render_error"]  # StrictUndefined surfaced it
    assert out["has_errors"] is True


def test_syntax_error_is_lint_finding():
    out = tools_ztpf.test_template(script="{{ unclosed ", context={})
    assert out["ok"] is True
    assert any(f.get("severity") == "error" for f in out["findings"])
    assert out["has_errors"] is True


def test_unknown_filter_is_warning_not_error():
    out = tools_ztpf.test_template(
        script="{{ 'x' | no_such_filter_xyz }}", context={})
    # A genuinely unknown filter: lint flags it (warning) and render reports a
    # real error (it is NOT a runtime-only-filter gap).
    assert any(f.get("code") == "unknown_jinja_filter" for f in out["findings"])
    assert out["render_error"] and "no_such_filter_xyz" in out["render_error"]
    assert out["offline_gap"] is None


def test_runtime_only_filter_is_offline_gap_not_error():
    # b64encode is a valid FSR/Ansible filter the OFFLINE renderer lacks -- it must
    # NOT be a render_error or set has_errors; it's an offline_gap (render on box).
    out = tools_ztpf.test_template(script="{{ 'hi' | b64encode }}", context={})
    assert out["ok"] is True
    assert out["render_error"] is None
    assert out["rendered"] is None
    assert out["offline_gap"] and "b64encode" in out["offline_gap"]
    assert out["has_errors"] is False
    # Lint is clean -- b64encode is a known filter.
    assert not any(f.get("code") == "unknown_jinja_filter" for f in out["findings"])


# The real "next test loopback" template, as captured: it numbers the next
# loopback with regex_replace and picks its address with ipaddr. Offline it
# used to stop at "render on the box", so the agent could never see what a
# template it drafted would produce.
_LOOPBACK = """{%- set net = input.lo_supernet -%}
{%- set max = {"id":0} -%}
{%- for i in data.testloints -%}
  {%- set c = i.name | regex_replace("\\D", "") -%}
  {%- if c and c|int > max.id -%}
    {%- do max.update({"id":c|int}) -%}
  {%- endif -%}
{%- endfor -%}
edit {{input.lo_name}}{{max.id+1}}
set ip {{net | ipaddr(max.id+1) | ipaddr('address') }}/32"""


def test_real_loopback_template_renders_offline():
    out = tools_ztpf.test_template(script=_LOOPBACK, context={
        "input": {"lo_name": "SingleLo", "lo_supernet": "192.0.2.72/24"},
        "data": {"testloints": [{"name": "SingleLo1"}, {"name": "SingleLo3"}]}})
    assert out["offline_gap"] is None and out["render_error"] is None
    assert out["rendered"] == "edit SingleLo4\nset ip 192.0.2.4/32"


def test_ipaddr_follows_ansible_semantics():
    ip = tools_ztpf._ipaddr
    assert ip("192.168.0.0/24", 5) == "192.168.0.5/24"
    assert ip("192.168.0.0/24", -1) == "192.168.0.255/24"
    assert ip("192.168.0.0/24", 300) is False
    assert ip("192.168.0.5/24", "address") == "192.168.0.5"
    assert ip("192.168.0.0/24", "address") is False      # a network has no host address
    assert ip("192.168.0.5/24", "host") == "192.168.0.5/24"
    assert ip("192.168.0.5/24", "network") == "192.168.0.0"
    assert ip("192.168.0.5/24", "netmask") == "255.255.255.0"
    assert ip("192.168.0.5/24", "prefix") == 24
    assert ip("192.168.0.5/24", "cidr") == "192.168.0.0/24"
    assert ip("192.168.0.0/24", "net") == "192.168.0.0/24"
    assert ip("not an ip") is False
    assert ip("10.0.0.1") == "10.0.0.1"


def test_regex_filters_render_offline():
    # Jinja string literals unescape once, so an author writes '\\d' and '\\1'.
    out = tools_ztpf.test_template(
        script=r"{{ 'port12' | regex_replace('\\d+', 'X') }} "
               r"{{ 'ab12cd34' | regex_findall('\\d+') | join(',') }} "
               r"{{ 'vlan-30' | regex_search('vlan-(\\d+)', '\\1') | first }}",
        context={})
    assert out["rendered"] == "portX 12,34 30", out


def test_an_uncarried_ipaddr_query_is_a_gap_not_an_error():
    out = tools_ztpf.test_template(script="{{ '10.0.0.1/24' | ipaddr('wrap') }}",
                                   context={})
    assert out["render_error"] is None and out["has_errors"] is False
    assert out["offline_gap"] and "ipaddr" in out["offline_gap"]


def test_empty_script_rejected():
    assert tools_ztpf.test_template(script="   ")["code"] == "empty_script"


# --- uuid path (record is its own fixture) ----------------------------------

def test_uuid_reads_script_and_example_vars(monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap, _Resp(200, {
        "script": "port {{ input.port }}",
        "exampleJinjaVars": {"input": {"port": 443}},
    }))
    out = tools_ztpf.test_template(module="ztpf_templates", uuid="u1")
    assert out["ok"] is True
    assert out["rendered"] == "port 443"
    method, url, _ = cap[0]
    assert method == "get"
    assert url == "/api/3/ztpf_templates/u1"


def test_uuid_example_vars_as_json_string(monkeypatch):
    # exampleJinjaVars sometimes arrives as a JSON string; accept it.
    cap = []
    _patch_client(monkeypatch, cap, _Resp(200, {
        "script": "{{ input.a }}",
        "exampleJinjaVars": '{"input": {"a": "ok"}}',
    }))
    out = tools_ztpf.test_template(uuid="u2")
    assert out["rendered"] == "ok"
    assert out["note"] is None


def test_uuid_bad_json_vars_notes_and_renders_empty(monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap, _Resp(200, {
        "script": "static text",
        "exampleJinjaVars": "{not json",
    }))
    out = tools_ztpf.test_template(uuid="u3")
    assert out["rendered"] == "static text"
    assert "not valid JSON" in (out["note"] or "")


def test_uuid_context_overrides_record_vars(monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap, _Resp(200, {
        "script": "{{ input.v }}",
        "exampleJinjaVars": {"input": {"v": "record"}},
    }))
    out = tools_ztpf.test_template(uuid="u4", context={"input": {"v": "override"}})
    assert out["rendered"] == "override"


def test_uuid_missing_script_rejected(monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap, _Resp(200, {"name": "no-body"}))
    assert tools_ztpf.test_template(uuid="u5")["code"] == "empty_script"


def test_uuid_http_error_surfaced(monkeypatch):
    cap = []
    _patch_client(monkeypatch, cap, _Resp(404, None))
    out = tools_ztpf.test_template(uuid="missing")
    assert out["ok"] is False and out["code"] == "http_404"


def test_no_uuid_no_script_rejected():
    assert tools_ztpf.test_template()["code"] == "missing_uuid"


# --- registration -----------------------------------------------------------

def test_registered_tier1_and_persona_only():
    from fsr_playbooks.llm.tools import REGISTRY, TOOL_TIERS
    from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    assert "test_template" in REGISTRY
    assert REGISTRY["test_template"].tier == 1
    assert REGISTRY["test_template"].confirm_mode == "auto"
    assert TOOL_TIERS["test_template"] == 1
    assert "test_template" in TRIAGE_ONLY_TOOLS


def test_an_extra_key_is_refused_before_the_tool_runs():
    from fsr_playbooks.llm.tools import dispatch
    out = dispatch("test_template", {"script": "x", "bogus": 1})
    assert out["ok"] is False and out["code"] == "invalid_tool_args"
    assert "bogus" in out["error"]


# --- the render must exercise the device data the template reads -------------
# Live: a template looping over data.dvmdb_ints_physical was "tested" with no
# such key in the context. The loop ran zero times, the render came back
# `config system interface / end` with no findings, and on the device every
# line came out glued together ("config system interfaceedit \"port1\"").

LIVE_SCRIPT = (
    "{%- set ifs = data.dvmdb_ints_physical | default([]) -%}\nconfig system interface\n"
    "{%- for i in ifs | sort(attribute='name') -%}\n"
    "  {%- set ip = i.ip if i.ip is defined else none -%}\n"
    "  {%- if ip and ip != '0.0.0.0' -%}\n  edit \"{{ i.name }}\"\n"
    "    set description \"{{ (i.alias if i.alias is defined and i.alias else i.name) }} on {{ record.device.name }}\"\n"
    "    {%- set allow = i.allowaccess if i.allowaccess is defined else none -%}\n"
    "    {%- if allow -%}\n    set allowaccess {{ allow if allow is string else allow | join(' ') }}\n"
    "    {%- endif -%}\n  next\n  {%- endif -%}\n{%- endfor %}\nend")
PHYSICAL = [{"name": "port1", "alias": "MGMT", "ip": ["10.254.1.3", "255.255.255.0"],
             "allowaccess": ["ping", "https"]},
            {"name": "port2", "alias": "ISP1", "ip": ["100.100.101.101", "255.255.255.0"],
             "allowaccess": "ping"}]


def _offline(monkeypatch):
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: None)


def test_a_missing_data_key_is_filled_from_its_metadata_source(monkeypatch):
    _offline(monkeypatch)
    monkeypatch.setattr(tools_ztpf, "_source_example", lambda k: {
        "name": "DeviceDB System Interfaces - Physical", "value": PHYSICAL,
        "record": {"device": {"name": "FG1"}}} if k == "dvmdb_ints_physical" else None)
    out = tools_ztpf.test_template(script=LIVE_SCRIPT)
    assert out["context_filled"] == {
        "data.dvmdb_ints_physical": "example output of metadata source 'DeviceDB System Interfaces - Physical'",
        "record": "example record of the same metadata source"}
    assert "port1" in out["rendered"] and "MGMT on FG1" in out["rendered"]
    codes = [f.get("code") for f in out["findings"]]
    assert "cli_commands_run_together" in codes, out["rendered"]


def test_a_data_key_nothing_produces_is_a_finding_not_a_clean_pass(monkeypatch):
    _offline(monkeypatch)
    monkeypatch.setattr(tools_ztpf, "_source_example", lambda k: None)
    out = tools_ztpf.test_template(script="{% for i in data.nope | default([]) %}{{ i }}\n{% endfor %}")
    assert [f["code"] for f in out["findings"] if f.get("code")] == ["data_not_in_context"]


def test_a_correct_template_has_no_cli_finding(monkeypatch):
    _offline(monkeypatch)
    monkeypatch.setattr(tools_ztpf, "_source_example", lambda k: None)
    good = ("config system interface\n{% for i in data.ints %}  edit \"{{ i.name }}\"\n"
            "    set allowaccess ping\n  next\n{% endfor %}end")
    out = tools_ztpf.test_template(script=good, context={"data": {"ints": [{"name": "port1"}]}})
    assert out["findings"] == [] and out["context_filled"] is None
    assert out["rendered"].splitlines()[1].strip() == 'edit "port1"'


# --- any field of any record --------------------------------------------------
# Live (a ZTPF automation action): the record keeps four output templates and a
# `script` that is not Jinja. The analyst asked for a render of the JSON output
# template; the tool could only render `script`, and only for template records.

_ACTION = {
    "name": "Get DeviceDB Config - Interfaces",
    "script": "Collect DeviceDB config",
    "outputTemplateJSON": ("[{%- for d in data.ints -%}{{ {'name': d.name} | tojson }}"
                           "{%- if not loop.last %},{% endif -%}{%- endfor -%}]"),
    "outputStatusTemplate": "{{ data.ints | length }} interfaces",
    "outputTemplateHTML": "",
    "exampleJinjaVars": {"data": {"ints": [{"name": "port1"}, {"name": "port2"}]}},
}


def test_a_record_with_several_jinja_fields_names_them(monkeypatch):
    _patch_client(monkeypatch, [], _Resp(200, dict(_ACTION)))
    out = tools_ztpf.test_template(module="ztpf_automation_actions", uuid="a1")
    assert out["ok"] is False and out["code"] == "choose_field"
    assert out["jinja_fields"] == ["outputTemplateJSON", "outputStatusTemplate"]


def test_field_renders_that_field_against_the_records_vars(monkeypatch):
    _patch_client(monkeypatch, [], _Resp(200, dict(_ACTION)))
    # The case the model wrote live; the record spells it outputTemplateJSON.
    out = tools_ztpf.test_template(module="ztpf_automation_actions", uuid="a1",
                                   field="outputTemplateJson")
    assert out["ok"] is True and out["field"] == "outputTemplateJSON"
    assert out["rendered"] == '[{"name": "port1"},{"name": "port2"}]'
    assert out["has_errors"] is False and out["findings"] == []


def test_the_only_jinja_field_is_rendered_without_naming_it(monkeypatch):
    rec = {k: v for k, v in _ACTION.items() if k != "outputStatusTemplate"}
    _patch_client(monkeypatch, [], _Resp(200, rec))
    out = tools_ztpf.test_template(module="ztpf_automation_actions", uuid="a1")
    assert out["field"] == "outputTemplateJSON"
    assert out["rendered"].startswith('[{"name": "port1"}')


def test_an_unknown_or_empty_field_is_said_plainly(monkeypatch):
    _patch_client(monkeypatch, [], _Resp(200, dict(_ACTION)))
    out = tools_ztpf.test_template(module="ztpf_automation_actions", uuid="a1",
                                   field="outputTemplateXML")
    assert out["code"] == "unknown_field"
    assert "outputTemplateJSON" in out["message"]
    out = tools_ztpf.test_template(module="ztpf_automation_actions", uuid="a1",
                                   field="outputTemplateHTML")
    assert out["code"] == "empty_script"


def test_an_unsaved_edit_renders_against_the_records_vars(monkeypatch):
    """Testing an edit before saving it is the point of `script`; with a uuid
    it used to render against no vars at all."""
    _patch_client(monkeypatch, [], _Resp(200, dict(_ACTION)))
    out = tools_ztpf.test_template(module="ztpf_automation_actions", uuid="a1",
                                   script="{{ data.ints | map(attribute='name') | join(',') }}")
    assert out["rendered"] == "port1,port2"


def test_json_output_that_does_not_parse_is_an_error(monkeypatch):
    rec = dict(_ACTION, outputTemplateJSON=(
        "[{%- for d in data.ints -%}{{ {'name': d.name} | tojson }},{%- endfor -%}]"))
    _patch_client(monkeypatch, [], _Resp(200, rec))
    out = tools_ztpf.test_template(module="ztpf_automation_actions", uuid="a1",
                                   field="outputTemplateJSON")
    assert out["has_errors"] is True
    [f] = [f for f in out["findings"] if f["code"] == "rendered_not_json"]
    assert f["severity"] == "error" and "outputTemplateJSON" in f["message"]


def test_a_value_printed_from_nothing_is_a_finding(monkeypatch):
    """On the box a missing value renders as empty, so the render looks clean.
    Live: an interface table showed nothing for `allowaccess` on some ports."""
    script = "{% for d in data.ints %}{{ d.name }}={{ d.allowaccess }};{% endfor %}"
    ctx = {"data": {"ints": [{"name": "port1", "allowaccess": "ping"}, {"name": "port2"}]}}
    monkeypatch.setattr(tools_ztpf, "_live_render", lambda s, c: ("ok", "port1=ping;port2=;"))
    out = tools_ztpf.test_template(script=script, context=ctx)
    assert out["render_via"] == "live"
    [f] = [f for f in out["findings"] if f["code"] == "printed_undefined"]
    assert "'allowaccess'" in f["message"] and f["severity"] == "warning"


def test_guarded_missing_values_are_not_findings(monkeypatch):
    script = ("{% for d in data.ints %}{{ d.allowaccess | default('') }}"
              "{% if d.vdom %}{{ d.vdom }}{% endif %}"
              "{{ d.mtu if d.mtu is defined else 1500 }}{% endfor %}")
    ctx = {"data": {"ints": [{"name": "port2"}]}}
    monkeypatch.setattr(tools_ztpf, "_live_render", lambda s, c: ("ok", "1500"))
    out = tools_ztpf.test_template(script=script, context=ctx)
    assert [f for f in out["findings"] if f["code"] == "printed_undefined"] == []


def test_long_output_is_capped(monkeypatch):
    monkeypatch.setattr(tools_ztpf, "_live_render", lambda s, c: ("ok", "x" * 50000))
    out = tools_ztpf.test_template(script="{{ 1 }}", context={})
    assert len(out["rendered"]) < 20100 and "30000 more characters" in out["rendered"]


def test_the_model_is_told_it_renders_any_field():
    from fsr_playbooks.llm.tools import REGISTRY
    spec = REGISTRY["test_template"]
    desc = " ".join(spec.description.split())
    assert "any template field" in desc and "before saving" in desc
    assert "field" in spec.input_schema["properties"]


def test_table_rows_glued_by_whitespace_trims_are_a_finding():
    """Live: a Markdown table template's `{%- -%}` trims put every row on one
    line, and the table did not render."""
    glued = ("{%- for d in data.ints -%}| {{ d.name }} | {{ d.ip }} |{%- endfor -%}")
    ctx = {"data": {"ints": [{"name": "port1", "ip": "a"}, {"name": "port2", "ip": "b"}]}}
    out = tools_ztpf.test_template(script="| Name | IP |\n|---|---|\n" + glued, context=ctx)
    [f] = [f for f in out["findings"] if f["code"] == "table_rows_run_together"]
    # The leading `{%-` also ate the newline after the separator row.
    assert "line(s) [2]" in f["message"]
    fixed = "{% for d in data.ints %}| {{ d.name }} | {{ d.ip }} |\n{% endfor %}"
    out = tools_ztpf.test_template(script="| Name | IP |\n|---|---|\n" + fixed, context=ctx)
    assert [f for f in out["findings"] if f["code"] == "table_rows_run_together"] == []


def test_a_separator_row_with_cells_after_it_is_glued():
    out = tools_ztpf.test_template(script="| a | b |\n|---|---|| 1 | 2 |", context={})
    assert any(f["code"] == "table_rows_run_together" for f in out["findings"])
