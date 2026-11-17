"""Composable system-prompt assembly (Persona Spine Unification plan, P2).

The triage system prompt was a single ~470-line monolith
(``system_prompt_triage.md``). It is now carved into named, independently
loadable fragments under ``prompt_fragments/triage/`` — the concatenation of the
canonical fragment set reproduces that monolith BYTE-FOR-BYTE (locked by
``tests/test_prompt_fragments.py``). This lets a persona compose only the pieces
it needs (e.g. an authoring persona reuses ``role`` + tool-mechanics + hard-rules
without the hunt-budget discipline that makes no sense for authoring), instead of
inheriting the whole triage slab or hand-writing a prompt from scratch.

Turn MODE (T2 fix): the hunt-vs-contain tension in triage is distributed across
fragments (containment imperatives in ``what_you_do`` / ``hard_rules`` pull the
model toward containment even on a pure hunt ask — the T2 drift). A ``mode``
directive re-weights that emphasis for a turn. Default ``mode=None`` appends
nothing, so the assembled prompt stays byte-identical to the monolith; wiring the
mode into the live triage turn (the behavior change) is P2b.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

_FRAG_DIR = Path(__file__).resolve().parent / "prompt_fragments"

# Canonical fragment order for the built-in triage persona. Concatenating these
# reproduces system_prompt_triage.md byte-for-byte (see test).
TRIAGE_FRAGMENTS: tuple[str, ...] = (
    "00_role",
    "10_record_context",
    "20_order_of_operations",
    "30_what_you_do",
    "40_hunting_instincts",
    "50_hard_rules",
    "55_reply_style",
    "60_quick_actions",
)

# T2 fix — turn-mode directives that re-weight hunt vs contain. Appended to the
# assembled body (recency), so `None` leaves the prompt byte-identical. These
# REINFORCE rules already in the fragments rather than adding new policy, so the
# emphasis wins over the inline containment imperatives without contradicting
# them. P2b wires the live triage turn to pick a mode from the analyst's ask.
_MODE_DIRECTIVES: dict[str, str] = {
    "hunt": (
        "\n# Turn mode — hunt\n\n"
        "This turn's ask is investigation / enrichment / summary, NOT a "
        "containment request. Deliver the findings the analyst asked for "
        "(an `info_card` / `ioc_card` of what you found). Do NOT call "
        "`find(kind='action', action_type='containment')`, do NOT stage an `emit_card(card_type='action')`, and do "
        "NOT end the turn on a containment `capability_gap` — unless the analyst "
        "EXPLICITLY asked you to block / isolate / quarantine / contain. If "
        "containment looks warranted, offer it as a one-line prose next step; do "
        "not self-assign it.\n"
    ),
    "contain": (
        "\n# Turn mode — contain\n\n"
        "The analyst has asked you to act. Once the scope is clear, resolve the "
        "response op via `find(kind='action', action_type='containment')` and stage a SINGLE "
        "`emit_card(card_type='action')` (or one `emit_card(card_type='choice')`) — that staged card is "
        "the deliverable, not an afterthought. Stage it before further enrichment "
        "crowds the budget, and once it's staged you are done.\n"
    ),
}

MODES = tuple(_MODE_DIRECTIVES.keys())


# Off a SOC record (alert / case / war room), the proactive "want me to save this
# as a playbook?" offer is noise: the analyst on a device or custom-module page
# asked to list steps, not to author automation. `30_what_you_do` tells the model
# to offer at the close of a containment, so — like the turn modes — this
# directive re-weights that instruction rather than adding new policy. Appended
# last (recency) so it wins over the fragment's proactive bullet.
#
# Deliberately narrow: it suppresses only the UNPROMPTED offer. An explicit
# "save this as a playbook" still works on every module — that path is a direct
# analyst request, and refusing it would be the old "authoring is out of scope"
# defect the HARD RULE in `30_what_you_do` exists to prevent.
_NO_PROACTIVE_OFFER_DIRECTIVE = (
    "\n# Playbook offers on this page\n\n"
    "This is not an alert / case / war-room record, so do NOT proactively offer "
    "to turn this session into a playbook: no unprompted `emit_card(card_type='playbook_offer')`, "
    "and no 'want me to automate this?' closing line. Answer what was asked and "
    "stop.\n\n"
    "This does NOT restrict you when the analyst ASKS. If they say 'build/save/"
    "make a playbook from this' (or ask for the YAML), call "
    "`emit_card(card_type='playbook_offer')` exactly as you would on an alert — the request is "
    "explicit, so honor it here too.\n"
)


def proactive_offer_directive(module, *, is_triage_module=None) -> str:
    """Directive suppressing the UNPROMPTED playbook offer off a SOC record.

    ``module`` is the page the turn is mounted on. Returns ``""`` (append
    nothing, behavior unchanged) for a triage record page, or when the module is
    unknown/absent — an unmounted turn keeps today's behavior rather than being
    silently narrowed. ``is_triage_module`` lets a caller inject the predicate
    (tests, or a host that already resolved it); by default the canonical
    server-side answer in ``triage_sources`` is used, so there is exactly one
    definition of "triage record page".
    """
    if not module:
        return ""
    if is_triage_module is None:
        from .triage_sources import is_triage_record_module as is_triage_module
    return "" if is_triage_module(module) else _NO_PROACTIVE_OFFER_DIRECTIVE


# Containment verbs — an explicit ask to ACT. Word-boundary matched.
_CONTAIN_SIGNALS = (
    "block", "isolate", "quarantine", "contain", "disable", "remediate",
    "kill", "take down", "shut down", "add to group", "tag as malicious",
)
# Hunt/enrich/summary signals — an ask to INVESTIGATE and report.
_HUNT_SIGNALS = (
    "hunt", "enrich", "ioc", "iocs", "indicator", "indicators", "blast radius",
    "timeline", "related activity", "related alerts", "related cases",
    "what's going on", "whats going on", "summarize", "summary", "scope",
    "investigate", "look into", "what happened", "correlate",
)


def classify_turn_mode(text) -> str | None:
    """Classify a triage turn's ask into a MODE (T2 fix), or None.

    - ``contain`` — the analyst used an explicit containment verb (act on it).
    - ``hunt``    — a clear investigate/enrich/summarize ask with NO containment
      verb (the T2 case: deliver findings, don't drift into containment).
    - ``None``    — ambiguous / generic ("triage this", "go", "what's next").
      Conservative on purpose: a false ``None`` just leaves today's prompt
      unchanged, so the mode never *removes* guidance — it only re-weights when
      the ask is unambiguous. Containment wins ties (never suppress a real
      contain ask with a hunt directive).
    """
    if not isinstance(text, str):
        return None
    t = " ".join(text.strip().lower().split())
    if not t:
        return None
    if any(s in t for s in _CONTAIN_SIGNALS):
        return "contain"
    if any(s in t for s in _HUNT_SIGNALS):
        return "hunt"
    return None


def mode_directive(mode: str | None) -> str:
    """The turn-mode directive string for ``mode`` (empty when None/unknown)."""
    return _MODE_DIRECTIVES.get(mode or "", "")


def load_fragment(group: str, name: str) -> str:
    """Read one fragment file (``prompt_fragments/<group>/<name>.md``)."""
    return (_FRAG_DIR / group / f"{name}.md").read_text(encoding="utf-8")


def fragments_available(group: str = "triage") -> bool:
    """True when the fragment directory ships alongside this module (it may be
    absent on an older tarball → callers fall back to the monolith .md)."""
    return (_FRAG_DIR / group).is_dir()


def assemble_prompt(fragments: Sequence[str], group: str = "triage",
                    mode: str | None = None) -> str:
    """Concatenate the named fragments (in the given order) and append the
    optional turn-mode directive. ``mode=None`` appends nothing."""
    body = "".join(load_fragment(group, f) for f in fragments)
    directive = _MODE_DIRECTIVES.get(mode or "", "")
    return body + directive


def assemble_triage_prompt(mode: str | None = None) -> str:
    """The built-in triage system prompt, assembled from the canonical fragment
    set. ``mode=None`` is byte-identical to the legacy monolith."""
    return assemble_prompt(TRIAGE_FRAGMENTS, group="triage", mode=mode)
