"""Module-scoped assistant personas, defined in the FortiSOAR Key Store.

A **persona** lets the agentic assistant carry a per-module system prompt and a
per-module tool subset, so that -- mounted on a given module -- it helps *author*
records/playbooks for that domain instead of running alert triage. See
`fsr_all_widgets/docs/plans/module-scoped-assistant-personas.md` for the full
design.

Maintenance model (the whole point -- no connector edits to add a persona):
the connector ships the full tool catalog once; a user/admin defines a persona
by creating **one Key Store (`keys`) record** in the FortiSOAR UI. This module
is the read + parse half -- it turns that record into a typed :class:`Profile`.

Key Store convention (§2 of the plan), one entry per module:

    key        fsr_assistant_profile:<module>   (e.g. ...:ztpf_templates)
    value       the system prompt (plaintext -- Key Store values need not be secret)
    jSONValue  {"tools": {"allow": [...]}, "may_write": [...],
                "bind_modules": [...], "version": 1}
    notes       human description

Design posture -- **fail-open**, mirroring the case-state spine and the live
re-auth wrappers: any read/parse error yields ``None`` so the caller falls back
to built-in triage/build behavior. A malformed persona record must never break a
turn.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# Key convention. One `keys` entry per module: `key = KEY_PREFIX + <module>`.
KEY_PREFIX = "fsr_assistant_profile:"

# ---------------------------------------------------------------------------
# `assistant_personas` module (P0 -- persona substrate).
#
# Personas historically lived only as opaque JSON blobs in the Key Store (see
# KEY_PREFIX above). P0 gives them a first-class, grid-editable custom module --
# the same treatment `assistant_skills` already has -- so a user edits a persona
# as record fields instead of hand-editing Key Store JSON. The runtime reads the
# module FIRST and falls back to the Key Store, so existing Key Store personas
# keep resolving unchanged (back-compat).
#
# FortiSOAR modules have no native list/object field type, so the list/nested
# fields ride as JSON-bearing Long Text columns; `_json_field` parses them
# fail-open. The flat/human fields are first-class columns.
PERSONA_MODULE = "assistant_personas"
PF_SCOPE = "scopeModule"        # Text  -- the module this persona serves (== Profile.module)
PF_LABEL = "label"              # Text
PF_PROMPT = "prompt"            # Long Text -- the system prompt (== keys record `value`)
PF_VERSION = "version"          # Integer
PF_ENABLED = "enabled"          # Checkbox -- a disabled row resolves to no persona
PF_TOOLS_ALLOW = "toolsAllow"       # Long Text JSON array
PF_MAY_WRITE = "mayWrite"           # Long Text JSON array
PF_RP_ALLOW = "runPlaybookAllow"    # Long Text JSON array
PF_RP_AUTO = "runPlaybookAuto"      # Long Text JSON array
PF_BIND = "bindModules"             # Long Text JSON array
PF_ADVANCED = "advancedJson"        # Long Text JSON object: {mcp, capabilities, ui}

# Upper bound on the Key Store page we scan for the `bind_modules` fallback (a
# persona that serves several modules from one record). The direct per-module
# lookup is one indexed equality query; this scan only runs on a miss. Key Store
# instances are small (tens of entries); if a deployment ever exceeds this we log
# a truncation note rather than silently under-matching.
_SCAN_LIMIT = 200


class PersonaCapabilities(BaseModel):
    """The dynamic turn-shaping machinery a persona opts into (P3).

    Historically these powers were welded to the string ``"triage"`` -- the only
    intent that ran preflight, built a CaseState, and carried a continuation
    directive across turns. A persona now declares them explicitly, so a
    non-triage persona (an IR runbook, a hunt assistant) can earn grounding
    without connector code. ``grounding`` is the umbrella flag: it gates the
    whole preflight → CaseState → continuation-directive spine as one unit (they
    always co-vary today). Absent/unknown ⇒ off -- a persona without an explicit
    declaration keeps its prompt-only behavior.
    """

    model_config = ConfigDict(extra="forbid")

    grounding: bool = False


# Built-in intent capabilities (no persona layered). `triage` grounds -- it runs
# the record preflight, builds a CaseState, and continues the case across turns;
# `build` (authoring) does not. This is the ONLY place triage-ness maps to
# grounding now; every other path gates on `PersonaCapabilities`.
BUILTIN_CAPABILITIES: dict[str, PersonaCapabilities] = {
    "triage": PersonaCapabilities(grounding=True),
    "build": PersonaCapabilities(grounding=False),
}


class PersonaCapabilityDedup(BaseModel):
    """One curated↔MCP capability-overlap override for a persona (M2).

    ``curated`` is the first-class helper that WINS this capability; ``mcp`` are
    the materialized MCP tool name(s) it supersedes (dropped from the surface when
    the curated tool is present). Merged over the connector's static base map."""

    model_config = ConfigDict(extra="forbid")

    curated: str
    mcp: list[str] = Field(default_factory=list)


class PersonaMcp(BaseModel):
    """Per-module MCP *surfacing policy* carried by a persona (M2).

    The connector's ``mcp_allowlist`` config holds *what's connected + creds*;
    this block holds *how it surfaces on this module* -- the clean split. Both
    fields override the connector's static affordance-class defaults:

    - ``servers``: the MCP server keys (the ``<server>`` in ``mcp_<server>__…``)
      surfaced on this page. ``None`` ⇒ no override (use the affordance default);
      ``[]`` ⇒ surface NO MCP servers on this page.
    - ``capability_dedup``: curated↔MCP overrides merged OVER the static base map,
      keyed by capability name.
    """

    model_config = ConfigDict(extra="forbid")

    servers: Optional[list[str]] = None
    capability_dedup: dict[str, PersonaCapabilityDedup] = Field(default_factory=dict)


# Providers a persona's `models` map may name (operations._selected_provider).
_PROVIDERS = frozenset({"openai", "anthropic"})


class Profile(BaseModel):
    """A resolved, validated persona for one mounted module.

    Built from a `keys` record by :func:`parse_profile_record`. ``module`` is the
    module the profile was resolved *for* (which may differ from the record's own
    module when matched via ``bind_modules``).
    """

    model_config = ConfigDict(extra="forbid")

    module: str
    prompt: str
    tools_allow: list[str] = Field(default_factory=list)
    # Per-module MCP surfacing policy (M2). ``None`` ⇒ no persona override; the
    # module falls back to the connector's static affordance-class default.
    mcp: Optional[PersonaMcp] = None
    # Opt-in turn-shaping capabilities (P3). ``None`` ⇒ the persona made no
    # declaration and keeps prompt-only behavior (no grounding); a present value
    # means the persona explicitly chose its capabilities.
    capabilities: Optional[PersonaCapabilities] = None
    may_write: list[str] = Field(default_factory=list)
    # Playbooks this persona may trigger via ``run_playbook`` (by name or uuid).
    # ``run_playbook_allow`` is the authorization boundary -- an empty list means
    # the persona runs no playbooks. ``run_playbook_auto`` ⊆ allow names the
    # subset that auto-runs WITHOUT an approval card (read-only validation
    # playbooks the admin trusts); anything in allow but not auto is still
    # approval-carded. Parsed from ``jSONValue.run_playbook.{allow,auto}``.
    run_playbook_allow: list[str] = Field(default_factory=list)
    run_playbook_auto: list[str] = Field(default_factory=list)
    bind_modules: list[str] = Field(default_factory=list)
    version: int = 1
    # Optional widget-framing hints (jSONValue.ui). Opaque to the connector -- it
    # never renders them; they ride out via the `resolve_persona` operation so
    # the drawer can drop SOC-triage framing (greeting, quick-action deck,
    # placeholder, footer) for an authoring persona. See the widget's
    # `resolvePersona` + the module-scoped-personas plan (Phase 4).
    label: Optional[str] = None
    ui: Optional[dict] = None
    # The model this persona's turns run on, per provider: {"openai": "gpt-5.4"}.
    # Authoring a template or a profile is harder than triage, and a small model
    # fails it in ways no tool hint fixes; the persona picks the model for its
    # job while everything else stays on the connector config's model. Keyed by
    # provider so a persona never hands an OpenAI model id to an Anthropic
    # config. Empty = the config's model.
    models: dict[str, str] = Field(default_factory=dict)

    def model_for(self, provider: str) -> Optional[str]:
        """The model this persona asks for under ``provider``, or None."""
        m = self.models.get(provider)
        return m.strip() if isinstance(m, str) and m.strip() else None

    def may_write_module(self, module: str) -> bool:
        """Whether record writes to ``module`` are permitted by this persona.

        Empty/absent ``may_write`` ⇒ read-only persona (no writes anywhere)."""
        return bool(module) and module in self.may_write

    def may_run_playbook(self, playbook: str) -> bool:
        """Whether the persona permits triggering ``playbook`` at all (by name
        or uuid). Empty/absent ``run_playbook_allow`` ⇒ runs no playbooks."""
        return bool(playbook) and playbook in self.run_playbook_allow

    def may_autorun_playbook(self, playbook: str) -> bool:
        """Whether ``playbook`` may run WITHOUT an approval card. True only when
        it is both allowed and explicitly in the ``auto`` subset."""
        return (bool(playbook) and playbook in self.run_playbook_auto
                and self.may_run_playbook(playbook))


# The turn's persona and mounted record live in the framework's SessionState
# (`fsr_playbooks.llm.session_state`), beside the rest of what the host binds
# for a turn, so one bind covers a turn and one load covers a resume. These
# accessors keep their names for the tools that read them.
#
# They used to be two ContextVars here, and this package is importable under
# two names on a live box (the top-level `fsr_soc_triage` and the loader's
# package-qualified copy), each with its own vars: whichever copy the platform
# loaded second read None for every binding (#148). SessionState lives in
# `fsr_playbooks`, which has one name, so the two copies share it.


def set_active_profile(profile: "Optional[Profile]"):
    """Bind the active persona for this turn; returns a reset token."""
    from fsr_playbooks.llm import session_state
    return session_state.update(profile=profile)


def reset_active_profile(token) -> None:
    """Restore the prior active-persona binding (pair with set_active_profile)."""
    from fsr_playbooks.llm import session_state
    session_state.reset(token)


def get_active_profile() -> "Optional[Profile]":
    """The persona bound for the current turn, or None."""
    from fsr_playbooks.llm import session_state
    return session_state.current().profile


def set_active_record_iri(iri: "Optional[str]"):
    """Bind the mounted record IRI for this turn; returns a reset token. A
    record-action playbook needs it as its selected record: `run_playbook`
    defaults to it when the agent omits `record`."""
    from fsr_playbooks.llm import session_state
    return session_state.update(record_iri=iri or None)


def reset_active_record_iri(token) -> None:
    """Undo a :func:`set_active_record_iri` using its token."""
    from fsr_playbooks.llm import session_state
    session_state.reset(token)


def get_active_record_iri() -> "Optional[str]":
    """The IRI of the record the current turn is mounted on, or None."""
    from fsr_playbooks.llm import session_state
    return session_state.current().record_iri


def get_created_record_source() -> "Optional[Any]":
    """The host's live list of records this conversation created
    (SessionState.created_records): a zero-argument callable returning IRIs,
    or None when the host bound none or the framework predates the field."""
    from fsr_playbooks.llm import session_state
    return getattr(session_state.current(), "created_records", None)


def record_iri_parts(iri: Any) -> "Optional[tuple[str, str]]":
    """``(module, uuid)`` of a full ``/api/3/<module>/<uuid>`` IRI, or None.
    The shape the record tools document; anything else is not comparable."""
    if not isinstance(iri, str):
        return None
    parts = [p for p in iri.strip().split("/") if p]
    if len(parts) != 4 or parts[:2] != ["api", "3"]:
        return None
    return parts[2], parts[3].lower()


def record_iri_from_entity(entity: Any) -> "Optional[str]":
    """The record IRI carried by a widget-supplied ``entity`` (contract §2), or
    None. Prefers the explicit ``iri``; else composes ``/api/3/<module>/<uuid|id>``.
    Single source for both turn-drive entries (``operations.chat_turn`` and the
    ``tools_agent.chat`` MCP tool) so the two can't drift -- the drift that left the
    live path unbound while the MCP path worked."""
    if not isinstance(entity, dict):
        return None
    iri = entity.get("iri")
    if iri:
        return iri
    module = entity.get("module") or entity.get("type")
    ident = entity.get("uuid") or entity.get("id")
    if module and ident:
        return f"/api/3/{module}/{ident}"
    return None


def _as_str_list(value: Any) -> list[str]:
    """Coerce a jSONValue list field into a clean ``list[str]`` (dropping
    non-string / empty members). A malformed scalar becomes an empty list rather
    than raising -- parsing is best-effort."""
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _parse_persona_mcp(raw: Any) -> Optional["PersonaMcp"]:
    """Parse a persona ``jSONValue.mcp`` block into a :class:`PersonaMcp`, or
    ``None`` when absent/garbled (⇒ no override → static affordance default).

    Best-effort, mirroring the fail-open posture of the rest of the parser: a
    non-list ``servers`` or a malformed ``capability_dedup`` entry is dropped
    rather than raising. ``servers`` present-but-non-list ⇒ ``None`` (no override,
    NOT an empty surface) so a typo can't silently blank the page's MCP surface.
    """
    if not isinstance(raw, dict):
        return None
    try:
        sv = raw.get("servers")
        servers = _as_str_list(sv) if isinstance(sv, list) else None
        dedup: dict[str, PersonaCapabilityDedup] = {}
        dd = raw.get("capability_dedup")
        if isinstance(dd, dict):
            for cap, spec in dd.items():
                if (isinstance(cap, str) and cap.strip()
                        and isinstance(spec, dict)
                        and isinstance(spec.get("curated"), str)
                        and spec["curated"].strip()):
                    dedup[cap.strip()] = PersonaCapabilityDedup(
                        curated=spec["curated"].strip(),
                        mcp=_as_str_list(spec.get("mcp")))
        # Nothing usable declared ⇒ no override.
        if servers is None and not dedup:
            return None
        return PersonaMcp(servers=servers, capability_dedup=dedup)
    except Exception:  # noqa: BLE001 -- malformed mcp block ⇒ no override, not fatal.
        return None


def parse_profile_record(rec: Any, module: str) -> Optional[Profile]:
    """Turn one `keys` record dict into a :class:`Profile` for ``module``.

    Returns ``None`` (never raises) when the record is missing, has no prompt
    text, or is otherwise malformed -- the caller then falls back to built-in
    behavior. Kept client-free so it is unit-testable against literal records.

    - ``value`` is the base system prompt; an empty/blank prompt is treated as
      malformed (a persona with no prompt guides nothing).
    - ``jSONValue`` carries the tool allowlist, write scope, extra bound modules,
      and contract version. A missing/garbled ``jSONValue`` degrades to a
      prompt-only, read-only, no-tools-narrowing profile rather than failing.
    """
    try:
        if not isinstance(rec, dict):
            return None
        prompt = rec.get("value")
        if not isinstance(prompt, str) or not prompt.strip():
            return None

        jv = rec.get("jSONValue")
        if not isinstance(jv, dict):
            jv = {}

        tools = jv.get("tools")
        tools_allow = _as_str_list(tools.get("allow")) if isinstance(tools, dict) else []

        raw_version = jv.get("version", 1)
        version = raw_version if isinstance(raw_version, int) else 1

        raw_label = jv.get("label")
        label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else None
        raw_ui = jv.get("ui")
        ui = raw_ui if isinstance(raw_ui, dict) else None

        # Opt-in capabilities (P3). Present-but-garbled degrades to all-off
        # rather than failing the persona; absent ⇒ None (no declaration).
        raw_caps = jv.get("capabilities")
        if isinstance(raw_caps, dict):
            try:
                capabilities = PersonaCapabilities(
                    grounding=bool(raw_caps.get("grounding", False)))
            except Exception:  # noqa: BLE001 -- malformed caps ⇒ all-off, not fatal.
                capabilities = PersonaCapabilities()
        else:
            capabilities = None

        # MCP surfacing policy (M2). Present-but-garbled degrades to None (no
        # override → static affordance default) rather than failing the persona.
        mcp = _parse_persona_mcp(jv.get("mcp"))

        # run_playbook allow/auto (§ persona playbook execution). Auto is
        # intersected with allow so a mis-authored `auto` entry can never grant
        # card-free execution of a playbook that isn't even allowed.
        rp = jv.get("run_playbook")
        rp_allow = _as_str_list(rp.get("allow")) if isinstance(rp, dict) else []
        rp_auto_raw = _as_str_list(rp.get("auto")) if isinstance(rp, dict) else []
        rp_auto = [n for n in rp_auto_raw if n in rp_allow]

        raw_models = jv.get("models")
        models = ({str(k): v.strip() for k, v in raw_models.items()
                   if isinstance(v, str) and v.strip()}
                  if isinstance(raw_models, dict) else {})

        return Profile(
            module=module,
            prompt=prompt.strip(),
            tools_allow=tools_allow,
            may_write=_as_str_list(jv.get("may_write")),
            run_playbook_allow=rp_allow,
            run_playbook_auto=rp_auto,
            bind_modules=_as_str_list(jv.get("bind_modules")),
            version=version,
            label=label,
            ui=ui,
            capabilities=capabilities,
            mcp=mcp,
            models=models,
        )
    except Exception:  # noqa: BLE001 -- fail-open: any parse error ⇒ no persona.
        return None


def _json_field(raw: Any, default: Any) -> Any:
    """Parse an `assistant_personas` JSON-bearing Long Text column, fail-open.

    Accepts an already-decoded value (a live crudhub read may hand back the
    parsed list/dict), a JSON string (the grid stores text), or junk ⇒ ``default``.
    """
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 -- malformed JSON column ⇒ default.
            return default
    return default


def module_record_to_keys_record(rec: Any) -> Optional[dict]:
    """Convert one `assistant_personas` module record into the Key Store record
    shape (`{value, jSONValue}`) that :func:`parse_profile_record` understands.

    Reuses the whole fail-open parser rather than re-validating each field. A
    non-dict record ⇒ ``None``.
    """
    if not isinstance(rec, dict):
        return None
    adv = _json_field(rec.get(PF_ADVANCED), {})
    if not isinstance(adv, dict):
        adv = {}
    jv = {
        "tools": {"allow": _json_field(rec.get(PF_TOOLS_ALLOW), [])},
        "may_write": _json_field(rec.get(PF_MAY_WRITE), []),
        "run_playbook": {
            "allow": _json_field(rec.get(PF_RP_ALLOW), []),
            "auto": _json_field(rec.get(PF_RP_AUTO), []),
        },
        "bind_modules": _json_field(rec.get(PF_BIND), []),
        "version": rec.get(PF_VERSION, 1),
        "label": rec.get(PF_LABEL),
        "ui": adv.get("ui"),
        "capabilities": adv.get("capabilities"),
        "mcp": adv.get("mcp"),
        "models": adv.get("models"),
    }
    return {"value": rec.get(PF_PROMPT), "jSONValue": jv}


def parse_persona_module_record(rec: Any, module: str) -> Optional[Profile]:
    """Parse an `assistant_personas` module record into a :class:`Profile` for
    ``module``, or ``None``. A row with ``enabled`` explicitly False resolves to
    no persona (a soft off-switch without deleting the record). Otherwise defers
    to :func:`parse_profile_record` via the Key-Store-shape adapter."""
    if isinstance(rec, dict) and rec.get(PF_ENABLED) is False:
        return None
    keys_rec = module_record_to_keys_record(rec)
    if keys_rec is None:
        return None
    return parse_profile_record(keys_rec, module)


def profile_to_module_record(profile: "Profile") -> dict:
    """Serialize a :class:`Profile` into an `assistant_personas` module record
    dict (JSON columns as compact strings). Inverse of
    :func:`parse_persona_module_record` for the fields the runtime honors -- the
    round-trip ``parse(serialize(p)) == p`` holds. Used by the Key-Store→module
    migration and by tests."""
    advanced: dict[str, Any] = {}
    if profile.mcp is not None:
        advanced["mcp"] = profile.mcp.model_dump(exclude_none=False)
    if profile.capabilities is not None:
        advanced["capabilities"] = profile.capabilities.model_dump()
    if profile.ui is not None:
        advanced["ui"] = profile.ui
    if profile.models:
        advanced["models"] = dict(profile.models)
    rec: dict[str, Any] = {
        PF_SCOPE: profile.module,
        PF_PROMPT: profile.prompt,
        PF_VERSION: profile.version,
        PF_ENABLED: True,
        PF_TOOLS_ALLOW: json.dumps(profile.tools_allow),
        PF_MAY_WRITE: json.dumps(profile.may_write),
        PF_RP_ALLOW: json.dumps(profile.run_playbook_allow),
        PF_RP_AUTO: json.dumps(profile.run_playbook_auto),
        PF_BIND: json.dumps(profile.bind_modules),
        PF_ADVANCED: json.dumps(advanced) if advanced else "",
    }
    if profile.label is not None:
        rec[PF_LABEL] = profile.label
    return rec


class PersonaIssue(BaseModel):
    """One actionable finding from :func:`validate_persona_record`.

    ``level`` is ``"error"`` (the record won't parse into a usable persona at
    all -- runtime falls back to built-in behavior) or ``"warning"`` (the record
    parses, but something is silently ignored/degraded and probably not what the
    author intended). ``field`` names the offending jSONValue path."""

    level: str
    field: str
    message: str


def validate_persona_record(
    rec: Any, known_tools: Optional[set[str]] = None,
) -> list["PersonaIssue"]:
    """Lint a `keys` persona record and return actionable findings (never
    raises). This is the authoring-time counterpart to the fail-open runtime
    :func:`parse_profile_record`: at runtime a malformed field is silently
    dropped so a turn never breaks; at *save* time we want the author to SEE
    what will be ignored. An empty list means the record is clean.

    ``known_tools`` -- when provided, every ``tools.allow`` entry not in the set
    is flagged as an unknown tool (the connector ships the full catalog; a typo
    would silently narrow the persona to nothing useful). Omit it to skip
    tool-name checking (e.g. when the registry isn't importable in the caller).

    The checks mirror what ``parse_profile_record`` reads, so a clean lint means
    every field the runtime honors was well-formed.
    """
    issues: list[PersonaIssue] = []

    def add(level: str, field: str, message: str) -> None:
        issues.append(PersonaIssue(level=level, field=field, message=message))

    if not isinstance(rec, dict):
        add("error", "(record)", "persona record must be a JSON object")
        return issues

    prompt = rec.get("value")
    if not isinstance(prompt, str) or not prompt.strip():
        add("error", "value",
            "missing/empty prompt -- `value` must be the persona system prompt; "
            "the record will not resolve to a persona")

    jv = rec.get("jSONValue")
    if jv is None:
        return issues  # prompt-only persona; nothing else to check.
    if not isinstance(jv, dict):
        add("warning", "jSONValue",
            "not a JSON object -- tools/capabilities/may_write will be ignored "
            "(prompt-only, read-only persona)")
        return issues

    tools = jv.get("tools")
    if tools is not None:
        if not isinstance(tools, dict) or not isinstance(tools.get("allow"), list):
            add("warning", "tools.allow",
                "not a list -- the tool allowlist is ignored; the persona keeps "
                "the built-in intent's tool slice")
        else:
            allow = tools["allow"]
            bad = [t for t in allow if not isinstance(t, str) or not t.strip()]
            if bad:
                add("warning", "tools.allow",
                    f"non-string/empty entries dropped: {bad!r}")
            if known_tools is not None:
                unknown = sorted({t for t in allow
                                  if isinstance(t, str) and t.strip()
                                  and t not in known_tools})
                if unknown:
                    add("error", "tools.allow",
                        f"unknown tool name(s) {unknown} -- not in the connector "
                        f"tool catalog; the persona would be narrowed to nothing "
                        f"useful. Check for typos.")

    caps = jv.get("capabilities")
    if caps is not None:
        if not isinstance(caps, dict):
            add("warning", "capabilities",
                "not a JSON object -- capability declaration ignored (persona "
                "stays prompt-only, no grounding)")
        else:
            known_caps = set(PersonaCapabilities.model_fields)
            for k, v in caps.items():
                if k not in known_caps:
                    add("warning", f"capabilities.{k}",
                        f"unknown capability -- ignored. Known: {sorted(known_caps)}")
                elif not isinstance(v, bool):
                    add("warning", f"capabilities.{k}",
                        f"non-boolean value {v!r} -- coerced via bool(); use "
                        f"true/false to be explicit")

    models = jv.get("models")
    if models is not None:
        if not isinstance(models, dict):
            add("warning", "models",
                'not a JSON object -- ignored; the persona runs on the config\'s '
                'model. Expected {"openai": "gpt-5.4"}.')
        else:
            for prov, m in models.items():
                if prov not in _PROVIDERS:
                    add("warning", f"models.{prov}",
                        f"unknown provider -- never used. Known: {sorted(_PROVIDERS)}")
                elif not isinstance(m, str) or not m.strip():
                    add("warning", f"models.{prov}",
                        "not a model name -- ignored (the config's model is used)")

    for list_field in ("may_write", "bind_modules"):
        val = jv.get(list_field)
        if val is not None and not isinstance(val, list):
            add("warning", list_field,
                "not a list -- ignored (treated as empty)")

    rp = jv.get("run_playbook")
    if rp is not None:
        if not isinstance(rp, dict):
            add("warning", "run_playbook",
                "not a JSON object -- ignored; the persona runs no playbooks. "
                "Expected {\"allow\": [...], \"auto\": [...]}.")
        else:
            for sub in ("allow", "auto"):
                v = rp.get(sub)
                if v is not None and not isinstance(v, list):
                    add("warning", f"run_playbook.{sub}",
                        "not a list -- ignored (treated as empty)")
            allow = rp.get("allow") if isinstance(rp.get("allow"), list) else []
            auto = rp.get("auto") if isinstance(rp.get("auto"), list) else []
            orphan = [n for n in auto if isinstance(n, str) and n not in allow]
            if orphan:
                add("warning", "run_playbook.auto",
                    f"entries not in `allow` are dropped (auto ⊆ allow): "
                    f"{orphan!r}. A playbook must be allowed before it can "
                    f"auto-run without an approval card.")

    mcp = jv.get("mcp")
    if mcp is not None:
        if not isinstance(mcp, dict):
            add("warning", "mcp",
                "not a JSON object -- MCP surfacing override ignored; the module "
                "keeps the static affordance-class default. Expected "
                "{\"servers\": [...], \"capability_dedup\": {...}}.")
        else:
            sv = mcp.get("servers")
            if sv is not None and not isinstance(sv, list):
                add("warning", "mcp.servers",
                    "not a list -- server override ignored (module keeps the "
                    "affordance default). Use [] to surface NO MCP servers.")
            dd = mcp.get("capability_dedup")
            if dd is not None and not isinstance(dd, dict):
                add("warning", "mcp.capability_dedup",
                    "not a JSON object -- capability overrides ignored.")
            elif isinstance(dd, dict):
                for cap, spec in dd.items():
                    if not isinstance(spec, dict) or not isinstance(
                            spec.get("curated"), str) or not spec["curated"].strip():
                        add("warning", f"mcp.capability_dedup.{cap}",
                            "each entry needs a non-empty string `curated` (the "
                            "winning tool) and an optional `mcp` list -- dropped.")

    version = jv.get("version")
    if version is not None and not isinstance(version, int):
        add("warning", "version",
            f"non-integer {version!r} -- defaults to 1")

    return issues


def _members(resp: Any) -> list[dict]:
    """The `hydra:member` list from a crudhub JSON body, or ``[]``."""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    members = data.get("hydra:member") if isinstance(data, dict) else None
    return [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []


def load_profile(client: Any, module: str,
                 get_with_reauth: Any = None) -> Optional[Profile]:
    """Resolve the persona for ``module`` from the Key Store, or ``None``.

    Thin wrapper over :func:`resolve_profile_status` returning just the profile
    (the historical signature). Prefer ``resolve_profile_status`` when the caller
    needs to distinguish a definitive "no persona" from a transient read failure
    (so a transient miss isn't cached -- see ``operations._resolve_profile``)."""
    return resolve_profile_status(client, module, get_with_reauth)[0]


def resolve_profile_status(
    client: Any, module: str, get_with_reauth: Any = None,
) -> tuple[Optional[Profile], bool]:
    """Resolve the persona for ``module``, returning ``(profile, definitive)``.

    Lookup order (P0):
      1. **The ``assistant_personas`` module** -- the grid-editable substrate.
      2. **The Key Store** (``fsr_assistant_profile:<module>``) -- back-compat for
         personas authored before the module existed / never migrated.

    Each source is tried direct-then-``bind_modules``-scan. A hit in the module
    wins; otherwise the Key Store answers. ``definitive`` follows the same
    contract as before -- True only when the source gave an authoritative answer
    (a parsed hit, or clean 200 reads with no match); a transient transport/non-200
    error yields ``(None, False)`` so the caller fails open WITHOUT caching the
    miss. The combined miss is definitive only when BOTH sources answered cleanly.
    """
    if not module or not isinstance(module, str) or client is None:
        return None, True  # a bad/absent input is a definitive "no persona"

    if get_with_reauth is None:
        from fsr_playbooks.mcp_server.tools_triage import _get_with_reauth as get_with_reauth  # local wrapper

    # 1. Module-first.
    prof, mod_def = _resolve_from_persona_module(client, module, get_with_reauth)
    if prof is not None:
        return prof, True
    # 2. Key Store fallback (unchanged behavior).
    ks_prof, ks_def = _resolve_from_key_store(client, module, get_with_reauth)
    if ks_prof is not None:
        return ks_prof, True
    # A miss is only trustworthy (cacheable) when BOTH sources answered cleanly.
    return None, (mod_def and ks_def)


def _resolve_from_persona_module(
    client: Any, module: str, get_with_reauth: Any,
) -> tuple[Optional[Profile], bool]:
    """Resolve ``module``'s persona from the ``assistant_personas`` module.

    Mirrors the Key Store resolver's direct-then-``bind_modules``-scan shape. A
    404 on the direct read (the module isn't provisioned on this box) is a
    *definitive* "no module persona" so the Key Store fallback's miss can still be
    cached; any other non-200/transport error is transient (``definitive=False``).
    """
    from urllib.parse import quote

    base = getattr(client, "base_url", "") or ""

    direct_clean = False
    try:
        q = quote(module, safe="")
        url = f"{base}/api/3/{PERSONA_MODULE}?{PF_SCOPE}={q}&$limit=1"
        r = get_with_reauth(client, url)
        code = getattr(r, "status_code", None)
        if code == 200:
            direct_clean = True
            members = _members(r)
            if members:
                prof = parse_persona_module_record(members[0], module)
                if prof is not None:
                    return prof, True
        elif code == 404:
            return None, True  # module absent ⇒ definitive "no module persona"
        else:
            return None, False  # transient non-200
    except Exception:  # noqa: BLE001 -- transient; try the scan.
        pass

    # bind_modules fallback -- bounded scan of the module's records.
    try:
        url = f"{base}/api/3/{PERSONA_MODULE}?$limit={_SCAN_LIMIT}"
        r = get_with_reauth(client, url)
        code = getattr(r, "status_code", None)
        if code == 404:
            return None, True
        if code != 200:
            return None, False
        for rec in _members(r):
            binds = _json_field(rec.get(PF_BIND), [])
            if isinstance(binds, list) and module in binds:
                prof = parse_persona_module_record(rec, module)
                if prof is not None:
                    return prof, True
    except Exception:  # noqa: BLE001
        return None, False
    return (None, True) if direct_clean else (None, False)


def _resolve_from_key_store(
    client: Any, module: str, get_with_reauth: Any,
) -> tuple[Optional[Profile], bool]:
    """Resolve ``module``'s persona from the Key Store (the pre-P0 path).

    Lookup order (§2):
      1. Direct: ``GET /api/3/keys?key=fsr_assistant_profile:<module>``.
      2. ``bind_modules`` fallback scan of ``fsr_assistant_profile:*``.
    ``definitive`` semantics unchanged from the original resolver.
    """
    from urllib.parse import quote

    base = getattr(client, "base_url", "") or ""

    # 1. Direct per-module lookup. `direct_clean` tracks whether we got an
    # authoritative (HTTP 200) answer from this read -- a transient transport
    # error leaves it False so a later clean scan can't upgrade the absence to
    # a *definitive* miss (that bug cached a permanent "no persona" on a
    # cold/recycling worker whose first read raised -- plan §7 open item).
    direct_clean = False
    try:
        key = quote(KEY_PREFIX + module, safe="")
        url = f"{base}/api/3/keys?key={key}&$limit=1"
        r = get_with_reauth(client, url)
        if getattr(r, "status_code", None) == 200:
            direct_clean = True
            members = _members(r)
            if members:
                prof = parse_profile_record(members[0], module)
                if prof is not None:
                    return prof, True  # authoritative hit
        else:
            return None, False  # transient: non-200 on the direct lookup
    except Exception:  # noqa: BLE001 -- transient transport error; try the scan.
        pass

    # 2. bind_modules fallback -- bounded scan of the persona namespace.
    try:
        url = f"{base}/api/3/keys?$limit={_SCAN_LIMIT}"
        r = get_with_reauth(client, url)
        if getattr(r, "status_code", None) != 200:
            return None, False  # transient: scan read failed
        for rec in _members(r):
            k = rec.get("key")
            if not isinstance(k, str) or not k.startswith(KEY_PREFIX):
                continue
            jv = rec.get("jSONValue")
            binds = _as_str_list(jv.get("bind_modules")) if isinstance(jv, dict) else []
            if module in binds:
                return parse_profile_record(rec, module), True
    except Exception:  # noqa: BLE001
        return None, False  # transient transport error on the scan
    # Both reads completed with no match ⇒ definitive miss -- but ONLY if the
    # direct lookup itself completed cleanly. If it raised transiently, the
    # absence isn't trustworthy, so report non-definitive and let the next turn
    # retry (don't cache it).
    return (None, True) if direct_clean else (None, False)


def _with_playbook(names: Any, playbook: str) -> list[str]:
    out = [str(n) for n in (names or []) if n]
    return out if playbook in out else out + [playbook]


def grant_playbook(client: Any, module: str, playbook: str) -> dict[str, Any]:
    """Let ``module``'s persona run ``playbook`` without asking again.

    The analyst's "Always allow" on a run-approval card. Adds ``playbook`` to
    both ``run_playbook.allow`` and ``run_playbook.auto`` on the record the
    resolver reads -- the ``assistant_personas`` row when one exists, else the
    Key Store entry -- so the next run of it skips the card. Read-merge-write:
    every other persona setting is kept. Returns ``{ok, source, playbook}`` or
    ``{ok: False, code, message}``; never raises.
    """
    from urllib.parse import quote
    from ._common import live_request

    if not (client and module and playbook):
        return {"ok": False, "code": "bad_input",
                "message": "a persona module and a playbook name are required"}
    base = getattr(client, "base_url", "") or ""
    try:
        r = live_request("get", client,
                         f"{base}/api/3/{PERSONA_MODULE}?{PF_SCOPE}={quote(module)}&$limit=1")
        rows = _members(r) if getattr(r, "status_code", None) == 200 else []
        if rows:
            rec = rows[0]
            body = {PF_RP_ALLOW: json.dumps(_with_playbook(
                        _json_field(rec.get(PF_RP_ALLOW), []), playbook)),
                    PF_RP_AUTO: json.dumps(_with_playbook(
                        _json_field(rec.get(PF_RP_AUTO), []), playbook))}
            w = live_request("put", client, f"{base}{rec['@id']}", json=body)
            source = PERSONA_MODULE
        else:
            r = live_request("get", client,
                             f"{base}/api/3/keys?key={quote(KEY_PREFIX + module, safe='')}&$limit=1")
            rows = _members(r) if getattr(r, "status_code", None) == 200 else []
            if not rows:
                return {"ok": False, "code": "no_persona",
                        "message": f"no persona is defined for {module!r}"}
            rec = rows[0]
            jv = rec.get("jSONValue")
            jv = dict(jv) if isinstance(jv, dict) else _json_field(jv, {})
            rp = dict(jv.get("run_playbook") or {})
            rp["allow"] = _with_playbook(rp.get("allow"), playbook)
            rp["auto"] = _with_playbook(rp.get("auto"), playbook)
            jv["run_playbook"] = rp
            w = live_request("put", client, f"{base}{rec['@id']}", json={"jSONValue": jv})
            source = "keys"
    except Exception as e:  # noqa: BLE001 -- report, never sink the approved run
        return {"ok": False, "code": "grant_failed", "message": str(e)[:300]}
    if getattr(w, "status_code", None) not in (200, 201):
        return {"ok": False, "code": f"http_{getattr(w, 'status_code', None)}",
                "message": (getattr(w, "text", "") or "")[:300]}
    return {"ok": True, "source": source, "module": module, "playbook": playbook}
