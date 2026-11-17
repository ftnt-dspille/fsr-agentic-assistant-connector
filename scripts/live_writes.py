#!/usr/bin/env python
"""Drive the sweep's WRITING rows on a writable box, approve the cards, and
read every write back.

Why this exists: three defects shipped past ~3,000 green tests and were found
only by reading back what an approved card saved:
  * the card executed the framework's PREVIEW of the call, so every long
    string (a template script) was saved cut at 600 characters;
  * update cards never said what they would overwrite;
  * `test_template` rendered against an empty context and always passed.
Every offline tier grades the card. This one grades the record.

For each row it:
  1. mounts the row's record from the box, as the widget would;
  2. sends the row's user steps through `chat_turn`;
  3. answers every card the way a careful analyst would: approves creates and
     playbook runs, approves an update only of a record the row lists in
     `may_change` ({module, name|uuid|any}; `may_change_mounted` adds the
     open record). Any other overwrite is REJECTED and fails the row. It
     answers playbook pauses (first device in a pick list, or `--device`;
     "Ok" on a button pause);
  4. after each approved create/update, reads the record back and compares it
     with the MODEL'S OWN call (the `tool_use` input, not the card): every
     field must hold exactly what the call sent, and an update must leave the
     fields it did not name as they were;
  5. checks `runs_on` (the record a playbook run got), the row's `world_after`
     clauses against the live box, and an optional `oracle`;
  6. deletes what it created and puts back what it updated (`--keep` skips).

Rows: sweep rows carrying `expect.world_after`, plus live-only rows in
`scripts/conversations/live_writes/`. It writes to the box, so it refuses to
run unless the env names the box writable (`FSR_WRITABLE=1`).

    make live MODE=writes ENV=<.env of a writable test box> [CATEGORY= S= RUNS= ARGS=]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from fsr_playbooks.harness.frames import (  # noqa: E402
    assistant_text,
    frame_types,
    tool_calls,
    tool_results,
)
from harness_caps import harness_caps, skip_reason  # noqa: E402
from scripts.drive import FsrTransport, drive, mount_entity  # noqa: E402

SWEEP = ROOT / "scripts" / "conversations" / "sweep"
LIVE_ONLY = ROOT / "scripts" / "conversations" / "live_writes"
_WRITE_TOOLS = ("create_record", "update_record")
_OVERWRITE_TOOLS = ("update_record", "delete_record")
# Fields the platform stamps on every write; a change in them is not the agent's.
_SYSTEM_FIELDS = {"modifyDate", "modifyUser", "modifiedBy", "updatedBy", "@id", "@type",
                  "id", "uuid", "createDate", "createUser", "createdBy", "_lock"}
_MAX_CARDS = 12


# ---------------------------------------------------------------- row loading
def load_rows(category: str | None, names: list[str]) -> tuple[list[dict], list[str]]:
    rows, skipped = [], []
    paths = sorted(SWEEP.glob("*.json")) + sorted(LIVE_ONLY.glob("*.json"))
    for p in paths:
        spec = json.loads(p.read_text())
        if names and spec["name"] not in names:
            continue
        if category and spec.get("category") != category:
            continue
        expect = spec.get("expect", {})
        live_only = p.parent == LIVE_ONLY
        if not (live_only or "world_after" in expect):
            continue
        if not spec.get("mount"):
            skipped.append(f"{spec['name']}: no mounted record")
            continue
        rows.append(spec)
    return rows, skipped


# ---------------------------------------------------------------- comparison
def _ids(v: Any) -> set[str]:
    """Every name a value can be matched by: a link stub by its @id, uuid,
    label or itemValue; a bare string by itself and its last path segment."""
    if isinstance(v, dict):
        out = set()
        for k in ("@id", "iri", "uuid", "itemValue", "name", "value"):
            if v.get(k) not in (None, ""):
                out |= _ids(v[k])
        return out
    s = str(v).strip().lower()
    return {s, s.rstrip("/").rsplit("/", 1)[-1]} if s else set()


def same_value(sent: Any, saved: Any) -> bool:
    """Did the box keep what the call sent? Text compares exactly (a value cut
    short is the defect this tier exists for); a link or picklist matches by
    any of its names; a list of links by the set of them."""
    if not isinstance(sent, (list, dict)) and isinstance(saved, list) and len(saved) == 1:
        return same_value(sent, saved[0])   # the box keeps a one-value tag as a list
    if isinstance(sent, list):
        if not isinstance(saved, list) or len(sent) != len(saved):
            return False
        if all(not isinstance(x, (dict, list)) for x in sent + saved):
            # A list of plain values (tags, ports) is a set to the box: it
            # hands tags back in its own order.
            return sorted(map(str, sent)) == sorted(map(str, saved))
        return all(any(same_value(s, g) for g in saved) for s in sent)
    if isinstance(saved, dict):
        if isinstance(sent, dict):
            return bool(_ids(sent) & _ids(saved)) or sent == saved
        return bool(_ids(sent) & _ids(saved))
    if isinstance(sent, dict):
        return json.dumps(sent, sort_keys=True) == json.dumps(saved, sort_keys=True)
    if isinstance(sent, bool) or isinstance(saved, bool):
        return str(sent).lower() == str(saved).lower()
    if isinstance(sent, (int, float)) and isinstance(saved, (int, float)):
        return float(sent) == float(saved)
    return str(sent if sent is not None else "") == str(saved if saved is not None else "")


def _short(v: Any, n: int = 80) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return repr(s[:n] + ("..." if len(s) > n else "")) + (f" ({len(s)} chars)" if len(s) > n else "")


def write_problems(call: dict, saved: dict, before: dict | None) -> list[str]:
    """What the saved record gets wrong against the model's own call.

    ``call`` is the tool_use input ({module, fields[, uuid]}); ``before`` is
    the record as it stood before an update (None for a create)."""
    fields = call.get("fields") or {}
    probs = [f"{k}: sent {_short(v)}, saved {_short(saved.get(k))}"
             for k, v in fields.items() if not same_value(v, saved.get(k))]
    if before is not None:
        for k, old in before.items():
            if k in fields or k in _SYSTEM_FIELDS or k.startswith("@"):
                continue
            if json.dumps(old, sort_keys=True, default=str) != \
                    json.dumps(saved.get(k), sort_keys=True, default=str):
                probs.append(f"{k}: not in the call, but changed from {_short(old)} to {_short(saved.get(k))}")
    return probs


# ---------------------------------------------------------------- the box
class LiveWorld:
    """`conv_scenarios.world_after` reads a fixture box; this answers the same
    questions from the live one."""
    writable = True

    def __init__(self, client, writes: list[dict]):
        self.client, self.writes = client, writes
        self.modules = _LiveModules(client)

    def record(self, module: str, uid: str) -> dict | None:
        try:
            return read(self.client, module, uid)
        except Exception:  # noqa: BLE001 -- not found
            return None


class _LiveModules(dict):
    def __init__(self, client):
        super().__init__()
        self.client = client

    def get(self, module, default=None):  # noqa: D401 -- dict.get, fetched on first use
        if module not in self:
            self[module] = all_rows(self.client, module)
        return dict.get(self, module, default)


_REL = {"$relationships": "true"}


def read(client, module: str, uid: str) -> dict:
    """One record WITH its many-to-many links: a plain GET leaves them null,
    which reads exactly like a link that was never saved."""
    return client.get(f"/api/3/{module}/{uid}", params=_REL)


def all_rows(client, module: str) -> list[dict]:
    rows, page = [], 1
    while True:
        r = client.get(f"/api/3/{module}", params={"$limit": 100, "$page": page, **_REL})
        got = r.get("hydra:member") or []
        rows += got
        if len(got) < 100:
            return rows
        page += 1


def _matches_named(rec: dict, module: str, named: list[dict]) -> bool:
    for n in named:
        if n.get("module") != module:
            continue
        if n.get("any"):
            return True
        if n.get("uuid") and str(rec.get("uuid", "")).startswith(n["uuid"]):
            return True
        if n.get("name") and str(rec.get("name", "")).strip().lower() == n["name"].strip().lower():
            return True
    return False


# ---------------------------------------------------------------- one arc
def row_requires(spec: dict) -> list[str]:
    """What a writing row needs from the box: its mounted record, and writes."""
    m = spec.get("mount") or {}
    reqs = ["writable"] + ([f"record:{m['module']}:{m['uuid']}"] if m else [])
    return reqs + list(spec.get("requires") or [])


def said_of(d: dict) -> str:
    return assistant_text(d)


def _body_dict(body) -> dict | None:
    """A tool result's body when it is an object, else None."""
    return body if isinstance(body, dict) else None


#: The halts this harness answers. Anything else a turn halts on is not its
#: to answer, so the arc stops there, as it always has.
_ANSWERED = ("manual_input", "approval_request", "action_card")


class _ArcTransport(FsrTransport):
    """The box transport, with every failed call noted on the arc's problems."""

    def __init__(self, fsr, sid: str, problems: list[str]):
        super().__init__(fsr, session_id=sid, timeout=300)
        self.problems = problems

    def _call(self, op: str, params: dict) -> dict:
        d = super()._call(op, params)
        if not d.get("ok") and d.get("error"):
            self.problems.append(f"{op} failed: {str(d.get('error'))[:200]}")
        return d


class Arc:
    """One row against the box: the conversation plus everything it wrote."""

    def __init__(self, fsr, spec: dict, device: str | None, log):
        self.fsr, self.spec, self.device, self.log = fsr, spec, device, log
        m = spec["mount"]
        self.mount_module = m["module"]
        self.mount = fsr.client.get(f"/api/3/{m['module']}/{m['uuid']}")  # as the widget sends it
        self.entity = mount_entity(spec, lambda *_: self.mount)
        self.sid = f"live-writes-{spec['name'][:30]}-{uuid.uuid4().hex[:6]}"
        self._recorded: set[int] = set()
        self.calls: dict[str, dict] = {}       # tool_use id -> {name, input}
        self.writes: list[dict] = []           # approved, read back
        self.created: list[tuple[str, str]] = []
        self.restore: list[tuple[str, str, dict]] = []
        self.pauses: list[dict] = []
        self.runs: list[dict] = []             # run_playbook results
        self.problems: list[str] = []
        self.transport = _ArcTransport(fsr, self.sid, self.problems)
        self.tools: list[str] = []
        self.said = ""
        self.picked_device: str | None = None
        may = list(spec.get("may_change") or [])
        if spec.get("may_change_mounted"):
            may.append({"module": m["module"], "uuid": m["uuid"]})
        self.may_change = may
        self.snapshot = self._snapshot()

    # A playbook run writes records no tool_use names (a "move step" playbook
    # renumbers a whole profile), so cleanup cannot undo it from the calls.
    # The records `world_after` grades are the ones the row expects to change:
    # remember them as they were, and put back whatever differs afterwards.
    _META = frozenset({"@id", "@type", "@context", "id", "uuid", "createDate", "modifyDate",
                       "createUser", "modifyUser", "_lastModified"})

    def _snapshot(self) -> list[tuple[str, dict]]:
        clauses = (self.spec.get("expect") or {}).get("world_after")
        if not clauses:
            return []
        from scripts.conv_scenarios import _world_matches
        snap = []
        for cl in clauses if isinstance(clauses, list) else [clauses]:
            for r in all_rows(self.fsr.client, cl["module"]):
                if _world_matches(r, cl.get("where") or {}):
                    snap.append((cl["module"], r))
        return snap

    # -- transport
    def _exec(self, op: str, params: dict) -> dict:
        return self.transport._call(op, params)

    def _record(self, d: dict) -> None:
        """Note every tool call, result and run an envelope carries. Idempotent:
        a halt's envelope is recorded before it is answered, and again when its
        step ends."""
        if id(d) in self._recorded:
            return
        self._recorded.add(id(d))
        for c in tool_calls(d):
            self.calls[c.id] = {"name": c.name, "input": c.args}
            self.tools.append(c.name)
        for r in tool_results(d):
            self._note_result(r)
        s = said_of(d)
        if s:
            self.said = s

    def _note_result(self, r) -> None:
        call = self.calls.get(r.tool_use_id) or {}
        body = _body_dict(r.body)
        if body is None:
            return
        if call.get("name") == "run_playbook" or "ran_on" in body:
            self.runs.append(body)

    # -- the arc
    def run(self) -> None:
        # `resume` steps in a spec are not sent: the cards are answered as they come.
        steps = [s for s in self.spec["steps"] if "user" in s]

        def announce(raw: dict) -> dict:
            self.log(f"   user: {raw['user'][:110]}")
            return {}

        drive(self.transport, self.spec, steps=steps,
              intent=self.spec.get("intent", "triage"), entity=self.entity,
              history="text", answer=self._answer, max_halts=_MAX_CARDS,
              turn_params=announce, on_step=self._on_step)

    def _on_step(self, step) -> None:
        for env in step.envelopes:
            self._record(env)
        if step.envelope is not None and step.answered_halts >= _MAX_CARDS \
                and any(t in _ANSWERED for t in frame_types(step.envelope)):
            self.problems.append(f"still carding after {_MAX_CARDS} answers")

    def _answer(self, halts: list[dict], env: dict) -> dict | None:
        """The answer policy: a halt the arc owns is answered (a gated write is
        read back, a card the row may not touch is rejected, a manual input is
        filled from its options). Returns the envelope the answer produced."""
        self._record(env)
        cards = [h for h in halts if h.get("type") in _ANSWERED]
        if not cards:
            return None
        c = cards[-1]
        if c["type"] == "action_card":
            return self._action(c)
        if c["type"] == "approval_request":
            self.log(f"   approve {c.get('tool')}")
            return self._exec("chat_resume", {"session_id": self.sid, "approval_id": c["approval_id"],
                                              "decision": "approve"})
        return self._pause(c)

    def _model_call(self, tool: str, args: dict) -> dict | None:
        """The model's own tool_use input for a call a batched card carries
        (the card holds the executed args; the model's call is the reference)."""
        for c in reversed(list(self.calls.values())):
            i = c.get("input") or {}
            if c.get("name") == tool and i.get("module") == args.get("module") \
                    and (i.get("uuid") or None) == (args.get("uuid") or None) \
                    and (tool != "create_record" or (i.get("fields") or {}).get("name")
                         == (args.get("fields") or {}).get("name")):
                return i
        return None

    def _action(self, c: dict) -> dict | None:
        tool, t = c.get("tool"), c.get("target") or {}
        call = (self.calls.get(c.get("preceding_tool_use_id")) or {}).get("input")
        if call is None:
            call = {"module": t.get("module"), "uuid": t.get("uuid"), "fields": c.get("args") or {}}
            self.problems.append(f"{tool} card: no tool_use for it in the transcript; checking the card's args")
        planned = [(tool, call)] + [
            (b.get("tool"), self._model_call(b.get("tool"), b.get("args") or {}) or b.get("args") or {})
            for b in c.get("batch") or [] if isinstance(b, dict) and b.get("tool")]
        befores: list[dict | None] = []
        for btool, bcall in planned:
            module = bcall.get("module") or t.get("module")
            before = None
            if btool in _OVERWRITE_TOOLS:
                before = read(self.fsr.client, module, bcall.get("uuid") or t.get("uuid"))
                if not _matches_named(before, module, self.may_change):
                    self.problems.append(
                        f"{btool} would overwrite {module} {before.get('name')!r} ({str(before.get('uuid'))[:8]}), "
                        "which the row does not let it change; rejected")
                    self.log(f"   REJECT {btool} of {before.get('name')!r} (not the row's to change)")
                    return self._exec("chat_resume", {"session_id": self.sid, "card_id": c["id"],
                                                      "decision": "reject", "args": c.get("args") or {}})
            befores.append(before)
        self.log(f"   approve {tool} {t.get('module') or ''} {t.get('name') or ''}"
                 + (f" (+{len(planned) - 1} batched)" if len(planned) > 1 else ""))
        d = self._exec("chat_resume", {"session_id": self.sid, "card_id": c["id"],
                                       "decision": "approve", "args": c.get("args") or {}})
        # The card's own executions, in order (the model's later calls in the
        # same response are not this card's writes).
        results = [b for b in (_body_dict(r.body) for r in tool_results(d)
                               if str(r.tool_use_id or "").startswith(("cardexec_", "exec-")))
                   if b is not None]
        for (btool, bcall), before in zip(planned, befores):
            if btool in _WRITE_TOOLS:
                self._read_back(btool, bcall.get("module") or t.get("module"), bcall, before, results)
        return d

    def _read_back(self, tool: str, module: str, call: dict, before: dict | None,
                   results: list[dict]) -> None:
        """Pop this call's result (in execution order) and read the record back."""
        verb = "create" if tool == "create_record" else "update"
        idx = next((i for i, r in enumerate(results)
                    if r.get("module") in (module, None) and r.get("action") in (verb, None)), None)
        res = results.pop(idx) if idx is not None else None
        if not res or not res.get("ok"):
            self.problems.append(f"approved {tool} on {module} but no successful write came back"
                                 + (f": {res.get('code')} {str(res.get('message'))[:160]}" if res else ""))
            return
        uid = res.get("uuid") or call.get("uuid")
        saved = read(self.fsr.client, module, uid)
        if tool == "create_record":
            self.created.append((module, uid))
        elif before is not None:
            self.restore.append((module, uid, {k: before.get(k) for k in (call.get("fields") or {})}))
        probs = write_problems(call, saved, before)
        self.writes.append({"tool": tool, "module": module, "uuid": uid, "name": saved.get("name"),
                            "fields": sorted((call.get("fields") or {})), "problems": probs})
        for p in probs:
            self.problems.append(f"{tool} {module} {saved.get('name')!r}: {p}")
        self.log(f"      read back {module} {saved.get('name')!r}: "
                 + ("holds what the call sent" if not probs else f"{len(probs)} difference(s)"))

    def _pause(self, c: dict) -> dict:
        self.pauses.append(c)
        p = {"session_id": self.sid, "input_id": c["id"], "decision": "approve",
             "workflow_run_iri": c.get("workflow_run_iri"), "values": {}}
        if c.get("fields"):
            for f in c["fields"]:
                if f.get("type") == "dynamic_list":
                    p["values"][f["name"]] = self._pick(f)
                elif f.get("type") == "select" and f.get("options"):
                    o = f["options"][0]
                    p["values"][f["name"]] = o.get("value") if isinstance(o, dict) else o
                elif f.get("type") == "checkbox":
                    p["values"][f["name"]] = False
            self.log(f"   pause '{(c.get('question') or c.get('title') or '').splitlines()[0][:60]}': submit {p['values']}")
        else:
            p["option"] = "Ok"
            self.log(f"   pause '{(c.get('question') or '').splitlines()[0][:60]}': Ok")
        return self._exec("respond_manual_input", p)

    def _pick(self, f: dict) -> list:
        for g in f.get("groups") or []:
            for o in g.get("options") or []:
                val = o.get("value") if isinstance(o, dict) else o
                if not self.device or str(val).split(" ")[0] == self.device:
                    self.picked_device = str(val).split(" ")[0]
                    return [g.get("value"), val]
        self.problems.append(f"no option for device {self.device!r} in the pick list")
        return []

    # -- grading
    def grade(self) -> None:
        exp = self.spec.get("expect", {})
        called = exp.get("called") or []
        want = called if isinstance(called, list) else called.get("all_of", [])
        missing = [t for t in want if t not in self.tools]
        if missing:
            self.problems.append(f"expected tools not called: {missing}")
        for want_run in self.spec.get("runs_on") or []:
            got = [r.get("ran_on") or {} for r in self.runs]
            if not any(_matches_named({"uuid": str(g.get("iri", "")).rsplit("/", 1)[-1], "name": g.get("name")},
                                      want_run["module"], [want_run]) for g in got):
                self.problems.append(f"no playbook run got {want_run}; runs got {[g.get('name') for g in got]}")
        if "world_after" in exp:
            from scripts import conv_scenarios as CS
            clauses = exp["world_after"] if isinstance(exp["world_after"], list) else [exp["world_after"]]
            ctx = type("Ctx", (), {"conv": type("Conv", (), {"world": LiveWorld(self.fsr.client, self.writes)})()})()
            ok, detail = CS.world_after(clauses)(ctx)
            if not ok:
                self.problems.append(f"world_after: {detail}")
        if self.spec.get("oracle"):
            from scripts import live_oracles
            self.problems += live_oracles.check(self.spec["oracle"], self)

    def cleanup(self) -> None:
        for module, uid in reversed(self.created):
            try:
                self.fsr.client.delete(f"/api/3/{module}/{uid}")
                self.log(f"   cleanup: deleted {module} {uid[:8]}")
            except Exception as e:  # noqa: BLE001
                self.log(f"   cleanup: could not delete {module} {uid}: {e}")
        for module, uid, fields in reversed(self.restore):
            try:
                self.fsr.client.put(f"/api/3/{module}/{uid}", _restorable(fields))
                self.log(f"   cleanup: restored {sorted(fields)} on {module} {uid[:8]}")
            except Exception as e:  # noqa: BLE001
                self.log(f"   cleanup: could not restore {module} {uid}: {e}")
        for module, before in self.snapshot:
            uid = before.get("uuid")
            try:
                now = read(self.fsr.client, module, uid)
            except Exception:  # noqa: BLE001 -- deleted by the arc: nothing to put back onto
                self.log(f"   cleanup: {module} {str(uid)[:8]} is gone; not recreated")
                continue
            changed = {k: v for k, v in before.items()
                       if k not in self._META and not same_value(v, now.get(k))}
            if not changed:
                continue
            try:
                self.fsr.client.put(f"/api/3/{module}/{uid}", _restorable(changed))
                self.log(f"   cleanup: put back {sorted(changed)} on {module} {before.get('name')!r}")
            except Exception as e:  # noqa: BLE001
                self.log(f"   cleanup: could not put back {module} {uid}: {e}")


def _restorable(fields: dict) -> dict:
    """A record read back holds link stubs; a PUT takes their IRIs."""
    out = {}
    for k, v in fields.items():
        if isinstance(v, dict) and v.get("@id"):
            out[k] = v["@id"]
        elif isinstance(v, list):
            out[k] = [x["@id"] if isinstance(x, dict) and x.get("@id") else x for x in v]
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category")
    ap.add_argument("--scenario", "-s", default="", help="comma-separated row names")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--device", default=os.environ.get("LIVE_WRITES_DEVICE"),
                    help="device to pick in a playbook's pick list (default: the first offered)")
    ap.add_argument("--keep", action="store_true", help="leave created records and updates in place")
    ap.add_argument("--out", help="write every run to this JSON file")
    args = ap.parse_args()

    if os.environ.get("FSR_WRITABLE") != "1":
        print("refusing: this runner writes to the box. Set FSR_WRITABLE=1 in the env file of a test box.")
        return 2
    rows, skipped = load_rows(args.category, [s for s in args.scenario.split(",") if s])
    if not rows:
        print("no runnable rows" + (": " + "; ".join(skipped) if skipped else ""))
        return 2
    from fsr_live import LiveFSR
    fsr = LiveFSR.from_env()
    print(f"live writes -> {fsr.base}  config={fsr.config}  rows={len(rows)} runs={args.runs}")
    for s in skipped:
        print(f"  skip  {s}")

    caps = harness_caps(fsr.client, records=[(r["mount"]["module"], r["mount"]["uuid"])
                                             for r in rows if r.get("mount")], env=os.environ)
    results, clean, skipped_live = [], 0, []
    for spec in rows:
        reason = skip_reason(row_requires(spec), caps)
        if reason:
            skipped_live.append(spec["name"])
            results.append({"row": spec["name"], "passed": None, "runs": []})
            print(f"SKIP  {spec['name']}  ({reason})")
            continue
        runs = []
        for i in range(args.runs):
            print(f"\n-- {spec['name']} run {i + 1}")
            t0 = time.time()
            arc = None
            try:
                arc = Arc(fsr, spec, args.device, lambda s: print(s, flush=True))
                arc.run()
                arc.grade()
            except Exception as e:  # noqa: BLE001 -- a row that cannot even mount is reported, not fatal
                if arc is None:
                    runs.append({"ok": False, "problems": [f"{type(e).__name__}: {e}"[:300]], "secs": 0})
                    continue
                arc.problems.append(f"{type(e).__name__}: {e}"[:300])
            finally:
                if arc is not None and not args.keep:
                    arc.cleanup()
            runs.append({"ok": not arc.problems, "problems": arc.problems, "writes": arc.writes,
                         "tools": arc.tools, "said": arc.said, "secs": round(time.time() - t0)})
        passed = sum(bool(r["ok"]) for r in runs)
        clean += passed == len(runs)
        results.append({"row": spec["name"], "passed": passed, "runs": runs})
        print(f"{'PASS' if passed == len(runs) else 'FAIL'}  {spec['name']}  {passed}/{len(runs)}")
        for j, r in enumerate(runs, 1):
            for p in r["problems"]:
                print(f"   run{j}: {p}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1, default=str))
    ran = len(rows) - len(skipped_live)
    print(f"\n{clean}/{ran} rows clean"
          + (f" ({len(skipped_live)} skipped: the box lacks a requirement)" if skipped_live else ""))
    if not ran:
        return 2  # nothing was measured: not a pass
    return 0 if clean == ran else 1


if __name__ == "__main__":
    raise SystemExit(main())
