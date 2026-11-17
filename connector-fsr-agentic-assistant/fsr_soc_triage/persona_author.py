"""Draft a module-scoped assistant persona for a stated task.

This is the drafting engine behind the persona builder (docs/PERSONA_BUILDER_PLAN.md):
the assistant authors a *good* persona -- one that carries the durable, expensive
-to-relearn knowledge of a module (its data model, how to work it, what it must
NOT touch) -- instead of the analyst hand-writing Key Store JSON.

It does NOT write anything. ``draft_persona`` returns a ``{value, jSONValue}``
Key Store record plus the ``validate_persona_record`` findings; saving that record
is a separate, approval-gated step (the CLI's ``--save``, or P2's offer card). The
drafter never sees raw record values -- :func:`gather_module_knowledge` passes
field *names* and relation *targets* only, so no record data leaks into the prompt.

The output contract is exactly the Key Store persona shape
(``value`` = system prompt, ``jSONValue`` = tools/may_write/capabilities/ui), so a
drafted persona resolves through the same path a hand-authored one does.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

from .profiles import KEY_PREFIX, validate_persona_record

# A good persona carries four things; the drafter is told to produce all four and
# to ground the data model / procedure in the real module, not guess. This mirrors
# the hand-written gold standard (the ZTPF orchestration persona).
_AUTHOR_SYSTEM = (
    "You author ONE FortiSOAR Agentic Assistant persona: the durable instructions "
    "that make the assistant, when mounted on a given module, an expert at a "
    "specific task on that module -- so it never has to relearn the module's shape "
    "every turn.\n\n"
    "A good persona carries FOUR things, and you must produce all four:\n"
    "1. ROLE + SCOPE: who this assistant is on this module, and -- just as "
    "important -- what it leaves to other assistants, so it stays on task.\n"
    "2. DATA MODEL: the module's own fields and its key relations (which field "
    "links to which other module). Use ONLY the fields and relations given to you "
    "below; never invent a field. This is the knowledge that is expensive to "
    "relearn -- spell it out.\n"
    "3. PROCEDURE: how to work this module well -- which tool to reach for first, "
    "when to read/validate before a write, how to report. Ground in the record on "
    "screen first; follow links instead of guessing.\n"
    "4. CONFIG: a tool allowlist drawn ONLY from the catalog given below; write "
    "scope (may_write) naming only the module(s) this persona should ever write; "
    "and a short widget framing block.\n\n"
    "Rules:\n"
    "- Give write tools (create_record/update_record) ONLY if the task authors or "
    "edits records, and then list every writable module in may_write. A read-only "
    "persona has an empty may_write.\n"
    "- Set capabilities.grounding true ONLY for an investigation/runbook persona "
    "that should carry case state across turns; leave it out for an authoring "
    "persona.\n"
    "- The system prompt is plain analyst-facing guidance: no tool JSON, no enum "
    "values, no emoji, hyphens not dashes.\n\n"
    "WHERE EACH PART GOES -- this is strict:\n"
    "- `value` holds ONLY parts 1-3 (role+scope, data model, procedure). Do NOT "
    "write a CONFIG section, tool names, or may_write into `value`.\n"
    "- Part 4 goes ENTIRELY into `jSONValue` as structured JSON. `jSONValue` must "
    "NOT be empty: `tools.allow` must be a non-empty array of tool names taken "
    "verbatim from the catalog below, and `label`/`ui` must be filled.\n\n"
    "Reply with EXACTLY ONE JSON object and nothing else:\n"
    '{"value": "<the system prompt, the four parts above woven into prose>", '
    '"jSONValue": {"label": "<short display name>", '
    '"tools": {"allow": ["<tool>", ...]}, '
    '"may_write": ["<module>", ...], '
    '"capabilities": {"grounding": false}, '
    '"ui": {"label": "<short name>", "greeting": "<one line, may use {module}/{name}>", '
    '"placeholder": "<input hint>"}}}'
)


def gather_module_knowledge(
    client: Any, module: str, *, samples: int = 3,
) -> dict[str, Any]:
    """Read a module's real shape from a few records: field NAMES and relation
    targets only -- never raw values, so no record data reaches the drafter.

    Returns ``{module, fields, relations}`` where ``relations`` maps a relation
    field to the module it points at (derived from the related record's ``@id``
    path). An empty module (no records, or an unreadable one) yields empty lists
    rather than raising -- the drafter still produces a prompt-only persona.
    """
    fields: set[str] = set()
    relations: dict[str, str] = {}
    try:
        r = client.get(f"/api/3/{module}", params={"$limit": max(1, samples)})
        rows = r.get("hydra:member", r) if isinstance(r, dict) else r
    except Exception:  # noqa: BLE001 - knowledge is best-effort, never fatal
        rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            if k.startswith("@") or k.startswith("hydra:"):
                continue
            fields.add(k)
            target = _relation_target(v)
            if target:
                relations[k] = target
    return {"module": module, "fields": sorted(fields), "relations": relations}


def _relation_target(value: Any) -> Optional[str]:
    """The related module name from a relation value's ``@id`` (``/api/3/<mod>/<uuid>``),
    or None when the value is a scalar/plain field. Looks one level into a list."""
    item = value[0] if isinstance(value, list) and value else value
    if isinstance(item, dict):
        iri = item.get("@id")
        if isinstance(iri, str):
            m = re.match(r"/api/3/([^/]+)/", iri)
            if m:
                return m.group(1)
    return None


def default_known_tools() -> Optional[set[str]]:
    """The connector's full tool catalog, for lint and for telling the drafter
    which tools exist. None when the registry isn't importable (lint then skips
    the unknown-tool check rather than failing)."""
    try:
        from fsr_playbooks.llm.tools import anthropic_tools
        return {t["name"] for t in anthropic_tools() if t.get("name")}
    except Exception:  # noqa: BLE001
        return None


def _user_brief(module: str, goal: str, knowledge: dict[str, Any],
                known_tools: Optional[set[str]]) -> str:
    rel = knowledge.get("relations") or {}
    rel_txt = ("\n".join(f"  - {f} links to module {t}" for f, t in sorted(rel.items()))
               or "  (none detected)")
    fields = knowledge.get("fields") or []
    fields_txt = ", ".join(fields) if fields else "(no records readable; author prompt-only)"
    tools_txt = (", ".join(sorted(known_tools)) if known_tools
                 else "(catalog unavailable; use standard tool names)")
    return (
        f"Module: {module}\n"
        f"Task the analyst wants this assistant to be expert at:\n{goal}\n\n"
        f"The module's fields (use only these in the DATA MODEL):\n  {fields_txt}\n"
        f"Relations:\n{rel_txt}\n\n"
        f"Tools you may put in tools.allow (use only these names):\n  {tools_txt}\n"
    )


def _parse_record(said: str) -> Optional[dict[str, Any]]:
    """Extract the drafted ``{value, jSONValue}`` object from the model reply.
    None when no JSON object with a usable ``value`` is present."""
    m = re.search(r"\{.*\}", said or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("value"), str):
        return None
    rec: dict[str, Any] = {"value": obj["value"]}
    if isinstance(obj.get("jSONValue"), dict):
        rec["jSONValue"] = obj["jSONValue"]
    return rec


async def _draft_async(provider: Any, module: str, goal: str,
                       knowledge: dict[str, Any], known_tools: Optional[set[str]],
                       timeout: float) -> Optional[dict[str, Any]]:
    from fsr_playbooks.llm.provider import Message, TextEvent

    async def _go() -> str:
        said = ""
        async for ev in provider.stream(
                system=_AUTHOR_SYSTEM,
                messages=[Message(role="user",
                                  content=_user_brief(module, goal, knowledge, known_tools))],
                tools=[], tags={"intent": "persona_author"}, max_tool_turns=1):
            if isinstance(ev, TextEvent):
                said += ev.text
        return said

    try:
        said = await asyncio.wait_for(_go(), timeout)
    except Exception:  # noqa: BLE001
        return None
    return _parse_record(said)


def draft_persona(
    provider: Any, module: str, goal: str,
    knowledge: Optional[dict[str, Any]] = None, *,
    known_tools: Optional[set[str]] = "__default__",  # type: ignore[assignment]
    timeout: float = 90.0,
) -> tuple[Optional[dict[str, Any]], list]:
    """Draft a Key Store persona record for ``module`` + ``goal``.

    Returns ``(record, issues)``. ``record`` is a ``{value, jSONValue}`` dict
    ready for review/save, or None when the model produced nothing usable.
    ``issues`` is :func:`validate_persona_record`'s findings (empty == clean);
    a record with an ``error`` issue must not be saved as is.

    ``known_tools`` defaults to the live catalog (for both the drafter's tool
    menu and the lint); pass an explicit set in tests, or None to skip the
    unknown-tool check.
    """
    if known_tools == "__default__":
        known_tools = default_known_tools()
    if knowledge is None:
        knowledge = {"module": module, "fields": [], "relations": {}}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        raise RuntimeError("draft_persona is sync; call _draft_async under a running loop")
    rec = asyncio.run(_draft_async(provider, module, goal, knowledge, known_tools, timeout))
    if rec is None:
        return None, []
    issues = validate_persona_record(rec, known_tools=known_tools)
    return rec, issues


def key_for(module: str) -> str:
    """The Key Store ``key`` a persona for ``module`` is saved under."""
    return f"{KEY_PREFIX}{module}"


def summarize(rec: dict[str, Any]) -> str:
    """A short, human-readable summary of what a drafted persona grants -- what
    the approver sees before Save (the allowlist and write scope in plain words)."""
    jv = rec.get("jSONValue") or {}
    allow = (jv.get("tools") or {}).get("allow") or []
    may_write = jv.get("may_write") or []
    label = jv.get("label") or "(unnamed)"
    grounding = bool((jv.get("capabilities") or {}).get("grounding"))
    lines = [
        f"Persona: {label}",
        f"Tools it may use: {', '.join(allow) if allow else '(the module default)'}",
        f"May write to: {', '.join(may_write) if may_write else '(read-only)'}",
        f"Carries case state across turns (grounding): {'yes' if grounding else 'no'}",
    ]
    return "\n".join(lines)
