#!/usr/bin/env python3
"""T0 tool sweep -- deterministic, offline, no LLM, no box.

The question this answers: *does every tool in the registry honour the dispatch
contract?* `tool_probe.py` runs ONE tool against a live box with explicit args;
`t1_scenarios.py` grades whether a real model reaches for the right tool. This
sits below both: it walks the WHOLE registry and asserts the properties that
must hold for every tool regardless of model, box, or arguments.

Why contract invariants instead of "call each tool with valid args": most of the
surface (siem_*, faz_*, fmg_*, get_record, run_op, create_record) cannot execute
without an appliance, so an args-based sweep would either need a box -- defeating
the point -- or silently skip two thirds of the registry and still report green.
The invariants below hold offline for all of them, and they are exactly the bug
classes that have actually shipped:

  R  resolvable      -- dispatch() must not answer "unknown tool" for a name the
                        registry (or an advertised slice) contains. This is the
                        approve-never-executes bug: an approved `mcp_soc__*`
                        action resolved to None on the resume path and silently
                        never ran.
  N  never-raises    -- dispatch() returns a dict envelope for ANY arguments,
                        including junk and empty. A traceback escaping dispatch
                        becomes a 500 instead of a self-correctable tool_result.
  G  gate-integrity  -- a tier >= floor tool called WITHOUT approval must return
                        the pending_approval envelope and must NOT invoke the
                        underlying function. Proven with a tripwire, not by
                        reading the envelope: `run_op` was once advertised as
                        gated while `validating()` never wrapped it.
  I  injection       -- a wire-supplied `_approved` must be rejected
                        (`reserved_key_rejected`), never honoured. Tier-gate
                        bypass.
  S  schema-sanity   -- every `required` name in the advertised input_schema must
                        exist in the function signature, or the model is being
                        told to send an argument the tool cannot accept.
  V  validation      -- three bad inputs per tool (an unknown key beside
                        schema-valid args, the required args missing, one
                        property sent as a type the coercion cannot turn into
                        the declared one) must each come back `invalid_tool_args`
                        and must NOT run the tool body (tripwire, like G). The
                        advertised schema is the one argument contract (W1); a
                        tool the gate cannot hold to it fails here, not in a
                        traceback mid-turn.

  P  partial-shape   -- a gap condition (no FSR configured, unreachable source)
                        must answer {delivered, unknowns, next} via the
                        framework's PartialResult, not a bare refusal or error
                        string (W4). Registry-wide: every tool is run with no
                        FSR configured; plus `expect.partial` cases for the
                        gaps that need specific arguments.

On top of that, opt-in BEHAVIOURAL cases (same file format as tool_probe.py)
exercise tools that genuinely run offline -- the YAML authoring loop, the card
emitters, the static catalogue lookups. Coverage accounting reports which tools
have no case yet, so "test all the tools" becomes a number to drive down rather
than a claim.

Usage:
    make tool-sweep                 # contract invariants + behavioural cases
    make tool-sweep STRICT=1        # also fail on any tool lacking a case
    python scripts/tool_sweep.py --json
    python scripts/tool_sweep.py --tool run_op         # one tool, verbose
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

# $FSRPB_DB is the REFERENCE CORPUS (connectors, operations, step_types,
# jinja_macros, picklists), not merely an audit store -- every dispatch also
# appends an audit row to the same file. Pointing it at a blank temp DB
# therefore does not "isolate the audit trail"; it deletes the catalogue, and
# every discovery tool answers `OperationalError: no such table: connectors`.
# Copy the real DB instead: lookups resolve against the true corpus while audit
# writes land on the throwaway.
os.environ.setdefault("FSRPB_DEV", "1")
# The sweep is offline by definition: no FSR instance. Blank (not unset) the
# connection variables, because the framework's dotenv loader only fills
# variables that are missing, so a developer .env cannot turn the sweep live.
# The P invariant relies on this: every box-needing tool lands on its gap path.
for _k in ("FSR_BASE_URL", "FSR_API_KEY", "FSR_USERNAME", "FSR_PASSWORD"):
    os.environ[_k] = ""


def _isolated_db() -> str:
    import shutil
    from fsr_playbooks._db import default_db_path
    src = default_db_path()
    dst = Path(tempfile.gettempdir()) / "fsr_tool_sweep_ref.sqlite"
    if src.exists():
        # Refresh only when the source moved on, so repeat sweeps stay fast.
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(src, dst)
        return str(dst)
    return str(src)

CASES_PATH = _ROOT / "scripts" / "tool_sweep_cases.json"

# Tools observed to reach the network during the sweep. A leak is a finding, not
# a crash: silent fallthrough to a real appliance is the trap the widget
# harness's FSR_HERMETIC posture exists to kill (TESTING.md, "Two tiers") -- a
# sweep that quietly touches the box looks offline right up until the box is
# down, and then reds for the wrong reason.
NET_LEAKS: dict[str, int] = {}
_CURRENT_TOOL: list[str] = ["<setup>"]


def install_hermetic_guard() -> Callable[[], None]:
    """Make the sweep genuinely box-free, and make any escape attributable.

    Two layers, because there are two ways out. `_live_client()` is the
    framework's own seam -- priming its cache with None short-circuits every
    tool that politely asks for a client. But `fsr_soc_triage.tools_agent`
    `setdefault`s the framework repo's `.env` (FSR_HOST/creds) into os.environ on
    any tool-using turn, so a tool holding its own session can still dial out.
    The socket-level block is the backstop that makes that impossible rather
    than unlikely.
    """
    # Layer 1: the framework's client seam.
    try:
        from fsr_playbooks.mcp_server import _shared
        _shared._LIVE_CLIENT_CACHE["client"] = None
    except Exception:
        pass

    # Layer 2: hard socket block. dispatch() catches the exception and turns it
    # into an error envelope, which is the correct contract behaviour for a tool
    # that cannot reach its backend -- so blocking does not distort the test.
    import socket

    real_conn = socket.socket.connect

    def _blocked(self, addr, *a, **k):
        # Unix sockets / loopback stay open: a local sqlite or IPC path is not
        # an appliance call, and blocking it would break the audit store. A
        # Unix socket's address is a path (str/bytes), not a (host, port)
        # tuple -- treating it as a host blocked multiprocessing's forkserver.
        if isinstance(addr, (str, bytes)):
            return real_conn(self, addr, *a, **k)
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if isinstance(host, str) and (host.startswith("127.") or host == "::1"
                                      or host == "localhost"):
            return real_conn(self, addr, *a, **k)
        NET_LEAKS[_CURRENT_TOOL[0]] = NET_LEAKS.get(_CURRENT_TOOL[0], 0) + 1
        raise OSError(
            f"[SWEEP-HERMETIC] blocked outbound connection to {host} "
            f"during tool {_CURRENT_TOOL[0]!r}"
        )

    socket.socket.connect = _blocked

    # Layer 3: no real waiting. The sweep grades a tool's CONTRACT, not its pacing, yet
    # a transient-fault retry (0.5s) and the FortiSIEM poll loop (1s x 9) slept for real
    # on every empty/junk probe: 5 tools cost ~48s of a ~55s test. A 1 ms cap keeps
    # deadline loops advancing without burning wall time.
    import time as _t
    real_sleep = _t.sleep
    _t.sleep = lambda s=0, _real=real_sleep: _real(min(float(s or 0), 0.001))

    def uninstall() -> None:
        socket.socket.connect = real_conn
        _t.sleep = real_sleep
    return uninstall


# --------------------------------------------------------------------------
# registry loading
# --------------------------------------------------------------------------
def load_registry() -> tuple[dict, dict[str, set[str]]]:
    """Import the framework registry WITH the connector's triage tools injected.

    The connector mutates SAFE_TOOLS / TOOL_TIERS / REGISTRY at import
    (Option-A posture), so a sweep that imports only the framework sees ~40 of
    the ~61 tools and reports a falsely clean bill of health.
    """
    try:
        from fsr_soc_triage.registry import register_triage_tools
        register_triage_tools()
    except Exception as e:  # pragma: no cover - surfaced, never swallowed
        print(f"WARNING: connector triage tools not registered ({type(e).__name__}: {e});"
              f" sweeping the framework surface only", file=sys.stderr)

    from fsr_playbooks.llm import tools as T
    from fsr_playbooks.llm.intents import tools_for_intent

    slices: dict[str, set[str]] = {
        "build": {t["name"] for t in tools_for_intent("build")},
        "triage": {t["name"] for t in tools_for_intent("triage")},
    }
    return T.REGISTRY, slices


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------
def _junk_args() -> dict:
    """Arguments deliberately wrong for any tool.

    Probed alongside empty args because the two exercise different paths: empty
    args reach `fn` for the 49 tools with no required parameters, while an
    unexpected key exercises the arg-validation/TypeError path. Neither may
    produce anything but a dict envelope.
    """
    return {"__sweep_bogus__": {"nested": ["junk", 1, None]}}


_FILL = {"string": "sweep", "integer": 1, "number": 1, "boolean": False,
         "object": {}, "array": []}


def _valid_args(spec: Any) -> dict:
    """Schema-valid filler for every required argument.

    The G probe needs a call that clears the arg gate: with `{}`, dispatch now
    refuses a tool with required arguments BEFORE the tier gate is consulted,
    so a tripwire on `fn` would stay silent even with the approval gate gone --
    G would pass vacuously.
    """
    schema = getattr(spec, "input_schema", None) or {}
    props = schema.get("properties") or {}
    out: dict = {}
    for r in schema.get("required") or []:
        d = props.get(r) or {}
        if d.get("enum"):
            out[r] = d["enum"][0]
            continue
        t = d.get("type")
        t = next((x for x in t if x != "null"), "string") if isinstance(t, list) else t
        v = _FILL.get(t or "string", "sweep")
        if t == "object" and d.get("minProperties"):
            v = {"sweep": "sweep"}
        elif t == "array" and d.get("minItems"):
            v = ["sweep"]
        out[r] = v
    return out


# A value of each JSON type, for the wrong-type probe. A STRING is never used:
# coercion legitimately turns "25" into 25 and "false" into False.
_WRONG = (("object", {"sweep": 1}), ("array", [{"sweep": 1}]), ("number", 1.5),
          ("boolean", True))


def _types_of(prop: Any) -> set[str]:
    t = prop.get("type") if isinstance(prop, dict) else None
    kinds = set(t) if isinstance(t, list) else ({t} if isinstance(t, str) else set())
    if "number" in kinds:
        kinds.add("integer")
    return kinds


def _bad_inputs(spec: Any) -> list[tuple[str, dict]]:
    """(label, args) for the V probes this tool's schema allows."""
    schema = getattr(spec, "input_schema", None) or {}
    props = schema.get("properties") or {}
    base = _valid_args(spec)
    out = [("unknown key", {**base, "sweep_not_an_argument": "x"})]
    if schema.get("required"):
        out.append(("missing required", {}))
    # Prefer a required property, so the probe cannot pass by tripping some
    # other check first; any typed property otherwise.
    order = list(schema.get("required") or []) + [k for k in props
                                                  if k not in (schema.get("required") or [])]
    for key in order:
        kinds = _types_of(props.get(key))
        if not kinds:
            continue  # declared as any JSON value: no wrong type exists
        wrong = next((v for t, v in _WRONG
                      if t not in kinds and not (t == "number" and "integer" in kinds)),
                     None)
        if wrong is not None:
            out.append((f"wrong type for {key!r}", {**base, key: wrong}))
            break
    return out



def _is_no_fsr_gap(out: Any) -> bool:
    """True when a tool result is the 'no FSR instance configured' gap, in any of
    the shapes tools used before W4 (typed code, or a bare error string)."""
    if not isinstance(out, dict):
        return False
    from fsr_playbooks.llm.tool_result import NO_FSR_CODES
    if out.get("code") in NO_FSR_CODES:
        return True
    text = " ".join(str(out.get(k) or "") for k in ("error", "message"))
    return "FSR instance not configured" in text or "no FSR instance is configured" in text


def _is_source_gap(out: Any) -> bool:
    """True for a failed answer carrying one of the source-gap codes."""
    if not isinstance(out, dict) or out.get("ok") is not False:
        return False
    from fsr_playbooks.llm.tool_result import SOURCE_GAP_CODES
    return out.get("code") in SOURCE_GAP_CODES


class _DeadSession:
    """Every HTTP method fails in transit, like a box that dropped the socket."""

    def __getattr__(self, name: str) -> Any:
        def _boom(*a: Any, **k: Any) -> Any:
            raise ConnectionError(f"[SWEEP-DOWN] {name} failed in transit")
        return _boom


class _DeadClient:
    base_url = "https://fortisoar.example.com"
    verify_ssl = False

    def __init__(self) -> None:
        self.session = _DeadSession()


@contextlib.contextmanager
def _unreachable_source() -> Any:
    """FSR looks configured, but nothing answers: client requests raise and
    every connector op (`run_op`) answers the transport-failure error."""
    from fsr_playbooks import execution_api
    from fsr_playbooks.mcp_server import _shared
    saved_client = _shared._LIVE_CLIENT_CACHE.get("client")
    saved_run_op = execution_api.run_op
    _shared._LIVE_CLIENT_CACHE["client"] = _DeadClient()
    execution_api.run_op = lambda *a, **k: {
        "ok": False, "code": "transport_failed", "status": "?",
        "message": "[SWEEP-DOWN] connection failed"}
    try:
        yield
    finally:
        _shared._LIVE_CLIENT_CACHE["client"] = saved_client
        execution_api.run_op = saved_run_op


def _partial_problems(out: Any) -> list[str]:
    from fsr_playbooks.llm.tool_result import partial_problems
    return partial_problems(out)


def check_tool(name: str, spec: Any, T: Any, floor: int) -> list[dict]:
    """Run every contract invariant against one tool. Returns finding dicts."""
    findings: list[dict] = []

    def fail(inv: str, detail: str) -> None:
        findings.append({"tool": name, "invariant": inv, "detail": detail})

    # ---- R: resolvable ---------------------------------------------------
    try:
        res = T.dispatch(name, {})
    except BaseException as e:  # noqa: BLE001
        fail("N", f"dispatch raised on the resolvability probe: "
                  f"{type(e).__name__}: {e}")
        res = None
    if isinstance(res, dict) and res.get("error") == f"unknown tool: {name}":
        fail("R", "dispatch reports 'unknown tool' for a registered name")
        return findings  # nothing else is meaningful

    # ---- N: never raises, always a dict ----------------------------------
    for label, args in (("empty", {}), ("junk", _junk_args())):
        try:
            out = T.dispatch(name, dict(args))
        except BaseException as e:  # noqa: BLE001 - a raise IS the defect
            fail("N", f"dispatch raised on {label} args: {type(e).__name__}: {e}")
            continue
        if not isinstance(out, dict):
            fail("N", f"{label} args returned {type(out).__name__}, not a dict")

    # ---- I: wire-supplied _approved must be rejected ----------------------
    # Guarded like the others: an unwrapped probe lets a raising tool abort the
    # whole sweep instead of being reported as the N violation it is.
    try:
        inj = T.dispatch(name, {"_approved": True})
    except BaseException as e:  # noqa: BLE001
        fail("N", f"dispatch raised on the _approved probe: {type(e).__name__}: {e}")
        inj = None
    if isinstance(inj, dict) and inj.get("code") != "reserved_key_rejected":
        fail("I", f"wire-supplied _approved not rejected (code={inj.get('code')!r})")

    # ---- G: tier gate actually blocks execution --------------------------
    # Resolve the tier the way dispatch does, so dynamic (-1) tools are judged
    # on their real, argument-derived tier rather than the static placeholder.
    try:
        tier = T._resolve_tier(name, {})
    except Exception:
        tier = getattr(spec, "tier", 0)
    if tier >= floor:
        tripped = {"hit": False}
        original = spec.fn

        def _tripwire(*a, **k):
            tripped["hit"] = True
            return original(*a, **k)

        # ToolSpec is a frozen dataclass, so swap the REGISTRY entry rather than
        # the field -- dispatch resolves the spec via REGISTRY.get(name) on every
        # call, so this is the same seam the real call path uses.
        import dataclasses
        T.REGISTRY[name] = dataclasses.replace(spec, fn=_tripwire)
        try:
            out = T.dispatch(name, _valid_args(spec))
        except BaseException as e:  # noqa: BLE001
            fail("G", f"dispatch raised while gated: {type(e).__name__}: {e}")
            out = {}
        finally:
            T.REGISTRY[name] = spec
        if tripped["hit"]:
            fail("G", f"tier-{tier} tool EXECUTED without approval "
                      f"(gate bypassed)")
        elif isinstance(out, dict) and out.get("code") == "invalid_tool_args":
            fail("G", "could not build schema-valid args to probe the tier "
                      f"gate (arg gate refused: {out.get('error', '')[:120]})")
        elif isinstance(out, dict) and not (
            out.get("pending_approval")
            # A precard rejection / arg-validation bounce is also a non-execution,
            # which is the property under test.
            or out.get("code") or out.get("error")
        ):
            fail("G", f"tier-{tier} tool returned neither pending_approval nor "
                      f"an error envelope: {json.dumps(out)[:160]}")

    # ---- V: bad input is refused by the gate, never reaches the body ------
    for label, args in _bad_inputs(spec):
        tripped = {"hit": False}
        original = spec.fn

        # wraps() keeps the real signature visible: the gate reads it to find
        # unknown keys, and a bare (*a, **k) would read as "accepts anything".
        @functools.wraps(original)
        def _tripwire(*a, **k):
            tripped["hit"] = True
            return original(*a, **k)

        import dataclasses
        T.REGISTRY[name] = dataclasses.replace(spec, fn=_tripwire)
        try:
            out = T.dispatch(name, args)
        except BaseException as e:  # noqa: BLE001
            fail("V", f"dispatch raised on {label}: {type(e).__name__}: {e}")
            continue
        finally:
            T.REGISTRY[name] = spec
        if tripped["hit"]:
            fail("V", f"{label}: the tool body RAN ({json.dumps(args, default=str)[:120]})")
        elif not (isinstance(out, dict) and out.get("code") == "invalid_tool_args"):
            fail("V", f"{label}: expected invalid_tool_args, got "
                      f"{json.dumps(out, default=str)[:160]}")

    # ---- P: a no-FSR gap answers the partial shape (W4) ---------------------
    # The sweep runs with no FSR instance configured, so any tool that needs one
    # and is not approval-gated lands on its gap path here. Whatever it answers
    # (a dict, or a list of dicts) must be a valid PartialResult, not a bare
    # refusal or `{"error": ...}`. Not case-driven: it covers every tool.
    # An approval-gated tool is also run once as the host does after the analyst
    # approves (nothing can reach a box: no FSR is configured and the hermetic
    # guard blocks outbound), so its body's gap path is judged too.
    probes = [dict(_valid_args(spec))]
    gated = tier >= floor
    for call_args in probes:
        try:
            gap_out = (T.dispatch(name, {**call_args, "_approved": True},
                                  _internal=True, approved_by="human")
                       if gated else T.dispatch(name, call_args))
        except BaseException:  # noqa: BLE001 - N already reports raises
            gap_out = None
        for item in (gap_out if isinstance(gap_out, list) else [gap_out]):
            if _is_no_fsr_gap(item):
                for prob in _partial_problems(item):
                    fail("P", f"no-FSR gap is not a partial result: {prob}")

    # ---- P (unreachable source): a transport failure answers the partial shape
    # The same probe again with FSR "configured" but every request, and every
    # connector op, failing in transit (nothing leaves the process). Any failed
    # answer that names a source-gap code (transport / upstream / rejected /
    # connector unusable) must be a valid PartialResult. This judges the code the
    # tool answers with; a tool that swallows the failure into another code or
    # an empty ok result is not seen here (see the closure debt note in
    # scripts/tool_sweep_cases.json).
    with _unreachable_source():
        try:
            down_out = (T.dispatch(name, {**dict(_valid_args(spec)), "_approved": True},
                                   _internal=True, approved_by="human")
                        if gated else T.dispatch(name, dict(_valid_args(spec))))
        except BaseException:  # noqa: BLE001 - N already reports raises
            down_out = None
    for item in (down_out if isinstance(down_out, list) else [down_out]):
        if _is_source_gap(item):
            for prob in _partial_problems(item):
                fail("P", f"unreachable-source gap ({item.get('code')}) is not a "
                          f"partial result: {prob}")

    # ---- S: advertised required args exist on the function ---------------
    import inspect
    required = ((spec.input_schema or {}).get("required") or [])
    try:
        sig = inspect.signature(spec.fn)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                             for p in sig.parameters.values())
        if not accepts_kwargs:
            for arg in required:
                if arg not in sig.parameters:
                    fail("S", f"input_schema requires {arg!r} but the function "
                              f"signature has no such parameter")
    return findings


# --------------------------------------------------------------------------
# behavioural cases (opt-in, offline)
# --------------------------------------------------------------------------
def _check_expect(result: Any, expect: dict) -> list[str]:
    """Shared shape with tool_probe.py's _check, so cases port between them."""
    fails: list[str] = []
    # Not every tool answers with an envelope: the catalogue lookups
    # (find_jinja_filter, search_playbooks, ...) return a bare list of rows.
    # Treat a list as a countable result rather than a contract violation.
    if isinstance(result, list):
        if expect.get("is_list") is False:
            fails.append("result is a list, expected an object")
        if "min_count" in expect and len(result) < expect["min_count"]:
            fails.append(f"count: expected >= {expect['min_count']}, got {len(result)}")
        for frag in expect.get("contains", []):
            if frag not in json.dumps(result, default=str):
                fails.append(f"result does not contain {frag!r}")
        if expect.get("ok") is False:
            fails.append("expected a failure envelope, got a list of results")
        return fails
    if not isinstance(result, dict):
        return [f"result is {type(result).__name__}, not a dict or list"]
    if expect.get("partial"):
        # W4: a gap condition answers delivered / unknowns / next, not a bare
        # refusal. The shape is the framework's PartialResult model.
        from fsr_playbooks.llm.tool_result import partial_problems
        fails.extend(f"partial shape: {p}" for p in partial_problems(result))
    if "ok" in expect and bool(result.get("ok")) != bool(expect["ok"]):
        fails.append(f"ok: expected {expect['ok']}, got {result.get('ok')}")
    if "code" in expect and result.get("code") != expect["code"]:
        fails.append(f"code: expected {expect['code']!r}, got {result.get('code')!r}")
    if expect.get("no_error") and (result.get("error") or result.get("code")):
        fails.append(f"unexpected error/code: {result.get('code') or result.get('error')}")
    for k in expect.get("has_keys", []):
        if k not in result:
            fails.append(f"missing key {k!r}")
    for frag in expect.get("contains", []):
        if frag not in json.dumps(result, default=str):
            fails.append(f"result does not contain {frag!r}")
    if "min_count" in expect:
        n = result.get("count")
        if n is None:
            for key in ("results", "playbooks", "records", "matches"):
                if isinstance(result.get(key), list):
                    n = len(result[key]); break
        if n is None or n < expect["min_count"]:
            fails.append(f"count: expected >= {expect['min_count']}, got {n}")
    return fails


_FIXTURES = _ROOT / "scripts" / "eval_fixtures"


def _case_args(case: dict) -> dict:
    """Resolve a case's arguments, inlining any fixture-sourced YAML.

    Cases reference fixtures by bare name (`marker_emitter.yaml`) rather than
    embedding playbook YAML in JSON, so the sweep exercises the SAME fixtures
    the T1 scenarios mount -- one corpus, no divergent copies.
    """
    args = dict(case.get("args") or {})
    for key, fixture in (case.get("args_from_fixture") or {}).items():
        path = Path(fixture)
        if not path.is_absolute():
            path = _FIXTURES / fixture
        args[key] = path.read_text()
    return args


def run_cases(T: Any, cases: list[dict], only: str | None) -> tuple[list[dict], set[str]]:
    results: list[dict] = []
    covered: set[str] = set()
    for case in cases:
        tool = case["tool"]
        if only and tool != only:
            continue
        covered.add(tool)
        try:
            _CURRENT_TOOL[0] = tool
            out = T.dispatch(tool, _case_args(case))
        except BaseException as e:  # noqa: BLE001
            results.append({"name": case.get("name") or tool, "tool": tool,
                            "fails": [f"raised {type(e).__name__}: {e}"]})
            continue
        results.append({"name": case.get("name") or tool, "tool": tool,
                        "fails": _check_expect(out, case.get("expect") or {}),
                        "result": out})
    return results, covered


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def _query_timeout_findings() -> list[dict]:
    """Invariant T (I2): a SIEM query whose poll budget runs out must not read as
    an ok:true answer with zero events. The FortiSIEM here accepts the query and
    never reports progress 100."""
    import time as _time

    import fsr_playbooks.execution_api as ea
    from fsr_playbooks.mcp_server import tools_triage as TT

    def never_finishes(connector, op, params=None, **kw):
        ep = str((params or {}).get("endpoint") or "")
        if ep.endswith("/eventQuery"):
            return {"ok": True, "data": {"queryId": "sweep-t"}}
        if ep.endswith("/progress"):
            return {"ok": True, "data": {"progress": 10}}
        return {"ok": False, "code": "op_request_rejected", "message": ep}

    saved = (ea.run_op, TT._siem_connector, _time.sleep)
    ea.run_op = never_finishes
    TT._siem_connector = lambda: "fortinet-fortisiemv2"
    _time.sleep = lambda s: None
    try:
        out = TT.siem_search_ip("10.0.0.1")
    except Exception as e:  # noqa: BLE001 - a raise is a finding too
        out = {"ok": None, "detail": repr(e)}
    finally:
        ea.run_op, TT._siem_connector, _time.sleep = saved
    if out.get("ok") is not False or out.get("partial") is not True:
        return [{"tool": "siem_search_ip", "invariant": "T",
                 "detail": "a FortiSIEM query still running at the poll budget "
                           f"answered {{ok: {out.get('ok')}, count: {out.get('count')}}}"
                           " instead of a partial result (a timeout read as zero)"}]
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T0 offline tool sweep")
    ap.add_argument("--tool", default=None, help="sweep one tool only")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="also fail when a registered tool has no behavioural case")
    ap.add_argument("--cases", default=str(CASES_PATH))
    args = ap.parse_args(argv)

    # The injection invariant deliberately trips the tier-bypass logger on every
    # tool; that is the PASS path here, so keep it out of the report.
    import logging
    logging.getLogger("fsr_playbooks.llm.tools").setLevel(logging.CRITICAL)

    # Must precede the first dispatch (which opens the DB) but follow imports.
    os.environ["FSRPB_DB"] = _isolated_db()
    REGISTRY, slices = load_registry()
    # After import (the connector's .env setdefault has happened by now), before
    # any dispatch.
    install_hermetic_guard()
    from fsr_playbooks.llm import tools as T
    try:
        from fsr_playbooks.llm.authorization import _approval_floor
        floor = _approval_floor()
    except Exception:
        floor = 3

    names = sorted(REGISTRY)
    if args.tool:
        if args.tool not in REGISTRY:
            print(f"no such tool: {args.tool}", file=sys.stderr)
            return 2
        names = [args.tool]

    findings: list[dict] = []
    for n in names:
        _CURRENT_TOOL[0] = n
        findings.extend(check_tool(n, REGISTRY[n], T, floor))
    _CURRENT_TOOL[0] = "<cases>"

    # Slice hygiene: everything advertised must be dispatchable, and a tool in
    # no slice at all can never be reached by a model.
    for sname, members in slices.items():
        for m in sorted(members - set(REGISTRY)):
            findings.append({"tool": m, "invariant": "R",
                             "detail": f"advertised in the {sname} slice but absent "
                                       f"from REGISTRY"})

    # A: advertised-somewhere. A registered tool in NO slice is dead code from
    # the model's point of view -- it can never be called, no matter the intent.
    # `run_playbook` reached this state by being in BUILD_ONLY_TOOLS *and* being
    # added to TRIAGE_ONLY_TOOLS by the connector's register_triage_tools(), so
    # both slices subtract it. Parallel name lists drifting apart is a known bug
    # class here; assert the relationship rather than trusting the lists.
    # Retired names (framework CONSOLIDATED_AWAY + the connector's merged hunt
    # helpers) stay registered on purpose -- old sessions and persona
    # allowlists still name them -- and are reached through their union. They
    # are orphans only if that union is itself unadvertised.
    advertised = set().union(*slices.values()) if slices else set()
    try:
        from fsr_playbooks.llm.tools import retired_to_union
        retired = dict(retired_to_union())
    except ImportError:
        retired = {}
    for old_name, union in sorted(retired.items()):
        if old_name in REGISTRY and union not in advertised:
            findings.append({"tool": old_name, "invariant": "A",
                             "detail": f"retired into `{union}`, which is "
                                       "advertised in NO slice"})
    orphans = (sorted(set(REGISTRY) - advertised - set(retired))
               if slices else [])
    for o in orphans:
        findings.append({"tool": o, "invariant": "A",
                         "detail": "registered but advertised in NO slice -- "
                                   "unreachable by any model in any intent"})

    # T: a FortiSIEM query still running when its poll budget runs out is an
    # unknown, never a zero. Driven through the engine itself with a FortiSIEM
    # that never finishes; the answer must not be ok:true with no events.
    findings.extend(_query_timeout_findings())

    # (Invariant F checked that run-mode narrowing actually narrowed. Lever 2
    # was removed -- it failed open in practice -- so there is no narrowing to
    # assert. run_playbook's presence in the build slice is covered by
    # test_run_playbook_is_reachable_from_build.)

    cases: list[dict] = []
    box_required: set[str] = set()
    cpath = Path(args.cases)
    if cpath.exists():
        blob = json.loads(cpath.read_text())
        # Accept both the documented object form and a bare list of cases.
        if isinstance(blob, list):
            cases = blob
        else:
            cases = blob.get("cases") or []
            box_required = set(blob.get("box_required") or [])
    # --- acknowledged findings -------------------------------------------
    # A gate that sits perma-red stops being read. Known-and-triaged findings
    # are ACKNOWLEDGED here: still printed on every run, but not failing the
    # target. The baseline cannot rot, because an entry that no longer matches
    # anything is itself a failure -- so a fixed defect forces the acknowledgement
    # to be deleted rather than quietly outliving the bug it described.
    ack: dict[str, str] = {}
    ackfile = Path(args.cases).with_name("tool_sweep_acknowledged.json")
    if ackfile.exists():
        ack = {k: v for k, v in json.loads(ackfile.read_text()).items()
               if not k.startswith("_")}

    def _key(f: dict) -> str:
        return f"{f['invariant']}:{f['tool']}"

    acknowledged = [f for f in findings if _key(f) in ack]
    findings = [f for f in findings if _key(f) not in ack]
    stale_ack = sorted(set(ack) - {_key(f) for f in acknowledged})

    case_results, covered = run_cases(T, cases, args.tool)
    case_fails = [c for c in case_results if c["fails"]]

    # A box-only tool is not a coverage gap -- the contract invariants are its
    # coverage. Counting it as "uncovered" would permanently peg the number at
    # ~40% and make the metric useless to drive.
    offline_surface = set(names) - box_required
    uncovered = sorted(offline_surface - covered)
    stale_box = sorted(box_required & covered)

    ok = (not findings and not case_fails and not stale_ack
          and not (args.strict and uncovered))

    if args.json:
        print(json.dumps({
            "ok": ok,
            "tools_total": len(names),
            "contract_findings": findings,
            "cases_run": len(case_results),
            "case_failures": [{"name": c["name"], "tool": c["tool"],
                               "fails": c["fails"]} for c in case_fails],
            "coverage": {
                "covered": sorted(covered), "uncovered": uncovered,
                "box_required": sorted(box_required),
                "offline_surface": len(offline_surface),
                "pct": round(100.0 * len(covered & offline_surface)
                             / max(len(offline_surface), 1), 1),
            },
            "slice_orphans": orphans,
            "acknowledged": [{**f, "ack": ack[_key(f)]} for f in acknowledged],
            "stale_acknowledgements": stale_ack,
            "network_attempts": NET_LEAKS,
        }, indent=2, default=str))
        return 0 if ok else 1

    print("── T0 tool sweep ──────────────────────────────────────────")
    print(f"registry: {len(names)} tools   approval floor: tier {floor}")
    for sname, members in sorted(slices.items()):
        print(f"  slice {sname:<7} {len(members)} advertised")
    if orphans:
        print(f"  ⚠ in NO slice (unreachable by any model): {', '.join(orphans)}")

    print(f"\ncontract invariants (R/N/G/I/S/V/P/A/F/T) over {len(names)} tools: "
          f"{'PASS' if not findings else f'{len(findings)} FINDING(S)'}")
    for f in findings:
        print(f"  ✗ [{f['invariant']}] {f['tool']}: {f['detail']}")

    if acknowledged:
        print(f"\nacknowledged ({len(acknowledged)}) -- known, triaged, NOT failing "
              f"the gate:")
        for f in acknowledged:
            print(f"  ! [{f['invariant']}] {f['tool']}: {f['detail']}")
            print(f"      ack: {ack[_key(f)]}")
    if stale_ack:
        print(f"\n✗ STALE acknowledgement(s) -- the finding is GONE, so delete the "
              f"entry from {ackfile.name}:")
        for k in stale_ack:
            print(f"    · {k}")

    print(f"\nbehavioural cases: {len(case_results)} run, "
          f"{len(case_fails)} failed")
    for c in case_fails:
        print(f"  ✗ {c['name']} ({c['tool']})")
        for fl in c["fails"]:
            print(f"      · {fl}")

    if NET_LEAKS:
        total = sum(NET_LEAKS.values())
        print(f"\nhermetic: {total} outbound connection(s) BLOCKED across "
              f"{len(NET_LEAKS)} tool(s) -- these would have hit a real appliance")
        for t, c in sorted(NET_LEAKS.items(), key=lambda kv: -kv[1]):
            print(f"    · {t} ×{c}")
    else:
        print("\nhermetic: clean (no outbound connection attempted)")

    hit = len(covered & offline_surface)
    pct = 100.0 * hit / max(len(offline_surface), 1)
    print(f"\ncoverage: {hit}/{len(offline_surface)} offline-testable tools have "
          f"a case ({pct:.0f}%)   [{len(box_required)} more are box-only: contract "
          f"invariants are their coverage]")
    if uncovered:
        print(f"  no case yet ({len(uncovered)}): {', '.join(uncovered)}")
    if stale_box:
        print(f"  ⚠ listed box_required but a case ran offline anyway "
              f"({len(stale_box)}): {', '.join(stale_box)} -- reclassify")

    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
