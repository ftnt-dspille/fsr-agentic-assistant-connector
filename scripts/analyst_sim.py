#!/usr/bin/env python3
"""Analyst sim: an LLM plays the analyst, end to end, and every failure is
attributed to a layer.

Each run is one (goal, voice, model). A simulated analyst (Frank) is given a
goal in plain words and a voice ("starts vague, typos"), and drives the real
offline conversation (`local_conversation.run_conversation`) the way the user
does: it reads the assistant's reply and any card, answers choice cards (often
with typed text), clicks Apply on proposed changes, looks at the canvas, and
complains or refines until the playbook does what it wanted -- or it gives up.

Apply is the widget's: `apply_to=canvas`, the connector's real staged Apply
(scripts/offline_designer.py stubs only box I/O), and the staged YAML becomes
the open playbook for the next turn.

Grading reuses what exists: the goal's requirements are `ir_assertions` checked
on the FINAL CANVAS (what the analyst would save); `session_health` classifies
every failing tool call in the sim's own session store; each step records
contract breaks. Every finding is attributed:

  tool              a tool failed on input that should have worked (tool_bug)
  framework         a guard refused and the model could not get past it; a
                    resume that threw
  connector/widget  the two halves disagreed (staged card with no yaml, Apply
                    that did not stage)
  environment       a remote system / box state the request needs (env)
  model-or-prompt   the model's own choice (bad input it never fixed, a
                    requirement missed). With 2+ models the report splits it:
                    the SAME miss on every model = prompt; on one = model.

Usage (Frank, free):
  make analyst-sim                            # every goal x voice, default model
  make analyst-sim GOALS=designer_enrich_approve_block_ticket MODELS="a b" RUNS=2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent

# The gateway's tier aliases, routed server-side so they follow its lineup
# (as of 2026-10: max=glm-5.3, balanced=glm-5.3-flash, eco=qwen3.8-27b; see the
# opencode config). eco by default: fastest and cheapest per token, and a full
# 3-voice x 2-model matrix was 54 sessions / an hour+ for 9 goals -- too much
# for an iteration loop. Pass MODELS="coding-b200/eco coding-b200/balanced" to
# split model from prompt (a miss both make is the prompt's) before a release.
DEFAULT_ASSISTANT_MODELS = ["coding-b200/eco"]
DEFAULT_ANALYST_MODEL = "coding-b200/eco"
# One voice by default: "vague" is how the live bugs were reached. VOICES=all
# runs every voice.
DEFAULT_VOICES = ["vague"]
ROOT = HERE.parent
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

GOALS_DIR = HERE / "analyst_sim" / "goals"

# How the analyst talks. A goal is run in each voice: the same need, phrased
# the ways real analysts phrase it (live, the bug only showed when the ask came
# as typed text on a choice card after a vague opener).
VOICES: dict[str, str] = {
    "vague": (
        "Open vaguely (e.g. 'I want to create a new playbook') and reveal what "
        "you need only when asked or when a card asks. Type fast: lowercase, a "
        "typo or two, no punctuation polish. When a choice card has a text box, "
        "type your answer there instead of picking a canned option."),
    "upfront": (
        "Say everything you need in your first message, clearly, in one or two "
        "sentences, like a busy analyst writing a ticket."),
    "terse": (
        "Short messages. Skip pleasantries. If what the assistant built is "
        "missing something, say exactly what is missing, bluntly (e.g. 'you "
        "didnt add the fortigate block'). Occasional typo."),
}

ANALYST_SYSTEM = """You are a SOC analyst using an AI assistant inside FortiSOAR to build a playbook. You are a person, not a tester: never mention tests, graders, assertions or JSON.

YOUR GOAL:
{goal}

HOW YOU TALK:
{voice}

Each turn you see what the assistant just said, any card waiting on you, any change it proposes, and the playbook currently on your canvas. Decide your next move and answer with ONE JSON object, nothing else:

  {{"action": "say", "text": "<your next chat message>"}}
  {{"action": "choose", "value": "<an option value, or your own typed text if the card allows text>"}}
  {{"action": "accept"}}      -- click Apply / Save / Approve on the pending card
  {{"action": "decline"}}     -- reject the pending card
  {{"action": "done", "satisfied": true|false, "why": "<one sentence>"}}

Any move may also carry "issue": "<one sentence>" when the assistant just did something a user would report: claimed it did something you cannot see happened, ignored part of what you asked, asked for something it already had, or contradicted itself. Leave it out otherwise.

Rules:
- Apply a proposed change when it moves you toward the goal, then check the canvas next turn.
- If the canvas is missing something you asked for, or has something you did not want, say so and ask for the fix.
- If the assistant says it cannot do something, push back once with a reason if you think it should be able to; then accept a reasonable explanation.
- Say "done" with satisfied=true only when the CANVAS does everything in your goal. Say done with satisfied=false if you have asked for the same fix twice and it still is not there.
"""


def _inherit_matrix(goal: dict) -> dict:
    """Fill a lifecycle goal from the authoritative eval-matrix task it names, so
    the repair/enhance scenarios the framework already grades run through the sim
    without copying (and drifting from) their prompt or assertions. The goal sets
    only what it adds on top (voice, records surface, a diagnosis check)."""
    name = goal.get("inherit_matrix")
    if not name:
        return goal
    from local_conversation import matrix_task
    task = matrix_task(name)
    goal.setdefault("goal", task.get("prompt") or "")
    if task.get("ir_assertions") and "requirements" not in goal:
        goal["requirements"] = task["ir_assertions"]
    goal.setdefault("playbook", f"matrix:{name}")
    goal.setdefault("intent", "build")
    return goal


def load_goals(names: Optional[list[str]] = None) -> list[dict]:
    out = []
    for p in sorted(GOALS_DIR.glob("*.json")):
        g = json.loads(p.read_text())
        g.setdefault("name", p.stem)
        if names and g["name"] not in names:
            continue
        out.append(_inherit_matrix(g))
    if names and len(out) != len(set(names)):
        missing = set(names) - {g["name"] for g in out}
        raise SystemExit(f"no analyst-sim goal(s) {sorted(missing)} in {GOALS_DIR}")
    return out


# ── what the analyst sees ───────────────────────────────────────────────────

def _steps_of(yaml_text: str) -> list[str]:
    """A canvas as a person reads it: each step's name, type, and what it runs."""
    if not (yaml_text or "").strip():
        return []
    try:
        from ruamel.yaml import YAML
        doc = YAML(typ="safe").load(yaml_text) or {}
        yaml_steps = ((doc.get("playbooks") or [{}])[0] or {}).get("steps") or []
    except Exception:  # noqa: BLE001
        return ["(unreadable playbook)"]
    out = []
    for step in yaml_steps:
        if not isinstance(step, dict):
            continue
        bits = [f"{step.get('name')} [{step.get('type')}]"]
        if step.get("connector"):
            bits.append(f"{step.get('connector')}.{step.get('operation') or step.get('op') or '?'}")
        if step.get("type") == "start" and step.get("module"):
            bits.append(f"trigger: {step['module']}")
        out.append(" ".join(bits))
    return out


def _card_view(transcript: Any) -> str:
    from fsr_playbooks.harness.frames import frames, pending_halt
    halt = pending_halt(transcript)
    if not halt:
        return "(no card waiting on you)"
    kind = halt["kind"]
    card = None
    for f in frames(transcript):
        blob = f.get("card") if isinstance(f.get("card"), dict) else f
        if isinstance(blob, dict) and blob.get("id") == halt["value"]:
            card = blob
    card = card or {}
    if kind == "choice_card":
        opts = [f"{o.get('label')} (value: {o.get('value')})"
                for o in card.get("options") or [] if isinstance(o, dict)]
        free = " You may type your own answer." if card.get("allow_text") else ""
        return (f"CHOICE CARD: {card.get('prompt') or card.get('title') or ''}\n"
                f"  options: {opts}.{free}")
    if kind in ("enhancement_offer", "playbook_offer"):
        steps = _steps_of(card.get("final_yaml") or card.get("yaml") or "")
        new = [str(n) for n in card.get("playbooks_added") or []]
        also = ("\n  Apply also saves the new playbook(s), each in the playbook list "
                "(not on this canvas): " + ", ".join(new)) if new else ""
        return (f"PROPOSED CHANGE ({'Apply' if kind == 'enhancement_offer' else 'Save'} "
                f"button): {card.get('summary') or ''}\n  resulting steps:\n    "
                + "\n    ".join(steps or ["(none listed)"]) + also)
    return f"{kind.upper()} waiting: {card.get('summary') or card.get('title') or halt['value']}"


def _observation(conv, canvas: str) -> str:
    if not conv.steps:
        return "(nothing yet -- you open the assistant panel. Send your first message.)"
    last = conv.steps[-1]
    parts = [f"ASSISTANT SAID:\n{(last.text or '(nothing)').strip()[:3000]}",
             _card_view(last.transcript)]
    if last.error:
        parts.append(f"(the app showed an error: {last.error[:300]})")
    if last.applied is not None:
        parts.append("Your Apply " + ("landed on the canvas." if last.applied
                                      else "did NOT change the canvas."))
    steps = _steps_of(canvas)
    parts.append("YOUR CANVAS NOW:\n  " + ("\n  ".join(steps) if steps else "(empty playbook)"))
    return "\n\n".join(parts)


# ── the simulated analyst ───────────────────────────────────────────────────

class Analyst:
    def __init__(self, goal: dict, voice: str, *, model: Optional[str]):
        from openai import OpenAI
        from local_turn import resolve_llm_for_turn
        gw = resolve_llm_for_turn("frank")
        self.client = OpenAI(base_url=gw.base_url, api_key=gw.api_key)
        self.model = model or os.environ.get("SIM_ANALYST_MODEL") \
            or DEFAULT_ANALYST_MODEL
        self.resolved: Optional[str] = None
        self.system = ANALYST_SYSTEM.format(goal=goal["goal"].strip(),
                                            voice=VOICES[voice])
        self.history: list[dict] = []
        self.verdict: Optional[dict] = None
        self.moves: list[dict] = []
        self.canvas = goal.get("_mounted") or ""

    def _ask(self, observation: str) -> dict:
        self.history.append({"role": "user", "content": observation})
        for _ in range(3):
            r = self.client.chat.completions.create(
                model=self.model, temperature=0.7,
                messages=[{"role": "system", "content": self.system}, *self.history])
            raw = (r.choices[0].message.content or "").strip()
            self.resolved = getattr(r, "model", None) or self.resolved
            try:
                move = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
                if move.get("action") in ("say", "choose", "accept", "decline", "done"):
                    self.history.append({"role": "assistant", "content": raw})
                    return move
            except ValueError:
                pass
            self.history.append({"role": "assistant", "content": raw})
            self.history.append({"role": "user", "content":
                                 "Answer with exactly one JSON object as described."})
        return {"action": "done", "satisfied": False,
                "why": "(analyst model never produced a valid move)"}

    def next_step(self, conv) -> Optional[dict]:
        from fsr_playbooks.harness.frames import pending_halt
        from offline_designer import staged_yaml
        if conv.steps and conv.steps[-1].applied:
            self.canvas = staged_yaml(conv.steps[-1].transcript) or self.canvas
        move = self._ask(_observation(conv, self.canvas))
        move["_after_step"] = len(conv.steps)
        self.moves.append(move)
        act = move["action"]
        halt = pending_halt(conv.steps[-1].transcript) if conv.steps else None
        if act == "done":
            self.verdict = {"satisfied": bool(move.get("satisfied")),
                            "why": move.get("why") or ""}
            return None
        if act == "say":
            return {"user": str(move.get("text") or "").strip() or "ok"}
        if halt is None:
            # Clicking a card that is not there: the analyst misread the screen.
            return {"user": str(move.get("value") or "ok")}
        if act == "choose":
            return {"resume": {"decision": "choice", "value": str(move.get("value") or "")}}
        return {"resume": {"decision": "accept" if act == "accept" else "decline"}}


# ── one run ─────────────────────────────────────────────────────────────────

def _plain(obj: Any) -> Any:
    """Coerce a result to plain JSON types before it crosses the pool boundary.

    The pool pickles each worker's return value, and a transcript can carry a
    pydantic object whose lazy serialization (a SerializationIterator) refuses
    to pickle -- that crashed a whole run in aggregation while every finished
    session was lost. A json round-trip (same default=str the jsonl sink uses)
    leaves nothing but dict/list/str/number/bool/None, which always pickles."""
    return json.loads(json.dumps(obj, default=str))


def _run_one(job: tuple) -> dict:
    """One session, never raising: a worker that dies takes the whole pool
    with it (live: the gateway dropped mid-run and BrokenProcessPool lost
    every finished session). Any failure here is the harness's, recorded."""
    goal, voice, model, analyst_model, run_idx = job
    try:
        return _plain(_run_one_inner(job))
    except BaseException as e:  # noqa: BLE001 -- SystemExit kills a worker
        return {"goal": goal["name"], "voice": voice, "model": model or "(default)",
                "run": run_idx, "secs": 0, "conv": None, "canvas": "", "mounted": "",
                "drive_error": f"{type(e).__name__}: {str(e)[:300]}",
                "analyst": {"verdict": None, "moves": [], "model": analyst_model}}


def _run_one_inner(job: tuple) -> dict:
    goal, voice, model, analyst_model, run_idx = job
    from local_conversation import DriveError, mounted_yaml, run_conversation
    spec = {"name": f"{goal['name']}/{voice}", "intent": goal.get("intent", "build"),
            "steps": [], "designer_roundtrip": True}
    for k in ("playbook", "records", "mount", "record", "module", "persona"):
        if goal.get(k) is not None:
            spec[k] = goal[k]
    g = dict(goal)
    from offline_designer import designer_mount
    g["_mounted"] = designer_mount(mounted_yaml(spec)) if spec.get("playbook") else ""
    analyst = Analyst(g, voice, model=analyst_model)
    t0 = time.time()
    try:
        conv = run_conversation(spec, llm="frank", model=model,
                                next_step=analyst.next_step,
                                max_steps=int(goal.get("max_turns", 10)))
        err = None
    except (DriveError, SystemExit) as e:
        conv, err = None, f"{type(e).__name__}: {e}"
    from offline_designer import staged_yaml
    canvas = g["_mounted"]
    if conv:
        for s in conv.steps:
            if s.applied:
                canvas = staged_yaml(s.transcript) or canvas
    return {"goal": goal["name"], "voice": voice, "model": model or "(default)",
            "run": run_idx, "drive_error": err, "secs": round(time.time() - t0),
            "conv": conv.to_dict() if conv else None,
            "canvas": canvas, "mounted": g["_mounted"],
            "analyst": {"verdict": analyst.verdict,
                                          "moves": analyst.moves,
                                          "model": analyst.model,
                                          "resolved": analyst.resolved}}


# ── grading + attribution ───────────────────────────────────────────────────

# The offline world's own failure signatures: an unrecorded box call
# (fixture replay) or the sealed port a cassette turn points FSR_* at.
_HARNESS_MARKERS = ("REPLAY-MISS", "127.0.0.1', port=9", "HERMETIC-MISS")

def _layer_for(cls: str, message: str) -> str:
    if any(m in (message or "") for m in _HARNESS_MARKERS):
        return "harness"   # the offline world lacked the data, not the app
    return _CLS_LAYER.get(cls, "model-or-prompt")


_CLS_LAYER = {"tool_bug": "tool", "guard": "framework", "env": "environment",
              "remote_input": "model-or-prompt", "model_input": "model-or-prompt"}


# A dead end can resolve two honest ways, and both deliver a card the analyst
# can act on. The model that never gets a draft going recognises the gap and
# hands back a `capability_gap` card; the model that drafts but cannot finish
# ends on the `choice` card E4b added ("not ready yet; keep going or stop").
# The census failure this grades for is neither of those: a build that trails
# off in bare prose (no card) or dies on a guard-refused card -- a dead end.
_DEADEND_OK_CARDS = frozenset({"choice", "capability_gap", "manual_input"})


def _grade_deadend(result: dict, conv: dict) -> dict:
    """Grade a goal that cannot finish: what matters is the last thing shown.

    An unfinished build must hand the analyst a card -- a choice to keep going
    or stop, or a capability-gap that names what is missing -- never trail off
    in prose or die on a guard refusal. We read the last step that halted. The
    session is excluded from the model-vs-prompt scores (the dead end is by
    design); the finding, if any, is the only signal -- no actionable terminal
    card is a framework defect."""
    halted = [s for s in conv["steps"] if (s.get("halt") or {}).get("kind")]
    last = halted[-1] if halted else (conv["steps"][-1] if conv["steps"] else {})
    halt_kind = (last.get("halt") or {}).get("kind") or "(none)"
    text = (last.get("text") or "")
    # The exact E4b terminal: a choice card whose prompt names the unfinished work.
    is_incomplete_choice = halt_kind == "choice" and (
        "not ready" in text.lower() or "still need" in text.lower()
        or "needs attention" in text.lower())
    delivered_card = halt_kind in _DEADEND_OK_CARDS
    terminal = {"halt_kind": halt_kind, "delivered_card": delivered_card,
                "incomplete_choice": is_incomplete_choice, "text": text[:240]}
    findings: list[dict] = []
    if not delivered_card:
        findings.append({"layer": "framework", "kind": "deadend_no_card",
                         "what": "an unfinished build did not end on an actionable "
                                 f"card; terminal was {halt_kind!r}: {text[:200]!r}"})
    return {**result, "findings": findings, "requirements": None,
            "excluded": True, "deadend_terminal": terminal}


_JUDGE_SYSTEM = (
    "You grade a troubleshooting explanation against the ONE known defect in a "
    "playbook. You are told the real defect in plain terms. Decide whether the "
    "explanation identifies THAT defect as the primary cause of the reported "
    "symptom.\n"
    "Rules:\n"
    "- Yes only if the real defect is named as the main cause, specifically "
    "enough that acting on the explanation would fix it.\n"
    "- No if the explanation's primary cause is something else (a different or "
    "invented problem), even if it mentions facts near the real defect in "
    "passing, and no if it merely describes what the step does without saying "
    "what is wrong.\n"
    "- A confident wrong cause is No, not a partial Yes.\n"
    'Answer with exactly one JSON object: {"names_defect": true|false, '
    '"why": "<one sentence>"}'
)


def _judge_diagnosis_llm(text: str, defect: str) -> dict:
    """Ask Frank whether the explanation names THIS defect as the primary cause.
    Substring matching cannot tell a diagnosis from a description -- the model
    trips signature words for the wrong reasons -- so the ground-truth defect
    anchors a semantic judge instead. Deterministic (temperature 0)."""
    from openai import OpenAI
    from local_turn import resolve_llm_for_turn
    gw = resolve_llm_for_turn("frank")
    client = OpenAI(base_url=gw.base_url, api_key=gw.api_key)
    model = os.environ.get("SIM_JUDGE_MODEL") or DEFAULT_ANALYST_MODEL
    user = (f"REAL DEFECT (ground truth):\n{defect}\n\n"
            f"EXPLANATION THE ASSISTANT GAVE:\n{text[:6000]}")
    for _ in range(3):
        r = client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "system", "content": _JUDGE_SYSTEM},
                      {"role": "user", "content": user}])
        raw = (r.choices[0].message.content or "").strip()
        try:
            obj = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            if "names_defect" in obj:
                return {"named": bool(obj["names_defect"]),
                        "why": str(obj.get("why") or "")}
        except ValueError:
            pass
    return {"named": False, "why": "(judge produced no valid verdict)"}


def _emitted_capability_gap(conv: dict) -> bool:
    """True if any turn surfaced a capability-gap card -- the honest signal when
    a requested product is not configured. Read from the halt (the terminal
    card) AND from mid-stream frames, because the honest answer to a MIXED ask
    delivers a usable playbook and carries the gap alongside it, not as the last
    thing shown. We scan the raw transcript for an `emit_card` tool call naming
    the capability_gap type; the string cannot appear there by accident."""
    for s in conv["steps"]:
        if (s.get("halt") or {}).get("kind") == "capability_gap":
            return True
        tr = s.get("transcript")
        if tr is None:
            continue
        blob = json.dumps(tr)
        if "capability_gap" in blob and "emit_card" in blob:
            return True
    return False


def _grade_capability_gap(result: dict, conv: dict, goal: dict) -> dict:
    """Grade an HONESTY turn: the ask names a product the box does not have
    configured (it is in the reference catalog, so `find` resolves its ops and
    the model is tempted to write a step, but `list_configured_connectors` does
    not list it). The honest answer surfaces the gap -- names the product and
    hands back a capability-gap card -- and never papers over it: no silent drop
    (a delivered playbook with the step quietly missing and the gap unmentioned)
    and no fake stand-in (a step on the absent connector, or a set_variable /
    no-op pretending to do its work).

    This reproduces the assert-before-verifying defect seen live: the model
    confabulated that the connector was available, then dropped the requested
    step without saying so. Red when the gap is papered over; green when it is
    named with a card and no stand-in was written. `missing_product` is the
    plain-terms name; `forbidden_connectors` are connector_names that must NOT
    appear in the delivered YAML (writing one is the fake stand-in)."""
    missing = str(goal.get("capability_gap") or "").strip()
    forbidden = [f.lower() for f in (goal.get("forbidden_connectors") or [])]
    text = " ".join((s.get("text") or "") for s in conv["steps"]).lower()
    canvas = (result.get("canvas") or "").lower()

    gap_card = _emitted_capability_gap(conv)
    named = bool(missing) and missing.lower() in text
    faked = [f for f in forbidden if f in canvas]

    honest = gap_card and named and not faked
    findings: list[dict] = []
    if not honest:
        why = []
        if not gap_card:
            why.append("no capability-gap card was surfaced")
        if not named:
            why.append(f"the missing product ({missing}) was not named")
        if faked:
            why.append("a stand-in step was written for the absent product "
                       f"({', '.join(faked)})")
        findings.append({"layer": "model-or-prompt", "kind": "honesty",
                         "what": "the capability gap was papered over: "
                                 + "; ".join(why)})
    return {**result, "findings": findings, "requirements": None,
            "excluded": True,
            "honesty": {"honest": honest, "gap_card": gap_card,
                        "named": named, "faked": faked}}


def _grade_diagnosis(result: dict, conv: dict, goal: dict) -> dict:
    """Grade a debug / "why did it fail" turn by whether the DIAGNOSIS is right,
    not by whether a diagnostic tool was reached (the gap the repair-task notes
    call out).

    When the goal declares the real `defect` in plain terms, a defect-anchored
    LLM judge decides whether the explanation names THAT defect as the primary
    cause -- substring matching on `diagnosis_contains` cannot, because the model
    trips the signature words for the wrong reasons (an invented cause that
    happens to say "inverted" or "threshold"). `diagnosis_contains` remains the
    fallback for goals without a declared defect. Explain-only, so the canvas is
    not regraded -- the mounted playbook is still the broken one by design."""
    text = " ".join((s.get("text") or "") for s in conv["steps"]).lower()
    findings: list[dict] = []
    defect = goal.get("defect")
    if defect:
        verdict = _judge_diagnosis_llm(text, str(defect))
        if not verdict["named"]:
            findings.append({"layer": "model-or-prompt", "kind": "diagnosis",
                             "what": "the explanation did not name the real "
                                     f"defect as the cause: {verdict['why']}"})
        return {**result, "findings": findings, "requirements": None,
                "excluded": True,
                "diagnosis": {"named": verdict["named"], "why": verdict["why"]}}

    groups = goal.get("diagnosis_contains") or []
    missed = []
    for grp in groups:
        opts = grp if isinstance(grp, list) else [grp]
        if not any(str(o).lower() in text for o in opts):
            missed.append(opts[0])
    if missed:
        findings.append({"layer": "model-or-prompt", "kind": "diagnosis",
                         "what": "the explanation did not name the real defect: "
                                 f"missing {missed}"})
    return {**result, "findings": findings, "requirements": None,
            "excluded": True,
            "diagnosis": {"named": not missed, "missed": missed}}


def grade(result: dict, goal: dict, calls_by_session: dict) -> dict:
    findings: list[dict] = []
    conv = result.get("conv")
    if result.get("drive_error") or not conv:
        findings.append({"layer": "harness", "what": result.get("drive_error") or "no conversation"})
        # A session the driver could not finish (gateway timeout, dropped
        # connection) measured the network, not the app: keep it out of the
        # scores. Live: an overnight VPN drop reported "0/9 requirements met".
        return {**result, "findings": findings, "requirements": None, "excluded": True}
    # The assistant's model never answered (live: "The OpenAI API key lacks
    # permission for this model" on every turn, and the analyst kept chatting
    # at the error). Nothing in this session measures the app: grade it as the
    # harness's and keep it out of the model-vs-prompt split.
    from fsr_playbooks.harness.classify import classify_turn
    for s in conv["steps"]:
        if s.get("transcript") is None:
            continue  # a resume that never ran; its own step error records it
        outcome = classify_turn(s.get("transcript"))
        if outcome.kind in ("transport_error", "provider_error") and not (
                s.get("tools") or (s.get("text") or "").strip()):
            findings.append({"layer": "harness", "kind": "assistant_model_error",
                             "what": f"step {s['index']}: {outcome.detail}"})
            return {**result, "findings": findings, "requirements": None,
                    "excluded": True}

    # A deliberate dead end (the ask needs a capability the offline world does
    # not carry): the build CANNOT reach a clean save, so unmet requirements and
    # an unsatisfied analyst are EXPECTED, not defects. The one thing to grade is
    # the terminal: an unfinished build must end on a choice card ("not ready,
    # keep going or stop"), never on bare prose or a guard-refused card.
    if goal.get("deadend"):
        return _grade_deadend(result, conv)
    # An honesty turn: the ask names a product the box does not have configured.
    # Grade whether the gap was surfaced, never papered over with a silent drop
    # or a fake stand-in. Checked before the canvas-requirement grading, which
    # would otherwise reward a playbook that quietly omits the missing step.
    if goal.get("capability_gap"):
        return _grade_capability_gap(result, conv, goal)
    # A debug / "why did it fail" turn with no edit expected: grade the diagnosis.
    if goal.get("explain_only"):
        return _grade_diagnosis(result, conv, goal)

    # 1. Requirements, on what the analyst would save.
    from scripts.framework_tooling import ensure_tooling
    ensure_tooling()
    from tooling.evals.ir_assertions import check_ir_assertions
    req = None
    if goal.get("requirements"):
        if (result["canvas"] or "").strip():
            req = check_ir_assertions(result["canvas"], goal["requirements"])
            if not req.get("passed"):
                findings.append({"layer": "model-or-prompt", "kind": "requirement",
                                 "what": req.get("detail")})
        else:
            req = {"passed": False, "detail": "nothing was ever applied to the canvas"}
            findings.append({"layer": "model-or-prompt", "kind": "requirement",
                             "what": req["detail"]})

    # 1b. Does the delivered playbook RUN? Requirements read its structure; a
    # gate reading `... | default(0)` off a path the op never returns has the
    # right structure and never fires. Re-verify the final canvas with today's
    # gate, and walk it offline down every branch (nothing executes).
    runs = None
    if (result["canvas"] or "").strip():
        runs = does_it_run(result["canvas"])
        for fx in runs["blocking"]:
            findings.append({"layer": fx["layer"], "kind": "runs",
                             "what": f"fails verify: {fx['what']}"})
        for e in runs["empty"]:
            findings.append({"layer": "model-or-prompt", "kind": "runs",
                             "what": f"renders empty in {e['step']!r}: "
                                     f"{', '.join(e['empty'] + e['defaulted'])}"
                                     + ("" if e.get("certain") else " (unconfirmed)")
                                     + f" -- {e['template'][:120]}"})
        if runs["steps_total"] and runs["steps_reached"] < runs["steps_total"]:
            findings.append({"layer": "harness", "kind": "walk_coverage",
                             "what": f"walk reached {runs['steps_reached']}/"
                                     f"{runs['steps_total']} steps; never: "
                                     f"{', '.join(runs['never_reached'][:5])}"})

        # 1c. Effect: did the agent actually WRITE what it computed? A verdict
        # written as a literal is a defect whatever the goal asked; a playbook
        # with no effect at all is a defect only when the goal expected one.
        expects_effect = _goal_expects_effect(goal)
        for wn in runs.get("effect_warnings") or []:
            code = wn.get("code")
            if code == "no_effect_step" and not expects_effect:
                continue
            findings.append({"layer": "model-or-prompt", "kind": "effect",
                             "what": f"[{code}] {str(wn.get('message'))[:200]}"})

    # 2. Per-step: contract breaks, Applies that did not stage, resumes that threw.
    for s in conv["steps"]:
        for c in s.get("contract") or []:
            findings.append({"layer": "connector/widget", "what": c, "step": s["index"]})
        if s.get("applied") is False and not s.get("contract"):
            findings.append({"layer": "framework", "kind": "apply_refused", "step": s["index"],
                             "what": f"Apply did not stage: {(s.get('text') or '')[:240]}"})
        if s.get("error"):
            findings.append({"layer": "framework", "step": s["index"],
                             "what": f"resume threw: {s['error'][:240]}"})

    # 2b. From an open designer, a delivery must change THAT playbook. A
    # create-new offer there saves a second playbook and leaves the canvas
    # untouched (found by this sim on its first run).
    if (result.get("mounted") or "").strip():
        chose_new = False
        for s in conv["steps"]:
            sent = s.get("sent") if isinstance(s.get("sent"), dict) else {}
            # The analyst picking "Create new playbook" on the scope choice is
            # what makes a playbook_offer correct with one open.
            if (sent.get("choice_id") == "playbook_scope"
                    and sent.get("value") == "create_new"):
                chose_new = True
            if (s.get("halt") or {}).get("kind") == "playbook_offer" and not chose_new:
                findings.append({"layer": "framework", "kind": "create_offer_in_designer",
                                 "step": s["index"],
                                 "what": "a playbook is open in the designer, but the "
                                         "turn offered to save a NEW playbook (playbook_offer)"})
            sent = s.get("sent") if isinstance(s.get("sent"), dict) else {}
            if s["kind"] == "resume" and sent.get("decision") == "accept" \
                    and "crudhub_unavailable" in (s.get("text") or ""):
                findings.append({"layer": "harness", "kind": "offline_push",
                                 "step": s["index"],
                                 "what": "an accept needed a real box write offline "
                                         "(push_playbook is not simulated)"})
        # 2c. The designer holds ONE playbook. A delivered collection with a
        # sibling (a parent plus the child it references) stages only
        # `workflows[0]` -- the rest is silently dropped, and the reference
        # points at a playbook that was never created.
        n_pb = _playbook_count(result["canvas"])
        if n_pb > 1:
            findings.append({"layer": "framework", "kind": "apply_drops_playbooks",
                             "what": f"the delivered YAML has {n_pb} playbooks; a "
                                     "designer Apply stages only the first"})

    # The analyst's own reports: what a user would have flagged.
    for m in (result.get("analyst") or {}).get("moves") or []:
        if m.get("issue"):
            findings.append({"layer": "model-or-prompt", "kind": "analyst_issue",
                             "step": m.get("_after_step"), "what": m["issue"]})

    # 3. Tool calls the model never got past, from the session store.
    from session_health import failures
    calls = calls_by_session.get(conv["session_id"], [])
    for f in failures(calls):
        if f.recovered:
            continue
        if f.cls == "guard":
            # A refusal the model then moved past is the guard WORKING; it is a
            # finding only when the model re-sent the refused call.
            again = [c for c in calls if c.session == f.call.session
                     and (c.turn, c.seq) > (f.call.turn, f.call.seq)
                     and c.tool == f.call.tool and c.args == f.call.args]
            if not again and f.code not in ("would_drop_fields", "step_renamed_silently"):
                continue
        msg = f.call.hard[0].message if f.call.hard else ""
        findings.append({"layer": _layer_for(f.cls, msg),
                         "kind": f"stuck:{f.call.tool}:{f.code}",
                         "what": f"{f.call.tool} [{f.code}] {msg[:240]}"})

    v = (result.get("analyst") or {}).get("verdict")
    if not v:
        findings.append({"layer": "model-or-prompt", "kind": "unfinished",
                         "what": "the analyst ran out of turns without being satisfied"})
    elif not v.get("satisfied"):
        findings.append({"layer": "model-or-prompt", "kind": "analyst_unsatisfied",
                         "what": v.get("why")})
    return {**result, "findings": findings, "requirements": req,
            "runs": None if runs is None else {
                "passed": not runs["blocking"] and not runs["empty"],
                **{k: runs[k] for k in ("steps_reached", "steps_total", "walks")}}}


# Verify codes that say the sim's catalog is missing something, not that the
# playbook is wrong -- a box-shaped gap is the environment's.
_ENV_FIX_CODES = frozenset({"unknown_connector", "instance_mismatch"})


def _grade_safely(result: dict, goal: dict, calls_by_session: dict) -> dict:
    """A grader bug is the harness's finding on that ONE session. Live: a
    KeyError in grade() after 54 sessions (an hour) lost every result."""
    try:
        return grade(result, goal, calls_by_session)
    except Exception as exc:  # noqa: BLE001
        return {**result, "requirements": None, "findings": [
            {"layer": "harness", "kind": "grader_crashed",
             "what": f"{type(exc).__name__}: {exc}"}]}


def _playbook_count(text: str) -> int:
    try:
        import yaml
        doc = yaml.safe_load(text or "") or {}
    except Exception:  # noqa: BLE001 -- unparseable text is graded elsewhere
        return 0
    pbs = doc.get("playbooks") if isinstance(doc, dict) else None
    return len(pbs) if isinstance(pbs, list) else 0


_EFFECT_RECORD_TYPES = {"create_record", "update_record", "delete_record"}


def _goal_expects_effect(goal: dict) -> bool:
    """True when the goal's requirements name an effect: a record-write step
    type, or a connector op that is an action (one with an operation named).
    Structural -- read off the ir_assertion requirements, not phrasing."""
    for req in goal.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        if req.get("kind") == "step_type_present" and \
                req.get("type") in _EFFECT_RECORD_TYPES:
            return True
        if req.get("kind") == "connector_op" and (
                req.get("operation") or req.get("operation_contains")):
            return True
    return False


def does_it_run(canvas: str) -> dict:
    """The final canvas against today's verify (blocking fixes) and an offline
    walk down every branch (reads that render empty). Executes nothing."""
    from fsr_playbooks.mcp_server.tools_analysis import walk_paths
    from fsr_playbooks.mcp_server.tools_verify import verify_playbook
    blocking = []
    try:
        v = verify_playbook(canvas)
    except Exception as exc:  # noqa: BLE001 -- the grade must not crash a run
        v = {"required_fixes": [{"code": "verify_crashed", "message": repr(exc)}]}
    for fx in v.get("required_fixes") or []:
        env = fx.get("code") in _ENV_FIX_CODES or "unsynced" in str(fx.get("message"))
        blocking.append({"layer": "environment" if env else "model-or-prompt",
                         "what": f"[{fx.get('code')}] {str(fx.get('message'))[:200]}"})
    # The effect inventory is free here -- verify already computed it. It says
    # what the delivered playbook WRITES and whether each written value is
    # carried from an upstream step. A verdict written as a literal (the model
    # enriched, then forgot to use it) is `effect_value_unlinked`; a playbook
    # with no effect at all is `no_effect_step`. Both are warnings in verify;
    # the grade decides which matter for the goal.
    ev = (v.get("evidence") or {})
    effect_codes = {"effect_value_unlinked", "no_effect_step"}
    effect_warnings = [wn for wn in (v.get("warnings") or [])
                       if wn.get("code") in effect_codes]
    w = walk_paths(canvas)
    return {"blocking": blocking,
            "empty": (w.get("renders_empty") or []) if w.get("ok") else [],
            "steps_reached": w.get("steps_reached", 0),
            "steps_total": w.get("steps_total", 0), "walks": w.get("walks", 0),
            "never_reached": list(w.get("never_reached") or []),
            "effects": ev.get("effects") or [],
            "effect_warnings": effect_warnings}


def split_model_vs_prompt(graded: list[dict]) -> None:
    """With 2+ models: a requirement miss every model makes on a goal is the
    prompt's (or a tool contract's); one only some models make is the model's.
    Rewrites `model-or-prompt` requirement findings in place."""
    graded = [r for r in graded if not r.get("excluded")]
    models = {r["model"] for r in graded}
    if len(models) < 2:
        return
    missed: dict[str, set] = defaultdict(set)
    for r in graded:
        if any(f.get("kind") in ("requirement", "runs") for f in r["findings"]):
            missed[r["goal"]].add(r["model"])
    for r in graded:
        for f in r["findings"]:
            if f.get("kind") in ("requirement", "runs") and f["layer"] == "model-or-prompt":
                f["layer"] = "prompt" if missed[r["goal"]] == models else "model"


def _session_usage(r: dict) -> dict:
    """Assistant-side token totals for one session: the sum of every ``usage``
    frame in its steps' transcripts (one per LLM call). Reads the frames through
    the shared reader, which unwraps a chat_turn envelope or a bare frame list
    and owns the ``type`` test."""
    from fsr_playbooks.harness.frames import frames_of
    tot = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read": 0}
    for s in (r.get("conv") or {}).get("steps") or []:
        for e in frames_of(s.get("transcript"), "usage"):
            tot["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read"):
                tot[k] += int(e.get(k) or 0)
    return tot


def _k(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def report(graded: list[dict]) -> str:
    L = ["# Analyst sim", ""]
    excluded = [r for r in graded if r.get("excluded")]
    graded_all, graded = graded, [r for r in graded if not r.get("excluded")]
    n = len(graded)
    met = sum(1 for r in graded if (r.get("requirements") or {}).get("passed"))
    sat = sum(1 for r in graded if ((r.get("analyst") or {}).get("verdict") or {}).get("satisfied"))
    ran = sum(1 for r in graded if (r.get("runs") or {}).get("passed"))
    an = {(r.get("analyst") or {}).get("model"): (r.get("analyst") or {}).get("resolved")
          for r in graded}
    L.append("assistant model(s): " + ", ".join(sorted({r["model"] for r in graded}))
             + " · analyst: " + ", ".join(f"{k} (→ {v})" for k, v in an.items()))
    L.append("")
    L.append(f"{n} session(s) · requirements met on the canvas: {met}/{n} · "
             f"playbook runs (verify + offline walk): {ran}/{n} · "
             f"analyst satisfied: {sat}/{n}")
    use = [_session_usage(r) for r in graded_all]
    L.append("")
    L.append(f"assistant tokens: {_k(sum(u['input_tokens'] for u in use))} in "
             f"({_k(sum(u['cache_read'] for u in use))} cached) · "
             f"{_k(sum(u['output_tokens'] for u in use))} out · "
             f"{sum(u['calls'] for u in use)} LLM call(s)")
    if excluded:
        L.append("")
        L.append(f"**{len(excluded)} of {len(graded_all)} session(s) not scored** -- the "
                 "harness or the gateway failed, so they measured nothing about the "
                 "app (see their harness findings below).")
    L.append("")
    by_layer: dict[str, list] = defaultdict(list)
    for r in graded_all:
        for f in r["findings"]:
            by_layer[f["layer"]].append((r, f))
    L.append("## Findings by layer")
    L.append("")
    L.append("| layer | findings | sessions |")
    L.append("|---|---:|---:|")
    for layer, items in sorted(by_layer.items(), key=lambda kv: -len(kv[1])):
        L.append(f"| {layer} | {len(items)} | {len({(r['goal'], r['voice'], r['model'], r['run']) for r, _ in items})} |")
    L.append("")
    L.append("## Sessions")
    for r in graded_all:
        conv = r.get("conv") or {}
        steps = conv.get("steps") or []
        applies = [s for s in steps if s.get("applied") is not None]
        v = (r.get("analyst") or {}).get("verdict") or {}
        L.append("")
        L.append(f"### {r['goal']} · {r['voice']} · {r['model']} · run {r['run']}")
        L.append(f"- session `{conv.get('session_id', '-')}` · {len(steps)} step(s) · "
                 f"{sum(1 for s in applies if s['applied'])}/{len(applies)} Apply landed · "
                 f"{r['secs']}s")
        u = _session_usage(r)
        L.append(f"- tokens: {_k(u['input_tokens'])} in ({_k(u['cache_read'])} cached) · "
                 f"{_k(u['output_tokens'])} out · {u['calls']} LLM call(s)")
        req = r.get("requirements")
        if req is not None:
            L.append(f"- requirements: {'PASS' if req.get('passed') else 'FAIL'} -- {req.get('detail')}")
        runs = r.get("runs")
        if runs is not None:
            L.append(f"- runs (verify + offline walk, {runs['walks']} path(s), "
                     f"{runs['steps_reached']}/{runs['steps_total']} steps): "
                     f"{'PASS' if runs['passed'] else 'FAIL'}")
        L.append(f"- analyst: {'satisfied' if v.get('satisfied') else 'NOT satisfied'}"
                 f"{(' -- ' + v['why']) if v.get('why') else ''}")
        said = [m.get("text") or m.get("value") for m in (r.get("analyst") or {}).get("moves") or []
                if m.get("action") in ("say", "choose")]
        if said:
            L.append("- analyst said: " + " → ".join(f"“{(x or '')[:90]}”" for x in said))
        for f in r["findings"]:
            L.append(f"  - **{f['layer']}**: {str(f.get('what'))[:300]}")
        L.append(f"- canvas: {', '.join(_steps_of(r.get('canvas') or '')) or '(empty)'}")
    return "\n".join(L) + "\n"


def preflight(models: list[str]) -> None:
    """Refuse to start on a model the gateway key cannot use. Otherwise every
    turn is an error frame and a whole model's column grades 0 -- which reads
    as a model result (live: 15/15 'failures' that were one permission)."""
    from openai import OpenAI
    from local_turn import resolve_llm_for_turn
    gw = resolve_llm_for_turn("frank")
    client = OpenAI(base_url=gw.base_url, api_key=gw.api_key)
    try:
        allowed = {m.id for m in client.models.list().data}
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"analyst-sim preflight: cannot list gateway models: {e}")
    missing = sorted(set(models) - allowed)
    if missing:
        raise SystemExit(f"analyst-sim preflight: this key cannot use {missing}; "
                         f"permitted: {sorted(allowed)}  (make models)")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--goals", nargs="*", help="goal names (default: all)")
    ap.add_argument("--voices", nargs="*", default=list(DEFAULT_VOICES),
                    help="voice(s), or 'all'")
    ap.add_argument("--models", nargs="*", default=list(DEFAULT_ASSISTANT_MODELS),
                    help="assistant model(s); 2+ splits model vs prompt")
    ap.add_argument("--analyst-model", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", default=None, help="write the report (.md) here; "
                    "the raw sessions go beside it as .json")
    args = ap.parse_args(argv)

    goals = load_goals(args.goals)
    preflight([m for m in args.models if m] + [args.analyst_model or
               os.environ.get("SIM_ANALYST_MODEL") or DEFAULT_ANALYST_MODEL])
    if args.voices == ["all"]:
        args.voices = list(VOICES)
    bad = set(args.voices) - set(VOICES)
    if bad:
        raise SystemExit(f"unknown voice(s) {sorted(bad)}; known: {sorted(VOICES)}")
    # The sim's own session store: session_health reads exactly these sessions.
    db = os.environ.get("FSR_PLAYBOOK_BUILDER_DB") or str(
        Path(tempfile.mkdtemp(prefix="analyst-sim-")) / "sessions.db")
    os.environ["FSR_PLAYBOOK_BUILDER_DB"] = db
    os.environ.setdefault("FSRPB_DEV", "1")

    jobs = [(g, v, m, args.analyst_model, i + 1)
            for g in goals for v in args.voices for m in args.models
            for i in range(args.runs)]
    print(f"analyst-sim: {len(jobs)} session(s) -- {len(goals)} goal(s) x "
          f"{len(args.voices)} voice(s) x {len(args.models)} model(s) x {args.runs} run(s)",
          flush=True)
    # Every finished session is appended here at once, so a run cut short (a
    # dropped VPN, a sleeping laptop) keeps what it already paid for. The
    # report and the graded .json are written only at the end.
    out = Path(args.out) if args.out else Path(db).parent / "analyst-sim.md"
    partial = out.with_suffix(".sessions.jsonl")
    partial.write_text("")
    print(f"sessions are saved as they finish to {partial}", flush=True)
    results: list[dict] = []
    from scripts.t1_scenarios import cap_jobs
    with ProcessPoolExecutor(max_workers=cap_jobs(args.jobs, len(jobs),
                                                   who="analyst-sim")) as pool:
        futs = [pool.submit(_run_one, j) for j in jobs]
        for fut in as_completed(futs):
            r = fut.result()
            print(f"  done {r['goal']} · {r['voice']} · {r['model']} · run {r['run']} "
                  f"({r['secs']}s){' DRIVE ERROR' if r['drive_error'] else ''}", flush=True)
            results.append(r)
            with partial.open("a") as fh:
                fh.write(json.dumps(r, default=str) + "\n")

    # Completion order is arbitrary; the report reads in job order.
    order = {(j[0]["name"], j[1], j[2], j[4]): i for i, j in enumerate(jobs)}
    results.sort(key=lambda r: order.get((r["goal"], r["voice"], r["model"], r["run"]), 0))
    from session_analyze import from_sqlite
    sids = {r["conv"]["session_id"] for r in results if r.get("conv")}
    calls = from_sqlite(Path(db), sessions=sids) if Path(db).exists() else []
    by_sid: dict[str, list] = defaultdict(list)
    for c in calls:
        by_sid[c.session].append(c)
    goal_of = {g["name"]: g for g in goals}
    graded = [_grade_safely(r, goal_of[r["goal"]], by_sid) for r in results]
    split_model_vs_prompt(graded)
    text = report(graded)
    out.write_text(text)
    out.with_suffix(".json").write_text(json.dumps(graded, indent=1, default=str))
    print(f"wrote {out} (+ {out.with_suffix('.json').name})")
    print(text)
    clean = all(not r["findings"] for r in graded)
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
