"""Plug the connector into the framework's tool registry.

The record reads and hunt tools (get_record, search_module_records, siem_search,
faz_search, fmg_device, edr_host and the per-axis helpers they replaced) live in
and register with the framework (`fsr_playbooks.mcp_server.tools_triage` /
`tools_noc` / `tools_edr`, names in `fsr_playbooks.llm.hunt_tools`). What only
the connector knows reaches them through `_shared` hooks (`register_triage_tools`).

The record writes and playbook runs are the framework's too; the connector
supplies their policy (the persona's may_write scope, `register_record_tools`;
its run gate, auto-run list and allowlist flag, `register_playbook_tools`). The
tool that stays here is ZTPF's template test. They still
reach into the library's REGISTRY directly, reusing its ToolSpec / schema-builder
internals (D5 = Option A).
"""
from __future__ import annotations

import functools
import inspect
from typing import Any

# Internal library surface (accepted coupling under Option A).
from fsr_playbooks.llm.tools import (
    REGISTRY,
    SAFE_TOOLS,
    TOOL_TIERS,
    ToolSpec,
    TOOL_SCHEMA_OVERRIDES,
    _build_schema,
)


def register_triage_tools() -> None:
    """Give the framework's record and hunt tools the connector's context.
    Idempotent. (The record a turn is mounted on needs no hook: the tools read
    it from the framework's session state, which the connector binds per turn.)

    - which connector backs each hunt family this turn (the one the
      availability pass found ready, e.g. FortiSIEM v2 or v1);
    - this connector's name, so a hunt step lands on the build trace as a
      `call_mcp_tool` step on it;
    - the failed-run finder `why_did_playbook_fail` uses on a live box.
    """
    from fsr_playbooks.mcp_server import _shared, set_failed_run_provider
    from fsr_playbooks.mcp_server.tools_triage import list_recent_failed_runs

    from . import source_availability
    from ._common import connector_name

    _shared.set_source_connector_resolver(source_availability.resolve_connector)
    _shared.set_host_connector_name(connector_name)
    set_failed_run_provider(list_recent_failed_runs)


def register_record_tools() -> None:
    """Make the persona's ``may_write`` scope the write policy for the
    framework's ``create_record`` / ``update_record``. Idempotent.

    The framework registers both at tier 3 (approval-carded), triage-only, with
    its pre-card validator; that validator and the tool body both ask this gate,
    so an out-of-scope write is refused before a card is built. They surface
    only through a persona's ``tools.allow`` (the connector's
    ``_BUILD_ONLY_TOOLS`` keeps them out of the default triage slice)."""
    from fsr_playbooks.mcp_server import _shared

    from . import tools_records

    _shared.set_record_write_gate(tools_records._write_gate)


_ZTPF_TIER = 1  # test_template is read-only (render + lint) -- auto-confirm.

# test_template renders `script` against `exampleJinjaVars` and lints the Jinja.
# Precise schema (the auto-builder would emit a bare `object` for `context`).
_ZTPF_SCHEMA_OVERRIDES: dict[str, dict] = {
    "test_template": {
        "type": "object",
        "required": [],
        "additionalProperties": False,
        "properties": {
            "module": {"type": "string", "minLength": 1,
                       "description": "module the record lives in "
                                      "(default ztpf_templates)."},
            "uuid": {"type": "string",
                     "description": "the record's UUID; its example vars are the "
                                    "render context."},
            "field": {"type": "string",
                      "description": "the record field holding the Jinja to render, "
                                     "e.g. outputTemplateJSON; needed when the record "
                                     "has several."},
            "script": {"type": "string",
                       "description": "an unsaved Jinja body to render instead of "
                                      "the record's (an edit before saving it)."},
            "context": {"type": "object", "additionalProperties": True,
                        "description": "render vars; overrides the record's "
                                       "exampleJinjaVars."},
        },
    },
}


def register_ztpf_tools() -> None:
    """Register the ZTPF authoring tool ``test_template`` into the library
    REGISTRY at **tier 1** (read-only render + lint -- auto-confirm, no card).

    Like the record-write tools it is added to ``TRIAGE_ONLY_TOOLS`` (so the
    built-in *build* slice excludes it) and to the connector's
    ``_BUILD_ONLY_TOOLS`` (so the *triage* slice excludes it). Net: it surfaces
    ONLY through a persona's ``tools.allow`` -- no behavior change for any
    non-persona session. Idempotent."""
    from . import tools_ztpf
    try:
        from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    except ImportError:
        TRIAGE_ONLY_TOOLS = None
    for name in ("test_template",):
        fn = getattr(tools_ztpf, name)
        TOOL_TIERS[name] = _ZTPF_TIER
        if name not in SAFE_TOOLS:
            SAFE_TOOLS.append(name)
        if TRIAGE_ONLY_TOOLS is not None:
            TRIAGE_ONLY_TOOLS.add(name)
        if name in REGISTRY:
            continue
        desc = (inspect.getdoc(fn) or f"{name} (no docstring)").strip()
        short = desc.split("\n\n", 1)[0]  # the merged tools' axis list lives here
        REGISTRY[name] = ToolSpec(
            name=name,
            description=short,
            input_schema=(TOOL_SCHEMA_OVERRIDES.get(name)
                          or _ZTPF_SCHEMA_OVERRIDES.get(name)
                          or _build_schema(fn)),
            fn=fn,
            tier=_ZTPF_TIER,
            confirm_mode="auto",
        )


def register_prior_case_tools() -> None:
    """Register ``prior_cases`` (earlier alerts and incidents sharing an
    indicator) into the library REGISTRY at **tier 1** (read-only, no card).

    Unlike ``test_template`` it is a triage tool: it is in ``TRIAGE_ONLY_TOOLS``
    (the build slice leaves it out) and NOT in the connector's
    ``_BUILD_ONLY_TOOLS``, so the default triage slice advertises it. Idempotent."""
    from . import tools_prior_cases
    try:
        from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    except ImportError:
        TRIAGE_ONLY_TOOLS = None
    name = "prior_cases"
    fn = tools_prior_cases.prior_cases
    TOOL_TIERS[name] = 1
    if name not in SAFE_TOOLS:
        SAFE_TOOLS.append(name)
    if TRIAGE_ONLY_TOOLS is not None:
        TRIAGE_ONLY_TOOLS.add(name)
    if name in REGISTRY:
        return
    desc = (inspect.getdoc(fn) or name).strip()
    REGISTRY[name] = ToolSpec(
        name=name,
        description=desc.split("\n\n", 1)[0],
        input_schema=(TOOL_SCHEMA_OVERRIDES.get(name) or _build_schema(fn)),
        fn=fn,
        tier=1,
        confirm_mode="auto",
    )


def register_playbook_tools() -> None:
    """Make the persona the policy for the framework's ``run_playbook`` /
    ``resume_playbook`` / ``list_module_playbooks``. Idempotent.

    The framework registers the tools (run_playbook at a dynamic tier,
    resume/list at tier 1), their schemas and run_playbook's pre-card
    validator. The connector supplies three answers:

    * the run gate: no persona and no human approval => refused;
    * the auto-run resolver: a persona-``auto`` playbook drops to tier 2 (no
      card); everything else stays tier 3 (approval-carded);
    * the run annotation: a playbook outside the persona's advisory ``allow``
      list is flagged ``unlisted`` on its result.

    **run_playbook is advertised in BOTH slices** (changed 2026-07-31; the
    framework keeps it out of both TRIAGE_ONLY_TOOLS and BUILD_ONLY_TOOLS).
    Each list once excluded it from the other's slice, so the intersection was
    empty and the tool was unreachable in every intent. Triage needs it because
    running an existing playbook is a normal analyst action; build needs it
    because "run the playbook I just made" is a normal build-turn follow-up.

    Also wraps the framework's ``find`` so an unwarmed field lookup kicks a
    catalog warm (`_warm_on_unwarmed_fields`)."""
    import fsr_playbooks.llm.tools as _fw_tools
    from fsr_playbooks.mcp_server import _shared

    from . import tools_playbook

    _fw_tools.set_run_playbook_auto_resolver(tools_playbook._auto_resolver)
    _shared.set_playbook_run_gate(tools_playbook._playbook_run_gate)
    _shared.set_playbook_run_annotator(tools_playbook._annotate_run)

    _warm_on_unwarmed_fields()


def _warm_on_unwarmed_fields() -> None:
    """Start a background catalog warm when `find(kind='field')` reports
    ``module_fields_unwarmed`` -- the module is known but its field list was never
    read from this box (the shipped slim DB ships module_fields empty).

    The seam is the registry entry's own fn (a `dataclasses.replace` of the spec),
    so the framework's `find` is unchanged. The result is returned as the
    framework built it: the warm is non-blocking (`_trigger_background_warmup`
    returns at once and is single-flight), and the lookup the model made fails
    once, with a retry hint, instead of the turn stalling on a ~16s walk. Idempotent.
    """
    import dataclasses

    spec = REGISTRY.get("find")
    if spec is None or getattr(spec.fn, "_warms_unwarmed_fields", False):
        return
    REGISTRY["find"] = dataclasses.replace(
        spec, fn=with_unwarmed_field_warm(spec.fn))


# The connector's "start a catalog warm" callable, registered by ops_warmup at
# its import (`set_unwarmed_field_kick`). Held here rather than imported from the
# connector: this package loads as top-level `fsr_soc_triage` on the box and in
# tests, so a relative `..ops_warmup` import is not reliably resolvable.
_unwarmed_field_kick: Any = None


def set_unwarmed_field_kick(fn: Any) -> None:
    """Register the zero-arg callable that starts a background catalog warm."""
    global _unwarmed_field_kick
    _unwarmed_field_kick = fn


def with_unwarmed_field_warm(inner):
    """Wrap a `find` fn so a ``module_fields_unwarmed`` answer kicks the warm.

    The result is returned unchanged. Split out of `_warm_on_unwarmed_fields` so
    the seam is testable with a stand-in fn; the kick is non-blocking and
    single-flight (ops_warmup's `_trigger_background_warmup`), and a missing or
    failing kick never fails the lookup."""

    @functools.wraps(inner)
    def _find_warming(**kwargs):
        out = inner(**kwargs)
        if isinstance(out, dict) and out.get("code") == "module_fields_unwarmed":
            kick = _unwarmed_field_kick
            if kick is not None:
                try:
                    kick()
                except Exception:  # noqa: BLE001 -- a lookup must never fail on the warm kick
                    pass
        return out

    _find_warming._warms_unwarmed_fields = True
    return _find_warming


def register_triage_prompt() -> None:
    """Register the connector's triage system prompt with the library loader.

    The published library prunes ``agent/system_prompt_triage.md`` when triage
    moves out, so ``load_intent_prompt("triage")`` would fall back to a short
    inline stub. The full prompt now ships beside this package; seed it into the
    library's prompt cache so ``load_intent_prompt("triage")`` returns it
    verbatim (same Option-A posture as the REGISTRY injection above).
    """
    from pathlib import Path
    from fsr_playbooks.llm.intents import _PROMPT_CACHE, resolve_intent

    md = Path(__file__).resolve().parent / "system_prompt_triage.md"
    text = md.read_text(encoding="utf-8").strip()
    if text:
        _PROMPT_CACHE[resolve_intent("triage")] = text
    # Newer wheels expose a public hook; register the connector's own loader
    # (fragment assembly first, monolith fallback -- the same preference the
    # connector's _load_prompt has) so plan_turn's base_prompt is the
    # connector-authoritative text even if the fragments and the monolith
    # ever diverge. The cache seed above stays as the older-wheel path.
    try:
        from fsr_playbooks.llm.intents import register_intent_prompt
    except ImportError:  # pragma: no cover - pre-hook wheel: cache seed only
        return

    def _triage_prompt() -> str:
        try:
            from . import prompt_assembly
            if prompt_assembly.fragments_available("triage"):
                assembled = prompt_assembly.assemble_triage_prompt().strip()
                if assembled:
                    return assembled
        except Exception:  # noqa: BLE001 - fall back to the monolith
            pass
        return text

    register_intent_prompt("triage", _triage_prompt)
