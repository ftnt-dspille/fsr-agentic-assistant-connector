#!/usr/bin/env python3
"""Phase 1 contract self-test for the connector-fsr-agentic-assistant connector.

Mirrors §9 of FSR_PLAYBOOK_BUILDER_CONNECTOR_CONTRACT.md (v2.0.0). Drives
the connector's operation handlers directly -- the same
`(config, params) -> dict` entry points FortiSOAR calls via
`connector.py` -- and asserts the contract wire shape for T1..T12.

Run it before every release tag. Exit code is 0 only when every selected
test passes; any FAIL makes it non-zero so CI / the demo-prep checklist
can gate on it.

    # full run -- live tests drive a provider (default: the free Frank gateway)
    FRANK_BASE_URL=... FRANK_API_KEY=... python scripts/validate_connector.py
    # or a paid key when you explicitly want it
    ANTHROPIC_API_KEY=sk-... python scripts/validate_connector.py --llm anthropic

    # hermetic subset (T1, T8, T9, T10-T12) with egress hard-blocked.
    # This is what `make build` gates on.
    python scripts/validate_connector.py --hermetic

    # same subset, but egress merely unused rather than blocked
    python scripts/validate_connector.py --offline

    # a specific test or two
    python scripts/validate_connector.py -t T5 -t T6

Test taxonomy
-------------
* Hermetic (always run, fakes injected, NO network): T1, T8, T9, T10-T12.
  These gate `make build` -- keep them box-free or the gate is unshippable.
* Live (need a provider -- default Frank via FRANK_BASE_URL, or --llm anthropic
  with ANTHROPIC_API_KEY; auto-SKIP when none is configured): T1L, T2-T7.
* T9 (mock replay) is hermetic now that mock_replay has landed.

Notes
-----
* T6/T7 exercise the Model-A card resume: emit_action_card is tier-0 and
  stashes no SuspendedSession; the connector persists the conversation
  per session and chat_resume(card_id) reloads it, appends the decision,
  and re-runs the loop so the agent executes (see operations._resume_
  conversation). chat_resume(card_id) carries no messages[] (contract §4).
* T5 reliability depends on the system prompt steering the model to emit a
  card (Phase 4). The connector's current bare prompt does not strongly
  steer, so T5 may need the Phase 4 triage prompt to pass live.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Bootstrap: make the connector + fsr_playbooks importable off-platform.
# --------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent                       # ConnectorsV2/fsr-playbook-builder
_PKG_DIR = _REPO / "connector-fsr-agentic-assistant"  # hyphenated on-platform dir


def _stub_platform_imports() -> None:
    """Stub `connectors.core.connector` when we're not inside a live
    FortiSOAR worker (mirrors tests/conftest.py)."""
    import types
    if "connectors" in sys.modules:
        return
    pkg = types.ModuleType("connectors")
    core = types.ModuleType("connectors.core")
    conn = types.ModuleType("connectors.core.connector")

    class Connector:  # noqa: D401 - stub
        pass

    class ConnectorError(Exception):
        pass

    def get_logger(name):
        import logging
        return logging.getLogger(name)

    conn.Connector = Connector
    conn.ConnectorError = ConnectorError
    conn.get_logger = get_logger
    pkg.core = core
    core.connector = conn
    sys.modules["connectors"] = pkg
    sys.modules["connectors.core"] = core
    sys.modules["connectors.core.connector"] = conn


def _load_connector_package() -> None:
    """Import the hyphenated package dir as `connector_fsr_agentic_assistant`."""
    import importlib.util
    import types
    if "connector_fsr_agentic_assistant" in sys.modules:
        return
    init = _PKG_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "connector_fsr_agentic_assistant",
        str(init) if init.exists() else None,
        submodule_search_locations=[str(_PKG_DIR)],
    )
    if spec is None:
        pkg = types.ModuleType("connector_fsr_agentic_assistant")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["connector_fsr_agentic_assistant"] = pkg
    else:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["connector_fsr_agentic_assistant"] = mod
        if spec.loader:
            spec.loader.exec_module(mod)


def _ensure_fsr_playbooks() -> None:
    """fsr_playbooks must be importable. Dev: editable install of
    fsr-playbook-framework. On-platform: vendored sibling of operations.py.
    Last resort: FSRPLAYBOOKYAML_REPO env."""
    try:
        import fsr_playbooks  # noqa: F401
        return
    except ImportError:
        pass
    candidates = [
        _PKG_DIR,                                  # vendored sibling layout
        Path(os.environ.get("FSRPLAYBOOKYAML_REPO", "")) if os.environ.get("FSRPLAYBOOKYAML_REPO") else None,
    ]
    for c in candidates:
        if c and (c / "fsr_playbooks" / "__init__.py").exists():
            sys.path.insert(0, str(c))
            import fsr_playbooks  # noqa: F401
            return
    raise SystemExit(
        "fsr_playbooks is not importable. Install fsr-playbook-framework as an editable "
        "package, run from the vendored tarball layout, or set "
        "FSRPLAYBOOKYAML_REPO to the fsr-playbook-framework checkout."
    )


# --------------------------------------------------------------------------
# Tiny test harness.
# --------------------------------------------------------------------------

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Result:
    name: str
    op: str
    status: str
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Ctx:
    config: dict
    live: bool
    results: list[Result] = field(default_factory=list)


class TestSkip(Exception):
    """Raised inside a test body to report SKIP with a reason."""


class TestFail(AssertionError):
    """Raised inside a test body to report FAIL with a reason."""


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise TestFail(msg)


def _op(name: str, config: dict, params: dict):
    from connector_fsr_agentic_assistant.operations import operations
    handler = operations.get(name)
    if handler is None:
        raise TestFail(f"operation {name!r} not in dispatch table")
    return handler(config, params)


# Registry of (id, op-label, requires-live, fn). Populated by @test below.
_TESTS: list[tuple[str, str, bool, object]] = []


def test(tid: str, op: str, *, live: bool = False):
    def deco(fn):
        _TESTS.append((tid, op, live, fn))
        return fn
    return deco


# --------------------------------------------------------------------------
# Fakes (offline-safe tests).
# --------------------------------------------------------------------------

def _starter_yaml_provider():
    """FakeProvider that emits a fenced-yaml text turn + usage."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    def factory(_config, _gateway):
        return FakeProvider([[
            TextEvent(text="Here is a starter playbook:\n```yaml\n"
                           "collection: smoke\nplaybooks: []\n```"),
            UsageEvent(session_id="fake", turn=1, model="fake-1",
                       input_tokens=20, output_tokens=8,
                       cache_read=0, cache_write=0,
                       history_chars=50, stop_reason="end_turn"),
        ]])
    return factory


def _resume_provider():
    """Provider whose resume() emits a final text + usage, proving the
    suspended turn re-enters the loop."""
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent, DoneEvent

    class _P:
        name = "fake-resume"
        approval_gateway = None

        async def stream(self, *, system, messages, tools, tags=None, max_tool_turns=None):
            yield UsageEvent(session_id="x", turn=1, model="fake-1",
                             input_tokens=1, output_tokens=1, cache_read=0,
                             cache_write=0, history_chars=10,
                             stop_reason="end_turn")

        async def resume(self, *, suspended, decision):
            yield TextEvent(text=f"resumed with {decision}")
            yield UsageEvent(session_id=suspended.session_id, turn=1,
                             model="fake-1", input_tokens=5, output_tokens=3,
                             cache_read=0, cache_write=0, history_chars=20,
                             stop_reason="end_turn")
            yield DoneEvent(stop_reason="end_turn")

    return lambda _c, _g: _P()


def _exploding_provider():
    """Provider whose stream raises mid-turn -- stands in for an
    Anthropic 5xx. run_agent_turn must trap it into an ErrorEvent."""
    class _P:
        name = "fake-5xx"
        approval_gateway = None

        async def stream(self, *, system, messages, tools, tags=None, max_tool_turns=None):
            raise RuntimeError("simulated Anthropic 503 mid-stream")
            yield  # pragma: no cover - makes this an async generator

    return lambda _c, _g: _P()


def _stash_suspended(approval_id: str, session_id: str):
    from fsr_playbooks.llm.approvals import SuspendedSession, bind
    from connector_fsr_agentic_assistant.storage import (
        default_storage, PersistedApprovalGateway,
    )
    gw = PersistedApprovalGateway(default_storage())
    s = SuspendedSession(
        approval_id=approval_id, session_id=session_id, tool="run_op",
        tool_use_id=f"tu-{approval_id}", args={}, tier=3,
        history_snapshot=[], prior_tool_result_blocks=[],
        remaining_tool_calls=[], system="sys", tags={},
    )
    # S2 fail-closed: PersistedApprovalGateway.peek() runs approvals.verify()
    # and returns None for any record whose HMAC token is missing or stale, so
    # an unbound session reads back as `approval_not_found`. Production stashes
    # only bound sessions; the fixture must too, or T10/T11 assert against a
    # record the gateway will always refuse to hand back.
    bind(s)
    gw.stash(s)


def _reset_storage_singleton():
    """Simulate a worker restart: drop the in-process Storage so the
    next default_storage() re-opens the same sqlite file from scratch."""
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None


def _with_provider(factory, fn):
    """Run fn() with operations._build_provider swapped for `factory`,
    restoring it afterward."""
    from connector_fsr_agentic_assistant import operations as ops
    # Every module that binds it: each ops_* domain module imports its
    # helpers by name since the operations.py split.
    sites = [ops] + [m for k, m in list(sys.modules.items())
                     if m is not None and k.startswith("connector_fsr_agentic_assistant.ops_")
                     and "_build_provider" in vars(m)]
    saved = [(m, m._build_provider) for m in sites]
    for m in sites:
        m._build_provider = factory
    try:
        return fn()
    finally:
        for m, f in saved:
            m._build_provider = f


# --------------------------------------------------------------------------
# T1..T12
# --------------------------------------------------------------------------

@test("T1", "health_check")
def t1(ctx: Ctx) -> str:
    """Wiring only: the connector imports, pins a real fsr_playbooks and finds
    its reference DB.

    `skip_llm_probe` suppresses health_check's outbound call to the configured
    LLM endpoint. Without it this test egresses, which made the "offline-safe"
    subset neither offline nor deterministic -- it took ~10s and went red
    whenever the endpoint was down, so it could never gate a build. Actual
    reachability is asserted by T1L, which is correctly classified live."""
    out = _op("health_check", ctx.config, {"skip_llm_probe": True})
    _expect(out.get("reference_db_present") is True,
            f"reference_db_present false at {out.get('reference_db_path')}")
    ver = out.get("fsr_playbooks_version")
    _expect(bool(ver), "no fsr_playbooks_version reported")
    return f"fsr_playbooks={ver} db={out.get('reference_db_path')}"


@test("T1L", "health_check", live=True)
def t1l(ctx: Ctx) -> str:
    """The real egress probe T1 deliberately skips."""
    out = _op("health_check", ctx.config, {})
    _expect(out.get("ok") is True, f"ok != true: {out}")
    _expect(out.get("llm_reachable") is True,
            f"llm endpoint unreachable: {out.get('llm_probe')}")
    return f"llm reachable via {out.get('llm_probe')}"


@test("T2", "list_models", live=True)
def t2(ctx: Ctx) -> str:
    out = _op("list_models", ctx.config, {})
    _expect(isinstance(out, list) and out, f"empty/non-list result: {out!r}")
    values = [m.get("value") for m in out]
    # A failure sentinel comes back as a single item with an empty value.
    _expect(any(v for v in values),
            f"all values empty -- likely a key/SDK error: {out[:2]}")
    model = ctx.config.get("model")
    _expect(model in values,
            f"configured model {model!r} not in returned set "
            f"({len(values)} models)")
    return f"{len(out)} models, configured {model!r} present"


@test("T3", "chat_turn", live=True)
def t3(ctx: Ctx) -> str:
    out = _op("chat_turn", ctx.config, {
        "session_id": "selftest-t3",
        "intent": "Reply with one short sentence confirming you are ready. "
                  "Do NOT call any tools.",
    })
    _expect(out.get("ok") is True, f"ok != true: {out.get('error')}")
    _expect(out.get("stop_reason") == "end_turn",
            f"stop_reason {out.get('stop_reason')!r} != end_turn")
    texts = [e for e in out["transcript"] if e.get("type") == "text"]
    _expect(bool(texts), "no text event in transcript")
    return f"end_turn, {len(texts)} text event(s)"


@test("T4", "chat_turn", live=True)
def t4(ctx: Ctx) -> str:
    out = _op("chat_turn", ctx.config, {
        "session_id": "selftest-t4",
        "intent": "Before answering, use your read-only lookup tools to find "
                  "the FortiGate connector and an operation that adds an IP to "
                  "an address group. Call the lookup tool now; do not guess.",
    })
    _expect(out.get("ok") is True, f"ok != true: {out.get('error')}")
    tx = out["transcript"]
    uses = [e for e in tx if e.get("type") == "tool_use"]
    _expect(bool(uses), "no tool_use event -- model did not call a tool")
    use_ids = {e.get("id") for e in uses}
    paired = [e for e in tx if e.get("type") == "tool_result"
              and e.get("tool_use_id") in use_ids]
    _expect(bool(paired),
            "tool_use present but no matching tool_result (unpaired)")
    return (f"{len(uses)} tool_use, {len(paired)} paired tool_result "
            f"({', '.join(sorted({e.get('name') for e in uses}))})")


# The model the box actually serves is a preserved-thinking model; the live
# default (claude-sonnet-4-5) is NOT one, so a live run on the default would miss
# the preserved-thinking class entirely. Pin a prod-like model for T4b.
_PROD_LIKE_ANTHROPIC_MODEL = os.environ.get("FSR_BUILDER_LIVE_MODEL") or "claude-haiku-5-5"


@test("T4B", "chat_turn", live=True)
def t4b(ctx: Ctx) -> str:
    """Two SEQUENTIAL tool rounds on a preserved-thinking model -- the exact shape
    that shipped the live 400 ("thinking blocks in the latest assistant message
    cannot be modified"). A single tool round can't reproduce it (the thinking is
    still on the latest turn); the bug needs a THIRD request that replays an
    earlier turn's thinking. This forces a dependent second round and pins a
    preserved-thinking model so the server-side check actually runs."""
    if ctx.config.get("llm_provider") == "openai" or not ctx.config.get("anthropic_api_key"):
        raise TestSkip("preserved-thinking is Anthropic-specific; run with "
                       "--llm anthropic (Frank/OpenAI cannot reproduce it)")
    cfg = {**ctx.config, "model": _PROD_LIKE_ANTHROPIC_MODEL}
    out = _op("chat_turn", cfg, {
        "session_id": "selftest-t4b",
        "intent": "Work in two separate steps, one tool call at a time (do NOT "
                  "call tools in parallel). Step 1: list the configured "
                  "connectors. Step 2: only AFTER you see that result, look up "
                  "the operation on the FortiGate connector that adds an IP to an "
                  "address group. Then give a one-line summary and stop.",
    })
    # The preserved-thinking 400 surfaces as ok=False / a stop_reason of error.
    _expect(out.get("ok") is True,
            f"ok != true -- the preserved-thinking 400 lands here: {out.get('error')}")
    _expect(out.get("stop_reason") != "error", f"stop=error: {out.get('error')}")
    # Read the frame shape only through the shared reader (harness.frames), never
    # by testing e.get("type") here -- the copies drift (test_frame_reader_closure).
    from fsr_playbooks.harness.frames import frame_types
    # A second round = a tool_use that appears AFTER a tool_result (sequential,
    # not parallel). Without one, the replay path was never exercised.
    seen_result = False
    uses = second_round = 0
    for t in frame_types(out):
        if t == "tool_use":
            uses += 1
            second_round += int(seen_result)
        elif t == "tool_result":
            seen_result = True
    _expect(second_round >= 1,
            f"only one tool round ({uses} tool_use, no sequential second round) "
            "-- the preserved-thinking replay was not exercised; tighten the prompt")
    return (f"model={_PROD_LIKE_ANTHROPIC_MODEL}, {uses} tool_use across "
            f">=2 sequential rounds, stop={out.get('stop_reason')}")


@test("T5", "chat_turn", live=True)
def t5(ctx: Ctx) -> str:
    out = _op("chat_turn", ctx.config, {
        "session_id": "selftest-t5",
        "intent": "Block the malicious IP 1.2.3.4 at the FortiGate edge by "
                  "adding it to the soc-blocklist address group. Present an "
                  "action card for me to confirm before doing anything.",
    })
    _expect(out.get("ok") is True, f"ok != true: {out.get('error')}")
    _expect(out.get("stop_reason") == "awaiting_action_card",
            f"stop_reason {out.get('stop_reason')!r} != awaiting_action_card "
            "(needs Phase 4 steering prompt + emit_action_card path)")
    last = out["transcript"][-1]
    _expect(last.get("type") == "action_card",
            f"last event {last.get('type')!r} != action_card")
    # Stash the card id so a manual T6 run can reference it.
    ctx.config["_t5_card_id"] = last.get("id")
    return f"action_card id={last.get('id')!r} op={last.get('operation')!r}"


@test("T6", "chat_resume", live=True)
def t6(ctx: Ctx) -> str:
    # Model A: resume the action_card by card_id (no messages[] -- the
    # connector reloads the conversation from its store).
    card_id = ctx.config.get("_t5_card_id")
    _expect(card_id is not None,
            "T5 did not produce an action_card to resume (run T5 first)")
    out = _op("chat_resume", ctx.config, {
        "session_id": "selftest-t5",
        "card_id": card_id,
        "decision": "approve",
        "args": {"ip": "1.2.3.4", "group": "soc-blocklist"},
    })
    _expect(out.get("ok") is True, f"resume failed: {out.get('error')}")
    follow = [e for e in out["transcript"] if e.get("type") == "tool_result"]
    _expect(bool(follow), "no follow-up tool_result -- agent did not execute")
    return f"resumed, {len(follow)} tool_result(s), stop={out.get('stop_reason')}"


@test("T7", "chat_resume", live=True)
def t7(ctx: Ctx) -> str:
    card_id = ctx.config.get("_t5_card_id")
    _expect(card_id is not None, "needs a prior action_card (run T5)")
    out = _op("chat_resume", ctx.config, {
        "session_id": "selftest-t5",
        "card_id": card_id,
        "decision": "reject",
        "note": "not now",
    })
    _expect(out.get("ok") is True, f"reject failed: {out.get('error')}")
    return f"rejection acknowledged, stop={out.get('stop_reason')}"


@test("T8", "chat_history")
def t8(ctx: Ctx) -> str:
    # Deterministic + offline: drive two fake turns, then read them back.
    sess = "selftest-t8"
    def _run():
        _op("chat_turn", ctx.config, {"session_id": sess, "intent": "first"})
        _op("chat_turn", ctx.config, {"session_id": sess, "intent": "second"})
        return _op("chat_history", ctx.config, {"session_id": sess})
    out = _with_provider(_starter_yaml_provider(), _run)
    turns = out.get("turns") or []
    # Contract (operations.chat_history): each entry is {user: str} or
    # {transcript: [...wire events...]}, CHRONOLOGICAL, carrying `ts`. There is
    # no per-entry `turn` number and the order is not reversed -- the widget
    # replays these forward to rebuild the timeline. (The old assertion read
    # t["turn"] descending, which predates the transcript rewrite.)
    _expect(len(turns) >= 2, f"expected >=2 entries, got {len(turns)}")
    users = [t["user"] for t in turns if "user" in t]
    _expect(users[:2] == ["first", "second"],
            f"user prompts missing or out of order: {users}")
    ts = [t["ts"] for t in turns if "ts" in t]
    _expect(len(ts) == len(turns), "every entry must carry a ts")
    _expect(ts == sorted(ts), f"entries not chronological: {ts}")
    _expect(any("transcript" in t for t in turns),
            "no assistant transcript entry -- only user prompts were stored")
    return f"{len(turns)} entries chronological, {len(users)} user turns"


@test("T9", "chat_turn(mock)")
def t9(ctx: Ctx) -> str:
    try:
        import connector_fsr_agentic_assistant.mock_replay  # noqa: F401
    except ImportError:
        raise TestSkip("mock-mode replay not implemented yet (PLAN Phase 3)")
    out = _op("chat_turn", ctx.config, {
        "session_id": "selftest-t9",
        "intent": "begin",
        "mode": "mock",
        "mock_scenario": "incident_smtp_intrusion",
    })
    _expect(out.get("ok") is not False, f"mock turn errored: {out.get('error')}")
    _expect("stop_reason" in out, "no stop_reason in mock envelope")
    return f"mock resolved, stop={out.get('stop_reason')}"


@test("T10", "chat_resume")
def t10(ctx: Ctx) -> str:
    # Pause -> simulate worker kill -> resume must still resolve.
    appr = "selftest-t10-appr"
    _stash_suspended(appr, "selftest-t10")
    _reset_storage_singleton()  # the "worker restart"
    out = _with_provider(_resume_provider(), lambda: _op(
        "chat_resume", ctx.config, {"approval_id": appr, "decision": "approve"}))
    _expect(out.get("ok") is True, f"resume after restart failed: {out.get('error')}")
    _expect(out.get("session_id") == "selftest-t10",
            f"wrong session after restart: {out.get('session_id')}")
    _expect(any(e.get("type") == "text" and "resumed with approve" in
                e.get("text", "") for e in out["transcript"]),
            "resumed turn produced no text")
    return "survived worker restart, resumed cleanly"


@test("T11", "chat_resume")
def t11(ctx: Ctx) -> str:
    # Two sessions paused; resume in reverse order; no cross-talk.
    _stash_suspended("appr-A", "sess-A")
    _stash_suspended("appr-B", "sess-B")

    def _resume(appr):
        return _with_provider(_resume_provider(), lambda: _op(
            "chat_resume", ctx.config, {"approval_id": appr, "decision": "approve"}))

    out_b = _resume("appr-B")
    out_a = _resume("appr-A")
    _expect(out_b.get("ok") and out_b.get("session_id") == "sess-B",
            f"B resolved wrong: {out_b.get('session_id')} ok={out_b.get('ok')}")
    _expect(out_a.get("ok") and out_a.get("session_id") == "sess-A",
            f"A resolved wrong: {out_a.get('session_id')} ok={out_a.get('ok')}")
    return "both resolved reverse-order, no cross-talk"


@test("T12", "chat_turn")
def t12(ctx: Ctx) -> str:
    out = _with_provider(_exploding_provider(), lambda: _op(
        "chat_turn", ctx.config, {"session_id": "selftest-t12",
                                  "intent": "trigger a 5xx"}))
    _expect(out.get("ok") is False, "ok should be False on stream error")
    _expect(out.get("stop_reason") == "error",
            f"stop_reason {out.get('stop_reason')!r} != error")
    last = out["transcript"][-1] if out.get("transcript") else {}
    _expect(last.get("type") == "error",
            f"last event {last.get('type')!r} != error")
    return "5xx trapped into structured error event, no crash"


# --------------------------------------------------------------------------
# Runner.
# --------------------------------------------------------------------------

_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


def _resolve_live_config(kind: str, model: str | None, offline: bool):
    """Pick the LLM that drives the live tier, via the SHARED harness resolver.

    The live tests used to require a paid ANTHROPIC_API_KEY. That is the wrong
    default for routine validation: the house rule is to prove quality on the
    free Frank gateway (any OpenAI-compatible endpoint) and keep paid runs rare
    and explicit. `kind` defaults to `frank`, so with FRANK_BASE_URL configured
    the live tier runs for free; `--llm anthropic` still uses a key when set.

    Returns (config, live, skip_reason, model_label). A missing endpoint/key is
    NOT an error -- it just means the live tier skips, with the reason named."""
    placeholder = {"anthropic_api_key": "sk-offline-placeholder",
                   "model": model or os.environ.get("FSR_BUILDER_MODEL")
                   or _DEFAULT_MODEL}
    if offline:
        return placeholder, False, "--offline", placeholder["model"]
    try:
        from fsr_playbooks.harness.llm import LLMConfigError, resolve_llm
    except Exception as e:  # noqa: BLE001
        return placeholder, False, f"resolver unavailable ({e})", placeholder["model"]
    want = model or os.environ.get("FSR_BUILDER_MODEL")
    try:
        resolved = resolve_llm(kind, want, env=os.environ)
    except LLMConfigError as e:
        return placeholder, False, str(e), placeholder["model"]
    return resolved.connector_config(), True, None, resolved.model


def _block_egress() -> None:
    """Refuse every outbound TCP connect for the rest of the process.

    The hermetic subset passes today with the network cut, but several ops
    (health_check's crudhub transport diag, the tool-registry warmup) still
    *opportunistically* poke the platform and swallow the failure. That is
    fine -- until someone makes one of those calls load-bearing, at which
    point the build gate starts depending on a lab appliance and goes red for
    everyone the next time it's down. Blocking the socket here makes that
    regression fail loudly, in the commit that introduces it, instead of
    months later in someone else's build."""
    import socket

    def _refuse(*a, **k):
        raise OSError("EGRESS BLOCKED: --hermetic forbids network access. "
                      "An op under test grew a load-bearing platform call.")

    socket.socket.connect = lambda self, addr: _refuse(addr)
    socket.create_connection = _refuse


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="skip every live test even if a provider is available")
    ap.add_argument("--llm", default="frank",
                    choices=["fake", "frank", "openai", "anthropic"],
                    help="which provider drives the live tier (default: frank, "
                         "the free gateway -- needs FRANK_BASE_URL). `anthropic` "
                         "uses ANTHROPIC_API_KEY; `fake` forces offline.")
    ap.add_argument("-t", "--test", action="append", dest="only", default=[],
                    metavar="Tn", help="run only these test ids (repeatable)")
    ap.add_argument("--strict", action="store_true",
                    help="treat SKIP as failure (non-zero exit)")
    ap.add_argument("--hermetic", action="store_true",
                    help="implies --offline, and hard-blocks all outbound TCP "
                         "so a box dependency fails loudly instead of silently "
                         "creeping in (use this for the build gate)")
    ap.add_argument("--db", default=None,
                    help="sqlite path for the connector store "
                         "(default: a throwaway temp file)")
    args = ap.parse_args(argv)
    if args.hermetic:
        args.offline = True
        _block_egress()
    if args.offline:
        # Offline means no box. The framework's live-client helper reads a box
        # from the developer's .env (FSR_BASE_URL) and would try to log in,
        # which --hermetic then blocks. An empty value here wins over the .env
        # (it only fills unset keys), so offline never looks a box up.
        os.environ["FSR_BASE_URL"] = ""

    # Isolate the store so a self-test run never clobbers a dev/prod DB.
    # When we mint the default throwaway path, wipe it first so each run is
    # deterministic (turn counts, history). A user-supplied --db or a
    # pre-set env var is left untouched.
    if "FSR_PLAYBOOK_BUILDER_DB" not in os.environ:
        import tempfile
        default_db = str(Path(tempfile.gettempdir()) / "fsr_builder_selftest.db")
        db = args.db or default_db
        os.environ["FSR_PLAYBOOK_BUILDER_DB"] = db
        if db == default_db:
            for suffix in ("", "-wal", "-shm"):
                p = Path(db + suffix)
                if p.exists():
                    p.unlink()

    _stub_platform_imports()
    _ensure_fsr_playbooks()
    _load_connector_package()

    # `fake` means "no real provider" -- treat it as offline for the live tier.
    kind = args.llm
    offline = args.offline or kind == "fake"
    config, live, _skip_reason, model_label = _resolve_live_config(
        kind, os.environ.get("FSR_BUILDER_MODEL"), offline)
    ctx = Ctx(config=config, live=live)

    only = {t.upper() for t in args.only}
    selected = [(tid, op, req_live, fn) for (tid, op, req_live, fn) in _TESTS
                if not only or tid in only]

    print(f"connector-fsr-agentic-assistant contract self-test  "
          f"(mode={'LIVE (' + kind + ')' if live else 'OFFLINE'}, "
          f"model={model_label}, db={os.environ['FSR_PLAYBOOK_BUILDER_DB']})")
    print("-" * 78)

    for tid, op, req_live, fn in selected:
        t0 = time.monotonic()
        if req_live and not live:
            # _skip_reason names exactly why the live tier is off -- the missing
            # endpoint/key for the chosen provider, or --offline/fake.
            ctx.results.append(Result(tid, op, SKIP,
                                      f"live test skipped ({_skip_reason})"))
        else:
            try:
                detail = fn(ctx) or ""
                ctx.results.append(Result(tid, op, PASS, detail,
                                          time.monotonic() - t0))
            except TestSkip as s:
                ctx.results.append(Result(tid, op, SKIP, str(s)))
            except TestFail as f:
                ctx.results.append(Result(tid, op, FAIL, str(f),
                                          time.monotonic() - t0))
            except Exception:  # noqa: BLE001 - unexpected; capture trace
                ctx.results.append(Result(
                    tid, op, FAIL,
                    "unexpected: " + traceback.format_exc().strip().splitlines()[-1],
                    time.monotonic() - t0))
        r = ctx.results[-1]
        glyph = {PASS: "✓", FAIL: "✗", SKIP: "-"}[r.status]
        dur = f"{r.seconds*1000:5.0f}ms" if r.seconds else "      "
        print(f"  {glyph} {r.name:<4} {r.op:<18} {dur}  {r.detail}")

    print("-" * 78)
    n_pass = sum(1 for r in ctx.results if r.status == PASS)
    n_fail = sum(1 for r in ctx.results if r.status == FAIL)
    n_skip = sum(1 for r in ctx.results if r.status == SKIP)
    print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped")

    failed = n_fail > 0 or (args.strict and n_skip > 0)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
