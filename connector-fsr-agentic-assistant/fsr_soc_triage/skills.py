"""Learned house rules ("skills") -- records in the ``assistant_skills`` module.

A **persona** (see :mod:`profiles`) says *who the assistant is* on a module -- its
identity prompt and tool allowlist. A **skill** is one granular, editable house
rule that shapes *how the generic tools are used* for a task on that module,
without touching connector code:

    "when listing playbooks, do not offer any whose name contains 'deprecated'"
    "when summarizing run groups, include how long ago each ran"
    "'run pending steps' means run the playbook 'Create Run Group and Execute Steps'"

Every one of those came from an analyst FIXME on a live box -- the corrections the
`session_analyze --replay` view surfaces. Storing them as records means:

  * **the tools stay generic** -- a skill is data, not a tool branch, so the same
    ``list_module_playbooks`` serves every deployment and the rule is editable
    without a connector release;
  * **the agent can write them** -- a skill is created/updated through the SAME
    persona-gated, approval-carded ``create_record`` / ``update_record`` the
    write tools already provide (module = ``assistant_skills``), so a FIXME can
    capture itself for next session;
  * **the user owns them** -- edited in a normal FortiSOAR grid, with record RBAC
    and history for free.

Module shape (one record per rule):

    scope_module   the module the rule applies to (e.g. ``ztpf_devices``); a
                   record with no/blank scope is a GLOBAL rule (applies anywhere)
    trigger        short human name for the rule ("list pending steps")
    instruction    the rule text injected into the system prompt
    enabled        bool -- a disabled rule is kept for history but not applied
    priority       int -- higher sorts earlier in the injected block
    source         "analyst" | "agent" -- provenance (who authored it)

Design posture -- **fail-open**, exactly like :mod:`profiles`: any read/parse
error, or an ``assistant_skills`` module that doesn't exist yet, yields an empty
list so the turn falls back to persona-only behavior. A malformed skill record
must never break a turn.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

# The module skills live in. A bare name; the loader hits `/api/3/<module>`.
SKILL_MODULE = "assistant_skills"

# Field names on an `assistant_skills` record (FSR camel-cases custom fields, so
# the stored name is `scopeModule`, not `scope_module`).
F_SCOPE = "scopeModule"
F_TRIGGER = "trigger"
F_INSTRUCTION = "instruction"
F_ENABLED = "enabled"
F_PRIORITY = "priority"
F_SOURCE = "source"

# Upper bound on the page we pull for a module. Skill sets are small (a handful
# per module); this is a runaway guard, not a paging strategy.
_SCAN_LIMIT = 100


class Skill(BaseModel):
    """One resolved, validated house rule for a module."""

    model_config = ConfigDict(extra="ignore")

    trigger: str = ""
    instruction: str
    scope_module: Optional[str] = None
    enabled: bool = True
    priority: int = 0
    source: Optional[str] = None
    uuid: Optional[str] = None


def _flatten(value: Any) -> Any:
    """Collapse a crudhub picklist/relationship wrapper to its display value, so
    ``enabled``/``source`` read the same whether the field is a scalar or a
    ``{itemValue: ...}`` picklist."""
    if isinstance(value, dict):
        if "itemValue" in value:
            return value.get("itemValue")
        return value.get("name") or value.get("value")
    return value


def parse_skill_record(rec: Any) -> Optional[Skill]:
    """Turn one `assistant_skills` record dict into a :class:`Skill`, or ``None``
    when it is missing an instruction or otherwise unusable. Never raises -- a
    malformed record is dropped, not fatal. Kept client-free so it is unit
    testable against literal records."""
    try:
        if not isinstance(rec, dict):
            return None
        instruction = rec.get(F_INSTRUCTION)
        if not isinstance(instruction, str) or not instruction.strip():
            return None
        enabled = _flatten(rec.get(F_ENABLED, True))
        # A picklist "No"/"false" string, or a real bool -- treat only an explicit
        # falsey as disabled; absent/unknown stays enabled (fail-open to applied).
        if isinstance(enabled, str):
            enabled_b = enabled.strip().lower() not in ("false", "no", "0", "off")
        else:
            enabled_b = bool(enabled) if enabled is not None else True
        raw_prio = rec.get(F_PRIORITY, 0)
        priority = raw_prio if isinstance(raw_prio, int) else 0
        scope = rec.get(F_SCOPE)
        scope = scope.strip() if isinstance(scope, str) and scope.strip() else None
        trig = rec.get(F_TRIGGER)
        trig = trig.strip() if isinstance(trig, str) else ""
        src = _flatten(rec.get(F_SOURCE))
        return Skill(
            trigger=trig,
            instruction=instruction.strip(),
            scope_module=scope,
            enabled=enabled_b,
            priority=priority,
            source=src if isinstance(src, str) else None,
            uuid=rec.get("uuid") or (rec.get("@id") or "").rsplit("/", 1)[-1] or None,
        )
    except Exception:  # noqa: BLE001 -- fail-open: a bad record contributes nothing.
        return None


def _members(resp: Any) -> list[dict]:
    """The `hydra:member` list from a crudhub JSON body, or ``[]``."""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    members = data.get("hydra:member") if isinstance(data, dict) else None
    return [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []


def load_skills(client: Any, module: str,
                get_with_reauth: Any = None) -> list[Skill]:
    """Resolve the enabled skills that apply to ``module``: rules scoped to it
    PLUS global rules (no scope), highest ``priority`` first.

    Fail-open in every failure mode -- no client, a missing ``assistant_skills``
    module (404), a transient read error, or a garbled body all yield ``[]`` so
    the turn keeps its persona-only behavior. This mirrors :func:`profiles.load_profile`.
    """
    if client is None or not module or not isinstance(module, str):
        return []
    if get_with_reauth is None:
        try:
            from fsr_playbooks.mcp_server.tools_triage import _get_with_reauth as get_with_reauth
        except Exception:  # noqa: BLE001
            return []

    base = getattr(client, "base_url", "") or ""
    url = f"{base}/api/3/{SKILL_MODULE}?$limit={_SCAN_LIMIT}"
    try:
        r = get_with_reauth(client, url)
    except Exception:  # noqa: BLE001 -- transient/transport; no skills this turn.
        return []
    if getattr(r, "status_code", None) != 200:
        return []  # 404 = module not created yet; anything else = transient.

    skills: list[Skill] = []
    for rec in _members(r):
        s = parse_skill_record(rec)
        if s is None or not s.enabled:
            continue
        # A skill with no scope is global; else it must match the mounted module.
        if s.scope_module is None or s.scope_module == module:
            skills.append(s)
    # Highest priority first; stable within a priority (server order preserved).
    skills.sort(key=lambda s: s.priority, reverse=True)
    return skills


def render_skills_block(skills: list[Skill], module: str) -> str:
    """Render resolved skills as a system-prompt section, or ``""`` when none.

    The block is appended AFTER the persona prompt (recency), and is framed as
    authoritative house rules the analyst/agent captured -- the model must honor
    them over its own defaults."""
    if not skills:
        return ""
    lines = [
        "\n\n## Learned house rules",
        (f"These are house rules captured for `{module}` by the analyst (and by "
         "you, in past sessions). They OVERRIDE your defaults -- follow them "
         "exactly. If a rule conflicts with a general instinct, the rule wins."),
    ]
    for s in skills:
        label = f" ({s.trigger})" if s.trigger else ""
        lines.append(f"- {s.instruction}{label}")
    return "\n".join(lines)


def skills_for_prompt(client: Any, module: str,
                      get_with_reauth: Any = None) -> str:
    """Convenience: load + render in one call. ``""`` when there are none or on
    any failure. This is the single entry point the turn uses."""
    try:
        return render_skills_block(load_skills(client, module, get_with_reauth),
                                   module)
    except Exception:  # noqa: BLE001 -- never let skills break a turn.
        return ""
