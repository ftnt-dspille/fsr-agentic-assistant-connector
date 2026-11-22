"""The offline client answers the verbs the framework calls on the live client.

`analyze_playbook` renders every templated step argument with
`client.post("/api/wf/api/jinja-editor/", ...)` on the live client. The
cassette client exposed only `.session`, so that call raised
`'_CassetteClient' object has no attribute 'post'`. The renderer's own
fallback then read the AttributeError as a broken playbook, and the tool
returned ok:false on every templated step. A miss on a read the cassette
cannot answer must go to the work list and fail the way an unreachable
renderer fails, never crash the walk.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import local_turn as lt  # noqa: E402

JINJA = "/api/wf/api/jinja-editor/"


def _client():
    return lt._CassetteClient(base_url=lt._CASSETTE_BASE_URL, rules=[], box=None)


@pytest.mark.parametrize("verb", ["get", "post", "put", "delete", "request"])
def test_cassette_client_exposes_the_pyfsr_verb(verb):
    """Method existence: every verb the framework calls on the live client."""
    assert callable(getattr(_client(), verb, None)), (
        f"_CassetteClient has no .{verb}; the framework calls client.{verb}(...) "
        "on the live client and an AttributeError reads as a broken playbook")


def test_jinja_render_is_served_locally_and_a_broken_template_is_an_http_error():
    """The renderer endpoint answers from the framework's local-render sandbox,
    the box's `{"result": ...}` shape. A template that cannot render is a 400
    the framework's caller can catch, never an AttributeError and never a
    silent pass."""
    c = _client()
    out = c.post(JINJA, data={"template": "{{ vars.a + 1 }}",
                              "values": {"vars": {"a": 2}}})
    assert out == {"result": 3}
    with pytest.raises(lt._CassetteHTTPError) as e:
        c.post(JINJA, data={"template": "{{ x ", "values": {}})
    assert e.value.status_code == 400
    assert not c.session.misses, "a render that was answered is not unserved"


def test_write_verbs_still_refuse_in_a_local_turn():
    """Answering the read verbs must not open a write path: a DELETE in a
    local turn still raises, so a tool that bypasses the tier gate is loud."""
    with pytest.raises(AssertionError):
        _client().delete("/api/3/alerts/00000000-0000-4000-8000-000000000001")


def test_explain_turn_analyze_playbook_succeeds_offline(monkeypatch):
    """Explain-turn seam: the framework's analyze_playbook, run against the
    installed cassette, reaches its jinja renders, falls back to local render
    where the template resolves locally, and returns ok. The render it could
    not serve is not swallowed."""
    from fsr_playbooks.mcp_server import _shared
    import fsr_playbooks.mcp_server.tools_analysis as TA
    fixture = _SCRIPTS / "eval_fixtures" / "real_loop_check_sla_violations.yaml"
    cas = _client()
    monkeypatch.setitem(_shared._LIVE_CLIENT_CACHE, "client", cas)
    out = TA.analyze_playbook(yaml_text=fixture.read_text())
    assert out.get("ok") is True, out.get("error")
    assert any(g.endswith(lt._RENDER_PATH) for g in cas.session.gets), (
        "the analysis must reach the renderer endpoint")
    assert not any(m.endswith(lt._RENDER_PATH) for m in cas.session.misses), (
        "renders are answered locally now; none is unserved")


@pytest.mark.parametrize("fixture", sorted(p.name for p in (_SCRIPTS / "eval_fixtures").glob("*.yaml")))
def test_no_eval_fixture_fails_on_a_missing_client_verb(fixture, monkeypatch):
    """Sweep: no offline fixture reports the cassette's own AttributeError as a
    playbook defect. Before the verbs existed, three of the ten fixtures did."""
    from fsr_playbooks.mcp_server import _shared
    import fsr_playbooks.mcp_server.tools_analysis as TA
    monkeypatch.setitem(_shared._LIVE_CLIENT_CACHE, "client", _client())
    out = TA.analyze_playbook(yaml_text=(_SCRIPTS / "eval_fixtures" / fixture).read_text())
    assert "has no attribute" not in (out.get("error") or ""), out.get("error")
