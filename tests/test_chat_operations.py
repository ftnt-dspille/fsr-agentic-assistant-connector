"""End-to-end tests for chat_turn / chat_resume / chat_history.

Driven by a FakeProvider injected into operations._build_provider via
monkeypatch so we never hit Anthropic. Verifies the connector's wire
shape (session_id, turn, transcript, last_assistant_yaml), real
persistence to the connector sqlite store, and that chat_history reads
back what chat_turn wrote.
"""
from __future__ import annotations

import pytest
import ops_approvals
import ops_autonomy
import ops_cards
import ops_chat
import ops_health
import ops_manual
import ops_playbooks
import ops_playbooks_persist
import ops_resume
import ops_scheduling
import ops_warmup


def _reference_db_is_slim() -> bool:
    """True when the vendored reference DB has no connector rows.

    `build.sh SLIM=1` (the deploy build) truncates the WARM tables --
    connectors/operations/etc. -- because the on-platform connector
    repopulates them from FortiSOAR at warmup. That artifact is correct for
    deploy but means offline `compile_yaml` can't resolve real connectors, so
    any test that compiles a real-connector trace must self-skip rather than
    fail. Checks the SAME db `compile_yaml` reads (`_shared.DB_PATH`)."""
    import sqlite3
    try:
        from fsr_playbooks.mcp_server._shared import DB_PATH
        con = sqlite3.connect(str(DB_PATH))
        try:
            return con.execute(
                "SELECT COUNT(*) FROM connectors").fetchone()[0] == 0
        finally:
            con.close()
    except Exception:
        return False


requires_full_reference_db = pytest.mark.skipif(
    _reference_db_is_slim(),
    reason="vendored reference DB is slim (build.sh SLIM=1); offline "
           "connector compile needs the full DB -- re-vendor "
           "store/fsr_reference.db to run this test",
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the connector's default_storage at a fresh tmp sqlite so
    tests don't share the developer's ~/.connector_fsr_agentic_assistant.db."""
    db = tmp_path / "test.db"
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(db))
    # Force re-instantiation of the lazy singleton.
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield db
    storage._DEFAULT_STORAGE = None


@pytest.fixture
def fake_provider(monkeypatch):
    """Replace _build_provider with one that returns a scripted
    FakeProvider so tests don't need real LLM access."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    def _usage(session="sess-test"):
        return UsageEvent(
            session_id=session, turn=1, model="fake-1",
            input_tokens=20, output_tokens=8,
            cache_read=0, cache_write=0,
            history_chars=50, stop_reason="end_turn",
        )

    def _factory(_config, _gateway):
        return FakeProvider([[
            TextEvent(text="Here's a starter playbook:\n```yaml\n"
                          "collection: smoke\nplaybooks:\n"
                          "  - name: Smoke\n    steps:\n"
                          "      - {name: Start, type: start, next: Note}\n"
                          "      - {name: Note, type: set_variable, "
                          "vars: {note: ok}}\n```"),
            _usage(),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    return _factory


# --- usage firehose (live token profile) ----------------------------------

def test_chat_turn_writes_usage_jsonl_firehose(
        config, tmp_db, fake_provider, tmp_path):
    """Each UsageEvent appends one JSONL line to the connector-config usage-log
    path in the Studio profiler's record shape, so the live connector's token
    profile is visible to the same tooling that reads the Studio firehose."""
    import json
    log = tmp_path / "usage.jsonl"
    cfg = {**config, "usage_log_path": str(log)}
    out = _sync_turn(cfg, {"session_id": "sess-usage", "intent": "build me one"})
    assert out["ok"] is True
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["model"] == "fake-1"
    assert rec["input_tokens"] == 20 and rec["output_tokens"] == 8
    assert rec["history_est_tokens"] == 50 // 4  # est_tokens(history_chars)
    assert "ts" in rec


def test_usage_log_path_prefers_config_then_storage_default(tmp_db):
    """The path comes from connector configuration (the in-platform mechanism;
    connectors have no env). Absent a config field, it co-locates beside the
    connector storage DB -- a connector-writable path on the SOAR host."""
    from connector_fsr_agentic_assistant.operations import _usage_log_path
    from connector_fsr_agentic_assistant.storage import _default_db_path
    # Config field wins.
    assert _usage_log_path({"usage_log_path": "/tmp/x.jsonl"}) == "/tmp/x.jsonl"
    # Default co-locates beside the storage DB.
    assert _usage_log_path({}) == str(
        _default_db_path().with_name(".connector_fsr_agentic_assistant_usage.jsonl"))
    assert _usage_log_path(None) == _usage_log_path({})


# --- chat_turn ------------------------------------------------------------

def test_chat_turn_returns_transcript(config, tmp_db, fake_provider):
    out = _sync_turn(config, {"session_id": "sess-1", "intent": "build me one"})
    assert out["ok"] is True
    assert out["session_id"] == "sess-1"
    assert out["turn"] == 1
    # The fake provider narrates a ```yaml fence without calling
    # emit_playbook_offer, so Phase 2.4's `_salvage_build_offer` backstop appends
    # the offer card and promotes end_turn -> awaiting_playbook_offer. That is
    # the intended build-completion path (see test_salvage_build_offer.py).
    assert out["stop_reason"] == "awaiting_playbook_offer"
    assert isinstance(out["transcript"], list) and out["transcript"]
    # Last assistant yaml was extracted from the fenced block.
    assert out["last_assistant_yaml"] is not None
    assert "collection: smoke" in out["last_assistant_yaml"]


def test_chat_turn_writes_streaming_progress(config, tmp_db, fake_provider):
    """A blocking chat_turn doubles as a streaming producer (contract
    2.5.0): it writes per-event progress frames + a terminal stream_end
    that chat_poll reads back by cursor."""
    from connector_fsr_agentic_assistant.operations import chat_poll
    _sync_turn(config, {"session_id": "sess-stream", "intent": "build me one"})

    poll = chat_poll(config, {"session_id": "sess-stream", "cursor": 0})
    assert poll["ok"] is True
    assert poll["turn"] == 1
    types = [f["type"] for f in poll["frames"]]
    # turn_start first, the assistant text, the per-round usage frame.
    assert types[0] == "turn_start"
    assert "text" in types
    assert "usage" in types
    # The feed is closed and reports the envelope stop_reason (promoted by the
    # Phase 2.4 build-offer salvage -- see test_chat_turn_returns_transcript).
    assert poll["done"] is True
    assert poll["stop_reason"] == "awaiting_playbook_offer"
    # A blocking turn's caller holds the transcript from the return, so the
    # terminal frame closes the feed signal-only (no second copy).
    end = next(f for f in poll["frames"] if f["type"] == "stream_end")
    assert "transcript" not in end
    assert end["stop_reason"] == "awaiting_playbook_offer" and end["ok"] is True

    # Re-poll from the watermark: nothing new, still done.
    again = chat_poll(config, {"session_id": "sess-stream", "cursor": poll["cursor"]})
    assert again["frames"] == []
    assert again["done"] is True


def _join_detached_threads(timeout=5.0):
    """Wait for chat_turn detached worker threads to finish (they're named
    `chat_turn:<session>:<turn>`)."""
    import threading
    for t in list(threading.enumerate()):
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(timeout)


def _sync_turn(config, params):
    """Test helper: chat_turn is always detached (contract 2.7.0) -- it returns
    an immediate accepted-ack and runs the turn on a daemon thread. This drives
    a turn to completion and reconstructs the envelope the pre-2.7.0 blocking
    return produced, from the (now enriched) terminal stream_end frame, so the
    blocking-style assertions in this suite keep working unchanged."""
    from connector_fsr_agentic_assistant.operations import chat_turn, chat_poll
    ack = chat_turn(config, params)
    if not ack.get("accepted"):
        return ack  # not a detached turn (shouldn't happen for chat_turn)
    _join_detached_threads()
    poll = chat_poll(config, {"session_id": ack["session_id"], "cursor": 0})
    end = next((f for f in poll.get("frames", []) if f.get("type") == "stream_end"), None)
    if end is None:
        return ack
    return {
        "ok": end.get("ok", True),
        "session_id": ack["session_id"],
        "turn": ack["turn"],
        "turn_id": end.get("turn_id", ack.get("turn_id")),
        "stop_reason": end.get("stop_reason"),
        "contract_version": end.get("contract_version"),
        "transcript": end.get("transcript", []),
        "last_assistant_yaml": end.get("last_assistant_yaml"),
        "tags": end.get("tags"),
        "error": end.get("error"),
    }


def test_chat_turn_detached_returns_accepted_and_streams(
        config, tmp_db, fake_provider):
    """Detached mode (contract 2.7.0): chat_turn returns an `accepted` ack
    immediately (no transcript), then the daemon thread finishes the run and
    writes the authoritative terminal stream_end frame to the poll feed."""
    from connector_fsr_agentic_assistant.operations import chat_turn, chat_poll
    out = chat_turn(config, {"session_id": "sess-detached",
                             "intent": "build me one", "detached": True})
    # The ack carries no transcript -- the widget must drive off chat_poll.
    assert out["accepted"] is True
    assert out["stop_reason"] == "accepted"
    assert out["turn"] == 1
    assert "turn_id" in out
    assert "transcript" not in out
    assert out["contract_version"] == "2.8.0"

    _join_detached_threads()

    poll = chat_poll(config, {"session_id": "sess-detached", "cursor": 0})
    assert poll["done"] is True
    # Promoted by the Phase 2.4 build-offer salvage, as in the sync path above.
    assert poll["stop_reason"] == "awaiting_playbook_offer"
    end = next(f for f in poll["frames"] if f["type"] == "stream_end")
    # The terminal frame mirrors the full authoritative envelope so the widget
    # can render without the (now absent) blocking return.
    assert isinstance(end["transcript"], list) and end["transcript"]
    assert end["turn_id"] == out["turn_id"]
    assert end["contract_version"] == "2.8.0"


def test_chat_turn_detached_error_writes_terminal_frame(
        config, tmp_db, monkeypatch):
    """A detached run that raises must still write a terminal stream_end
    (stop_reason=error) so chat_poll never hangs."""
    from connector_fsr_agentic_assistant import operations as ops

    class _BoomProvider:
        async def stream(self, *a, **k):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: _BoomProvider())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: _BoomProvider())
    out = ops.chat_turn(config, {"session_id": "sess-detached-boom",
                                 "intent": "x", "detached": True})
    assert out["accepted"] is True
    _join_detached_threads()

    poll = ops.chat_poll(config, {"session_id": "sess-detached-boom", "cursor": 0})
    assert poll["done"] is True
    assert poll["stop_reason"] == "error"


def test_chat_poll_lost_producer_synthesizes_terminal(config, tmp_db, monkeypatch):
    """A detached run whose worker is recycled never writes its terminal frame.
    chat_poll must declare the turn lost after the stale threshold and write a
    durable terminal error so the widget stops polling (contract 2.7.0)."""
    from connector_fsr_agentic_assistant import operations as ops
    from connector_fsr_agentic_assistant.storage import default_storage

    # Simulate a detached producer that wrote turn_start + a frame, then died:
    # no terminal frame is ever appended.
    st = default_storage()
    st.append_turn_progress("lost-sess", 1, {"type": "turn_start", "turn": 1})
    st.append_turn_progress("lost-sess", 1, {"type": "text", "text": "working"})

    # Fresh poll: not stale yet (frames just written) → still in flight.
    live = ops.chat_poll(config, {"session_id": "lost-sess", "cursor": 0})
    assert live["done"] is False

    # Advance the clock past the stale threshold.
    import time
    base = time.time()
    monkeypatch.setattr("time.time", lambda: base + ops._STREAM_STALE_SECS + 1)

    lost = ops.chat_poll(config, {"session_id": "lost-sess", "cursor": 0})
    assert lost["done"] is True
    assert lost["stop_reason"] == "error"
    end = next(f for f in lost["frames"] if f["type"] == "stream_end")
    assert "lost contact" in (end.get("error") or "")
    # Durable: a later poll (clock back to normal) still reports done.
    monkeypatch.undo()
    again = ops.chat_poll(config, {"session_id": "lost-sess", "cursor": 0})
    assert again["done"] is True
    assert again["stop_reason"] == "error"


def test_chat_poll_empty_session(config, tmp_db):
    from connector_fsr_agentic_assistant.operations import chat_poll
    out = chat_poll(config, {"session_id": "never-seen", "cursor": 0})
    assert out["ok"] is True
    assert out["turn"] is None
    assert out["frames"] == []
    assert out["done"] is False


def test_chat_turn_persists_to_history(config, tmp_db, fake_provider):
    # chat_history is now a render-fidelity transcript (refresh durability):
    # ordered {user}/{transcript} entries the widget replays verbatim, NOT the
    # old per-turn metadata rows.
    from connector_fsr_agentic_assistant.operations import chat_history
    _sync_turn(config, {"session_id": "sess-2", "intent": "compose"})
    history = chat_history(config, {"session_id": "sess-2"})
    turns = history["turns"]
    # One exchange ⇒ a user entry followed by an assistant transcript entry.
    # Entries also carry a `ts` send time (asserted in test_storage.py) -- this
    # test is about persistence, so it pins the content, not the timestamp.
    assert turns[0]["user"] == "compose"
    assert "transcript" in turns[1]
    # The assistant transcript is the full wire-event array the widget renders.
    assert isinstance(turns[1]["transcript"], list)
    assert any(ev.get("type") == "text" for ev in turns[1]["transcript"])


def test_chat_turn_increments_turn_number(config, tmp_db, fake_provider):
    out1 = _sync_turn(config, {"session_id": "sess-3", "intent": "first"})
    out2 = _sync_turn(config, {"session_id": "sess-3", "intent": "second"})
    assert out1["turn"] == 1
    assert out2["turn"] == 2


def test_chat_turn_isolates_sessions(config, tmp_db, fake_provider):
    from connector_fsr_agentic_assistant.operations import chat_history
    _sync_turn(config, {"session_id": "alpha", "intent": "x"})
    _sync_turn(config, {"session_id": "beta", "intent": "y"})
    alpha = chat_history(config, {"session_id": "alpha"})["turns"]
    beta = chat_history(config, {"session_id": "beta"})["turns"]
    # Each session holds exactly its own exchange (user + assistant), with no
    # bleed-through of the other session's user text.
    assert alpha[0]["user"] == "x"
    assert beta[0]["user"] == "y"
    assert all(t.get("user") != "y" for t in alpha)
    assert all(t.get("user") != "x" for t in beta)


def test_chat_turn_requires_api_key(tmp_db, fake_provider):
    with pytest.raises(Exception) as exc:
        _sync_turn({}, {"session_id": "s1", "intent": "x"})
    assert "anthropic_api_key" in str(exc.value)


def test_chat_turn_carries_prior_messages(config, tmp_db, fake_provider):
    out = _sync_turn(config, {
        "session_id": "sess-prior",
        "intent": "continue",
        "messages": [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "first reply"},
        ],
    })
    assert out["ok"] is True
    # Transcript built off prior + intent.


# --- entity / record context (contract §2) --------------------------------

_ENTITY = {
    "module": "incidents",
    "uuid": "a0668705",
    "iri": "/api/3/incidents/a0668705",
    "fields": {
        "name": "Detected intrusion 192.168.77.30",
        "severity": "Medium", "status": "Open", "phase": "Detection",
        "sourceIP": "192.168.77.30", "mitreattackid": "T1041",
    },
    "tags": ["Collection", "Suspicious IP"],
    "summary_seed": "Medium intrusion incident.",
}


def test_entity_context_block_renders_fields_tags_and_keys():
    from connector_fsr_agentic_assistant.operations import _entity_context_block
    block = _entity_context_block(_ENTITY)
    # Lead fields ordered first, then the rest; tags + summary + keys.
    assert "RECORD CONTEXT (incidents/a0668705)" in block
    assert "name: Detected intrusion 192.168.77.30" in block
    assert "severity: Medium" in block
    assert "sourceIP: 192.168.77.30" in block
    assert "tags: Collection, Suspicious IP" in block
    assert "Medium intrusion incident." in block
    assert "iri=/api/3/incidents/a0668705" in block


def test_entity_context_block_none_when_empty():
    from connector_fsr_agentic_assistant.operations import _entity_context_block
    assert _entity_context_block(None) is None
    assert _entity_context_block({}) is None
    assert _entity_context_block("not a dict") is None


# --- triage pre-flight (dynamic, record-grounded system prompt) -----------

def test_triage_lookup_key_prefers_iri_then_module_uuid():
    from connector_fsr_agentic_assistant.operations import _triage_lookup_key
    assert _triage_lookup_key({"iri": "/api/3/alerts/x"}) == "/api/3/alerts/x"
    assert _triage_lookup_key({"module": "alerts", "uuid": "x"}) == "alerts:x"
    assert _triage_lookup_key({"module": "alerts", "id": "y"}) == "alerts:y"
    assert _triage_lookup_key({}) is None
    assert _triage_lookup_key(None) is None


class _FakeProgressStore:
    def __init__(self):
        self.frames = []

    def append_turn_progress(self, session_id, turn_idx, frame):
        self.frames.append(frame)


def test_resolve_system_prompt_build_is_static():
    from connector_fsr_agentic_assistant import operations as ops
    store = _FakeProgressStore()
    out, _ = ops._resolve_system_prompt("build", _ENTITY, store, "s", 1)
    assert out == ops._load_prompt("build")
    assert store.frames == []  # build never pre-flights


def test_resolve_system_prompt_triage_grounds_and_emits_activity(monkeypatch):
    """Triage pre-flight folds the record's scenario into the system prompt
    and streams one `activity` frame per phase."""
    import json
    import pathlib
    import fsr_soc_triage
    from connector_fsr_agentic_assistant import operations as ops

    fix = (pathlib.Path(fsr_soc_triage.__file__).resolve().parent
           / "tests" / "fixtures" / "triage" / "alert_c2_exfil.json")
    raw = json.loads(fix.read_text())
    # Avoid a live FSR fetch -- feed the captured C2 alert straight in.
    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.fetch_raw_record", lambda _t: raw)

    store = _FakeProgressStore()
    entity = {"iri": "/api/3/alerts/d39ecc9a"}
    out, _ = ops._resolve_system_prompt("triage", entity, store, "s", 1)

    # Grounded: the scenario block + the exfil smoking-gun reach the prompt.
    assert "Likely scenario" in out
    assert "smithDesktop" in out
    # Activity trail streamed for transparency.
    phases = [f["phase"] for f in store.frames if f["type"] == "activity"]
    assert "normalize" in phases and "classify" in phases and "ground" in phases


def test_resolve_system_prompt_triage_low_signal_gates(monkeypatch):
    """A trivial user message ("test") appends the low-signal gate so the
    agent orients on the case instead of launching a full auto-investigation."""
    import json
    import pathlib
    import fsr_soc_triage
    from connector_fsr_agentic_assistant import operations as ops

    fix = (pathlib.Path(fsr_soc_triage.__file__).resolve().parent
           / "tests" / "fixtures" / "triage" / "alert_c2_exfil.json")
    raw = json.loads(fix.read_text())
    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.fetch_raw_record", lambda _t: raw)

    store = _FakeProgressStore()
    entity = {"iri": "/api/3/alerts/d39ecc9a"}
    out, _ = ops._resolve_system_prompt(
        "triage", entity, store, "s", 1, user_message="test")
    assert "Low-signal input" in out
    # A real directive leaves the prompt ungated.
    clean, _ = ops._resolve_system_prompt(
        "triage", entity, store, "s", 1, user_message="build the timeline")
    assert "Low-signal input" not in clean


def test_resolve_system_prompt_triage_no_entity_falls_back():
    from connector_fsr_agentic_assistant import operations as ops
    store = _FakeProgressStore()
    out, _ = ops._resolve_system_prompt("triage", None, store, "s", 1)
    assert out == ops._load_prompt("triage")
    assert store.frames == []


def test_chat_turn_injects_entity_into_first_user_message(
        config, tmp_db, fake_provider, monkeypatch):
    """The record context must reach the agent loop folded into the first
    user message (NOT the system prompt -- keeps the cached prefix stable)."""
    captured = {}

    # run_agent_turn is imported inside chat_turn from its source module.
    import fsr_playbooks.llm.run_turn as rt
    orig = rt.run_agent_turn

    async def _spy(provider=None, system=None, messages=None, **kw):
        captured["system"] = system
        captured["messages"] = list(messages or [])
        return await orig(
            provider=provider, system=system, messages=messages, **kw)

    monkeypatch.setattr(rt, "run_agent_turn", _spy)

    _sync_turn(config, {
        "session_id": "ent-1", "intent": "triage",
        "messages": [{"role": "user", "content": "attack timeline?"}],
        "entity": _ENTITY,
    })

    first_user = next(m for m in captured["messages"] if m.role == "user")
    assert "RECORD CONTEXT (incidents/a0668705)" in first_user.content
    assert "attack timeline?" in first_user.content
    # System prompt stays free of the per-record rendered block
    # (cache-friendly). The triage prompt's prose mentions "RECORD
    # CONTEXT", so assert on record-specific content instead.
    assert "incidents/a0668705" not in (captured["system"] or "")
    assert "sourceIP: 192.168.77.30" not in (captured["system"] or "")


def test_the_turn_binds_the_tools_it_offers_and_find_names_the_record_tools(
        config, tmp_db, fake_provider, monkeypatch):
    """find's record actions name update_record only when the host offered it
    this turn. Live on .159 the alerts persona lists no tools (= the default
    slice, which has the record writes), and find read that empty allowlist
    as "no tool", sending the close through cyops_utilities."""
    captured = {}
    import fsr_playbooks.llm.run_turn as rt
    from fsr_playbooks.llm import session_state
    from fsr_playbooks.mcp_server import tools_connector_discovery as tcd
    orig = rt.run_agent_turn

    async def _spy(provider=None, system=None, messages=None, **kw):
        captured["offered"] = session_state.current().offered_tools
        captured["names"] = {t.get("name") for t in kw.get("tools") or []}
        captured["record_tools"] = tcd._persona_record_tools("incidents")
        return await orig(
            provider=provider, system=system, messages=messages, **kw)

    monkeypatch.setattr(rt, "run_agent_turn", _spy)
    _sync_turn(config, {
        "session_id": "offered-1", "intent": "triage",
        "messages": [{"role": "user", "content": "close this as a false positive"}],
        "entity": _ENTITY,
    })
    assert captured["offered"] == frozenset(captured["names"])
    assert {"update_record", "create_record"} <= captured["names"]
    # No persona: the host's write profile is scoped to the mounted module.
    assert captured["record_tools"] == {"update_record", "create_record"}
    assert session_state.current().offered_tools is None   # unbound after the turn


@pytest.mark.parametrize("actor, unattended", [
    ({"name": "Auto-triage", "iri": "system:auto-triage", "system": True}, True),
    ({"name": "analyst", "iri": "/api/3/people/x"}, False),
    (None, False),
])
def test_only_a_system_actor_marks_the_turn_unattended(
        config, tmp_db, fake_provider, monkeypatch, actor, unattended):
    """The unattended tag is what lets a true_positive that stages nothing be
    asked for its containment; an analyst's chat never carries it."""
    captured = {}
    import fsr_playbooks.llm.run_turn as rt
    orig = rt.run_agent_turn

    async def _spy(provider=None, system=None, messages=None, **kw):
        captured["tags"] = dict(kw.get("tags") or {})
        return await orig(
            provider=provider, system=system, messages=messages, **kw)

    monkeypatch.setattr(rt, "run_agent_turn", _spy)
    params = {"session_id": f"un-{unattended}-{bool(actor)}", "intent": "triage",
              "messages": [{"role": "user", "content": "triage this"}],
              "entity": _ENTITY}
    if actor:
        params["actor"] = actor
    _sync_turn(config, params)
    assert bool(captured["tags"].get("unattended")) is unattended


@pytest.mark.parametrize("module, persona, verdicts", [
    ("ztpf_devices", True, False),   # a module persona answering questions
    ("incidents", True, None),       # a persona on an investigation page
    ("ztpf_devices", False, None),   # the built-in assistant
])
def test_a_module_persona_turn_is_not_forced_into_a_verdict(
        config, tmp_db, fake_provider, monkeypatch, module, persona, verdicts):
    """Live (a ZTPF device): "what was the latest automation on this device"
    was forced into a needs_more_info verdict card restating the answer."""
    import connector_fsr_agentic_assistant.ops_chat as oc
    from connector_fsr_agentic_assistant.ops_personas import ResolvedPersona

    class _Prof:
        tools_allow = ["get_record", "search_module_records"]
        prompt = "You help with ZTPF devices."
        capabilities = None

    monkeypatch.setattr(oc, "_resolve_turn_persona", lambda intent, mod, sid:
                        ResolvedPersona(intent="triage", profile=_Prof() if persona else None))
    captured = {}
    import fsr_playbooks.llm.run_turn as rt
    orig = rt.run_agent_turn

    async def _spy(provider=None, system=None, messages=None, **kw):
        captured["tags"] = dict(kw.get("tags") or {})
        return await orig(provider=provider, system=system, messages=messages, **kw)

    monkeypatch.setattr(rt, "run_agent_turn", _spy)
    _sync_turn(config, {"session_id": f"vd-{module}-{persona}", "intent": "triage",
                        "messages": [{"role": "user", "content": "latest automation?"}],
                        "entity": dict(_ENTITY, module=module,
                                       iri=f"/api/3/{module}/a0668705")})
    assert captured["tags"].get("verdicts") is verdicts


# --- tool-result duration passthrough -------------------------------------

def test_event_to_wire_includes_duration_ms():
    """A measured ToolResultEvent surfaces `duration_ms` on the wire frame
    so the widget can freeze a per-tool duration; an unmeasured one (e.g.
    approval-resume) omits the field so the widget keeps its ticker estimate."""
    from fsr_playbooks.llm.provider import ToolResultEvent
    from connector_fsr_agentic_assistant import operations as ops

    measured = ops._event_to_wire(
        ToolResultEvent(call_id="tu-1", result={"ok": True}, duration_ms=4711))
    assert measured["type"] == "tool_result"
    assert measured["tool_use_id"] == "tu-1"
    assert measured["duration_ms"] == 4711

    unmeasured = ops._event_to_wire(
        ToolResultEvent(call_id="tu-2", result={"ok": True}))
    assert "duration_ms" not in unmeasured


# --- card emitters --------------------------------------------------------

def test_emit_choice_card_appears_as_last_event(config, tmp_db, monkeypatch):
    """When the agent calls emit_choice_card, the wire transcript ends
    with a synthesized choice_card event and stop_reason flips to
    `awaiting_choice` (per the widget contract)."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import (TextEvent, ToolUseEvent,
                                       ToolResultEvent, UsageEvent)

    card_args = {
        "id": "intent",
        "prompt": "Pick approach:",
        "options": [{"label": "Take immediate action", "value": "immediate"},
                    {"label": "Build a playbook", "value": "playbook"}],
    }

    def _factory(_config, _gateway):
        return FakeProvider([[
            TextEvent(text="Thinking..."),
            ToolUseEvent(call_id="tu-emit-1", name="emit_choice_card",
                         arguments=card_args, tier=0),
            ToolResultEvent(call_id="tu-emit-1",
                            result={"ok": True, "card": {**card_args,
                                                         "type": "choice_card"}}),
            TextEvent(text="(this trailing text must be truncated)"),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=10, output_tokens=2,
                       cache_read=0, cache_write=0,
                       history_chars=30, stop_reason="tool_use"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    out = _sync_turn(config, {"session_id": "card-1", "intent": "begin"})
    assert out["stop_reason"] == "awaiting_choice"
    last = out["transcript"][-1]
    assert last["type"] == "choice_card"
    assert last["id"] == "intent"
    assert [o["value"] for o in last["options"]] == ["immediate", "playbook"]
    # Trailing text was dropped.
    assert not any(e.get("type") == "text"
                   and "trailing text must be truncated" in e.get("text", "")
                   for e in out["transcript"])


def test_emit_action_card_flips_stop_reason(config, tmp_db, monkeypatch):
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import (ToolUseEvent, ToolResultEvent,
                                       UsageEvent)

    card_args = {
        "id": "card-block-ip-1",
        "connector": "fortinet-fortigate",
        "operation": "block_ip",
        "summary": "FortiGate: block 1.2.3.4",
        "args": {"ip": "1.2.3.4", "policy": "block-list"},
        "editable_fields": ["ip", "policy"],
    }

    def _factory(_config, _gateway):
        return FakeProvider([[
            ToolUseEvent(call_id="tu-a-1", name="emit_action_card",
                         arguments=card_args, tier=0),
            ToolResultEvent(call_id="tu-a-1",
                            result={"ok": True, "card": {**card_args,
                                                         "type": "action_card"}}),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=8, output_tokens=2,
                       cache_read=0, cache_write=0,
                       history_chars=20, stop_reason="tool_use"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    out = _sync_turn(config, {"session_id": "card-2", "intent": "block"})
    assert out["stop_reason"] == "awaiting_action_card"
    assert out["transcript"][-1]["type"] == "action_card"
    assert out["transcript"][-1]["connector"] == "fortinet-fortigate"


def test_failed_emit_action_card_does_not_render_card(config, tmp_db, monkeypatch):
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import (ToolUseEvent, ToolResultEvent,
                                       UsageEvent)

    card_args = {
        "id": "bad-card",
        "connector": "fortigate-firewall",
        "operation": "block_ip_new",
        "summary": "Block 1.1.1.1",
        "args": {"method": "Quarantine Based",
                 "ip_addresses": "1.1.1.1",
                 "time_to_live": "12 Hour"},
        "editable_fields": ["ip_addresses", "time_to_live"],
    }

    def _factory(_config, _gateway):
        return FakeProvider([[
            ToolUseEvent(call_id="tu-bad-card", name="emit_action_card",
                         arguments=card_args, tier=0),
            ToolResultEvent(call_id="tu-bad-card",
                            result={"ok": False, "code": "bad_params",
                                    "message": "invalid args"}),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=8, output_tokens=2,
                       cache_read=0, cache_write=0,
                       history_chars=20, stop_reason="tool_use"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    out = _sync_turn(config, {"session_id": "card-failed", "intent": "block"})
    assert out["stop_reason"] != "awaiting_action_card"
    assert not any(e.get("type") == "action_card" for e in out["transcript"])
    assert out["transcript"][-1]["type"] == "usage"


def test_action_card_enrichment_adds_conditional_param_metadata(
        config, tmp_db, tmp_path, monkeypatch):
    import sqlite3
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import (ToolUseEvent, ToolResultEvent,
                                       UsageEvent)

    db = tmp_path / "ref.db"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE operation_params (
            connector_name TEXT, op_name TEXT, parent_param_name TEXT,
            condition_value TEXT, param_name TEXT, title TEXT, type TEXT,
            required INTEGER DEFAULT 0, default_value TEXT, options_json TEXT,
            tooltip TEXT, placeholder TEXT, description TEXT,
            visible INTEGER DEFAULT 1, editable INTEGER DEFAULT 1,
            ord INTEGER DEFAULT 0, observed_type TEXT, coerces_from TEXT
        )"""
    )
    con.executemany(
        """INSERT INTO operation_params
           (connector_name, op_name, parent_param_name, condition_value,
            param_name, title, type, required, default_value, options_json,
            description, visible, editable, ord)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("fortigate-firewall", "block_ip_new", "", "", "method",
             "Block Method", "select", 1, '"Quarantine Based"',
             '["Quarantine Based","Policy Based"]', "Block method", 1, 1, 0),
            ("fortigate-firewall", "block_ip_new", "method",
             "Quarantine Based", "ip_addresses", "IP Addresses", "text",
             1, "", "", "IPs to block", 1, 1, 0),
            ("fortigate-firewall", "block_ip_new", "method",
             "Quarantine Based", "time_to_live", "Time to Live", "select",
             1, '"12 Hour"', '["12 Hour","Custom Time"]', "", 1, 1, 1),
            ("fortigate-firewall", "block_ip_new", "time_to_live",
             "Custom Time", "duration", "Duration", "integer",
             1, "21600", "", "", 1, 1, 0),
            ("fortigate-firewall", "block_ip_new", "method",
             "Policy Based", "ip_type", "IP Type", "select",
             1, '"IPv4"', '["IPv4","IPv6"]', "", 1, 1, 0),
            ("fortigate-firewall", "block_ip_new", "ip_type",
             "IPv4", "ip", "IP Address", "text", 1, "", "", "", 1, 1, 0),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(ops_approvals, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ops_autonomy, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ops_cards, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ops_chat, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ops_health, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ops_playbooks, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ops_playbooks_persist, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ops_scheduling, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ops_warmup, "_reference_db_path", lambda: db)

    card_args = {
        "id": "card-block-ip-new",
        "connector": "fortigate-firewall",
        "operation": "block_ip_new",
        "summary": "Block 1.2.3.4",
        "args": {"method": "Quarantine Based",
                 "ip_addresses": "[\"1.2.3.4\"]"},
        "editable_fields": ["method", "ip_addresses"],
    }

    def _factory(_config, _gateway):
        return FakeProvider([[
            ToolUseEvent(call_id="tu-a-2", name="emit_action_card",
                         arguments=card_args, tier=0),
            ToolResultEvent(call_id="tu-a-2",
                            result={"ok": True, "card": {**card_args,
                                                         "type": "action_card"}}),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=8, output_tokens=2,
                       cache_read=0, cache_write=0,
                       history_chars=20, stop_reason="tool_use"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    out = _sync_turn(config, {"session_id": "card-schema", "intent": "block"})
    card = out["transcript"][-1]

    assert card["type"] == "action_card"
    assert card["visibility"]["always"] == ["method"]
    assert card["visibility"]["when"]["method=Quarantine Based"] == [
        "ip_addresses", "time_to_live"]
    assert card["visibility"]["when"]["time_to_live=Custom Time"] == ["duration"]
    method = next(p for p in card["param_schema"] if p["name"] == "method")
    assert method["type"] == "select"
    assert method["options"] == ["Quarantine Based", "Policy Based"]
    assert card["param_groups_by_select"]["method"]["Quarantine Based"][
        "nested_selects"]["time_to_live"]["Custom Time"] == ["duration"]


# --- card resume (Model A: re-enter the loop, no SuspendedSession) --------

def _action_card_factory(monkeypatch):
    """Factory that emits an action_card on chat_turn (tier-0, no gate)."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import (ToolUseEvent, ToolResultEvent,
                                       UsageEvent)

    card_args = {
        "id": "card-block-ip-1",
        "connector": "fortinet-fortigate",
        "operation": "block_ip",
        "summary": "FortiGate: block 1.2.3.4",
        "args": {"ip": "1.2.3.4", "policy": "block-list"},
        "editable_fields": ["ip", "policy"],
    }

    def _factory(_c, _g):
        return FakeProvider([[
            ToolUseEvent(call_id="tu-card", name="emit_action_card",
                         arguments=card_args, tier=0),
            ToolResultEvent(call_id="tu-card",
                            result={"ok": True, "card": {**card_args,
                                                         "type": "action_card"}}),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=8, output_tokens=2, cache_read=0,
                       cache_write=0, history_chars=20, stop_reason="tool_use"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    return card_args


def test_card_resume_approve_executes(config, tmp_db, monkeypatch):
    """emit_action_card ends the turn; chat_resume(card_id, approve) makes the
    CONNECTOR execute the operation directly (the triage prompt forbids the
    model from running mutating ops), splicing the real exec tool_result
    (contract T6) and then RE-ENTERING the agent loop once so the turn doesn't
    dead-end (2.5.1). The continuation's stop_reason flows through -- here the
    factory re-emits a card, so the approve response is awaiting_action_card."""
    from connector_fsr_agentic_assistant.operations import chat_resume
    import fsr_playbooks.llm.tools as fsr_tools

    _action_card_factory(monkeypatch)
    out = _sync_turn(config, {"session_id": "card-sess", "intent": "block 1.2.3.4"})
    assert out["stop_reason"] == "awaiting_action_card"
    card_id = out["transcript"][-1]["id"]

    # Stub the execution primitive -- the connector must call run_op directly
    # to fulfil the human approval (deterministic, not via the model).
    captured = {}

    def _fake_dispatch(name, arguments, **kwargs):
        captured["name"] = name
        captured["args"] = dict(arguments)
        return {"ok": True, "data": {"status": "blocked", "ip": "1.2.3.4"}}

    monkeypatch.setattr(fsr_tools, "dispatch", _fake_dispatch)

    res = chat_resume(config, {
        "session_id": "card-sess", "card_id": card_id, "decision": "approve",
        "args": {"ip": "1.2.3.4", "policy": "block-list"},
    })
    assert res["ok"] is True
    assert res["session_id"] == "card-sess"
    # Success continuation: the loop was re-entered (factory re-emits a card),
    # so the approve response carries the continuation's stop_reason -- NOT a
    # hard-coded end_turn.
    assert res["stop_reason"] == "awaiting_action_card"

    # The connector executed run_op directly with the edited args + both gate
    # bypasses (human-approved + confirm).
    assert captured["name"] == "run_op"
    assert captured["args"]["params"] == {"ip": "1.2.3.4", "policy": "block-list"}
    assert captured["args"]["confirm"] is True
    assert captured["args"]["_approved"] is True

    # The real exec tool_use + tool_result lead the transcript (T6), ahead of
    # the model's continuation.
    assert res["transcript"][0]["type"] == "tool_use"
    assert res["transcript"][0]["name"] == "run_op"
    tr = [e for e in res["transcript"] if e.get("type") == "tool_result"]
    assert tr and tr[0]["content"] == {"ok": True,
                                       "data": {"status": "blocked",
                                                "ip": "1.2.3.4"}}
    assert res["tags"]["executed"] is True


def test_card_resume_approve_continuation_falls_back_when_no_provider(
        config, tmp_db, monkeypatch):
    """Outage-survivable: if the post-approval continuation round-trip fails
    (provider stream errors, e.g. Anthropic down), the resume still closes
    deterministically with the executed result + a summary at end_turn --
    never an error envelope."""
    from connector_fsr_agentic_assistant.operations import chat_resume
    import fsr_playbooks.llm.tools as fsr_tools

    _action_card_factory(monkeypatch)
    out = _sync_turn(config, {"session_id": "card-noprov", "intent": "block 1.2.3.4"})
    card_id = out["transcript"][-1]["id"]

    monkeypatch.setattr(fsr_tools, "dispatch",
                        lambda n, a, **kw: {"ok": True, "data": {"status": "blocked"}})

    # Provider builds fine but its stream blows up -- the continuation is caught
    # and the connector falls back to the deterministic executed-result path.
    class _BoomProvider:
        def stream(self, **_kw):
            raise RuntimeError("anthropic unreachable")
    monkeypatch.setattr(ops_chat, "_build_provider", lambda _c, _g: _BoomProvider())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda _c, _g: _BoomProvider())

    res = chat_resume(config, {"session_id": "card-noprov", "card_id": card_id,
                               "decision": "approve"})
    assert res["ok"] is True
    assert res["stop_reason"] == "end_turn"
    assert res["tags"]["executed"] is True
    txt = [e for e in res["transcript"] if e.get("type") == "text"]
    assert txt and "completed." in txt[0]["text"]
    assert "Executed" not in txt[0]["text"]


def test_card_resume_approve_exec_failure_degrades(config, tmp_db, monkeypatch):
    """If the executed op fails, the resume still closes (end_turn) with the
    failure surfaced in the tool_result + an analyst-facing warning, rather
    than dead-ending the chat."""
    from connector_fsr_agentic_assistant.operations import chat_resume
    import fsr_playbooks.llm.tools as fsr_tools

    _action_card_factory(monkeypatch)
    out = _sync_turn(config, {"session_id": "card-fail", "intent": "block 1.2.3.4"})
    card_id = out["transcript"][-1]["id"]

    monkeypatch.setattr(fsr_tools, "dispatch",
                        lambda n, a, **kw: {"ok": False, "message": "no_live_fsr"})
    res = chat_resume(config, {"session_id": "card-fail", "card_id": card_id,
                               "decision": "approve"})
    assert res["ok"] is True
    assert res["stop_reason"] == "end_turn"
    assert res["tags"]["executed"] is False
    txt = [e for e in res["transcript"] if e.get("type") == "text"]
    assert txt and "did not complete" in txt[0]["text"]


def test_card_resume_correctable_failure_self_heals(config, tmp_db, monkeypatch):
    """sess-uq31go5p gap B: if the approved op fails with an AGENT-FIXABLE code
    (unknown_operation / bad_params / ...), the resume re-enters the provider
    loop so the agent self-corrects and emits a fresh, re-gated action_card --
    instead of dead-ending on the templated 'contain manually' message."""
    from connector_fsr_agentic_assistant.operations import chat_resume
    import fsr_playbooks.llm.tools as fsr_tools

    _action_card_factory(monkeypatch)
    out = _sync_turn(config, {"session_id": "card-heal", "intent": "block 1.2.3.4"})
    card_id = out["transcript"][-1]["id"]

    # Approved op fails with a correctable code → self-heal, not dead-end.
    monkeypatch.setattr(
        fsr_tools, "dispatch",
        lambda n, a, **kw: {"ok": False, "code": "unknown_operation",
                      "message": "operation 'block_ip' not found",
                      "suggestions": ["use find_operation to list real ops"]})
    res = chat_resume(config, {"session_id": "card-heal", "card_id": card_id,
                               "decision": "approve"})
    assert res["ok"] is True
    # Re-entered the loop; the factory re-emits a card → awaiting a fresh
    # approval (NOT the deterministic end_turn apology).
    assert res["stop_reason"] == "awaiting_action_card"
    txt = [e for e in res["transcript"] if e.get("type") == "text"]
    assert not any("contain manually" in e.get("text", "") for e in txt)


def test_card_resume_infra_failure_stays_deterministic(config, tmp_db, monkeypatch):
    """A genuine infra failure (non-correctable code) must NOT re-enter the
    provider loop -- it keeps the deterministic, outage-survivable path so the
    resume still closes when Anthropic is down."""
    from connector_fsr_agentic_assistant.operations import chat_resume
    import fsr_playbooks.llm.tools as fsr_tools

    _action_card_factory(monkeypatch)
    out = _sync_turn(config, {"session_id": "card-infra", "intent": "block 1.2.3.4"})
    card_id = out["transcript"][-1]["id"]

    monkeypatch.setattr(
        fsr_tools, "dispatch",
        lambda n, a: {"ok": False, "code": "transport_failed",
                      "message": "FSR unreachable"})
    res = chat_resume(config, {"session_id": "card-infra", "card_id": card_id,
                               "decision": "approve"})
    assert res["ok"] is True
    assert res["stop_reason"] == "end_turn"
    assert res["tags"]["executed"] is False
    txt = [e for e in res["transcript"] if e.get("type") == "text"]
    assert txt and "did not complete" in txt[0]["text"]


def test_card_resume_reject_acknowledges(config, tmp_db, monkeypatch):
    """chat_resume(card_id, reject) re-enters the loop; the agent
    acknowledges and does not execute (contract T7)."""
    from connector_fsr_agentic_assistant.operations import chat_resume

    _action_card_factory(monkeypatch)
    out = _sync_turn(config, {"session_id": "card-rej", "intent": "block 1.2.3.4"})
    card_id = out["transcript"][-1]["id"]

    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    def _ack_factory(_c, _g):
        return FakeProvider([[
            TextEvent(text="Okay, I won't block it. What would you like instead?"),
            UsageEvent(session_id="card-rej", turn=2, model="fake-1",
                       input_tokens=4, output_tokens=3, cache_read=0,
                       cache_write=0, history_chars=20, stop_reason="end_turn"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _ack_factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _ack_factory)
    res = chat_resume(config, {
        "session_id": "card-rej", "card_id": card_id, "decision": "reject",
        "note": "not now",
    })
    assert res["ok"] is True
    assert res["stop_reason"] == "end_turn"
    # The user message fed to the model carried the cancellation.
    # (Resume re-entered the loop rather than erroring with approval_not_found.)
    assert res["error"] is None


def test_card_resume_unknown_session_errors(config, tmp_db, fake_provider):
    """Resuming a card for a session with no stored conversation returns a
    structured error, not a crash."""
    from connector_fsr_agentic_assistant.operations import chat_resume
    res = chat_resume(config, {"session_id": "never", "card_id": "x",
                               "decision": "approve"})
    assert res["ok"] is False
    assert res["error"]["code"] == "no_conversation"


# --- respond_manual_input (HITL manual-input leg) -------------------------

def _manual_input_factory(monkeypatch):
    """Factory that emits a manual_input gate on chat_turn (tier-0)."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import (ToolUseEvent, ToolResultEvent,
                                       UsageEvent)

    mi_args = {
        "id": "mi-approve-sweep",
        "workflow_run_iri": "/api/3/workflow_runs/wfr-501",
        "question": "Approve the sweep against these 3 hosts?",
        "fields": [{"name": "approved_by", "label": "Approver",
                    "default": "labuser"}],
    }

    def _factory(_c, _g):
        return FakeProvider([[
            ToolUseEvent(call_id="tu-mi", name="emit_manual_input",
                         arguments=mi_args, tier=0),
            ToolResultEvent(call_id="tu-mi",
                            result={"ok": True, "card": {**mi_args,
                                                         "type": "manual_input"}}),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=8, output_tokens=2, cache_read=0,
                       cache_write=0, history_chars=20, stop_reason="tool_use"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    return mi_args


def test_manual_input_emits_awaiting_stop_reason(config, tmp_db, monkeypatch):
    _manual_input_factory(monkeypatch)
    out = _sync_turn(config, {"session_id": "mi-sess", "intent": "run the sweep"})
    assert out["stop_reason"] == "awaiting_manual_input"
    assert out["transcript"][-1]["type"] == "manual_input"


def test_respond_manual_input_approve_continues(config, tmp_db, monkeypatch):
    """respond_manual_input(approve) forwards (degrades cleanly off-SOAR)
    and re-enters the loop so the chat narrates the result."""
    from connector_fsr_agentic_assistant.operations import (respond_manual_input)

    mi = _manual_input_factory(monkeypatch)
    _sync_turn(config, {"session_id": "mi-sess", "intent": "run the sweep"})

    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    def _cont(_c, _g):
        return FakeProvider([[
            TextEvent(text="Sweep approved by labuser -- proceeding."),
            UsageEvent(session_id="mi-sess", turn=2, model="fake-1",
                       input_tokens=5, output_tokens=4, cache_read=0,
                       cache_write=0, history_chars=30, stop_reason="end_turn"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _cont)
    monkeypatch.setattr(ops_manual, "_build_provider", _cont)
    res = respond_manual_input(config, {
        "session_id": "mi-sess", "input_id": mi["id"],
        "workflow_run_iri": mi["workflow_run_iri"],
        "decision": "approve", "values": {"approved_by": "labuser"},
    })
    assert res["ok"] is True
    assert res["input_id"] == "mi-approve-sweep"
    assert res["stop_reason"] == "end_turn"
    # Off-SOAR there is no crudhub, so the forward degrades, not crashes.
    assert res["manual_input_forward"]["forwarded"] is False
    assert res["manual_input_forward"]["reason"] == "crudhub_unavailable"


def test_respond_manual_input_reject_skips_forward(config, tmp_db, monkeypatch):
    from connector_fsr_agentic_assistant.operations import (respond_manual_input)

    mi = _manual_input_factory(monkeypatch)
    _sync_turn(config, {"session_id": "mi-rej", "intent": "run the sweep"})

    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    def _ack(_c, _g):
        return FakeProvider([[
            TextEvent(text="Understood -- holding off on the sweep."),
            UsageEvent(session_id="mi-rej", turn=2, model="fake-1",
                       input_tokens=4, output_tokens=3, cache_read=0,
                       cache_write=0, history_chars=20, stop_reason="end_turn"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _ack)
    monkeypatch.setattr(ops_manual, "_build_provider", _ack)
    res = respond_manual_input(config, {
        "session_id": "mi-rej", "input_id": mi["id"],
        "workflow_run_iri": mi["workflow_run_iri"], "decision": "reject",
    })
    assert res["ok"] is True
    # Reject never attempts a forward.
    assert res["manual_input_forward"]["reason"] == "rejected"


def test_respond_manual_input_accepts_int_input_id(config, tmp_db, monkeypatch):
    """A NUMERIC input_id must be accepted as an int, not only as a string.

    /api/integration/execute/ coerces a numeric string param back to an int in
    flight, so a `str`-only annotation rejected the exact ids the platform
    hands the op ("input_id: Input should be a valid string
    [input_value=141, input_type=int]", live on 8.0.0) -- the HITL leg was
    unreachable for every real gate id.
    """
    from connector_fsr_agentic_assistant.operations import respond_manual_input

    _manual_input_factory(monkeypatch)
    _sync_turn(config, {"session_id": "mi-int", "intent": "run the sweep"})

    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    monkeypatch.setattr(ops_chat, "_build_provider", lambda _c, _g: FakeProvider([[
        TextEvent(text="Holding off."),
        UsageEvent(session_id="mi-int", turn=2, model="fake-1",
                   input_tokens=4, output_tokens=3, cache_read=0,
                   cache_write=0, history_chars=20, stop_reason="end_turn"),
    ]]))
    monkeypatch.setattr(ops_manual, "_build_provider", lambda _c, _g: FakeProvider([[
        TextEvent(text="Holding off."),
        UsageEvent(session_id="mi-int", turn=2, model="fake-1",
                   input_tokens=4, output_tokens=3, cache_read=0,
                   cache_write=0, history_chars=20, stop_reason="end_turn"),
    ]]))
    res = respond_manual_input(config, {
        "session_id": "mi-int", "input_id": 141, "decision": "reject",
    })
    assert res["ok"] is True
    assert str(res["input_id"]) == "141"


def test_respond_manual_input_bad_decision(config, tmp_db, fake_provider):
    from connector_fsr_agentic_assistant.operations import respond_manual_input
    with pytest.raises(Exception) as exc:
        respond_manual_input(config, {"session_id": "s", "input_id": "i",
                                      "decision": "maybe"})
    assert "approve" in str(exc.value) and "reject" in str(exc.value)


def test_respond_manual_input_is_registered():
    from connector_fsr_agentic_assistant.operations import operations
    assert "respond_manual_input" in operations


# --- chat_resume ----------------------------------------------------------

def test_chat_resume_unknown_approval_id(config, tmp_db, fake_provider):
    from connector_fsr_agentic_assistant.operations import chat_resume
    out = chat_resume(config, {"approval_id": "doesn't-exist", "decision": "approve"})
    assert out["ok"] is False
    assert out["error"]["code"] == "approval_not_found"


def test_chat_resume_rejects_bad_decision(config, tmp_db, fake_provider):
    from connector_fsr_agentic_assistant.operations import chat_resume
    with pytest.raises(Exception) as exc:
        chat_resume(config, {"approval_id": "x", "decision": "maybe"})
    assert "approve" in str(exc.value) and "reject" in str(exc.value)


def test_chat_resume_happy_path(config, tmp_db, monkeypatch):
    """Stash a SuspendedSession in the persistent gateway, then resume.
    Uses a FakeProvider whose resume() method emits a final text + usage."""
    from fsr_playbooks.llm.approvals import SuspendedSession, bind
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent, DoneEvent
    from connector_fsr_agentic_assistant.storage import (
        default_storage, PersistedApprovalGateway,
    )

    class _ResumeProvider:
        name = "fake-resume"
        approval_gateway = None

        async def stream(self, *, system, messages, tools, tags=None, case_state=None, max_tool_turns=None):
            yield UsageEvent(
                session_id="sess-r", turn=1, model="fake-1",
                input_tokens=1, output_tokens=1,
                cache_read=0, cache_write=0,
                history_chars=10, stop_reason="end_turn",
            )

        async def resume(self, *, suspended, decision):
            yield TextEvent(text=f"resumed with {decision}")
            yield UsageEvent(
                session_id=suspended.session_id, turn=1, model="fake-1",
                input_tokens=5, output_tokens=3,
                cache_read=0, cache_write=0,
                history_chars=20, stop_reason="end_turn",
            )
            yield DoneEvent(stop_reason="end_turn")

    monkeypatch.setattr(ops_chat, "_build_provider", lambda c, g: _ResumeProvider())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda c, g: _ResumeProvider())

    storage = default_storage()
    gateway = PersistedApprovalGateway(storage)
    _susp = SuspendedSession(
        approval_id="appr-xyz", session_id="sess-r", tool="run_op",
        tool_use_id="tu-1", args={}, tier=3,
        history_snapshot=[], prior_tool_result_blocks=[],
        remaining_tool_calls=[], system="sys", tags={},
    )
    bind(_susp)  # mirror production bind-before-stash (S2 verify gate)
    gateway.stash(_susp)

    from connector_fsr_agentic_assistant.operations import chat_resume
    out = chat_resume(config, {"approval_id": "appr-xyz", "decision": "approve"})
    assert out["ok"] is True
    assert out["session_id"] == "sess-r"
    assert out["stop_reason"] == "end_turn"
    # Transcript contains the resumed text event.
    assert any(e.get("type") == "text" and "resumed with approve" in e.get("text", "")
               for e in out["transcript"])


# --- chat_history --------------------------------------------------------

def test_chat_history_empty_for_unknown_session(config, tmp_db):
    from connector_fsr_agentic_assistant.operations import chat_history
    out = chat_history(config, {"session_id": "never-existed"})
    assert out["turns"] == []


def test_chat_history_limit_param(config, tmp_db, fake_provider):
    from connector_fsr_agentic_assistant.operations import chat_history
    for _ in range(3):
        _sync_turn(config, {"session_id": "many", "intent": "x"})
    out = chat_history(config, {"session_id": "many", "limit": 2})
    assert len(out["turns"]) == 2


# --- playbook_offer accept / decline (contract 2.6.0 §5) -------------------

def _seed_offer_trace(session_id: str):
    """Persist a 2-step triage trace (enrich → isolate, value-matched wire)
    under session_id so the offer accept path has something to compile."""
    from fsr_playbooks.agent.skill_trace import SkillTrace
    from connector_fsr_agentic_assistant.storage import default_storage
    # Real connector/op names so the connector's strict compile_yaml (which
    # validates against the reference DB) passes.
    t = SkillTrace()
    t.record_run_op(
        "virustotal", "query_ip", {"ip": "203.0.113.77"},
        {"attributes": {"network_addr": "203.0.113.77"}}, ref_prefix="data")
    # block_ip_new's real params. This fixture used to pass `ip_address`,
    # which is not one: the saved playbook compiled green and would have
    # blocked nothing, until the compiler started refusing keys FSR ignores.
    t.record_run_op(
        "fortigate-firewall", "block_ip_new",
        {"method": "Quarantine Based", "ip_addresses": "203.0.113.77"},
        {"status": "blocked"})
    default_storage().set_session_trace(session_id, t.to_json())


@requires_full_reference_db
def test_playbook_offer_accept_compiles_and_pushes(config, tmp_db, monkeypatch):
    """ACCEPT compiles the recorded trace and pushes it -- the connector, not
    the model, authors the playbook (no provider needed)."""
    from connector_fsr_agentic_assistant import operations as ops
    _seed_offer_trace("sess-offer-accept")

    captured = {}

    def _fake_push(cfg, params):
        captured["workflow_json"] = params.get("workflow_json")
        return {"ok": True, "collection_uuid": "coll-123",
                "workflow_uuids": ["wf-123"]}

    monkeypatch.setattr(ops_autonomy, "push_playbook", _fake_push)
    monkeypatch.setattr(ops, "push_playbook", _fake_push)
    monkeypatch.setattr(ops_playbooks, "push_playbook", _fake_push)
    monkeypatch.setattr(ops_resume, "push_playbook", _fake_push)
    monkeypatch.setattr(ops_scheduling, "push_playbook", _fake_push)

    out = ops.chat_resume(config, {
        "session_id": "sess-offer-accept", "decision": "accept",
        "card_id": "pb-offer-1", "title": "C2 Containment Playbook"})

    assert out["ok"] is True
    assert out["stop_reason"] == "end_turn"
    assert out["contract_version"] == "2.8.0"
    assert out["tags"]["saved"] is True
    # The compiled workflow_json was handed to push.
    assert captured["workflow_json"] is not None
    # Transcript confirms the save with a playbook_pushed info_card.
    types = [(e.get("type"), e.get("variant")) for e in out["transcript"]]
    assert ("info_card", "playbook_pushed") in types
    info = next(e for e in out["transcript"]
               if e.get("variant") == "playbook_pushed")
    assert info["collection_uuid"] == "coll-123"


def test_playbook_offer_accept_empty_trace_does_not_dead_end(
        config, tmp_db, monkeypatch):
    """ACCEPT with no recorded trace returns a graceful end_turn, never an
    error (the offer should not have been emitted, but we don't crash)."""
    from connector_fsr_agentic_assistant import operations as ops
    monkeypatch.setattr(ops_autonomy, "push_playbook",
                        lambda c, p: {"ok": True})
    monkeypatch.setattr(ops, "push_playbook",
                        lambda c, p: {"ok": True})
    monkeypatch.setattr(ops_playbooks, "push_playbook",
                        lambda c, p: {"ok": True})
    monkeypatch.setattr(ops_resume, "push_playbook",
                        lambda c, p: {"ok": True})
    monkeypatch.setattr(ops_scheduling, "push_playbook",
                        lambda c, p: {"ok": True})  # must NOT be called
    out = ops.chat_resume(config, {
        "session_id": "sess-no-trace", "decision": "accept",
        "card_id": "pb-offer-x", "title": "Nothing"})
    assert out["ok"] is True
    assert out["stop_reason"] == "end_turn"
    assert out["tags"]["saved"] is False
    assert out["tags"]["code"] == "empty_trace"


def test_playbook_offer_decline_ends_turn_without_push(
        config, tmp_db, monkeypatch):
    """DECLINE ends the session with a direct end_turn -- no push, no agent
    round-trip, no conversation dependency (contract §5)."""
    from connector_fsr_agentic_assistant import operations as ops
    called = {"push": False}
    monkeypatch.setattr(
        ops_autonomy, "push_playbook",
        lambda c, p: called.__setitem__("push", True) or {"ok": True})
    monkeypatch.setattr(
        ops, "push_playbook",
        lambda c, p: called.__setitem__("push", True) or {"ok": True})
    monkeypatch.setattr(
        ops_playbooks, "push_playbook",
        lambda c, p: called.__setitem__("push", True) or {"ok": True})
    monkeypatch.setattr(
        ops_resume, "push_playbook",
        lambda c, p: called.__setitem__("push", True) or {"ok": True})
    monkeypatch.setattr(
        ops_scheduling, "push_playbook",
        lambda c, p: called.__setitem__("push", True) or {"ok": True})

    out = ops.chat_resume(config, {
        "session_id": "sess-offer-decline", "decision": "decline",
        "card_id": "pb-offer-1"})

    assert out["ok"] is True
    assert out["stop_reason"] == "end_turn"
    assert out["tags"]["saved"] is False
    assert called["push"] is False


def test_the_final_transcript_is_built_with_the_mounted_record_bound(
        config, tmp_db, fake_provider, monkeypatch):
    """The record-write card's context_match reads the mounted record. Live,
    every card said "unknown", because the turn reset that binding before the
    final transcript (where approvals become action cards) was built. A card
    that cannot say "this is the record you have open" did not warn an analyst
    about to overwrite it."""
    from connector_fsr_agentic_assistant import operations as ops
    from fsr_soc_triage.profiles import get_active_record_iri
    seen = []
    orig = ops._wire_transcript

    def _spy(transcript, **kw):
        seen.append(get_active_record_iri())
        return orig(transcript, **kw)

    monkeypatch.setattr(ops_chat, "_wire_transcript", _spy)
    monkeypatch.setattr(ops_resume, "_wire_transcript", _spy)
    _sync_turn(config, {
        "session_id": "ent-wire", "intent": "triage",
        "messages": [{"role": "user", "content": "attack timeline?"}],
        "entity": _ENTITY,
    })
    assert seen and seen[-1] == _ENTITY["iri"]
    assert get_active_record_iri() is None, "the bind must not leak past the turn"


# --- prompt + grounding seen by a real turn --------------------------------
#
# These drive chat_turn end to end and read what the model stream actually saw
# (the system prompt, the bound grounding), not the source text that builds them.

_TURN_PLAYBOOK = (
    "playbooks:\n- name: Hunt Indicators\n  steps:\n"
    "  - name: Start\n    type: start\n"
)


@pytest.fixture
def recording_provider(monkeypatch):
    """Install a FakeProvider that records what each turn streamed with.

    Returns the list of providers built, newest last. Each records the
    grounding bound at the moment the stream starts.
    """
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    from fsr_playbooks.mcp_server._shared import get_grounded_yaml

    built: list = []

    class _Recording(FakeProvider):
        def __init__(self):
            super().__init__([[
                TextEvent(text="ok"),
                UsageEvent(session_id="s", turn=1, model="fake-1",
                           input_tokens=1, output_tokens=1, cache_read=0,
                           cache_write=0, history_chars=0,
                           stop_reason="end_turn"),
            ]])
            self.grounding_seen: list = []
            built.append(self)

        async def stream(self, **kwargs):
            self.grounding_seen.append(get_grounded_yaml())
            async for ev in super().stream(**kwargs):
                yield ev

    monkeypatch.setattr(ops_chat, "_build_provider", lambda _c, _g: _Recording())
    monkeypatch.setattr(ops_manual, "_build_provider", lambda _c, _g: _Recording())
    return built


def test_connector_invariants_reach_the_model_on_every_turn(
        config, tmp_db, recording_provider):
    from ops_entity import _CONNECTOR_INVARIANT_RULES
    out = _sync_turn(config, {"session_id": "sess-inv", "intent": "build me one"})
    assert out["ok"] is True
    assert _CONNECTOR_INVARIANT_RULES in recording_provider[-1].last_system


def test_degraded_persona_is_disclosed_to_the_model(
        config, tmp_db, recording_provider):
    import ops_turn_state
    key = ("sess-degraded", "alerts")
    ops_turn_state._PROFILE_DEGRADED_TS[key] = ops_turn_state._profile_clock()
    try:
        out = _sync_turn(config, {
            "session_id": "sess-degraded", "intent": "triage",
            "messages": [{"role": "user", "content": "triage this alert"}],
            "entity": {"module": "alerts"}})
    finally:
        ops_turn_state._PROFILE_DEGRADED_TS.pop(key, None)
    assert out["ok"] is True
    assert "Degraded turn -- persona unavailable" in recording_provider[-1].last_system


def test_open_playbook_is_bound_during_the_turn_and_released_after(
        config, tmp_db, recording_provider):
    from fsr_playbooks.mcp_server._shared import get_grounded_yaml
    out = _sync_turn(config, {
        "session_id": "sess-bind", "intent": "triage",
        "messages": [{"role": "user", "content": "what is open?"}],
        "entity": {"module": "workflows", "playbook_yaml": _TURN_PLAYBOOK}})
    assert out["ok"] is True
    # the scope declaration is a model call too: the turn's own call is the last
    assert recording_provider[-1].grounding_seen[-1] == _TURN_PLAYBOOK
    assert get_grounded_yaml() is None, "the grounding must not leak past the turn"


def test_grounding_is_released_when_the_turn_fails(
        config, tmp_db, monkeypatch):
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.mcp_server._shared import get_grounded_yaml

    class _Down(FakeProvider):
        async def stream(self, **kwargs):
            raise RuntimeError("provider down")
            yield  # pragma: no cover  (makes this an async generator)

    monkeypatch.setattr(ops_chat, "_build_provider", lambda _c, _g: _Down())
    _sync_turn(config, {
        "session_id": "sess-bind-fail", "intent": "triage",
        "messages": [{"role": "user", "content": "what is open?"}],
        "entity": {"module": "workflows", "playbook_yaml": _TURN_PLAYBOOK}})
    assert get_grounded_yaml() is None, "a raising turn must not leak its grounding"
