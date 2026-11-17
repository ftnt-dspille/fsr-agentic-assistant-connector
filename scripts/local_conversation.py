#!/usr/bin/env python3
"""T1 CONVERSATION driver -- multi-turn, box-free, real model.

The gap this fills: `local_turn.py --message` is repeatable, which reads like
multi-turn, but every `-m` becomes another `role: user` entry inside ONE
`chat_turn`. There are no assistant turns and no tool results between them, so
until now nothing offline exercised a conversation at all -- and nothing at all
exercised `chat_resume`. The halt/resume surface (approval gates, action cards,
choice cards, offers) was reachable only on a box, which is where the
approve-never-executes defect got to hide.

A conversation here is a scripted list of steps:

    {"user": "..."}                      -> a new analyst message
    {"resume": {"decision": "approve"}}  -> resume whatever the last turn halted on
    {"resume": {"decision": "choice", "value": "fgt"}}

State is carried the way the WIDGET carries it, which is the only reason this
proves anything: `messages[]` accumulates [user, assistant, user, ...] and is
passed whole to each `chat_turn` (the connector reads `params["messages"]`, it
does not reconstruct history from storage). Resume is the exception -- per
contract §4 the widget sends NO messages[] on resume; the connector reloads its
canonical conversation for the session. So resume steps deliberately pass only
the decision, and the session_id is what stitches them together.

Fidelity line (unchanged from T1): Frank runs a gateway model (Qwen3.8 / GLM-5.3),
the box runs its configured model. Grade STRUCTURE here -- did state survive the turn, did the
approved action actually execute, did a halt round-trip -- never disposition.

Usage:
    make conv SCRIPT=fix_then_enhance                # by name
    python scripts/local_conversation.py --script scripts/conversations/x.json \
        --llm frank --model coding-b200/glm-5.3 --json
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from scripts.local_turn import local_turn, _bootstrap, _playbook_entity, active_box  # noqa: E402
from scripts.drive import DriveError, TraceStep, drive, mount_entity  # noqa: E402
from scripts.local_turn import PatchScope, TurnResult, in_patch_scope  # noqa: E402

FIXTURES = _HERE / "eval_fixtures"
SCRIPTS = _HERE / "conversations"



def framework_root() -> Path:
    """The framework repo the sweep runs against (FSRPB_DEV=1 makes that the
    editable clone; FSRPB_FRAMEWORK_DIR overrides)."""
    from scripts.framework_tooling import framework_root as _root
    return _root()


def matrix_task(name: str) -> dict:
    """A framework eval-matrix task by its `name`, read from the framework
    repo the sweep is running against (FSRPB_DEV=1 makes that the editable
    clone). The sweep's build_matrix rows grade against these assertions
    rather than copies of them -- a copy would drift, and the drift would read
    as a model regression."""
    root = framework_root()
    for path in sorted((root / "tooling" / "evals" / "tasks").glob("*.json")):
        task = json.loads(path.read_text())
        if task.get("name") == name:
            return task
    raise ValueError(f"no eval-matrix task named {name!r} under {root}")


def mounted_yaml(spec: dict) -> str:
    """The playbook text a scenario mounts, or "" when it mounts a record.

    `"playbook": "matrix:<task>"` mounts an eval-matrix task's starting
    playbook from the framework itself -- a REPAIR task's broken one, or an
    ENHANCE task's `before_scenario` (what tooling/evals/tasks.py `open_yaml`
    mounts). A copy here would drift from the assertions that grade the fix."""
    if not spec.get("playbook"):
        return ""
    if str(spec["playbook"]).startswith("matrix:"):
        task = matrix_task(str(spec["playbook"])[len("matrix:"):])
        if task.get("broken_yaml_path"):
            text = (framework_root() / task["broken_yaml_path"]).read_text()
        elif task.get("before_scenario"):
            scen = (framework_root() / "tooling" / "evals" / "enhance_scenarios"
                    / f"{task['before_scenario']}.json")
            text = json.loads(scen.read_text()).get("before_yaml") or ""
            if not text:
                raise ValueError(f"{spec['playbook']}: {scen.name} has no before_yaml")
        else:
            raise ValueError(f"{spec['playbook']}: task has no broken_yaml_path or before_scenario")
        # The fixture's comments explain the bug ("Cause: ...") for the people
        # who maintain it; mounted with them, the model reads the answer.
        # The collection header says the same ("Deliberately broken
        # playbooks"); an analyst's playbook would not.
        out = []
        for ln in text.splitlines(keepends=True):
            if ln.lstrip().startswith("#"):
                continue
            if ln.startswith("collection:"):
                ln = "collection: SOC Response\n"
            elif ln.startswith("description:"):
                ln = "description: SOC response playbooks.\n"
            out.append(ln)
        return "".join(out)
    p = Path(spec["playbook"])
    if not p.is_absolute():
        p = FIXTURES / spec["playbook"]
    return p.read_text()

def unserved_shape(url: str) -> str:
    """Collapse an unserved read URL to the RULE that would serve it.

    The raw miss list is long and mostly unique -- every `get_record` carries a
    different uuid, every search a different query string -- so a raw union
    reads as hundreds of distinct gaps when it is really a handful of endpoint
    shapes. Grouping by shape is what turns the miss list into a work list you
    can actually capture against: uuids collapse to `<id>`, query strings drop.

    Deliberately crude (no urllib parse of a possibly-relative URL): strip the
    query, then replace uuid-ish and numeric path segments.
    """
    # A known-answer world's unrecorded run_op ("OP connector.op") is already
    # its own shape; the uuid heuristic below would collapse it to `<id>`.
    if url.startswith("OP "):
        return url
    path = url.split("?", 1)[0].rstrip("/")
    segs = path.split("/")
    out = []
    for i, seg in enumerate(segs):
        # `/api/3/...` -- the `3` is the API VERSION, not a record id. Collapsing
        # it turned every shape into `/api/<id>/...`, which is noise in exactly
        # the place the report is supposed to be readable.
        after_api = i > 0 and segs[i - 1] == "api"
        if (seg.isdigit() and not after_api) or (len(seg) >= 32 and "-" in seg):
            out.append("<id>")
        else:
            out.append(seg)
    return "/".join(out)

# The transcript readers are the shared harness's (fsr_playbooks.harness.frames),
# re-exported under their historical names so the drivers and graders that import
# them from here keep working. CARD_RESUME_KEY is asserted against the connector's
# own card vocabulary in tests/test_conv_graders.py.
from fsr_playbooks.harness.frames import (  # noqa: E402,F401
    CARD_RESUME_KEY,
    assistant_summary,
    assistant_text,
    frames,
    guards_fired,
    pending_halt,
    stop_reason,
    tools_called,
)


# --------------------------------------------------------------------------
# the conversation
# --------------------------------------------------------------------------
@dataclass
class Step:
    index: int
    kind: str                      # "user" | "resume"
    sent: Any
    transcript: Any = None
    text: str = ""
    tools: list[str] = field(default_factory=list)
    stop: Optional[str] = None
    halt: Optional[dict] = None
    error: Optional[str] = None
    # Which tool slice this step ran under. Recorded so a grader can say "the
    # BUILD turn could not read the record" rather than the weaker "some turn
    # could not"; on a resume step it stays None.
    intent: Optional[str] = None
    # Cassette reads this step issued that matched NO rule. The cassette answers
    # a miss with an empty 200/404 body, which is indistinguishable from "the
    # search legitimately found nothing" -- so a clean grade on a triage row can
    # mean the agent investigated, or that nobody answered and it fell back to
    # the mounted entity. Recording the misses is what makes those two states
    # tellable apart, and the union across a sweep IS the cassette's work list.
    unserved: list[str] = field(default_factory=list)
    # EVERY cassette read this step issued, served or not. `unserved` says what
    # nobody answered; this says what path actually ran. The two answer
    # different questions and only this one can distinguish "the persona
    # resolved off the 8.0 `assistant_personas` module" from "the module read
    # returned nothing and the Key Store fallback quietly saved the turn" --
    # both of which produce the same persona, the same tools and the same
    # prose. Without it, a shipped read path can go unexercised by 100% of rows
    # and every row still passes, which is how it did.
    reads: list[str] = field(default_factory=list)
    # A designer Apply (enhancement_offer accept with apply_to=canvas): True
    # when the staged YAML landed in the canvas the next turn edits.
    applied: Optional[bool] = None
    # Connector<->widget contract breaks seen on this step (e.g. a staged card
    # with no `yaml`): the widget could not do its half, whatever the model did.
    contract: list[str] = field(default_factory=list)
    # Terminal-action guards that fired on this step. See `guards_fired()`.
    guards: list[str] = field(default_factory=list)


@dataclass
class Conversation:
    name: str
    steps: list[Step] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    session_id: str = ""
    model: str = ""
    # The fixture box behind the arc, when the scenario named a `records`
    # bundle. A `writable` bundle's box carries the approved writes, which is
    # what `world_after` grades.
    world: Any = None
    # The record the conversation was MOUNTED on (a `records` + `mount` row),
    # as the fixture holds it. A provenance grader asks whether a value was
    # absent from the record the analyst started from; None when nothing was
    # mounted.
    mounted: Any = None
    # The record an auto_triage row triages, carried as conversation metadata.
    # (The `escalates` grader no longer reads it now the model self-close is
    # retired and closes are policy-driven, but it stays for provenance.) ""
    # otherwise.
    triaged_uuid: str = ""
    triaged_module: str = ""
    # Per-run tokens a planter put into external sources, {label: token}. Empty
    # offline; a live row fills it from the planter's state so a grader can ask
    # whether a token reached the answer through a tool result.
    planted: dict = field(default_factory=dict)

    @property
    def all_tools(self) -> list[str]:
        return [t for s in self.steps for t in s.tools]

    @property
    def all_guards(self) -> list[str]:
        return [g for s in self.steps for g in s.guards]

    @property
    def reads(self) -> list[str]:
        """Every cassette read across the conversation, de-duplicated and
        order-preserved."""
        seen: dict[str, None] = {}
        for s in self.steps:
            for u in s.reads:
                seen.setdefault(u, None)
        return list(seen)

    @property
    def unserved(self) -> list[str]:
        """Every unmatched cassette read across the conversation, de-duplicated
        and order-preserved. Reported per-row so the sweep names exactly which
        reads the cassette owes an answer for."""
        seen: dict[str, None] = {}
        for s in self.steps:
            for u in s.unserved:
                seen.setdefault(u, None)
        return list(seen)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "session_id": self.session_id,
            "model": self.model,
            "tools_called": self.all_tools,
            "guards_fired": self.all_guards,
            "unserved": self.unserved,
            "reads": self.reads,
            "triaged_uuid": self.triaged_uuid,
            "triaged_module": self.triaged_module,
            # The approved writes the writable box applied, with the record each
            # produced: a `world_after` failure is read from these, not re-driven.
            "world_writes": list(getattr(self.world, "writes", None) or []),
            "steps": [
                {"index": s.index, "kind": s.kind, "sent": s.sent,
                 "text": s.text, "tools": s.tools, "stop": s.stop,
                 "halt": s.halt, "error": s.error, "unserved": s.unserved,
                 "reads": s.reads,
                 "guards": s.guards,
                 "intent": s.intent,
                 "applied": s.applied, "contract": s.contract,
                 # The RAW transcript, not just the rendered text. Several
                 # graders read frames directly (`yaml_of` needs the fenceless
                 # YAML carriers; `never_acts_on` inspects tool_use arguments),
                 # so a dump without it can only support a WEAKER re-grade than
                 # the original -- and a replay tier that grades differently
                 # from the live tier is worse than none.
                 "transcript": s.transcript}
                for s in self.steps
            ],
        }


def conversation_from_dict(d: dict) -> Conversation:
    """Rebuild a Conversation from `to_dict()` -- the replay half of the tier.

    Grading is PURE: it reads a finished conversation and returns verdicts. So
    iterating on a grader never needs the model, only the transcripts it
    already produced. Driving 7 rows against the gateway costs ~15 minutes and
    real tokens; re-grading the same 7 stored rows costs milliseconds and none.
    Anything that changes only grader code belongs on this path.

    Faithfulness is the whole contract -- `tests/test_conv_graders.py` asserts a
    round-tripped conversation grades IDENTICALLY to the live one. A replay
    tier that quietly grades differently would send us chasing model
    regressions that are really serialization gaps.
    """
    c = Conversation(name=d.get("name") or "unnamed",
                     session_id=d.get("session_id") or "",
                     model=d.get("model") or "",
                     triaged_uuid=d.get("triaged_uuid") or "",
                     triaged_module=d.get("triaged_module") or "")
    for s in d.get("steps") or []:
        c.steps.append(Step(
            index=s["index"], kind=s["kind"], sent=s.get("sent"),
            transcript=s.get("transcript"), text=s.get("text") or "",
            tools=list(s.get("tools") or []), stop=s.get("stop"),
            halt=s.get("halt"), error=s.get("error"),
            intent=s.get("intent"), unserved=list(s.get("unserved") or []),
            reads=list(s.get("reads") or []),
            guards=list(s.get("guards") or []),
            applied=s.get("applied"), contract=list(s.get("contract") or []),
        ))
    return c


class LocalTransport:
    """The in-process transport: each turn is `local_turn` (the cassette, the
    fixture box, the fake or gateway model), each resume is the connector's
    `chat_resume` against the config the last turn used.

    It holds the state a conversation carries between turns: the playbook a
    designer session has open, the entity that names it, and the outcome of
    the last designer Apply, which the caller records on the step."""

    def __init__(self, *, ops, llm: str, model: Optional[str], tools_backend: str,
                 module: Optional[str], persona: str, records: Optional[str],
                 playbook_yaml: Optional[str], entity: Optional[dict],
                 actor: Optional[dict] = None, scope: Optional[PatchScope] = None):
        # The conversation's patch scope: resume runs between turns and needs
        # the seams the last turn installed.
        self.scope = scope
        self.ops = ops
        self.llm, self.model, self.tools_backend = llm, model, tools_backend
        self.module, self.persona, self.records = module, persona, records
        self.playbook_yaml, self.entity, self.actor = playbook_yaml, entity, actor
        self.session_id = f"conv-{uuid.uuid4().hex[:12]}"
        self.last: Optional[TurnResult] = None   # the last user turn's result
        self.config: Optional[dict] = None
        self.applied: Optional[bool] = None      # the last resume's designer Apply
        self.contract: list[str] = []

    def turn(self, params: dict) -> Any:
        try:
            res = local_turn(
                messages=list(params["messages"]), intent=params["intent"],
                module=self.module, entity=params.get("entity"),
                llm=self.llm, persona=self.persona, tools=self.tools_backend,
                playbook_yaml=self.playbook_yaml, model=self.model,
                records=self.records, session_id=self.session_id,
                # A step opened by a widget chip carries its key.
                quick_action=params.get("quick_action"),
                # The op's own actor: it is what marks the turn unattended.
                actor=self.actor, scope=self.scope)
        except SystemExit as e:
            raise DriveError(f"could not be driven: {e}") from e
        self.last, self.config = res, res.config
        return res.transcript

    def resume(self, params: dict) -> Any:
        self.applied, self.contract = None, []
        # Apply from the open designer, as the widget does it: stage into the
        # canvas, then the canvas is the playbook the next turn edits.
        designer_apply = params.get("apply_to") == "canvas"
        if designer_apply:
            from scripts.offline_designer import offline_designer
            canvas_now = self.playbook_yaml
            with offline_designer(self.ops, lambda: canvas_now):
                out = self.ops.chat_resume(self.config or {}, params)
            from scripts.offline_designer import staged_yaml
            y = staged_yaml(out)
            self.applied = bool(y)
            if y:
                self.playbook_yaml = y
                if isinstance(self.entity, dict):
                    self.entity = {**self.entity, "playbook_yaml": y}
            elif y == "":
                self.contract.append(
                    "playbook_staged card carries no yaml: the widget "
                    "cannot make the canvas the next turn's playbook")
            return out
        return self.ops.chat_resume(self.config or {}, params)


@in_patch_scope
def run_conversation(spec: dict, *, llm: str = "frank",
                     model: Optional[str] = None,
                     tools_backend: str = "cassette",
                     verbose: bool = False,
                     next_step: Optional[Callable[["Conversation"], Optional[dict]]] = None,
                     max_steps: int = 12,
                     scope: Optional[PatchScope] = None) -> Conversation:
    """Drive a conversation. `spec["steps"]` run first; then, when `next_step`
    is given, it chooses each further step from the conversation so far (the
    analyst-sim's simulated analyst) until it returns None or `max_steps` is hit.

    The whole conversation runs under one patch scope, undone on return or
    raise: the cassette seams must outlive each turn, because a resume between
    turns calls the connector's chat_resume."""
    _bootstrap()
    from connector_fsr_agentic_assistant import operations as ops

    playbook_yaml = None
    entity = None
    mounted_rec = None
    module = spec.get("module")
    if spec.get("playbook"):
        playbook_yaml = mounted_yaml(spec)
        if spec.get("designer_roundtrip"):
            from scripts.offline_designer import designer_mount
            playbook_yaml = designer_mount(playbook_yaml)
        # Built here, not left to local_turn, because resume needs its identity:
        # the widget passes the open playbook's workflow_iri/workflow_uuid on an
        # accept. A driver that omits them gets "I couldn't tell which playbook
        # to update", which looks like a connector bug and is an unfaithful harness.
        entity = _playbook_entity(playbook_yaml)
    elif spec.get("record"):
        # A conversation mounted on an ALERT/INCIDENT drawer rather than a
        # playbook. The loaders are IMPORTED from the single-turn suite, never
        # re-implemented: a second copy of the entity shape would drift.
        from scripts.t1_scenarios import load_record, record_entity
        module = module or "alerts"
        entity = record_entity(load_record(spec["record"]), module)
    elif spec.get("records") and spec.get("mount"):
        # A record that lives in a FIXTURE BUNDLE, so its related records come
        # with it and the drawer record and the queryable table are the same row.
        from scripts.fixture_box import FixtureBox, load_bundle
        _box = FixtureBox(load_bundle(spec["records"]))
        module = module or spec["mount"].get("module")
        entity = mount_entity(
            {**spec, "mount": {**spec["mount"], "module": module}},
            lambda mod, uid: _box.record(mod, uid))
        mounted_rec = _box.record(module, spec["mount"]["uuid"])
        if spec.get("auto_triage"):
            # An unattended auto-triage turn mounts the record the way the
            # `auto_triage` op does, not the way the drawer does.
            rec = _box.record(module, spec["mount"]["uuid"])
            entity = ops._record_entity(
                rec, module,
                rec.get("@id") or f"/api/3/{module}/{spec['mount']['uuid']}")

    if spec.get("auto_triage"):
        # The op's own prompt is the whole conversation: a row that wrote its
        # own message would grade a turn the box never sends.
        if spec.get("steps"):
            raise DriveError("an auto_triage row takes no `steps`: it sends "
                             "the auto_triage op's own prompt")
        if not (spec.get("records") and spec.get("mount")):
            raise DriveError("an auto_triage row needs `records` + `mount`")
        spec = {**spec, "steps": [{"user": ops._AUTO_TRIAGE_PROMPT}]}

    transport = LocalTransport(
        ops=ops, llm=llm, model=model, tools_backend=tools_backend, module=module,
        persona=spec.get("persona", "none"), records=spec.get("records"),
        playbook_yaml=playbook_yaml, entity=entity,
        actor=ops._AUTO_TRIAGE_ACTOR if spec.get("auto_triage") else None,
        scope=scope)
    conv = Conversation(name=spec.get("name") or "unnamed", session_id=transport.session_id,
                        mounted=mounted_rec)
    if spec.get("auto_triage"):
        conv.triaged_uuid = spec["mount"]["uuid"]
        conv.triaged_module = module or spec["mount"].get("module") or ""
    intent = spec.get("intent", "build")

    def turn_params(raw: dict) -> dict:
        # A step may override the conversation intent. This is how the widget
        # behaves: the build-from-triage handoff flips intent mid-conversation,
        # on the same session and the same mounted record.
        extra: dict[str, Any] = {"entity": transport.entity}
        if raw.get("quick_action"):
            extra["quick_action"] = raw["quick_action"]
        return extra

    def resume_params(raw: dict, halt: Optional[dict]) -> dict:
        r = raw["resume"]
        params: dict[str, Any] = {"session_id": transport.session_id,
                                  "decision": r.get("decision", "approve")}
        if r.get("id"):
            params[r.get("key", "card_id")] = r["id"]
        else:
            params[halt["key"]] = halt["value"]
        if r.get("value") is not None:
            params["value"] = r["value"]
        # Mirror the widget: an accept against an open playbook carries its
        # identity, since chat_resume has no entity block to fall back on.
        if transport.entity:
            params.setdefault("workflow_iri", transport.entity.get("iri"))
            params.setdefault("workflow_uuid", transport.entity.get("uuid"))
        if (transport.playbook_yaml is not None and halt
                and halt.get("kind") == "enhancement_offer"
                and params["decision"] == "accept"):
            params["apply_to"] = "canvas"
        return params

    def on_step(ts: TraceStep) -> Optional[list[dict]]:
        step = Step(index=ts.index, kind=ts.kind, sent=ts.sent, error=ts.error,
                    intent=ts.intent)
        if ts.kind == "user" and transport.last is not None and ts.envelope is not None:
            step.unserved = list(transport.last.cassette_misses)
            step.reads = list(transport.last.cassette_gets)
            conv.model = (transport.last.config or {}).get("openai_model") or \
                         (transport.last.config or {}).get("model") or ""
        if ts.kind == "resume" and transport.applied is not None:
            step.applied = transport.applied
        step.contract = list(transport.contract) if ts.kind == "resume" else []
        step.transcript = ts.envelope
        step.text, step.tools, step.stop = ts.text, ts.tools, ts.stop
        step.guards, step.halt = ts.guards, ts.halt
        if spec.get("records"):
            conv.world = active_box()
        if ts.kind == "user":
            conv.messages.append({"role": "user", "content": ts.sent})
        summary = assistant_summary(ts.envelope) if ts.envelope is not None else []
        if summary:
            conv.messages.append({"role": "assistant", "content": summary})
        conv.steps.append(step)
        again = (ts.raw.get("resume") or {}).get("repeat") if ts.raw and "resume" in ts.raw else None
        requeue = []
        if isinstance(again, int) and again > 1 and step.halt and not step.error:
            requeue = [{"resume": {**ts.raw["resume"], "repeat": again - 1}}]
        if verbose and not ts.error:
            print(f"  [{ts.index}/{len(spec.get('steps') or []) or '~'}] {step.kind}: "
                  f"{len(step.text)} chars, tools={step.tools or '-'}, "
                  f"stop={step.stop}"
                  + (f", unserved={len(step.unserved)}" if step.unserved else ""))
        return requeue

    drive(transport, spec, intent=intent, history="blocks",
          next_step=(lambda _trace: next_step(conv)) if next_step else None,
          max_steps=max_steps, turn_params=turn_params, resume_params=resume_params,
          on_step=on_step)
    return conv


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def load_spec(name_or_path: str) -> dict:
    p = Path(name_or_path)
    if not p.exists():
        cand = SCRIPTS / f"{name_or_path}.json"
        if cand.exists():
            p = cand
        else:
            raise SystemExit(f"no conversation script {name_or_path!r} "
                             f"(looked in {SCRIPTS})")
    return json.loads(p.read_text())


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="T1 multi-turn conversation driver")
    ap.add_argument("--script", required=True,
                    help="conversation spec: a path, or a bare name resolved "
                         "against scripts/conversations/")
    ap.add_argument("--llm", default="frank",
                    choices=["fake", "frank", "openai", "anthropic"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--tools", default="cassette", choices=["cassette", "live"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    spec = load_spec(args.script)
    try:
        conv = run_conversation(spec, llm=args.llm, model=args.model,
                                tools_backend=args.tools,
                                verbose=not args.json)
    except DriveError as e:
        print(f"[[DRIVE-ERROR]] {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(conv.to_dict(), indent=2, default=str))
        return 0

    print(f"\n── conversation: {conv.name} ─────────────────────────")
    print(f"model={conv.model}  session={conv.session_id}  "
          f"steps={len(conv.steps)}")
    for s in conv.steps:
        head = s.sent if isinstance(s.sent, str) else json.dumps(s.sent)
        print(f"\n[{s.index}] {s.kind.upper()}: {str(head)[:110]}")
        if s.error:
            print(f"    ✗ {s.error}")
        if s.text:
            print(f"    ⟩ {s.text[:400]}")
        if s.tools:
            print(f"    tools: {', '.join(s.tools)}")
        if s.stop:
            print(f"    stop: {s.stop}")
        if s.halt:
            print(f"    halt: {s.halt['kind']} ({s.halt['key']}={s.halt['value']})")
    print(f"\ntools across conversation: {', '.join(conv.all_tools) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
