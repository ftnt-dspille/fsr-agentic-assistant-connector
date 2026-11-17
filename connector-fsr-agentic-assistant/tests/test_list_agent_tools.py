"""list_agent_tools -- runtime introspection of the agentic tool surface.

The op is the box-truth counterpart to the framework's static SAFE_TOOLS
registry: it must return exactly what a real turn's `_tools_for_persona` slice
advertises, enriched with each tool's tier/confirm_mode and its provenance
(safe / mcp:<server> / widget). These tests pin:

  - the envelope shape (ok, per-intent surfaces, typed rows with a tier), and
  - that the surface matches the intent slice the turn itself uses (so the op
    can never silently drift from the real tool list), and
  - that `_tool_source` tags a materialized MCP tool by its origin server and a
    card emitter as `widget`.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_HAVE_FW = importlib.util.find_spec("fsr_playbooks") is not None


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


pytestmark = pytest.mark.skipif(
    not _HAVE_FW, reason="fsr_playbooks framework not importable in this env")


def _surface(result, intent):
    return next(s for s in result["surfaces"] if s["intent"] == intent)


def test_envelope_shape_both_intents(ops):
    out = ops.list_agent_tools({}, {"intent": "both", "include_mcp": False})
    assert out["ok"] is True
    assert out["persona_applied"] is False
    intents = {s["intent"] for s in out["surfaces"]}
    assert intents == {"triage", "build"}
    for s in out["surfaces"]:
        assert s["count"] == len(s["tools"]) > 0
        row = s["tools"][0]
        # typed row carries provenance + approval metadata
        assert set(("name", "tier", "confirm_mode", "source")) <= set(row)
        # names sorted
        names = [t["name"] for t in s["tools"]]
        assert names == sorted(names)


def test_surface_matches_the_turn_slice(ops):
    """The op must not drift from what a real turn advertises."""
    out = ops.list_agent_tools({}, {"intent": "both", "include_mcp": False})
    for intent in ("triage", "build"):
        got = {t["name"] for t in _surface(out, intent)["tools"]}
        expected = {t["name"] for t in ops._advertised_tools(
            {}, ops.ResolvedPersona(intent=intent), include_mcp=False)}
        assert got == expected, f"{intent} surface drifted from turn slice"


def test_intent_scoping_boundaries(ops):
    out = ops.list_agent_tools({}, {"intent": "both", "include_mcp": False})
    build = {t["name"] for t in _surface(out, "build")["tools"]}
    triage = {t["name"] for t in _surface(out, "triage")["tools"]}
    # Phase 3 full-surface: the two introspected surfaces are identical; the
    # prior only changes the prompt (disposition), never the tool list.
    assert build == triage
    assert "run_op" in build and "validate_yaml" in triage


def test_single_intent_returns_one_surface(ops):
    out = ops.list_agent_tools({}, {"intent": "triage", "include_mcp": False})
    assert [s["intent"] for s in out["surfaces"]] == ["triage"]


def test_verbose_includes_input_schema(ops):
    lean = ops.list_agent_tools({}, {"intent": "triage", "include_mcp": False})
    assert lean["surfaces"][0]["tools"][0].get("input_schema") is None
    verbose = ops.list_agent_tools(
        {}, {"intent": "triage", "include_mcp": False, "verbose": True})
    assert verbose["surfaces"][0]["tools"][0].get("input_schema") is not None


def test_tool_source_tags_mcp_and_widget(ops, monkeypatch):
    from fsr_playbooks.mcp_server import materializer
    monkeypatch.setattr(
        materializer, "SERVER_MAP",
        {"mcp_soc__get_alert": ("soc", "get_alert")}, raising=False)
    assert ops._tool_source("mcp_soc__get_alert") == "mcp:soc"
    assert ops._tool_source("emit_action_card") == "widget"
    assert ops._tool_source("get_record") == "safe"


# --- MCP consistency: the seam materializes mcp_* tools into every path ------

def _stub_mcp_client(tools_by_server):
    """A pyfsr-like client whose `.mcp.list_tools(server)` returns canned tools --
    the same shape the framework materializer test uses."""
    class _MCP:
        def list_tools(self, server):
            return tools_by_server.get(server, [])
        def call_tool(self, server, name, arguments=None):
            return {"called": name, "server": server}
    class _Client:
        def supports_native_mcp(self):
            return True
        mcp = _MCP()
    return _Client()


_SOC_TOOL = [{"name": "get_alert", "description": "get an alert",
              "input_schema": {"type": "object", "properties": {}}}]

# A connector config that actually NAMES soc. These tests used to pass `{}` and
# inject the allowlist straight into the materializer, relying on the op's
# up-front `_apply_mcp_allowlist` being a no-op. That stopped modelling a real
# box in 0.5.77 (tracker #62): introspection now scopes its rows to THIS
# config's allowlist keys, so a config-less connector correctly reports zero
# mcp_* tools -- and these three tests were asserting the pre-fix behaviour.
# Driving the allowlist through `config` exercises the real
# config → _parse_mcp_allowlist → materializer path instead of bypassing it.
_MCP_CONFIG = {"mcp_allowlist": {"soc": {"tools": "*", "tier": "read_only"}}}


@pytest.fixture
def mcp_soc(monkeypatch):
    """Materializer wired to `_MCP_CONFIG` over a stub transport.

    `_apply_mcp_allowlist` builds its client factory from
    `fsr_soc_triage._live_mcp.build_client`; patching that keeps the op's real
    apply step in the path while it stays hermetic (no gateway call)."""
    from fsr_playbooks.mcp_server import materializer as M
    from fsr_soc_triage import _live_mcp
    monkeypatch.setattr(
        _live_mcp, "build_client",
        lambda **kw: _stub_mcp_client({"soc": _SOC_TOOL}))
    M.reset()
    try:
        yield _MCP_CONFIG
    finally:
        M.reset()


def test_list_agent_tools_surfaces_materialized_mcp_tools(ops, mcp_soc):
    """Regression for the M3 finding: a real turn materialized mcp_* tools that
    list_agent_tools silently missed. Both now route through _advertised_tools,
    so introspection surfaces exactly the materialized set a turn would."""
    out = ops.list_agent_tools(mcp_soc, {"intent": "triage", "include_mcp": True})
    names = {t["name"] for s in out["surfaces"] for t in s["tools"]}
    assert "mcp_soc__get_alert" in names


def test_mcp_tools_are_scoped_to_this_config(ops, mcp_soc, monkeypatch):
    """The other half of #62: the materialized surface is a process-global
    shared by every config on the worker, so a config that does NOT name `soc`
    must not report a sibling's server -- even though it is materialized."""
    ops.list_agent_tools(mcp_soc, {"intent": "triage", "include_mcp": True})
    out = ops.list_agent_tools({}, {"intent": "triage", "include_mcp": True})
    names = {t["name"] for s in out["surfaces"] for t in s["tools"]}
    assert "mcp_soc__get_alert" not in names
    assert names, "scoping must drop only mcp_* rows, not the curated surface"


def test_turn_surface_is_scoped_to_this_config(ops, mcp_soc):
    """Tracker #63 -- the same scoping, on the surface a TURN advertises from.

    #62 scoped the two introspection ops and deliberately left this one alone,
    because narrowing what the chat loop advertises changes what an
    investigation can reach. That left the real hole open: `_advertised_tools`
    sliced `materializer.SERVER_MAP` / `llm.tools.REGISTRY`, a process-global
    every config on the worker shares, so a worker that had served a `soc`
    config carried `soc` into the next config's turn. On .159 an empty-allowlist
    config advertised a byte-identical 66-tool surface to a 3-server one.

    This is an authorization assertion, not a cosmetic one: `mcp_soc__*`
    includes `block_indicator`.
    """
    persona = ops.ResolvedPersona(intent="triage", profile=None)

    warm = {t["name"] for t in ops._advertised_tools(mcp_soc, persona)}
    assert "mcp_soc__get_alert" in warm, "fixture did not materialize soc"

    # Same warm worker, a config that never named soc.
    other = {t["name"] for t in ops._advertised_tools({}, persona)}
    assert "mcp_soc__get_alert" not in other
    assert not {n for n in other if n.startswith("mcp_soc__")}
    assert other, "scoping must drop only mcp_* tools, not the curated surface"


def test_turn_surface_scoping_is_not_reachable_on_ask(ops, mcp_soc):
    """Scoping runs BEFORE M2.1 surfacing, so `reach_servers` cannot pull back a
    server this config never authorized. Reach decides what of the *authorized*
    set is on the default surface; it is not a second authorization path."""
    persona = ops.ResolvedPersona(intent="triage", profile=None)
    ops._advertised_tools(mcp_soc, persona)  # warm the global

    reached = {t["name"] for t in ops._advertised_tools(
        {}, persona, module="workflows", reach_servers=["soc"])}
    assert "mcp_soc__get_alert" not in reached


def test_misconfigured_allowlist_fails_closed_on_the_turn_path(ops, mcp_soc):
    """A typo'd allowlist must advertise NO mcp_* -- not silently inherit
    whatever a sibling config left materialized. Same answer `_mcp_registry_for`
    gives, which is the point: one rule, every path."""
    persona = ops.ResolvedPersona(intent="triage", profile=None)
    ops._advertised_tools(mcp_soc, persona)

    broken = {t["name"] for t in ops._advertised_tools(
        {"mcp_allowlist": "{not json"}, persona)}
    assert not {n for n in broken if n.startswith("mcp_")}
    assert broken


def test_env_allowlist_fallback_still_authorizes(ops, mcp_soc, monkeypatch):
    """The `FSRPB_MCP_ALLOWLIST` env fallback is honored when config names
    nothing. It is process-wide and identical for every config, so it is not the
    cross-config leak #63 is about -- scoping it away would break the dev/test
    path `_apply_mcp_allowlist` explicitly supports."""
    ops._advertised_tools(mcp_soc, ops.ResolvedPersona(intent="triage", profile=None))
    monkeypatch.setenv("FSRPB_MCP_ALLOWLIST",
                       '{"soc": {"tools": "*", "tier": "read_only"}}')
    names = {t["name"] for t in ops._advertised_tools(
        {}, ops.ResolvedPersona(intent="triage", profile=None))}
    assert "mcp_soc__get_alert" in names


def test_list_mcp_servers_enumerates_and_annotates(ops, mcp_soc, monkeypatch):
    """M2.1 discovery: list_mcp_servers groups materialized mcp_* by server and
    marks each on/off the module's default surface (soc is on `alerts`, off
    `workflows`)."""
    # Hermetic: no Key Store persona (avoid a live box read in resolution).
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_mcp"], "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_personas"], "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_tools"], "_resolve_profile", lambda *a, **k: None)
    alerts = ops.list_mcp_servers(mcp_soc, {"module": "alerts"})
    assert alerts["ok"] is True
    soc = next(r for r in alerts["servers"] if r["server"] == "soc")
    assert soc["on_surface"] is True
    assert "mcp_soc__get_alert" in soc["tools"]
    assert alerts["surfaced_servers"] == ["fortisiem", "soc"]

    wf = ops.list_mcp_servers(mcp_soc, {"module": "workflows"})
    soc_wf = next(r for r in wf["servers"] if r["server"] == "soc")
    assert soc_wf["on_surface"] is False   # off-surface on a build page

    reached = ops.list_mcp_servers(
        mcp_soc, {"module": "workflows", "reach_servers": "soc"})
    soc_r = next(r for r in reached["servers"] if r["server"] == "soc")
    assert soc_r["on_surface"] is True      # reach flips it on


def test_reach_widens_the_RANKING_now_that_nothing_is_hidden(ops, mcp_soc,
                                                             monkeypatch):
    """M2.1 after A1.3. Reach used to widen the ADVERTISED set, because a page
    hid the servers it didn't prefer. Pages no longer hide anything, so what
    reach widens is the page's stated PREFERENCE -- and `mcp_inventory`'s
    `on_surface` flag, which is what the discovery op reports.

    The tool is advertised either way; whether the analyst had to ask for it
    changes what the prompt recommends, not what dispatch will run. Execution
    is still governed by tier-3 and the dispatch gates."""
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_mcp"], "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_personas"], "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_resolve_profile", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_tools"], "_resolve_profile", lambda *a, **k: None)
    base = ops.list_agent_tools(
        mcp_soc, {"intent": "triage", "module": "workflows", "include_mcp": True})
    base_names = {t["name"] for s in base["surfaces"] for t in s["tools"]}
    assert "mcp_soc__get_alert" in base_names, (
        "A1.3: a build page no longer subtracts the soc server -- it just "
        "doesn't rank it")

    from fsr_soc_triage.triage_sources import page_tool_ranking
    slice_ = [{"name": n} for n in base_names]
    assert not any(n.startswith("mcp_soc__")
                   for n in page_tool_ranking(slice_, "workflows"))
    reached = page_tool_ranking(slice_, "workflows", reach_servers=["soc"])
    assert any(n.startswith("mcp_soc__") for n in reached)


def test_reach_servers_param_coercion():
    """The reach_servers text param (op UI) coerces to a list; a JSON array and a
    comma/space string both parse; junk fails open to None."""
    from pydantic_models import ListMcpServersParams as P
    assert P(reach_servers="deepwiki, soc").reach_servers == ["deepwiki", "soc"]
    assert P(reach_servers='["a","b"]').reach_servers == ["a", "b"]
    assert P(reach_servers=["x"]).reach_servers == ["x"]
    assert P(reach_servers="").reach_servers is None
    assert P(reach_servers=None).reach_servers is None


def test_every_info_json_operation_has_a_handler(ops):
    """Drift guard: every operation declared in info.json must resolve to a
    handler in the `operations` registry (annotation → callable). This catches
    the class where a new op is added to info.json + operations.py but never
    wired into `_LIVE_OPERATIONS`, which the box reports as
    'unknown operation: <name>' only at call time on a real deploy."""
    import json
    info = json.loads((ROOT / "info.json").read_text())
    registry = set(ops.operations)
    missing = []
    for op in info.get("operations", []):
        handler = op.get("annotation") or op.get("operation")
        if handler and handler not in registry:
            missing.append(handler)
    assert not missing, f"info.json operations with no handler in registry: {missing}"


def _dispatch_tool_sources():
    """For every agent-turn dispatch call, where its `tools=` value came from.

    Returns [(function_name, lineno, source_kind)] with source_kind one of
    "_advertised_tools" (inline, via a local bound from it, or a conditional
    whose every branch is itself acceptable), "absent" (no tools= -- resume
    reuses the suspended state), or the offending expression.

    A conditional branch may also be an explicit EMPTY tool list -- a
    deliberate strip, as the `_budget_ask` "deliver" resume does: no tools at
    all is not a second way to compute a tool surface, so it cannot drift from
    the seam. Any other local or expression is still reported.
    """
    import ast
    tree = ast.parse("\n".join(l for _p in sorted(ROOT.glob("ops_*.py")) + [ROOT / "operations.py"] for l in _p.read_text(encoding="utf-8").splitlines() if not l.startswith("from __future__")))
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Locals in THIS function bound from `_advertised_tools(...)`.
        seam_locals = {
            t.id
            for node in ast.walk(fn) if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_advertised_tools"
        }
        # Locals in THIS function bound only to None / [] -- a strip switch, not
        # a second tool-surface builder.
        strip_locals = set()
        for node in ast.walk(fn):
            for t in (node.targets if isinstance(node, ast.Assign) else []):
                if not isinstance(t, ast.Name):
                    continue
                v = node.value
                empty = ((isinstance(v, ast.Constant) and v.value is None)
                         or (isinstance(v, ast.List) and not v.elts))
                if empty:
                    strip_locals.add(t.id)
                else:
                    strip_locals.discard(t.id)

        def _kind(arg):
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) \
                    and arg.func.id == "_advertised_tools":
                return "_advertised_tools"
            if isinstance(arg, ast.Name) and arg.id in seam_locals:
                return "_advertised_tools"
            if isinstance(arg, ast.Name) and arg.id in strip_locals:
                return "_advertised_tools"  # strip switch; see the docstring
            if isinstance(arg, ast.List) and not arg.elts:
                return "_advertised_tools"
            if isinstance(arg, ast.IfExp):
                kinds = {_kind(arg.body), _kind(arg.orelse)}
                return "_advertised_tools" if kinds == {"_advertised_tools"} \
                    else ast.unparse(arg)
            return ast.unparse(arg)

        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id in ("run_agent_turn", "resume_agent_turn")):
                continue
            arg = next((k.value for k in call.keywords if k.arg == "tools"), None)
            kind = "absent" if arg is None else _kind(arg)
            out.append((fn.name, call.lineno, kind))
    return out


def test_advertised_tools_is_the_only_dispatch_seam():
    """Structural guard: every turn-like dispatch site must compute its tool
    surface via `_advertised_tools`, so the MCP-apply + slice can't drift
    between paths again.

    This used to be three substring checks plus `src.count("_advertised_tools(")
    >= 4`. Both halves were blind (#56): the count is satisfied by calls
    ANYWHERE in the file, including ones that feed nothing, and the
    `tools=_tools_for_persona(` checks only catch a direct inline call --
    `chat_turn` in fact passes `tools=_turn_tools`, a local, which neither half
    could evaluate. It passed by not looking. This resolves the local instead."""
    sites = _dispatch_tool_sources()
    assert sites, "no agent-turn dispatch sites found -- the gate lost its subject"
    bad = [s for s in sites if s[2] not in ("_advertised_tools", "absent")]
    assert not bad, (
        "dispatch site(s) not using the _advertised_tools seam: "
        + "; ".join(f"{fn}:{line} -> tools={kind}" for fn, line, kind in bad))
    # At least one site must genuinely route through the seam, or "no bad
    # sites" would be true of a file with no dispatch at all.
    assert any(s[2] == "_advertised_tools" for s in sites)


def test_the_seam_scan_can_tell_a_bad_site_from_a_good_one(tmp_path, monkeypatch):
    """Self-proof (#56): a structural gate must demonstrate it flags its own
    subject, otherwise "no offenders" is indistinguishable from "cannot see"."""
    good_inline = "def a():\n    run_agent_turn(tools=_advertised_tools(x))\n"
    good_local = ("def b():\n    t = _advertised_tools(x)\n"
                  "    run_agent_turn(tools=t)\n")
    bad_direct = "def c():\n    run_agent_turn(tools=_tools_for_persona(x))\n"
    bad_foreign = ("def d():\n    t = _some_other_surface(x)\n"
                   "    run_agent_turn(tools=t)\n")

    # Exercise the real function against a temp module by pointing ROOT at it.
    for src, expect_bad in ((good_inline, False), (good_local, False),
                            (bad_direct, True), (bad_foreign, True)):
        (tmp_path / "operations.py").write_text(src)
        monkeypatch.setitem(globals(), "ROOT", tmp_path)
        sites = _dispatch_tool_sources()
        assert sites, f"scan found no dispatch site in:\n{src}"
        is_bad = any(s[2] not in ("_advertised_tools", "absent") for s in sites)
        assert is_bad is expect_bad, (
            f"scan verdict wrong for:\n{src}got bad={is_bad}")
