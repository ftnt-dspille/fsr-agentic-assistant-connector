"""P2 — composable prompt fragments (Persona Spine Unification plan).

Locks the lossless split: the canonical triage fragment set concatenates to the
legacy monolith BYTE-FOR-BYTE, so carving it up changed no behavior (the golden
matrix separately pins the assembled turn). Also covers the turn-mode directive
(the T2 fix mechanism; wired live in P2b).
"""
from __future__ import annotations

from pathlib import Path

import fsr_soc_triage  # noqa: F401 — package import
from fsr_soc_triage import prompt_assembly as pa

_PKG = Path(fsr_soc_triage.__file__).resolve().parent
_MONOLITH = _PKG / "system_prompt_triage.md"


def test_fragments_reproduce_monolith_byte_for_byte():
    """The whole point of the split being safe: assemble(mode=None) == monolith.
    If this fails, a fragment edit drifted from the shipped .md — reconcile
    (and regenerate the golden) deliberately."""
    assert pa.fragments_available("triage")
    assembled = pa.assemble_triage_prompt()
    monolith = _MONOLITH.read_text(encoding="utf-8")
    assert assembled == monolith, (
        f"assembled ({len(assembled)}) != monolith ({len(monolith)}) — "
        "a fragment drifted from system_prompt_triage.md")


def test_every_canonical_fragment_file_exists():
    for name in pa.TRIAGE_FRAGMENTS:
        assert (_PKG / "prompt_fragments" / "triage" / f"{name}.md").is_file(), name


def test_mode_none_appends_nothing():
    assert pa.assemble_triage_prompt(mode=None) == pa.assemble_triage_prompt()


def test_mode_hunt_and_contain_inject_directive():
    base = pa.assemble_triage_prompt()
    hunt = pa.assemble_triage_prompt(mode="hunt")
    contain = pa.assemble_triage_prompt(mode="contain")
    # Directive is appended (base stays a prefix), and each mode is distinct.
    assert hunt.startswith(base) and len(hunt) > len(base)
    assert contain.startswith(base) and len(contain) > len(base)
    assert hunt != contain
    assert "Turn mode — hunt" in hunt
    assert "do NOT stage an `emit_card(card_type='action')`" in hunt
    assert "Turn mode — contain" in contain


def test_classify_turn_mode():
    # Ambiguous / generic → None (unchanged behavior — the golden relies on this).
    for t in ["go", "triage this incident", "what's next", "", None, "help me here"]:
        assert pa.classify_turn_mode(t) is None, t
    # Clear hunt/enrich/summary asks → hunt.
    for t in ["hunt for related activity", "give me the IOCs",
              "what's the blast radius", "build the attack timeline",
              "summarize what's going on"]:
        assert pa.classify_turn_mode(t) == "hunt", t
    # Explicit containment verbs → contain, and WIN over hunt signals in the
    # same message (never suppress a real contain ask with a hunt directive).
    for t in ["block that IP", "isolate the host", "quarantine it",
              "hunt the IOCs then block them"]:
        assert pa.classify_turn_mode(t) == "contain", t


def test_mode_directive_helper():
    assert pa.mode_directive(None) == ""
    assert pa.mode_directive("nope") == ""
    assert "Turn mode — hunt" in pa.mode_directive("hunt")
    assert "Turn mode — contain" in pa.mode_directive("contain")


def test_assemble_subset_for_a_persona():
    """A persona can compose a subset — e.g. role + hard-rules only — without the
    hunt-discipline slab. Proves fragments are independently composable."""
    subset = pa.assemble_prompt(["00_role", "50_hard_rules"], group="triage")
    assert "incident-response assistant" in subset  # role
    assert "Mutating actions always go through" in subset  # hard rules
    assert "Hunting instincts" not in subset  # hunt slab excluded


def test_a_record_write_waits_for_the_verdict_like_containment():
    """Live-shaped (Frank, ka_policy_would_close_fp): the model closed the alert
    with update_record before delivering its verdict, and the close policy
    rightly refused it -- no verdict before the action. The order rule named
    only containment."""
    prompt = pa.assemble_triage_prompt()
    assert "deliver the verdict card FIRST, then\nthe action card" in prompt
    assert ("The same holds for a write to the record itself (closing it, or changing its\n"
            "status or severity): deliver the verdict card, then stage the write.") in prompt
