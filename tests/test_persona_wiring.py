"""Wiring seams for module-scoped personas in operations.py.

Two invariants:
  1. Backward-compat — with NO persona, `_resolve_system_prompt` and the tool
     slice are byte-identical to the built-in triage/build behavior.
  2. Persona active — the prompt is replaced by the persona prompt and the tool
     slice is narrowed to the persona allowlist (from the FULL registry).

`_resolve_profile` must also stay fail-open (no live client ⇒ None). The
`connectors.*` SDK + `connector_fsr_agentic_assistant` package come from the suite
conftest.
"""
from __future__ import annotations


from connector_fsr_agentic_assistant import operations as ops
from fsr_soc_triage.profiles import Profile
import ops_mcp
import ops_personas
import ops_resume
import ops_tools


def _profile(**kw):
    base = dict(module="ztpf_templates", prompt="You author ZTPF templates.")
    base.update(kw)
    return Profile(**base)


# --- backward-compat: no persona ⇒ unchanged --------------------------------

def test_persona_slice_no_profile_matches_intent_slice():
    for intent in ("triage", "build"):
        got = [t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent=intent, profile=None), include_mcp=False)]
        want = [t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent=intent), include_mcp=False)]
        assert got == want


def test_persona_slice_empty_allowlist_unchanged():
    got = [t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build", profile=_profile()), include_mcp=False)]
    want = [t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build"), include_mcp=False)]
    assert got == want


def test_resolve_prompt_no_profile_uses_builtin():
    prompt, cs = ops._resolve_system_prompt(
        "build", None, storage=None, session_id="s", turn_idx=1, profile=None)
    assert prompt == ops._load_prompt("build")
    assert cs is None


# --- persona active ---------------------------------------------------------

def test_resolve_prompt_persona_is_authoritative():
    prompt, cs = ops._resolve_system_prompt(
        "build", {"module": "ztpf_templates"}, storage=None, session_id="s",
        turn_idx=1, profile=_profile())
    assert prompt == "You author ZTPF templates."
    assert cs is None  # persona turns are not case-grounded


def test_persona_slice_narrows_to_allowlist():
    allow = ["get_record", "find_operation"]
    got = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build", profile=_profile(tools_allow=allow)), include_mcp=False)}
    # A retired name in the allowlist grants its union ("find_operation" ->
    # "find"), so the granted set may contain the union alongside the literal
    # names -- but nothing beyond that translation.
    retired = ops._retired_to_union()
    allowed = set(allow) | {retired[n] for n in set(allow) & set(retired)}
    allowed |= ops._PERSONA_IMPLIED_TOOLS  # read-only tools every persona carries
    assert got <= allowed
    assert got  # at least one allowlisted tool exists in the registry


def test_persona_slice_drops_unknown_names():
    p = _profile(tools_allow=["get_record", "definitely_not_a_real_tool"])
    got = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build", profile=p), include_mcp=False)}
    assert "definitely_not_a_real_tool" not in got


# --- record-write tools: triage-reachable, build-excluded -------------------

def test_write_tools_triage_reachable_build_excluded():
    # 4435be4: record writes surface in TRIAGE (the analyst acts on the record
    # it is triaging) and stay OUT of build (authoring never writes host
    # records). Safety is NOT slice-absence — both tools are tier 3 /
    # confirm_mode="approve" and _write_gate refuses with no active persona;
    # _host_record_write_profile scopes writes to the mounted record's module.
    # (Importing fsr_soc_triage.profiles above ran register_record_tools(), so
    # the write tools are in the registry deterministically here.)
    tri = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="triage", profile=None), include_mcp=False)}
    bld = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build", profile=None), include_mcp=False)}
    for w in ("create_record", "update_record"):
        assert w in tri, f"{w} must be reachable in the triage slice"
        # Phase 3 full-surface: advertised in build too; the tier-3 /
        # _write_gate approval path (not slice absence) is the safety.
        assert w in bld, f"{w} must be reachable in the build slice"


def test_write_tools_surface_only_via_persona_allowlist():
    p = _profile(tools_allow=["create_record", "update_record", "get_record"])
    names = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build", profile=p), include_mcp=False)}
    assert {"create_record", "update_record"} <= names


# --- _resolve_profile fail-open ---------------------------------------------

def test_resolve_profile_bad_module_none():
    assert ops._resolve_profile(None, "s") is None
    assert ops._resolve_profile("", "s") is None


def test_resolve_profile_no_live_client_none(monkeypatch):
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: None)
    ops._PROFILE_CACHE.clear()
    assert ops._resolve_profile("ztpf_templates", "sess-x") is None


def test_resolve_profile_caches_a_definitive_resolution(monkeypatch):
    """A DEFINITIVE resolve is cached per (session, module) — resolved once.

    This test used to fake a ``None`` live client and expect that to cache. It
    no longer can, by design: no client means "we couldn't read the Key Store",
    which `_resolve_profile` classifies as transient (see the sibling test
    below). Only a completed read is cacheable, so the caching invariant has to
    be asserted through one.
    """
    from fsr_playbooks.mcp_server import _shared
    from fsr_soc_triage import profiles
    calls = {"n": 0}
    prof = _profile()

    monkeypatch.setattr(_shared, "_live_client", lambda: object())

    def _resolve(client, module):
        calls["n"] += 1
        return prof, True  # definitive: the read completed

    monkeypatch.setattr(profiles, "resolve_profile_status", _resolve)
    ops._PROFILE_CACHE.clear()
    assert ops._resolve_profile("ztpf_templates", "sess-y") is prof
    assert ops._resolve_profile("ztpf_templates", "sess-y") is prof
    assert calls["n"] == 1  # second call served from cache


def test_resolve_profile_does_not_cache_a_transient_miss(monkeypatch):
    """No live client is "couldn't read", NOT "no persona" — never cache it.

    A cold resume worker that cached its own unreadable Key Store would pin
    "no persona" for the worker's whole life and refuse a later persona-gated
    write. Each call must retry instead.
    """
    from fsr_playbooks.mcp_server import _shared
    calls = {"n": 0}

    def _client():
        calls["n"] += 1
        return None

    monkeypatch.setattr(_shared, "_live_client", _client)
    ops._PROFILE_CACHE.clear()
    assert ops._resolve_profile("ztpf_templates", "sess-y") is None
    assert ops._resolve_profile("ztpf_templates", "sess-y") is None
    assert calls["n"] == 2, "a transient read must be retried, not cached"
    assert ("sess-y", "ztpf_templates") not in ops._PROFILE_CACHE


# --- resolve_persona operation (Phase 4 widget-framing fallback) -------------

def test_resolve_persona_returns_ui_and_label(monkeypatch):
    prof = _profile(label="ZTPF Template Author",
                    ui={"greeting": "Authoring {module}: {name}",
                        "placeholder": "Describe the template…"})
    monkeypatch.setattr(ops_mcp, "_resolve_profile", lambda module, sess: prof)
    monkeypatch.setattr(ops_personas, "_resolve_profile", lambda module, sess: prof)
    monkeypatch.setattr(ops_resume, "_resolve_profile", lambda module, sess: prof)
    monkeypatch.setattr(ops_tools, "_resolve_profile", lambda module, sess: prof)
    out = ops.resolve_persona({}, {"module": "ztpf_templates"})
    assert out == {"found": True, "module": "ztpf_templates",
                   "label": "ZTPF Template Author",
                   "ui": {"greeting": "Authoring {module}: {name}",
                          "placeholder": "Describe the template…"}}


def test_resolve_persona_no_persona_is_found_false(monkeypatch):
    monkeypatch.setattr(ops_mcp, "_resolve_profile", lambda module, sess: None)
    monkeypatch.setattr(ops_personas, "_resolve_profile", lambda module, sess: None)
    monkeypatch.setattr(ops_resume, "_resolve_profile", lambda module, sess: None)
    monkeypatch.setattr(ops_tools, "_resolve_profile", lambda module, sess: None)
    assert ops.resolve_persona({}, {"module": "alerts"}) == {
        "found": False, "module": "alerts"}


def test_resolve_persona_missing_module_is_found_false():
    assert ops.resolve_persona({}, {}) == {"found": False, "module": None}
    assert ops.resolve_persona({}, {"module": ""}) == {"found": False, "module": ""}


def test_resolve_persona_fail_open_on_error(monkeypatch):
    def _boom(module, sess):
        raise RuntimeError("keystore down")
    monkeypatch.setattr(ops_mcp, "_resolve_profile", _boom)
    monkeypatch.setattr(ops_personas, "_resolve_profile", _boom)
    monkeypatch.setattr(ops_resume, "_resolve_profile", _boom)
    monkeypatch.setattr(ops_tools, "_resolve_profile", _boom)
    assert ops.resolve_persona({}, {"module": "ztpf_templates"}) == {
        "found": False, "module": "ztpf_templates"}


def test_resolve_persona_registered_in_dispatch():
    assert "resolve_persona" in ops.operations
