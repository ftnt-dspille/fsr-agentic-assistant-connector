"""P0 — characterization goldens (Persona Spine Unification plan, phase P0).

Snapshot, for each turn context, the three things a turn's wiring decides:

  * ``tools``        -- the ordered advertised tool-name slice;
  * ``turn_context`` -- the "## Turn context (a prior, not a cage)" block, taken
                        from the TurnPlan that drove the turn (not scraped out
                        of the assembled prompt), and asserted to appear
                        verbatim in the assembled system prompt;
  * ``sections``     -- the markdown section titles of the assembled system
                        prompt, in order (fenced code is skipped). This is what
                        tells the contexts apart: which disposition / persona /
                        mode / grounding sections are present, and where.

The base prompt BODY is deliberately NOT snapshotted. A wording edit inside a
section leaves every golden green; adding, removing, renaming or reordering a
section, dropping or adding a tool, or changing the turn-context block turns the
matching golden red. Every snapshot is driven through the real spine by the PL
harness (fake provider), so the goldens lock the wiring, not the prose.

Regenerate after a reviewed change::

    PL_UPDATE_GOLDENS=1 .venv/bin/python -m pytest tests/test_turn_context_goldens.py -q

Determinism: every context below is stable across runs (verified — no session
id / uuid / timestamp leaks into `system`). Contexts that need record grounding
or a recorded trace (triage-grounded, triage-resumed, build-with-trace) are
follow-ups — they require served record fixtures / seeded storage and are
tracked in GOLDEN_TODO below.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import local_turn as lt  # noqa: E402
import fixtures_records as fx  # noqa: E402

_INCIDENT = fx.incident_beaconing()
_INCIDENT_ENTITY = {"module": "incidents", "uuid": _INCIDENT["uuid"],
                    "iri": _INCIDENT["@id"]}

_GOLDEN_DIR = _HERE / "golden" / "turn_context"
_UPDATE = os.environ.get("PL_UPDATE_GOLDENS") == "1"


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    """Every prompt states today's date; a golden must not change at midnight."""
    monkeypatch.setenv("FSRPB_TODAY", "2026-09-27 (Sunday)")


@pytest.fixture(autouse=True)
def _pinned_source_availability(monkeypatch):
    """The slice withholds FAZ/FMG/SIEM tools the box can't back, from a verdict
    memoized per process for a minute. Pin it to "unknown" (not on a box), as
    the goldens were recorded: an earlier turn whose fake client listed no
    connectors otherwise left them "not configured" and the slice lost
    faz_search and fmg_device only when the goldens ran inside that minute."""
    from fsr_soc_triage import source_availability as sa
    sa.forget()
    monkeypatch.setattr(sa, "_live_inputs", lambda: None)
    yield
    sa.forget()


# Each context: (name, kwargs to local_turn). Fake provider only — deterministic.
_CUSTOM_PERSONA = {
    "hydra:member": [
        lt._keys_member("alerts", prompt="Alert desk persona.",
                        tools_allow=["get_record", "run_op", "emit_action_card"],
                        may_write=["alerts"]),
    ],
    "hydra:totalItems": 1,
}

CONTEXTS = [
    ("triage_no_persona",
     dict(intent="triage", module="incidents", persona="none")),
    # Same as triage_no_persona but a HUNT-mode ask (P2b): the turn-mode directive
    # is appended. Locks the T2-fix wiring — an unambiguous hunt ask re-weights
    # the prompt while the generic "go" turn above stays unchanged.
    ("triage_hunt_mode",
     dict(intent="triage", module="incidents", persona="none",
          msg="hunt for related activity and give me the IOCs")),
    ("build_no_persona_no_trace",
     dict(intent="build", module=None, persona="none")),
    ("persona_ztpf_templates",
     dict(intent="triage", module="ztpf_templates", persona="fixture")),
    ("persona_custom_allowlist",
     dict(intent="triage", module="alerts", persona="fixture",
          persona_fixture=_CUSTOM_PERSONA)),
    # Grounded triage (preflight ran: fetch → normalize → classify → scenario)
    # with a hunt-mode ask. Locks the full grounded prompt assembly + the mode
    # directive together — the path the T2 grade exercises.
    ("triage_grounded_hunt",
     dict(intent="triage", module="incidents", persona="none",
          msg="hunt for related activity and give me the IOCs",
          entity=_INCIDENT_ENTITY, extra_reads=[fx.cassette_rule_for(_INCIDENT)])),
]

# Contexts still to characterize (need seeded storage): triage_resumed,
# build_with_trace.
GOLDEN_TODO = ["triage_resumed", "build_with_trace"]

_HEADING = re.compile(r"^(#{1,6}) \S.*$")
# A fence is a line that is ONLY an opener/closer. Prose mentions of a backtick
# fence mid-line ("type a ```yaml block") must not toggle the state.
_FENCE_OPEN = re.compile(r"^\s*```[\w+-]*\s*$")
_FENCE_CLOSE = re.compile(r"^\s*```\s*$")


def _section_titles(system: str) -> list[str]:
    """Markdown headings of the assembled prompt, in order. Lines inside a
    fenced code block are skipped (a YAML comment is not a section)."""
    titles: list[str] = []
    fenced = False
    for line in system.splitlines():
        if fenced:
            if _FENCE_CLOSE.match(line):
                fenced = False
            continue
        if _FENCE_OPEN.match(line):
            fenced = True
            continue
        if _HEADING.match(line):
            titles.append(line.rstrip())
    return titles


def _capture(kw: dict) -> dict:
    """Drive one turn and return its snapshot: tools, turn-context block (from
    the plan that drove it), and section titles of the assembled system."""
    from fsr_playbooks.llm import turn_plan as tp

    kw = dict(kw)
    msg = kw.pop("msg", "go")
    real_plan_turn = tp.plan_turn
    plans: list = []

    def _recording_plan_turn(*args, **kwargs):
        plan = real_plan_turn(*args, **kwargs)
        plans.append(plan)
        return plan

    with mock.patch.object(tp, "plan_turn", _recording_plan_turn):
        res = lt.local_turn(messages=[{"role": "user", "content": msg}],
                            llm="fake", **kw)
    assert res.tools is not None, "fake provider must capture the tool slice"
    system = res.system or ""

    live = [p for p in plans if p is not None]
    assert live, "the turn derived no TurnPlan to read the context block from"
    plan = live[-1]
    block = tp._context_prior(plan.intent, plan.context)
    assert block.strip() in system, (
        "the TurnPlan's turn-context block is not in the assembled prompt; "
        "the plan and the shipped prompt have diverged")

    return {
        "tools": list(res.tools),
        "turn_context": block.strip(),
        "sections": _section_titles(system),
    }


def _path(name: str) -> Path:
    return _GOLDEN_DIR / f"{name}.json"


@pytest.mark.parametrize("name,kw", CONTEXTS, ids=[c[0] for c in CONTEXTS])
def test_turn_context_matches_golden(name: str, kw: dict):
    snap = _capture(kw)
    path = _path(name)

    if _UPDATE:
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snap, indent=2, ensure_ascii=True) + "\n")
        pytest.skip(f"golden updated: {name}")

    assert path.exists(), (
        f"missing golden for {name!r} — run with PL_UPDATE_GOLDENS=1 first")
    exp = json.loads(path.read_text())

    assert snap["tools"] == exp["tools"], (
        f"[{name}] tool slice drifted from golden.\n"
        f"  golden: {exp['tools']}\n  now:    {snap['tools']}\n"
        f"If intended, regenerate with PL_UPDATE_GOLDENS=1.")
    assert snap["turn_context"] == exp["turn_context"], (
        f"[{name}] turn-context block drifted from golden.\n"
        f"  golden: {exp['turn_context']!r}\n  now:    {snap['turn_context']!r}\n"
        f"If intended, regenerate with PL_UPDATE_GOLDENS=1.")
    assert snap["sections"] == exp["sections"], (
        f"[{name}] section layout drifted from golden.\n"
        f"  added:   {sorted(set(snap['sections']) - set(exp['sections']))}\n"
        f"  removed: {sorted(set(exp['sections']) - set(snap['sections']))}\n"
        f"  now order: {snap['sections']}\n"
        f"If intended, regenerate with PL_UPDATE_GOLDENS=1.")


def test_context_matrix_is_registered():
    """Guard: every golden on disk maps to a live context (no orphans), and
    every context has its golden file. Catches a renamed/dropped context."""
    if not _GOLDEN_DIR.exists():
        pytest.skip("no goldens yet — run PL_UPDATE_GOLDENS=1")
    names = {c[0] for c in CONTEXTS}
    on_disk = {p.stem for p in _GOLDEN_DIR.glob("*.json")}
    orphans = on_disk - names
    assert not orphans, f"golden files with no registered context: {orphans}"
    missing = names - on_disk
    assert not missing, f"registered contexts with no golden file: {missing}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
