"""personas domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from .pydantic_models import (
    ResolvePersonaParams,
)

from .ops_common import _check_params
from .ops_turn_state import _resolve_profile



@dataclass
class ResolvedPersona:
    """A fully-resolved turn persona: a built-in base intent (triage|build)
    with an optional Key Store Profile layered over it."""

    intent: str                 # normalized built-in base: "triage" | "build"
    profile: Any = None         # Key Store Profile layered over the base, or None

    @property
    def tools_allow(self) -> Any:
        """The persona's tool allowlist, or None to use the intent slice."""
        return getattr(self.profile, "tools_allow", None) if self.profile else None

    @property
    def prompt_override(self) -> Any:
        """The persona's system-prompt override, or None to use the built-in."""
        return (getattr(self.profile, "prompt", None) or None) if self.profile else None

    @property
    def mcp_servers(self) -> Any:
        """The persona's per-module MCP server surfacing override (M2), or None
        (⇒ use the static affordance-class default). ``[]`` means surface none."""
        mcp = getattr(self.profile, "mcp", None) if self.profile else None
        return getattr(mcp, "servers", None) if mcp else None

    @property
    def mcp_capability_dedup(self) -> Any:
        """The persona's curated↔MCP capability-dedup overrides (M2), merged over
        the connector's static base map. None/empty ⇒ base map only."""
        mcp = getattr(self.profile, "mcp", None) if self.profile else None
        return getattr(mcp, "capability_dedup", None) if mcp else None

    @property
    def capabilities(self) -> Any:
        """The turn-shaping capabilities in effect (P3): a ``PersonaCapabilities``.

        A Key Store persona that *declares* capabilities wins. A persona that
        makes no declaration keeps prompt-only behavior (all-off) -- grounding is
        never silently inherited from the built-in base, preserving today's
        contract that a persona turn is not case-grounded. With no persona, the
        built-in intent's defaults apply (triage grounds, build does not)."""
        from fsr_soc_triage.profiles import BUILTIN_CAPABILITIES, PersonaCapabilities
        if self.profile is not None:
            declared = getattr(self.profile, "capabilities", None)
            return declared if declared is not None else PersonaCapabilities()
        return BUILTIN_CAPABILITIES.get(self.intent, PersonaCapabilities())


    def concludes_with_verdict(self, module: Any) -> bool:
        """Whether this turn's investigation ends in a verdict card.

        It does on the built-in assistant, on an alert, incident or war-room
        page, and for a persona that opted into case grounding. A module
        persona elsewhere (a ZTPF device, an automation action) answers
        questions about its records: live, a device question was forced into
        a needs_more_info verdict card that restated the answer."""
        if self.profile is None:
            return True
        from fsr_soc_triage.triage_sources import is_triage_record_module
        if is_triage_record_module(module):
            return True
        return bool(getattr(self.capabilities, "grounding", False))


def resolve_persona(config: dict, params: dict) -> dict:
    """Resolve the Key Store persona's UI framing for a mounted module.

    The widget's ``resolvePersona`` reads the Key Store directly first; this
    operation is its fallback for boxes where the in-browser ``/api/3`` read is
    403-gated, and it also resolves ``bind_modules`` personas (which a direct
    per-module read misses). Reuses the same ``_resolve_profile`` path the turn
    uses, so what the drawer frames from matches what the turn runs under.

    Params: ``{module: str}``. Returns ``{found, module, label, ui}`` -- ``ui`` is
    the persona's opaque ``jSONValue.ui`` block ({label, greeting, placeholder,
    quickActions, footer}) or ``None``. Fail-open: no persona / no live client /
    any error ⇒ ``{found: False}`` so the widget falls back to triage framing.

    Deliberately does NOT carry a "is this a triage page?" flag: the widget's
    primary persona path is a direct Key Store read that never reaches this op,
    so the flag would be populated on some mounts and absent on others -- a
    half-wired mechanism is worse than an honest second definition. The widget
    owns that predicate in `fsrPbAgent.service.js`, pinned to the connector's
    canonical set by a parity test (see test_triage_module_parity.py)."""
    _check_params(ResolvePersonaParams, params)
    module = (params or {}).get("module")
    if not module or not isinstance(module, str):
        return {"found": False, "module": module if isinstance(module, str) else None}
    try:
        profile = _resolve_profile(module, "")
    except Exception:  # noqa: BLE001 -- fail-open to triage framing.
        profile = None
    if profile is None:
        return {"found": False, "module": module}
    return {
        "found": True,
        "module": module,
        "label": getattr(profile, "label", None),
        "ui": getattr(profile, "ui", None),
    }
