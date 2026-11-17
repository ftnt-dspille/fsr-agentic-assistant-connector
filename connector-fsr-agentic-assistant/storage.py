"""Connector-side persistence.

Two stores live here:

1. **history.db** -- chat turns + per-message rows, keyed by `session_id`
   the widget passes. Parallels the schema of
   fsr-playbook-framework/web/backend/history.py so transcripts look the same
   in either consumer.

2. **PersistedApprovalGateway** -- sqlite-backed implementation of
   fsr_playbooks.protocols.ApprovalGateway. Paused HITL turns survive worker
   restarts, which the web app's in-memory gateway does not.

Both share a single sqlite file (default: ~/.connector_fsr_agentic_assistant.db,
override via FSR_PLAYBOOK_BUILDER_DB).

Several uWSGI worker processes open this file at once. Every operation opens
its own connection (never shared across a thread or a fork); reads run
autocommit or in one snapshot, and every read-modify-write runs in a
`BEGIN IMMEDIATE` transaction, so sqlite's locking is the only coordination.
Each streaming turn is owned by the worker that reserved it (`turn_runs`: owner
pid + heartbeat), so a turn whose worker dies is failed, not polled forever.
The operations are declared as the `SessionStore` protocol in store_protocol.py.
"""
from __future__ import annotations

import json
import logging
import os

# SECURITY: pickle is used ONLY to round-trip SuspendedSession through
# our own sqlite file. Both writer (stash) and reader (pop/peek) run
# inside the same connector process and never deserialize bytes from
# any external source. The threat model is "another process tampered
# with our sqlite file on disk", which already implies code-exec
# capability. If we ever expose this gateway to multi-tenant traffic or
# cross-process IPC, swap for a schema-validated deserializer
# (msgspec.Struct or pydantic) -- the SuspendedSession surface is small.
import pickle  # noqa: S403
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


def _turn_start_frame(turn_idx: int) -> dict:
    """Build the `turn_start` frame through `TurnStartFrame` rather than as a
    dict literal -- see `operations._frame` for why every outbound frame is
    constructed through its model.

    Imported lazily and both ways because `storage` is loaded two ways: as
    `_fsrpb_conn.storage` inside the connector package, and as a bare top-level
    `storage` by the tests that `from storage import Storage`. A plain relative
    import raises `attempted relative import with no known parent package` in
    the second case."""
    try:
        from .pydantic_models import TurnStartFrame
    except ImportError:  # loaded as a top-level module (tests)
        from pydantic_models import TurnStartFrame
    return TurnStartFrame(type="turn_start",
                          turn=turn_idx).model_dump(exclude_unset=True)


def _models():
    """The row models, imported the same dual way as `_turn_start_frame` (this
    module is loaded both as `_fsrpb_conn.storage` and as a bare top-level
    `storage`), and lazily so a missing/renamed model can never stop the
    connector from persisting a turn."""
    try:
        from . import pydantic_models as M
    except ImportError:  # loaded as a top-level module (tests)
        import pydantic_models as M  # type: ignore[no-redef]
    return M


def _check_row(model_name: str, data) -> None:
    """Validate a persisted row / payload for SIDE EFFECT only (#50).

    Six models described these shapes and were checked by nothing but their own
    unit tests -- `storage.py` imported exactly one model (`TurnStartFrame`) and
    everything the load/save path touched was a raw dict. An authored model that
    checks nothing is worse than no model, because it READS as coverage.

    Three deliberate properties:

    * **Never substitutes a validated object for the dict.** Callers read these
      dicts directly, and every model is `extra="allow"`, so returning a
      `model_dump()` could drop or retype a key -- `operations._check_params`'s
      posture, for the same reason.
    * **Permissive.** These rows are read back from a database written by OLDER
      connector versions, so unknown-key strictness would be an upgrade hazard,
      not a check. What it catches is a field that changed TYPE or a pinned
      field that vanished.
    * **Warns in production, raises under `FSRPB_STRICT_MODELS=1`** (the same
      switch and the same policy as `operations._check_response`). The check is
      duplicated here rather than imported because `operations` imports
      `storage`, and a top-level import back would be a cycle.

    Why it is worth checking at all: the persisted transcript is what
    `chat_history` and `chat_resume` REPLAY, so a row that drifts does not fail
    at write time -- it fails a turn later, on the resume, where the cause is
    furthest from the symptom. Same distance-from-cause that made the
    `stream_end` dropped-key defect expensive.

    Never raises outside strict mode, including if the model itself is missing:
    a validation-reporting defect must not fail the turn it is reporting on.
    """
    try:
        model = getattr(_models(), model_name)
        model(**(data or {}))
    except Exception as e:  # noqa: BLE001 -- see docstring
        if os.environ.get("FSRPB_STRICT_MODELS") == "1":
            raise
        logging.getLogger("connector-fsr-agentic-assistant").warning(
            "storage row validation failed for %s: %s", model_name, e)


def _default_db_path() -> Path:
    """Resolve the default sqlite path from the environment at call time.

    Read lazily (not as a module-level constant) so tests can point the
    store at a tmp file via FSR_PLAYBOOK_BUILDER_DB after import.
    """
    return Path(os.environ.get(
        "FSR_PLAYBOOK_BUILDER_DB",
        str(Path.home() / ".connector_fsr_agentic_assistant.db"),
    )).expanduser()

# The sessions db was named after the connector (`soc_assistant`) before the
# rename to Agentic Assistant, and the default path follows the name, so every
# install after the rename started an empty db and orphaned the history.
_LEGACY_DB_NAME = ".connector_fsr_soc_assistant.db"


def _adopt_legacy_db(new_path: Path) -> None:
    """Copy the pre-rename sessions db to `new_path`, ONCE: only when `new_path`
    does not exist and the legacy file does. The legacy file is only read (never
    moved, modified or deleted). Best-effort: any failure leaves a fresh db, the
    behaviour before this existed."""
    legacy = new_path.with_name(_LEGACY_DB_NAME)
    if new_path.exists() or not legacy.is_file():
        return
    tmp = new_path.with_name(f"{new_path.name}.adopt-{os.getpid()}")
    try:
        try:  # a consistent snapshot, reading the legacy file read-only
            src = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        except sqlite3.Error:
            # A WAL db cannot be opened read-only; a plain copy is only safe
            # when no WAL holds uncheckpointed pages.
            wal = legacy.with_name(legacy.name + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise
            import shutil
            shutil.copy2(legacy, tmp)
        os.link(tmp, new_path)   # atomic, and loses cleanly to a racing worker
        logging.getLogger("connector-fsr-agentic-assistant").info(
            "adopted the pre-rename sessions db %s", legacy)
    except FileExistsError:
        pass
    except Exception as e:  # noqa: BLE001
        logging.getLogger("connector-fsr-agentic-assistant").warning(
            "could not adopt the pre-rename sessions db: %s", e)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


_TTL_SECONDS = 600  # mirrors fsr_playbooks.llm.approvals._TTL_SECONDS

# How long a writer waits for another process's write lock before sqlite
# raises "database is locked". Writes here are a row or two, so a wait this
# long means something is wedged, not merely busy.
_BUSY_TIMEOUT_S = 15.0

# Turn liveness. The owning worker beats `turn_runs.heartbeat_at` every
# TURN_HEARTBEAT_SECS; a running turn whose owner process is gone, or whose beat
# is older than TURN_STALE_SECS, is failed by the poll path. A turn with a
# heartbeat but no frame for the silence window is still failed: that is a
# producer hung past every timeout it has (ops_turn_io._STREAM_STALE_SECS).
TURN_HEARTBEAT_SECS = 10.0
TURN_STALE_SECS = 60.0
_TURN_TRACK_MAX_SECS = 3 * 3600

# Builds the terminal error frame a failed turn is closed with. ops_common
# installs it at import: outbound frames are built through their pydantic model
# there (ops_common._frame), never as literals here. This module cannot import
# ops_common itself (the tests import it as a top-level module).
TERMINAL_ERROR_FRAME: Callable[[str], dict] | None = None

# Analyst-readable text for each way a turn is declared failed. The poll
# response carries it as a terminal stream_end (ok=false) with an error event
# in its transcript, which is the shape the widget already renders.
TURN_FAILURE_MESSAGES = {
    "owner_gone": ("The server worker running this turn stopped before it "
                   "finished (a worker restart or crash), so the answer was "
                   "lost. Please retry."),
    "stale_heartbeat": ("The server worker running this turn stopped reporting "
                        "progress, so the turn was marked failed. Please retry."),
    "silent": ("The build lost contact with the server and may have been "
               "interrupted. Please retry."),
}

# Grace window (seconds) between a HITL gate being stashed and the session
# activity that is considered "after" it. Suspending a turn writes the gate's
# own terminal frame to `turn_progress` at essentially the same instant as the
# `suspended_sessions` row, so a strict `last_activity > gate` comparison would
# call every gate stale the moment it was raised. Anything past this window is
# a genuinely later turn.
_GATE_GRACE_SECONDS = 15


def _gate_is_live(gate_at: float | None, last_activity: float | None) -> bool:
    """True when a stashed HITL gate is still the session's latest word.

    A suspended row only disappears when the approval is answered
    (`SuspendedStore.pop`) or when it ages out at TTL. An analyst who ignores
    the gate and types the next message leaves it behind, so presence alone
    over-reports `waiting_approval` -- the monitor showed pending sessions with
    nothing pending in them. Later session activity means the conversation ran
    on past the gate, which is only possible once the analyst moved on.

    Unknown `last_activity` (a session with no streamed frames at all) leaves
    the gate live: the alternative silently hides a real pending approval.
    """
    if gate_at is None:
        return False
    if last_activity is None:
        return True
    return last_activity <= gate_at + _GATE_GRACE_SECONDS


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_turns (
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read INTEGER,
    cache_write INTEGER,
    stop_reason TEXT,
    tags_json TEXT,
    tool_calls_json TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, turn)
);

CREATE INDEX IF NOT EXISTS idx_chat_turns_session
    ON chat_turns(session_id, created_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- user | assistant_text | tool_use | tool_result
    name TEXT,                     -- tool name when kind in (tool_use, tool_result)
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, turn, seq)
);

CREATE TABLE IF NOT EXISTS active_session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    session_id TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS suspended_sessions (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    payload BLOB NOT NULL,         -- pickled SuspendedSession
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_suspended_session
    ON suspended_sessions(session_id, created_at);

-- Canonical per-session conversation the connector feeds Anthropic on a
-- card/choice resume. The widget re-sends full messages on every
-- chat_turn but sends NONE on chat_resume (contract §4), so the
-- connector owns conversation state across the resume gap. Text-collapsed
-- {role, content:str} entries -- robust against Anthropic's strict
-- tool_use/tool_result pairing. Overwritten each turn; the widget's own
-- messages[] is authoritative again on the next real chat_turn.
CREATE TABLE IF NOT EXISTS session_conversation (
    session_id TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- The task intent (triage|build) a session was opened with, so a later
-- chat_resume (which carries no intent) re-enters under the same system
-- prompt + tool slice. Overwritten whenever a chat_turn sets it.
-- The analyst's answer to the "Modify this playbook / Create new playbook"
-- choice; bound into the framework on every later turn of the session.
CREATE TABLE IF NOT EXISTS session_playbook_scope (
    session_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_intent (
    session_id TEXT PRIMARY KEY,
    intent TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- The mounted-record module a session was opened against, so a later
-- chat_resume (which carries no entity) can re-resolve the same Key Store
-- persona and keep its prompt + tool slice across the HITL gap. Overwritten
-- whenever a chat_turn sets it. NULL/absent ⇒ no persona (built-in behavior).
CREATE TABLE IF NOT EXISTS session_module (
    session_id TEXT PRIMARY KEY,
    module TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- The resolved persona (Profile JSON) for a session, so a later chat_resume can
-- re-bind the SAME persona that authorized the write WITHOUT re-resolving it
-- from the Key Store. A resume runs in a separate (often cold) worker whose
-- Key Store read may yield no persona → the may_write gate would then wrongly
-- refuse the already-approved write. Written at turn time whenever a persona is
-- active; read back on resume.
CREATE TABLE IF NOT EXISTS session_profile (
    session_id TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- The OPEN playbook's YAML for a session, so a later chat_resume can re-bind
-- the framework's grounding ContextVar. chat_resume carries no entity block
-- (contract 4), and the grounding was bound ONLY in chat_turn -- so every
-- tier-3 tool, which by definition executes on the resume path after a human
-- approves, ran with NO open playbook. Third member of the same family as
-- session_module and session_profile, and the one nobody added.
CREATE TABLE IF NOT EXISTS session_playbook (
    session_id TEXT PRIMARY KEY,
    playbook_yaml TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- The playbook this conversation SAVED (an accepted playbook_offer), so a
-- later "now also add X" edits it -- with an Apply that writes to it -- instead
-- of re-typing a new draft. The widget only knows a playbook's id when it is
-- open in the designer; a playbook saved from chat is not, so the id is kept
-- here. Updated again when an Apply lands.
CREATE TABLE IF NOT EXISTS session_saved_playbook (
    session_id TEXT PRIMARY KEY,
    workflow_uuid TEXT NOT NULL,
    playbook_yaml TEXT NOT NULL,
    saved_at REAL NOT NULL
);

-- Every playbook the assistant bottled (saved from chat), one row per workflow,
-- so playbook_health can list them all. session_saved_playbook keeps only the
-- latest save per session; this keeps the history.
CREATE TABLE IF NOT EXISTS bottled_playbook (
    workflow_uuid TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    saved_at REAL NOT NULL
);

-- Where the turn's grounded playbook came from ('' = open in the designer,
-- 'draft', 'saved'), for the resume -- same family as session_playbook.
CREATE TABLE IF NOT EXISTS session_grounded_source (
    session_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Records this conversation CREATED (approved create_record), so a later turn
-- can name them. The widget sends prose history only, so the new record's IRI
-- lived in one tool result the next turn never saw: asked to "render <the new
-- template>", the model knew only the record on screen and refused, or ran on
-- the on-screen one instead.
CREATE TABLE IF NOT EXISTS session_created_records (
    session_id TEXT NOT NULL,
    iri TEXT NOT NULL,
    module TEXT NOT NULL,
    name TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, iri)
);

-- One decision per card. An approved action_card executes from its stored
-- copy, so nothing stopped a second approve -- a double click, a second tab,
-- or the chat drawer and the Monitor queue answering the same card -- from
-- running the write twice. The first decision claims the row; later ones are
-- told who decided it.
CREATE TABLE IF NOT EXISTS session_card_decisions (
    session_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor TEXT,
    decided_at REAL NOT NULL,
    PRIMARY KEY (session_id, card_id)
);

-- What an approved run_op card ACTUALLY ran: the analyst may edit args at
-- approve time and the stored card keeps the proposed ones, so an undo built
-- from the card could reverse the wrong target. One row per card; the undo
-- columns claim the reversal once, like a decision.
CREATE TABLE IF NOT EXISTS session_card_executions (
    session_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    connector TEXT NOT NULL,
    operation TEXT NOT NULL,
    config TEXT,
    args_json TEXT NOT NULL,
    ok INTEGER NOT NULL,
    executed_at REAL NOT NULL,
    undone_by TEXT,
    undone_at REAL,
    undo_json TEXT,
    PRIMARY KEY (session_id, card_id)
);

-- Was the TURN a read-only quick-action chip? Fifth member of the same family,
-- and the one still missing when #118 shipped. `_READ_ONLY_TURN` is bound from
-- `quick_action` in chat_turn and nowhere else, and it defaults to False --
-- i.e. NOT read-only, the permissive value. A read-only turn keeps the HITL
-- emit_* cards in its slice, so it can suspend on a choice or manual-input
-- card and come back through chat_resume with the read-only gate silently off.
CREATE TABLE IF NOT EXISTS session_read_only (
    session_id TEXT PRIMARY KEY,
    read_only INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

-- Cards emitted by a turn, so a later chat_resume can recover the
-- connector/operation/args the agent proposed (the resume payload only
-- carries card_id + edited args).
CREATE TABLE IF NOT EXISTS session_cards (
    session_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    card_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, card_id)
);

-- Per-session replay cursor for mock-mode (PLAN Phase 3). Tracks which
-- fixture response indices a (session, scenario) pair has already
-- consumed so repeated calls advance through the script instead of
-- re-serving the first match. consumed_json is a JSON list of ints.
CREATE TABLE IF NOT EXISTS mock_cursor (
    session_id TEXT NOT NULL,
    scenario TEXT NOT NULL,
    consumed_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, scenario)
);

-- Append-only per-turn progress feed for action-based streaming (contract
-- 2.5.0). A blocking chat_turn writes one frame per emitted event as the
-- agent streams; a concurrent chat_poll reads frames with cursor > its last
-- seen value. The autoincrement `cursor` is a monotonic, db-global read
-- cursor -- a poll never has to know the connector-assigned turn number. The
-- terminal frame (is_terminal=1) carries the envelope stop_reason so the
-- poller knows the turn finished. Frames are the RAW per-event wire shapes
-- (_event_to_wire); injected cards / coalesced text live only in the
-- authoritative chat_turn return, which the widget swaps in on `done`.
CREATE TABLE IF NOT EXISTS turn_progress (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    frame_json TEXT NOT NULL,
    is_terminal INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turn_progress_session
    ON turn_progress(session_id, cursor);

-- C6: a turn the analyst stopped. Written by chat_cancel, read by the agent
-- loop between rounds and before each tool dispatch (cancel_check).
CREATE TABLE IF NOT EXISTS turn_cancel (
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    requested_at REAL NOT NULL,
    PRIMARY KEY (session_id, turn)
);

-- One row per turn: who is running it and whether it is still alive. The
-- owner pid and heartbeat let a poll (or the sweep on worker start) tell a
-- turn that is running from one whose worker died with it still "running".
-- status is running | done | failed; the terminal frame and this row change in
-- the same transaction (append_turn_progress).
CREATE TABLE IF NOT EXISTS turn_runs (
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    owner_pid INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    finished_at REAL,
    PRIMARY KEY (session_id, turn)
);
CREATE INDEX IF NOT EXISTS idx_turn_runs_status ON turn_runs(status);

-- Append-only, render-fidelity transcript for refresh durability. Each row
-- is one visible exchange in chronological order: a `user` row carries the
-- analyst's text; an `assistant` row carries the FULL wire_transcript event
-- array the widget rendered live (text + tool_call + cards + activity). On a
-- page refresh the widget replays these rows through the same render pipeline,
-- so the timeline returns byte-for-byte -- unlike `session_conversation`, which
-- is a text-collapsed form for the model, not for display. Append-only (never
-- overwritten) so the history grows across turns AND across resume gaps.
CREATE TABLE IF NOT EXISTS session_transcript (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,            -- user | assistant | resume
    payload_json TEXT NOT NULL,   -- user: {"text": str}; assistant: {"transcript": [...]}
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_transcript
    ON session_transcript(session_id, seq);

-- The SOAR user who STARTED the conversation (first actor seen), so a session
-- opened from the history browser attributes to its real initiator instead of
-- whoever is currently viewing. First-writer-wins (the opener owns it).
CREATE TABLE IF NOT EXISTS session_initiator (
    session_id TEXT PRIMARY KEY,
    author_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Alert-storm grouping before auto-triage. `triage_log` has one row per record
-- the trigger has seen: a lead (got a session), a member (attached to a
-- lead's session) or deferred (over the rate cap, no session). `triage_group`
-- holds the keys a lead can be matched on.
CREATE TABLE IF NOT EXISTS triage_log (
    record_iri TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    session_id TEXT,
    lead_session TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triage_log_role
    ON triage_log(role, created_at);
CREATE TABLE IF NOT EXISTS triage_group (
    group_key TEXT NOT NULL,
    lead_session TEXT NOT NULL,
    lead_record TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (group_key, lead_session)
);

-- The typed action trace (SkillTrace JSON) accumulated across a session's
-- run_op calls, persisted next to the transcript so the trace-compiler
-- (build_playbook_from_trace) can replay it at build time instead of the
-- agent guessing jinja wiring. See SKILL_BASED_PLAYBOOK_PLAN §2.
CREATE TABLE IF NOT EXISTS session_trace (
    session_id TEXT PRIMARY KEY,
    trace_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- The model-provider redaction map for a session (value <-> typed placeholder),
-- so `<IP_1>` means the same address in every turn and in a resume that runs
-- on another worker. Written only when the redaction mode is not off. The
-- values are the real ones: this table is as sensitive as the transcript.
CREATE TABLE IF NOT EXISTS session_redaction (
    session_id TEXT PRIMARY KEY,
    map_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Per-session case state (CASE_STATE_SPINE_DESIGN P1/P2), keyed by
-- session_id. Stores the persistent case-state JSON across turns and
-- resume gaps. Overwritten each turn; the agent's in-turn state_events
-- are authoritative; this table recovers state after a resume.
CREATE TABLE IF NOT EXISTS session_case_state (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Analyst feedback on a session: a thumbs up/down so a human triaging the
-- history can flag the sessions worth investigating/fixing. `rating` is
-- 'up' | 'down' | NULL (cleared). Optional free-text `note`. One row/session.
CREATE TABLE IF NOT EXISTS session_rating (
    session_id TEXT PRIMARY KEY,
    rating TEXT,
    note TEXT,
    updated_at REAL NOT NULL
);

-- Scheduled agent tasks (scheduled-agent-tasks plan). The schedule ROW
-- itself lives on the FortiSOAR scheduler (django-celery-beat, editable via
-- /api/wf/api/scheduled/); this table holds the task *definition* the widget
-- manages -- the prompt, intent, allow_mutating flag -- keyed by the schedule
-- display `name` (the stable lookup key, since the schedule `id` is a rotating
-- Fernet token). `wf_iri` is the generated runner playbook's IRI
-- (/api/3/workflows/<uuid>), set after push_playbook. `agent_config` is the
-- connector config name the runner's Connectors step invokes.
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    name TEXT PRIMARY KEY,
    cron TEXT NOT NULL,
    intent TEXT NOT NULL,             -- triage | build
    prompt TEXT NOT NULL,
    allow_mutating INTEGER NOT NULL DEFAULT 0,
    agent_config TEXT,                 -- connector config name, e.g. fsrpb-anthropic
    wf_iri TEXT,                       -- /api/3/workflows/<uuid>
    wf_uuid TEXT,                      -- the runner playbook uuid
    collection_uuid TEXT,              -- the workflow_collection uuid
    timezone TEXT NOT NULL DEFAULT 'UTC',
    enabled INTEGER NOT NULL DEFAULT 1,
    notify_target TEXT,                -- email/slack destination for notify_finding
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Scheduled hunts (X6): a saved hunt question the assistant runs unattended on
-- the FortiSOAR scheduler. The schedule row (agent-hunt:<name>) and its runner
-- playbook are plumbing keyed by `name`, like scheduled_tasks; this table is the
-- definition the Monitor edits. `case_iri` is the open case the hunt's findings
-- accumulate on: the next finding comments on it while it is still open.
CREATE TABLE IF NOT EXISTS hunts (
    name TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    cron TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    module TEXT NOT NULL DEFAULT 'alerts',   -- alerts | incidents: where a finding opens a case
    enabled INTEGER NOT NULL DEFAULT 1,
    agent_config TEXT,
    wf_iri TEXT,
    wf_uuid TEXT,
    collection_uuid TEXT,
    case_iri TEXT,
    created_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- One row per hunt run (a cron fire or a manual run). `outcome` is derived from
-- the stored verdict card, never from prose: finding | quiet | inconclusive | error.
CREATE TABLE IF NOT EXISTS hunt_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hunt_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    outcome TEXT NOT NULL,
    disposition TEXT,
    severity TEXT,
    confidence REAL,
    summary TEXT,
    case_iri TEXT,
    case_action TEXT,                 -- opened | updated | none
    waiting INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_hunt_runs_name ON hunt_runs(hunt_name, started_at);
CREATE INDEX IF NOT EXISTS idx_hunt_runs_started ON hunt_runs(started_at);

-- One row per outbound notification the assistant decided to send (X6): an
-- unattended turn that ended on an escalation or a threat verdict, or a shift
-- summary. UNIQUE (kind, ref) makes the send once-only: the first claim wins, so
-- a retried playbook step or a second worker never sends a duplicate. `status`
-- is sent | skipped | failed; `detail` names the channel or the reason.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,               -- hunt | auto_triage | shift
    ref TEXT NOT NULL,                -- the session id, or the shift window
    trigger_code TEXT,                -- escalation | verdict | shift
    subject TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    channel TEXT,
    detail TEXT,
    created_at REAL NOT NULL,
    UNIQUE (kind, ref)
);

-- Per-LLM-turn usage telemetry (agentic-assistant-monitor plan). The richer
-- companion to /api/3/llm_activity_logs: holds the fields the platform module
-- lacks (tool_calls, cache stats, tags, session_id, intent, user). Populated
-- alongside the existing usage_log.log_turn() JSONL write + the platform
-- module POST. Best-effort -- telemetry never breaks chat.
CREATE TABLE IF NOT EXISTS agent_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                  -- ISO 8601 UTC
    session_id TEXT NOT NULL,
    user_iri TEXT,                     -- /api/3/people/<uuid>, nullable
    user_name TEXT,                     -- resolved display name, nullable
    intent TEXT,                        -- triage | build
    model TEXT,
    turn INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read INTEGER,
    cache_write INTEGER,
    cost_usd REAL,
    latency_ms REAL,
    stop_reason TEXT,
    status TEXT,                        -- success | error
    error TEXT,
    tool_calls_json TEXT,              -- JSON array
    tags_json TEXT,                     -- JSON object (attribution)
    prompt TEXT,                        -- full prompt (audit)
    response TEXT,                      -- full agent text output (audit)
    agent_thinking TEXT,                -- reasoning text (extended-thinking models; empty for standard)
    approvals_count INTEGER DEFAULT 0,  -- HITL gates hit in this round-trip
    activity_log_iri TEXT,              -- /api/3/llm_activity_logs/<uuid> mirror, nullable
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_usage_ts ON agent_usage(ts);
CREATE INDEX IF NOT EXISTS idx_agent_usage_session ON agent_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_usage_user ON agent_usage(user_iri);

-- Per-tool-call detail for the audit trail (agentic-assistant-monitor §10.1). One
-- row per tool call within an LLM round-trip, linked to its parent agent_usage
-- row by `usage_id`. Holds the fields the aggregate `agent_usage.tool_calls_json`
-- blob cannot: the actual params, the result summary, the derived status, and
-- the per-call wall-time. Populated by `record_agent_tool_calls` from the
-- ToolUseEvent/ToolResultEvent frames buffered during the turn. Per-call token
-- counts / cost are intentionally absent -- the framework's UsageEvent reports
-- totals per round-trip, not per call; faking them would mislead the audit view.
CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_id INTEGER NOT NULL,
    turn INTEGER,
    tool_call_index INTEGER NOT NULL DEFAULT 0,
    tool_name TEXT NOT NULL,
    params_json TEXT,
    result_status TEXT,                -- success | error
    result_summary TEXT,
    latency_ms REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (usage_id) REFERENCES agent_usage(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_usage ON agent_tool_calls(usage_id);

-- Per-session live-session descriptor (agentic-assistant-monitor §10.2). Holds the
-- fields the existing session tables don't expose cleanly to the monitor's Live
-- Sessions panel: the mounted record (uuid + module) and the latest user
-- prompt. UPSERTed once per chat_turn (the only "session is being touched"
-- hook); `started_at` is first-writer-wins, the rest last-writer-wins. The
-- live `status` (active/waiting_approval/idle) is NOT stored here -- it is
-- DERIVED at read time in list_active_sessions from turn_progress (in-flight
-- turn) + suspended_sessions (HITL-parked), so a stale column can never lie.
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    record_uuid TEXT,
    record_module TEXT,
    current_prompt TEXT,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Analyst feedback on one auto-triage decision (Monitor M3): "right" or "wrong"
-- plus an optional short note. One row per (record, analyst); a later write
-- replaces the earlier one. `rule` is the autonomy-policy rule that covered the
-- decision (when one did), kept so per-rule readiness can count it.
CREATE TABLE IF NOT EXISTS decision_feedback (
    record_iri TEXT NOT NULL,
    analyst TEXT NOT NULL,
    session_id TEXT NOT NULL,
    rating TEXT NOT NULL,
    note TEXT,
    rule TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (record_iri, analyst)
);
CREATE INDEX IF NOT EXISTS idx_decision_feedback_rule
    ON decision_feedback(rule, updated_at);
"""


def _connect(path: Path | str) -> sqlite3.Connection:
    """A fresh autocommit connection. Never cached and never shared: a sqlite
    connection must not cross a thread or a fork, so each operation opens its
    own and closes it (see `_closing`)."""
    return sqlite3.connect(path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)


@contextmanager
def _closing(path: Path | str) -> Iterator[sqlite3.Connection]:
    c = _connect(path)
    try:
        yield c
    finally:
        c.close()


def _add_missing_columns(c: sqlite3.Connection, table: str,
                         columns: dict[str, str]) -> None:
    """Additive column migration. Must run inside a write transaction: the
    existence check and the ALTER have to be one unit, or two workers opening
    an old DB together both see the column missing and the second ALTER fails
    with "duplicate column name"."""
    have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in columns.items():
        if name not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _log_store_error(what: str, exc: BaseException) -> None:
    logging.getLogger("connector-fsr-agentic-assistant").warning(
        "storage: %s failed: %s: %s", what, type(exc).__name__, exc)


def _pid_alive(pid: int | None) -> bool:
    """Whether an owning worker process still exists on this host. EPERM means
    it exists under another uid, so it counts as alive."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class _TurnHeartbeat:
    """Keeps `turn_runs.heartbeat_at` fresh for every turn this process owns.

    One daemon thread per worker process, started on demand and retired when
    it has nothing left to beat. A heartbeat thread (rather than the producer
    thread) keeps beating while the producer sits in a long LLM round, so a
    slow round is never mistaken for a dead owner; a worker that is gone or
    frozen stops beating and the poll path reports its turn as failed.

    Fork-safe: `os.register_at_fork` resets the state in the child, so a worker
    never beats a turn its parent owned."""

    def __init__(self) -> None:
        self._reset()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset)

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[tuple[str, str, int], float] = {}
        self._failing: set[tuple[str, str, int]] = set()
        self._thread: threading.Thread | None = None

    def add(self, db_path: Path | str, session_id: str, turn: int) -> None:
        with self._lock:
            self._turns[(str(db_path), session_id, turn)] = time.time()
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="turn-heartbeat", daemon=True)
                self._thread.start()

    def discard(self, db_path: Path | str, session_id: str, turn: int) -> None:
        with self._lock:
            self._turns.pop((str(db_path), session_id, turn), None)
            self._failing.discard((str(db_path), session_id, turn))

    def _run(self) -> None:
        pid = os.getpid()
        while True:
            time.sleep(TURN_HEARTBEAT_SECS)
            now = time.time()
            with self._lock:
                # Forget turns that are too old to still be running, so a turn
                # whose terminal never landed cannot be beaten forever.
                for key, added in list(self._turns.items()):
                    if now - added > _TURN_TRACK_MAX_SECS:
                        del self._turns[key]
                live = list(self._turns)
                if not live:
                    self._thread = None
                    return
            for db_path, session_id, turn in live:
                try:
                    with _closing(db_path) as c:
                        cur = c.execute(
                            "UPDATE turn_runs SET heartbeat_at=? WHERE "
                            "session_id=? AND turn=? AND owner_pid=? AND "
                            "status='running'",
                            (now, session_id, turn, pid))
                        if cur.rowcount == 0:  # finished or swept: stop beating
                            self.discard(db_path, session_id, turn)
                    self._failing.discard((db_path, session_id, turn))
                except sqlite3.Error as e:
                    key = (db_path, session_id, turn)
                    if not os.path.exists(db_path):
                        # The store is gone, so nothing can poll this turn
                        # either. Beating on would fail every interval until
                        # the 3h cap.
                        self.discard(db_path, session_id, turn)
                        _log_store_error(
                            f"turn heartbeat {session_id}/{turn} (database gone, stopped)", e)
                    elif key not in self._failing:
                        # Once per failing streak: a locked or unreadable DB
                        # would otherwise log every interval.
                        self._failing.add(key)
                        _log_store_error(f"turn heartbeat {session_id}/{turn}", e)


_HEARTBEAT = _TurnHeartbeat()


class Storage:
    """The sqlite-backed `SessionStore` (see store_protocol.py).

    Cheap to construct and safe to share across threads and processes: it
    holds only the path. Every operation opens its own connection, so sqlite's
    locking is the only coordination between threads and worker processes.
    Reads run on autocommit connections; writes run in `BEGIN IMMEDIATE`
    transactions, which take the write lock up front so a read-modify-write
    cannot interleave with another writer."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not db_path and "FSR_PLAYBOOK_BUILDER_DB" not in os.environ:
            _adopt_legacy_db(self.db_path)   # only the true default location
        self._ensure_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Autocommit connection for reads. Closed on exit."""
        with _closing(self.db_path) as c:
            yield c

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. BEGIN IMMEDIATE takes the write lock before
        the first read, so a read-modify-write inside it is atomic across
        processes; a deferred transaction that reads first can instead fail
        with SQLITE_BUSY on upgrade rather than wait."""
        with _closing(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        """Read transaction: every SELECT inside sees one WAL snapshot, so a
        writer cannot land between two of them."""
        with _closing(self.db_path) as c:
            c.execute("BEGIN")
            try:
                yield c
            finally:
                c.execute("COMMIT")

    def _ensure_schema(self) -> None:
        with _closing(self.db_path) as c:
            # WAL is a property of the file, so setting it once is enough.
            c.execute("PRAGMA journal_mode=WAL")
            # Every statement is IF NOT EXISTS, so concurrent first opens are
            # safe; the busy timeout covers the brief write lock of a create.
            c.executescript(_SCHEMA)
        with self._write() as c:
            _add_missing_columns(c, "agent_usage", {
                "agent_thinking": "TEXT",
                "approvals_count": "INTEGER DEFAULT 0"})
            _add_missing_columns(c, "scheduled_tasks", {"notify_target": "TEXT"})

    # ----- HistorySink ----------------------------------------------------

    def write_active_session(self, session_id: str) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO active_session (id, session_id, updated_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET session_id=excluded.session_id, "
                "updated_at=excluded.updated_at",
                (session_id, time.time()),
            )

    def record_chat_turn(self, record: dict) -> None:
        with self._write() as c:
            c.execute(
                "INSERT OR REPLACE INTO chat_turns "
                "(session_id, turn, model, input_tokens, output_tokens, "
                " cache_read, cache_write, stop_reason, tags_json, "
                " tool_calls_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.get("session"), record.get("turn"),
                    record.get("model"),
                    record.get("input_tokens"), record.get("output_tokens"),
                    record.get("cache_read"), record.get("cache_write"),
                    record.get("stop_reason"),
                    json.dumps(record.get("tags") or {}),
                    json.dumps(record.get("tool_calls") or []),
                    time.time(),
                ),
            )

    def next_turn_idx(self, session_id: str) -> int:
        """The turn number to assign the next chat_turn for this session.

        Derived from the MAX turn already seen in EITHER `chat_turns` OR
        `turn_progress`. Using `chat_turns` alone is unsafe: a `chat_turns`
        row is only written on a UsageEvent, so a turn that fails before the
        provider emits usage (e.g. the LLM gateway is unreachable, or any
        error in the prologue) leaves no row -- and the NEXT turn would then
        reuse the same turn_idx. The widget's poll fence (`since_turn`) is
        monotonic per committed transcript, so a reused turn_idx makes
        read_turn_progress fence out every live turn (turn=None, empty
        frames, no live messages). `turn_progress` gets a `turn_start` row
        for EVERY turn up front, so MAX(turn) across both tables increments
        unconditionally and keeps the connector's numbering aligned with the
        widget's fence."""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(t) FROM ("
                "  SELECT MAX(turn) AS t FROM chat_turns WHERE session_id=?"
                "  UNION ALL"
                "  SELECT MAX(turn) AS t FROM turn_progress WHERE session_id=?"
                ")",
                (session_id, session_id),
            ).fetchone()
        return (row[0] or 0) + 1

    def reserve_next_turn(self, session_id: str) -> int:
        """Atomically derive AND reserve the next turn index for a session.

        `next_turn_idx()` only READS the MAX; the caller then wrote the
        `turn_start` row in a SEPARATE transaction. Two concurrent
        `chat_turn`s -- including separate worker processes -- could both
        read the same MAX before either wrote its `turn_start`, minting a
        duplicate turn (turn_progress has no unique constraint on turn).

        The MAX read, the `turn_start` frame and the `turn_runs` owner row are
        written in ONE `BEGIN IMMEDIATE` transaction, so the read-then-write is
        serialized across connections and processes, and the turn is owned from
        the moment it exists. The caller MUST NOT append a second `turn_start`
        frame for the returned turn."""
        now = time.time()
        with self._write() as c:
            row = c.execute(
                "SELECT MAX(t) FROM ("
                "  SELECT MAX(turn) AS t FROM chat_turns WHERE session_id=?"
                "  UNION ALL"
                "  SELECT MAX(turn) AS t FROM turn_progress WHERE session_id=?"
                ")",
                (session_id, session_id),
            ).fetchone()
            turn_idx = (row[0] or 0) + 1
            c.execute(
                "INSERT INTO turn_progress "
                "(session_id, turn, frame_json, is_terminal, stop_reason, "
                " created_at) VALUES (?, ?, ?, 0, NULL, ?)",
                (session_id, turn_idx,
                 json.dumps(_turn_start_frame(turn_idx)), now),
            )
            c.execute(
                "INSERT INTO turn_runs (session_id, turn, owner_pid, status, "
                " started_at, heartbeat_at) VALUES (?, ?, ?, 'running', ?, ?)",
                (session_id, turn_idx, os.getpid(), now, now),
            )
        _HEARTBEAT.add(self.db_path, session_id, turn_idx)
        return turn_idx

    def record_chat_message(
        self, session_id: str, turn: int, seq: int,
        kind: str, content: str, name: str | None = None,
    ) -> None:
        # Checked on the WRITE, not the load path: nothing in the connector ever
        # SELECTs from chat_messages -- it is the per-event audit table, read
        # only by ad-hoc `query_store` SQL. So the insert is the only boundary
        # this row ever crosses, and a load-path check would be a check that
        # never runs.
        _check_row("ChatMessageRow",
                   {"session_id": session_id, "turn": turn, "seq": seq,
                    "kind": kind, "name": name, "content": content})
        with self._write() as c:
            c.execute(
                "INSERT OR REPLACE INTO chat_messages "
                "(session_id, turn, seq, kind, name, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, turn, seq, kind, name, content, time.time()),
            )

    # ----- conversation state (for card/choice resume) -------------------

    def save_conversation(self, session_id: str, messages: list[dict]) -> None:
        """Overwrite the canonical conversation for a session. `messages`
        is a list of {role, content} replayed on a resume turn; content is a
        string or an assistant block list (history_replay)."""
        with self._write() as c:
            c.execute(
                "INSERT INTO session_conversation "
                "(session_id, messages_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "messages_json=excluded.messages_json, "
                "updated_at=excluded.updated_at",
                (session_id, json.dumps(messages), time.time()),
            )

    def load_conversation(self, session_id: str) -> list[dict]:
        """Recover the canonical conversation. Fail-soft: a missing OR
        unparseable row reads as "no prior conversation" ([]) rather than
        raising -- a corrupt/half-written row must never crash a resume turn
        (matches get_case_state's guard)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT messages_json FROM session_conversation "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return []
        try:
            messages = json.loads(row[0])
        except (ValueError, TypeError):
            return []
        for msg in messages if isinstance(messages, list) else []:
            _check_row("ConversationMessage", msg)
        return messages

    # ----- session intent (triage|build) for resume continuity -----------

    def set_session_intent(self, session_id: str, intent: str) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO session_intent (session_id, intent, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "intent=excluded.intent, updated_at=excluded.updated_at",
                (session_id, intent, time.time()),
            )

    def get_session_intent(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT intent FROM session_intent WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return row[0] if row else None

    # ----- session playbook scope (modify | create_new) ---------------------

    def set_session_playbook_scope(self, session_id: str, scope: str) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO session_playbook_scope (session_id, scope, "
                "updated_at) VALUES (?, ?, ?) ON CONFLICT(session_id) DO "
                "UPDATE SET scope=excluded.scope, updated_at=excluded.updated_at",
                (session_id, scope, time.time()),
            )

    def get_session_playbook_scope(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT scope FROM session_playbook_scope WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return row[0] if row else None

    # ----- session module (mounted record's module) for persona resume ----

    def set_session_module(self, session_id: str, module: str) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO session_module (session_id, module, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "module=excluded.module, updated_at=excluded.updated_at",
                (session_id, module, time.time()),
            )

    def get_session_module(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT module FROM session_module WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return row[0] if row else None

    # ----- session open playbook (YAML) for grounding on resume -----------

    def set_session_playbook_yaml(self, session_id: str, yaml_text: str | None) -> None:
        """Persist the turn's OPEN playbook so chat_resume can re-ground.

        Without this, a tier-3 tool -- which only ever runs after a human
        approves, i.e. on the resume path -- sees no open playbook, and any
        guard that compares against it silently passes. None (nothing open)
        is stored too, as "", so a resume never grounds on an earlier turn's
        playbook.
        """
        yaml_text = yaml_text or ""
        with self._write() as c:
            c.execute(
                "INSERT INTO session_playbook (session_id, playbook_yaml, "
                "updated_at) VALUES (?, ?, ?) ON CONFLICT(session_id) DO "
                "UPDATE SET playbook_yaml=excluded.playbook_yaml, "
                "updated_at=excluded.updated_at",
                (session_id, yaml_text, time.time()),
            )

    def get_session_playbook_yaml(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT playbook_yaml FROM session_playbook WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return (row[0] or None) if row else None

    # ----- the playbook this conversation built --------------------------

    def set_session_saved_playbook(self, session_id: str, workflow_uuid: str,
                                   yaml_text: str) -> None:
        now = time.time()   # one read, shared by both rows (a clock-stubbed test counts calls)
        with self._write() as c:
            c.execute(
                "INSERT INTO session_saved_playbook (session_id, workflow_uuid, "
                "playbook_yaml, saved_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "workflow_uuid=excluded.workflow_uuid, "
                "playbook_yaml=excluded.playbook_yaml, saved_at=excluded.saved_at",
                (session_id, workflow_uuid, yaml_text, now),
            )
            c.execute(
                "INSERT INTO bottled_playbook (workflow_uuid, session_id, saved_at) "
                "VALUES (?, ?, ?) ON CONFLICT(workflow_uuid) DO UPDATE SET "
                "session_id=excluded.session_id, saved_at=excluded.saved_at",
                (workflow_uuid, session_id, now),
            )

    def list_bottled_playbooks(self, limit: int = 50) -> list[dict]:
        """Playbooks the assistant bottled, newest save first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT workflow_uuid, session_id, saved_at FROM bottled_playbook "
                "ORDER BY saved_at DESC LIMIT ?", (int(limit),),
            ).fetchall()
        return [{"workflow_uuid": r[0], "session_id": r[1], "saved_at": r[2]}
                for r in rows]

    def get_session_saved_playbook(self, session_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT workflow_uuid, playbook_yaml, saved_at FROM "
                "session_saved_playbook WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {"workflow_uuid": row[0], "yaml": row[1], "saved_at": row[2]}

    def latest_card_of_type(self, session_id: str,
                            card_type: str) -> tuple[dict, float] | None:
        """(card, stored_at) for the session's most recently stored card of
        ``card_type``. A re-offer under the same id replaces its row, so the
        latest is the one the analyst was shown last. Fail-soft like get_card."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT card_json, created_at FROM session_cards "
                "WHERE session_id=? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        for raw, at in rows:
            try:
                card = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(card, dict) and card.get("type") == card_type:
                return card, at
        return None

    def set_session_grounded_source(self, session_id: str,
                                    source: str | None) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO session_grounded_source (session_id, source, "
                "updated_at) VALUES (?, ?, ?) ON CONFLICT(session_id) DO "
                "UPDATE SET source=excluded.source, "
                "updated_at=excluded.updated_at",
                (session_id, source or "", time.time()),
            )

    def get_session_grounded_source(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT source FROM session_grounded_source WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return (row[0] or None) if row else None

    # ----- session read-only turn, for the dispatch gate's resume ---------

    def set_session_read_only(self, session_id: str, read_only: bool) -> None:
        """Persist whether the TURN was a read-only quick-action chip.

        A resume carries no `quick_action`, so it cannot re-derive this -- and `_READ_ONLY_TURN`
        defaults to False, the permissive value. Without this, suspending a
        read-only turn on a card and resuming it restores the write frontier
        the chip withheld.
        """
        with self._write() as c:
            c.execute(
                "INSERT INTO session_read_only (session_id, read_only, "
                "updated_at) VALUES (?, ?, ?) ON CONFLICT(session_id) DO "
                "UPDATE SET read_only=excluded.read_only, "
                "updated_at=excluded.updated_at",
                (session_id, 1 if read_only else 0, time.time()),
            )

    def get_session_read_only(self, session_id: str) -> bool | None:
        """The turn's read-only flag, or None when nothing was recorded
        (callers fail OPEN on None -- absence must not invent a refusal)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT read_only FROM session_read_only WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return None if row is None else bool(row[0])

    # ----- records the conversation created ---------------------------------

    def add_session_record(self, session_id: str, module: str, iri: str,
                           name: str | None) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO session_created_records (session_id, iri, module, "
                "name, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, "
                "iri) DO UPDATE SET name=excluded.name",
                (session_id, iri, module, name, time.time()),
            )

    def get_session_records(self, session_id: str) -> list[dict]:
        """Oldest first: {module, iri, name}."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT module, iri, name FROM session_created_records "
                "WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [{"module": m, "iri": i, "name": n} for m, i, n in rows]

    # ----- session persona (resolved Profile JSON) for persona resume ------

    def set_session_profile(self, session_id: str, profile_json: str) -> None:
        """Persist the turn's resolved persona so chat_resume can re-bind it
        without a Key Store re-read (which a cold resume worker may fail)."""
        with self._write() as c:
            c.execute(
                "INSERT INTO session_profile (session_id, profile_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "profile_json=excluded.profile_json, updated_at=excluded.updated_at",
                (session_id, profile_json, time.time()),
            )

    def get_session_profile(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT profile_json FROM session_profile WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return row[0] if row else None

    def set_session_redaction(self, session_id: str, map_json: str) -> None:
        """Persist the session's redaction map (placeholder <-> real value)."""
        with self._write() as c:
            c.execute(
                "INSERT INTO session_redaction (session_id, map_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "map_json=excluded.map_json, updated_at=excluded.updated_at",
                (session_id, map_json, time.time()),
            )

    def get_session_redaction(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT map_json FROM session_redaction WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return row[0] if row else None

    def set_session_trace(self, session_id: str, trace_json: str) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO session_trace (session_id, trace_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "trace_json=excluded.trace_json, updated_at=excluded.updated_at",
                (session_id, trace_json, time.time()),
            )

    def get_session_trace(self, session_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT trace_json FROM session_trace WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return row[0] if row else None

    # ----- case state (CASE_STATE_SPINE_DESIGN P1/P2) -----------------------

    def save_case_state(self, session_id: str, state: dict) -> None:
        """Overwrite the case state for a session. `state` is a dict
        (typically deserialized from the state_json wire contract).
        JSON encoding errors are NOT caught; caller handles validation."""
        with self._write() as c:
            c.execute(
                "INSERT INTO session_case_state "
                "(session_id, state_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "state_json=excluded.state_json, updated_at=excluded.updated_at",
                (session_id, json.dumps(state), time.time()),
            )

    def get_case_state(self, session_id: str) -> dict | None:
        """Recover the case state for a session. Returns None if the row
        does not exist or if the stored state_json is unparseable (fail
        open: returns None rather than raising on corrupt JSON)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT state_json FROM session_case_state WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            # Fail open: return None for unparseable JSON
            return None

    def save_card(self, session_id: str, card: dict) -> None:
        # Most cards key by `id`. An approval_request keys by `approval_id` -- that is the id the widget
        # resumes with, and the id _execute_approved_card looks the card up by.
        cid = card.get("id") or card.get("approval_id")
        if not cid:
            return
        with self._write() as c:
            c.execute(
                "INSERT OR REPLACE INTO session_cards "
                "(session_id, card_id, card_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, cid, json.dumps(card), time.time()),
            )

    def claim_card_decision(self, session_id: str, card_id: str, decision: str,
                            actor: str | None = None) -> dict | None:
        """Claim the decision on a card. None when this call claimed it; the
        earlier decision ({decision, actor, decided_at}) when one already had."""
        with self._write() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO session_card_decisions (session_id, "
                "card_id, decision, actor, decided_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, card_id, decision, actor, time.time()),
            )
            if cur.rowcount == 1:
                return None
            row = c.execute(
                "SELECT decision, actor, decided_at FROM session_card_decisions "
                "WHERE session_id=? AND card_id=?", (session_id, card_id),
            ).fetchone()
        return {"decision": row[0], "actor": row[1], "decided_at": row[2]} if row else None

    def record_card_execution(self, session_id: str, card_id: str, *,
                              connector: str, operation: str,
                              config: str | None, args: dict, ok: bool) -> None:
        """Remember what an approved run_op card ran (the args after any
        analyst edit). A re-execution of the same card replaces the row but
        never clears an undo already claimed."""
        with self._write() as c:
            c.execute(
                "INSERT INTO session_card_executions (session_id, card_id, "
                "connector, operation, config, args_json, ok, executed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, card_id) "
                "DO UPDATE SET connector=excluded.connector, "
                "operation=excluded.operation, config=excluded.config, "
                "args_json=excluded.args_json, ok=excluded.ok, "
                "executed_at=excluded.executed_at",
                (session_id, card_id, connector, operation, config,
                 json.dumps(args, default=str), 1 if ok else 0, time.time()),
            )

    def get_card_execution(self, session_id: str, card_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT connector, operation, config, args_json, ok, executed_at, "
                "undone_by, undone_at, undo_json FROM session_card_executions "
                "WHERE session_id=? AND card_id=?", (session_id, card_id),
            ).fetchone()
        if not row:
            return None
        try:
            args = json.loads(row[3])
        except (TypeError, ValueError):
            args = {}
        try:
            undo = json.loads(row[8]) if row[8] else None
        except (TypeError, ValueError):
            undo = None
        return {"connector": row[0], "operation": row[1], "config": row[2],
                "args": args if isinstance(args, dict) else {},
                "ok": bool(row[4]), "executed_at": row[5],
                "undone_by": row[6], "undone_at": row[7], "undo": undo}

    def claim_card_undo(self, session_id: str, card_id: str,
                        actor: str | None) -> dict | None:
        """Claim the reversal of an executed card. None when this call claimed
        it; the earlier claim ({undone_by, undone_at}) when one already had."""
        with self._write() as c:
            cur = c.execute(
                "UPDATE session_card_executions SET undone_by=?, undone_at=? "
                "WHERE session_id=? AND card_id=? AND undone_at IS NULL",
                (actor or "", time.time(), session_id, card_id),
            )
            if cur.rowcount == 1:
                return None
            row = c.execute(
                "SELECT undone_by, undone_at FROM session_card_executions "
                "WHERE session_id=? AND card_id=?", (session_id, card_id),
            ).fetchone()
        return {"undone_by": row[0], "undone_at": row[1]} if row else None

    def finish_card_undo(self, session_id: str, card_id: str, result: dict,
                         *, release: bool) -> None:
        """Store the reversal's result. `release` (the reversal did not run)
        clears the claim so it can be tried again."""
        with self._write() as c:
            if release:
                c.execute(
                    "UPDATE session_card_executions SET undone_by=NULL, "
                    "undone_at=NULL, undo_json=? WHERE session_id=? AND card_id=?",
                    (json.dumps(result, default=str), session_id, card_id))
            else:
                c.execute(
                    "UPDATE session_card_executions SET undo_json=? "
                    "WHERE session_id=? AND card_id=?",
                    (json.dumps(result, default=str), session_id, card_id))

    def release_card_decision(self, session_id: str, card_id: str) -> None:
        """Undo a claim whose decision did nothing (the card could not be
        resumed), so the analyst can answer it again."""
        with self._write() as c:
            c.execute("DELETE FROM session_card_decisions WHERE session_id=? "
                      "AND card_id=?", (session_id, card_id))

    def card_decision(self, session_id: str, card_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT decision, actor, decided_at FROM session_card_decisions "
                "WHERE session_id=? AND card_id=?", (session_id, card_id),
            ).fetchone()
        return {"decision": row[0], "actor": row[1], "decided_at": row[2]} if row else None

    def list_card_decisions(self, session_id: str) -> list[dict]:
        """Every decided card of the session, oldest first, so a reopened
        chat shows a card someone else answered as answered."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT card_id, decision, actor, decided_at FROM session_card_decisions "
                "WHERE session_id=? ORDER BY decided_at", (session_id,),
            ).fetchall()
        return [{"card_id": r[0], "decision": r[1], "actor": r[2], "decided_at": r[3]}
                for r in rows]

    def audit_decisions(self, since: float, until: float) -> list[dict]:
        """Every card decision in [since, until) with what the approved card
        ran (the executed connector, operation and args) when it ran one."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT d.session_id, d.card_id, d.decision, d.actor, d.decided_at, "
                " k.card_json, e.connector, e.operation, e.args_json, e.ok, e.executed_at "
                "FROM session_card_decisions d "
                "LEFT JOIN session_cards k ON k.session_id=d.session_id AND k.card_id=d.card_id "
                "LEFT JOIN session_card_executions e "
                " ON e.session_id=d.session_id AND e.card_id=d.card_id "
                "WHERE d.decided_at>=? AND d.decided_at<? ORDER BY d.decided_at",
                (since, until)).fetchall()
        out = []
        for (sid, cid, dec, actor, at, card_json, conn, op, args, ok, ex_at) in rows:
            try:
                card = json.loads(card_json) if card_json else {}
            except ValueError:
                card = {}
            out.append({
                "session_id": sid, "card_id": cid, "decision": dec, "actor": actor,
                "decided_at": at, "card_type": card.get("type"),
                "tool": card.get("tool") or card.get("operation"),
                "executed": conn is not None, "connector": conn, "operation": op,
                "args": json.loads(args) if args else None,
                "ok": None if ok is None else bool(ok), "executed_at": ex_at})
        return out

    def audit_sessions(self, since: float, until: float) -> list[dict]:
        """Sessions touched in [since, until): the mounted record, the first
        prompt-bearing activity and who opened the conversation."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT s.session_id, s.record_module, s.record_uuid, s.started_at, "
                " s.updated_at, i.author_json FROM chat_sessions s "
                "LEFT JOIN session_initiator i ON i.session_id=s.session_id "
                "WHERE s.updated_at>=? AND s.started_at<? ORDER BY s.started_at",
                (since, until)).fetchall()
        out = []
        for sid, mod, uuid_, st, up, author in rows:
            try:
                who = json.loads(author) if author else None
            except ValueError:
                who = None
            out.append({"session_id": sid, "record_module": mod, "record_uuid": uuid_,
                        "started_at": st, "updated_at": up, "initiator": who})
        return out

    def shadow_decisions(self, since: float) -> list[dict]:
        """Every stored card since ``since`` that carries an autonomy-policy
        shadow decision, joined with the human decision (if any). Oldest first.
        Feeds the shadow table and the policy's rate counters."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT s.session_id, s.card_id, s.card_json, s.created_at, "
                "d.decision, d.actor, d.decided_at, x.ok, x.undone_by, "
                "x.undone_at FROM session_cards s "
                "LEFT JOIN session_card_decisions d ON d.session_id = s.session_id "
                "AND d.card_id = s.card_id "
                "LEFT JOIN session_card_executions x ON x.session_id = s.session_id "
                "AND x.card_id = s.card_id WHERE s.created_at >= ? "
                "AND s.card_json LIKE '%\"policy\"%' ORDER BY s.created_at",
                (since,),
            ).fetchall()
        out: list[dict] = []
        for (sid, cid, cj, at, decision, actor, decided_at,
             executed_ok, undone_by, undone_at) in rows:
            try:
                card = json.loads(cj)
            except (TypeError, ValueError):
                continue
            pol = card.get("policy")
            if not isinstance(pol, dict) or not pol.get("rule"):
                continue
            out.append({"session_id": sid, "card_id": cid, "at": at,
                        "connector": card.get("connector"),
                        # An approval_request (run_playbook, mcp_*) names
                        # its tool, not an operation.
                        "operation": card.get("operation") or card.get("tool"),
                        "summary": card.get("summary"), "policy": pol,
                        "human": decision, "actor": actor,
                        "decided_at": decided_at,
                        # None: never ran (or ran before executions were
                        # recorded); the Monitor offers undo only on True.
                        "executed": None if executed_ok is None else bool(executed_ok),
                        # Phase E: set when the policy ran the card itself
                        # ({by, rule, ok, verified}); the raw result stays out.
                        "enforced": ({k: v for k, v in card["enforced"].items()
                                      if k != "result"}
                                     if isinstance(card.get("enforced"), dict) else None),
                        "undone_by": undone_by, "undone_at": undone_at})
        return out

    # ----- Monitor: handled-cases data (M1-M5) ----------------------------

    def set_decision_feedback(self, record_iri: str, analyst: str, session_id: str,
                              rating: str | None, note: str | None = None,
                              rule: str | None = None) -> None:
        """Store one analyst's right/wrong on a decision. Idempotent per
        (record, analyst): last write wins. ``rating`` None clears it."""
        r = (rating or "").strip().lower() or None
        if r not in (None, "right", "wrong"):
            raise ValueError("rating must be 'right', 'wrong', or None")
        with self._write() as c:
            if r is None:
                c.execute("DELETE FROM decision_feedback WHERE record_iri=? "
                          "AND analyst=?", (record_iri, analyst))
                return
            c.execute(
                "INSERT INTO decision_feedback (record_iri, analyst, session_id, "
                "rating, note, rule, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(record_iri, analyst) DO UPDATE SET "
                "session_id=excluded.session_id, rating=excluded.rating, "
                "note=excluded.note, rule=excluded.rule, "
                "updated_at=excluded.updated_at",
                (record_iri, analyst, session_id, r, (note or None), rule,
                 time.time()))

    def get_decision_feedback(self, record_iri: str) -> list[dict]:
        """Every analyst's feedback on one record's decision, oldest first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT analyst, rating, note, rule, updated_at FROM "
                "decision_feedback WHERE record_iri=? ORDER BY updated_at",
                (record_iri,)).fetchall()
        return [{"analyst": a, "rating": r, "note": n, "rule": ru, "updated_at": u}
                for a, r, n, ru, u in rows]

    def rule_feedback_counts(self, since: float) -> dict[str, dict]:
        """Per policy rule: how many right and wrong ratings since ``since``."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT rule, rating, COUNT(*) FROM decision_feedback "
                "WHERE rule IS NOT NULL AND updated_at >= ? "
                "GROUP BY rule, rating", (since,)).fetchall()
        out: dict[str, dict] = {}
        for rule, rating, n in rows:
            out.setdefault(rule, {"right": 0, "wrong": 0})[rating] = n
        return out

    def auto_triage_cases(self, since: float, with_transcript: bool = True) -> list[dict]:
        """Every auto-triage session started since ``since``, newest first,
        with what the Monitor needs to describe it: its stored cards, the
        decisions and executions on them, its feedback, and (optionally) the
        assistant frames. A storm member has no session of its own, so it
        never appears here (a stray session for a record grouped under another
        lead is skipped too); a lead carries ``group_size`` (itself plus the
        members attached to it)."""
        with self._conn() as c:
            sessions = c.execute(
                "SELECT session_id, created_at, author_json FROM session_initiator "
                "WHERE session_id LIKE 'auto:%' AND created_at >= ? "
                "ORDER BY created_at DESC", (since,)).fetchall()
            members = {r[0] for r in c.execute(
                "SELECT record_iri FROM triage_log WHERE role='member'")}
            out: list[dict] = []
            for sid, started, author_json in sessions:
                try:
                    author = json.loads(author_json or "{}")
                except (TypeError, ValueError):
                    author = {}
                if not (isinstance(author, dict) and author.get("system")):
                    continue
                parts = sid.split(":", 2)
                if len(parts) == 3 and f"/api/3/{parts[1]}/{parts[2]}" in members:
                    continue
                cards = []
                for raw, at in c.execute(
                        "SELECT card_json, created_at FROM session_cards "
                        "WHERE session_id=? ORDER BY created_at", (sid,)):
                    try:
                        card = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(card, dict):
                        cards.append({**card, "_stored_at": at})
                decisions = {r[0]: {"decision": r[1], "actor": r[2], "decided_at": r[3]}
                             for r in c.execute(
                                 "SELECT card_id, decision, actor, decided_at FROM "
                                 "session_card_decisions WHERE session_id=?", (sid,))}
                execs = {r[0]: {"ok": bool(r[1]), "undone_at": r[2]}
                         for r in c.execute(
                             "SELECT card_id, ok, undone_at FROM "
                             "session_card_executions WHERE session_id=?", (sid,))}
                n = c.execute("SELECT COUNT(*) FROM triage_log WHERE lead_session=?",
                              (sid,)).fetchone()[0]
                feedback = [{"analyst": r[0], "rating": r[1], "note": r[2],
                             "updated_at": r[3]} for r in c.execute(
                                 "SELECT analyst, rating, note, updated_at FROM "
                                 "decision_feedback WHERE session_id=? "
                                 "ORDER BY updated_at", (sid,))]
                frames: list[dict] = []
                if with_transcript:
                    for (pj,) in c.execute(
                            "SELECT payload_json FROM session_transcript WHERE "
                            "session_id=? AND role='assistant' ORDER BY seq", (sid,)):
                        try:
                            frames += json.loads(pj).get("transcript") or []
                        except (TypeError, ValueError, AttributeError):
                            continue
                out.append({"session_id": sid, "started_at": started,
                            "cards": cards, "decisions": decisions,
                            "executions": execs, "group_size": n + 1,
                            "feedback": feedback, "frames": frames})
        return out

    def session_policy_rule(self, session_id: str) -> str | None:
        """The autonomy-policy rule that covered a session's decision, if any."""
        with self._conn() as c:
            rows = c.execute("SELECT card_json FROM session_cards WHERE "
                             "session_id=? AND card_json LIKE '%\"policy\"%' "
                             "ORDER BY created_at", (session_id,)).fetchall()
        for (raw,) in rows:
            try:
                rule = (json.loads(raw).get("policy") or {}).get("rule")
            except (TypeError, ValueError, AttributeError):
                continue
            if rule:
                return rule
        return None

    def triage_deferred_count(self, since: float, until: float | None = None) -> int:
        """Records the storm cap deferred since ``since`` (and before ``until``)."""
        sql = "SELECT COUNT(*) FROM triage_log WHERE role='deferred' AND created_at>=?"
        args: list = [since]
        if until is not None:
            sql += " AND created_at<?"
            args.append(until)
        with self._conn() as c:
            return c.execute(sql, args).fetchone()[0]


    def get_card(self, session_id: str, card_id: str) -> dict | None:
        """Recover a stored card. Fail-soft: a missing OR unparseable row
        returns None (a corrupt card degrades to a conversational resume
        rather than crashing the resume dispatch)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT card_json FROM session_cards "
                "WHERE session_id=? AND card_id=?",
                (session_id, card_id),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None

    # ----- mock-mode replay cursor ---------------------------------------

    def mock_consumed(self, session_id: str, scenario: str) -> list[int]:
        with self._conn() as c:
            row = c.execute(
                "SELECT consumed_json FROM mock_cursor "
                "WHERE session_id=? AND scenario=?",
                (session_id, scenario),
            ).fetchone()
        return json.loads(row[0]) if row else []

    def mock_mark(self, session_id: str, scenario: str, index: int) -> None:
        with self._write() as c:
            row = c.execute(
                "SELECT consumed_json FROM mock_cursor "
                "WHERE session_id=? AND scenario=?",
                (session_id, scenario),
            ).fetchone()
            consumed = json.loads(row[0]) if row else []
            if index not in consumed:
                consumed.append(index)
            c.execute(
                "INSERT INTO mock_cursor "
                "(session_id, scenario, consumed_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id, scenario) DO UPDATE SET "
                "consumed_json=excluded.consumed_json, "
                "updated_at=excluded.updated_at",
                (session_id, scenario, json.dumps(consumed), time.time()),
            )

    # ----- turn progress feed (action-based streaming, contract 2.5.0) ----

    def append_turn_progress(
        self, session_id: str, turn: int, frame: dict,
        terminal: bool = False, stop_reason: str | None = None,
    ) -> bool:
        """Append one progress frame for a streaming chat_turn. `frame` is a
        raw per-event wire dict (_event_to_wire). The terminal frame closes
        the feed for this turn and carries the envelope stop_reason.

        A turn has at most ONE terminal frame. Two writers can finish the same
        turn -- the producer and the poll path's liveness check -- and the
        first one wins: the second is dropped rather than leaving `done` to
        depend on which row a reader happens to see. The turn's `turn_runs`
        status changes in the same transaction. Returns whether this call
        wrote the frame."""
        frame_json = json.dumps(frame, default=str)
        # Write-side for the same reason as `record_chat_message`: the reader
        # (`read_turn_progress`) projects two columns out of this table and
        # never reconstitutes the row, so the insert is the only place the row
        # shape as a whole exists.
        _check_row("TurnProgressRow",
                   {"session_id": session_id, "turn": turn,
                    "frame_json": frame_json,
                    "is_terminal": 1 if terminal else 0,
                    "stop_reason": stop_reason})
        now = time.time()
        with self._write() as c:
            if terminal and c.execute(
                    "SELECT 1 FROM turn_progress WHERE session_id=? AND turn=? "
                    "AND is_terminal=1 LIMIT 1", (session_id, turn)).fetchone():
                landed = False
            else:
                c.execute(
                    "INSERT INTO turn_progress "
                    "(session_id, turn, frame_json, is_terminal, stop_reason, "
                    " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, turn, frame_json,
                     1 if terminal else 0, stop_reason, now),
                )
                landed = True
                if terminal:
                    status = "failed" if stop_reason == "error" else "done"
                    c.execute(
                        "UPDATE turn_runs SET status=?, finished_at=?, "
                        "heartbeat_at=? WHERE session_id=? AND turn=? "
                        "AND status='running'",
                        (status, now, now, session_id, turn))
        if terminal:
            _HEARTBEAT.discard(self.db_path, session_id, turn)
        return landed

    def request_turn_cancel(self, session_id: str, turn: int) -> None:
        """Mark one turn stopped by the analyst (C6). Idempotent."""
        import time as _time
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO turn_cancel "
                "(session_id, turn, requested_at) VALUES (?, ?, ?)",
                (session_id, turn, _time.time()),
            )

    def turn_cancel_requested(self, session_id: str, turn: int) -> bool:
        """True once the analyst stopped this turn (C6)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM turn_cancel WHERE session_id=? AND turn=? LIMIT 1",
                (session_id, turn),
            ).fetchone()
        return row is not None

    def turn_is_terminal(self, session_id: str, turn: int) -> bool:
        """True once a terminal frame has been written for this turn. Lets a
        detached producer verify (backstop) that its `done` frame actually
        landed before the thread exits -- if the terminal append silently
        failed, `done` would never flip and chat_poll would hang."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM turn_progress "
                "WHERE session_id=? AND turn=? AND is_terminal=1 LIMIT 1",
                (session_id, turn),
            ).fetchone()
        return row is not None

    def read_turn_progress(
        self, session_id: str, after_cursor: int = 0, since_turn: int = 0,
    ) -> dict:
        """Read progress frames for a session's CURRENT turn with cursor >
        after_cursor. The "current" turn is the HIGHEST turn STRICTLY GREATER
        than `since_turn`.

        The `since_turn` fence closes a race that scoping to a bare max(turn)
        could not: the widget begins polling CONCURRENTLY with chat_turn, and
        chat_turn does health-check + warmup before it writes the new turn's
        `turn_start`. A first poll that wins that race would otherwise see the
        PREVIOUS, already-terminal turn as max(turn) -- terminal frame, done=true
        -- and the widget would commit that completed turn's transcript as the
        new turn's result (export sess-ei6esw96: a build replayed the prior
        enrichment). With `since_turn` set to the last turn the caller already
        has, a completed prior turn (turn == since_turn) is excluded; until the
        new turn's first frame lands, this returns turn=None/done=false and the
        poller simply keeps polling. since_turn=0 (default) preserves the old
        unfenced behaviour for any caller that doesn't pass it.

        Returns the new cursor watermark, the turn number, and a done flag
        (+ stop_reason) once the terminal frame has been written for that turn."""
        with self._snapshot() as c:
            row = c.execute(
                "SELECT MAX(turn) FROM turn_progress "
                "WHERE session_id=? AND turn>?",
                (session_id, since_turn),
            ).fetchone()
            max_turn = row[0] if row else None
            if max_turn is None:
                return {"frames": [], "cursor": after_cursor, "turn": None,
                        "done": False, "stop_reason": None,
                        "last_activity": None}
            cur = c.execute(
                "SELECT cursor, frame_json FROM turn_progress "
                "WHERE session_id=? AND turn=? AND cursor>? ORDER BY cursor",
                (session_id, max_turn, after_cursor),
            )
            frames: list[dict] = []
            watermark = after_cursor
            for cursor_val, frame_json in cur.fetchall():
                frame = json.loads(frame_json)
                frame["cursor"] = cursor_val
                frames.append(frame)
                watermark = cursor_val
            # `done` is a property of the turn, not of this page: a terminal
            # frame anywhere in the turn (even already consumed) means the
            # turn finished, so a final empty poll still reports done.
            term = c.execute(
                "SELECT stop_reason FROM turn_progress "
                "WHERE session_id=? AND turn=? AND is_terminal=1 LIMIT 1",
                (session_id, max_turn),
            ).fetchone()
            # Newest frame timestamp for the turn (regardless of cursor) -- lets
            # a poller detect a silent/lost producer (e.g. a detached run whose
            # worker was recycled before it could write a terminal frame).
            act = c.execute(
                "SELECT MAX(created_at) FROM turn_progress "
                "WHERE session_id=? AND turn=?",
                (session_id, max_turn),
            ).fetchone()
        return {
            "frames": frames,
            "cursor": watermark,
            "turn": max_turn,
            "done": term is not None,
            "stop_reason": term[0] if term else None,
            "last_activity": act[0] if act else None,
        }

    # ----- turn ownership and liveness -----------------------------------

    def turn_run(self, session_id: str, turn: int) -> dict | None:
        """The owner row for a turn, or None for a turn that predates
        `turn_runs` (its liveness is judged by frame silence alone)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT owner_pid, status, started_at, heartbeat_at, finished_at "
                "FROM turn_runs WHERE session_id=? AND turn=?",
                (session_id, turn),
            ).fetchone()
        if not row:
            return None
        return {"owner_pid": row[0], "status": row[1], "started_at": row[2],
                "heartbeat_at": row[3], "finished_at": row[4]}

    def turn_orphan_reason(self, session_id: str, turn: int, *,
                           last_activity: float | None,
                           silent_after_s: float,
                           now: float | None = None) -> str | None:
        """Why a turn that has no terminal frame should be failed, or None when
        it is still alive. Reasons are keys of TURN_FAILURE_MESSAGES:

          owner_gone       -- the owning worker process no longer exists
          stale_heartbeat  -- the owner exists but has stopped beating
          silent           -- no frame for `silent_after_s` (a hung producer)

        A turn with no `turn_runs` row falls back to the silence rule alone."""
        now = time.time() if now is None else now
        silent = (last_activity is not None
                  and now - last_activity > silent_after_s)
        run = self.turn_run(session_id, turn)
        if run is None:
            return "silent" if silent else None
        if run["status"] != "running":
            return None
        if not _pid_alive(run["owner_pid"]):
            return "owner_gone"
        if now - run["heartbeat_at"] > TURN_STALE_SECS:
            return "stale_heartbeat"
        return "silent" if silent else None

    def fail_turn(self, session_id: str, turn: int, reason: str) -> str | None:
        """Terminate a turn as failed with an analyst-readable error frame.
        Returns the message when this call wrote the terminal frame, None when
        the turn had already finished (the caller should re-read it)."""
        message = TURN_FAILURE_MESSAGES[reason]
        if TERMINAL_ERROR_FRAME is None:
            raise RuntimeError("storage.TERMINAL_ERROR_FRAME is not installed; "
                               "import ops_common first")
        frame = TERMINAL_ERROR_FRAME(message)
        landed = self.append_turn_progress(session_id, turn, frame,
                                           terminal=True, stop_reason="error")
        return message if landed else None

    def sweep_orphaned_turns(self) -> int:
        """Fail every running turn whose owner process is gone. Run once per
        worker start (default_storage), so a turn cut off by a worker restart
        is closed out instead of staying "running" for the widget to poll
        forever. Turns whose owner is alive are left to the poll path."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT session_id, turn, owner_pid FROM turn_runs "
                "WHERE status='running'").fetchall()
        failed = 0
        for session_id, turn, owner_pid in rows:
            if not _pid_alive(owner_pid) and self.fail_turn(
                    session_id, turn, "owner_gone") is not None:
                failed += 1
        return failed

    # ----- render-fidelity transcript (refresh durability) ----------------

    def append_transcript_user(self, session_id: str, text: str,
                               author: dict | None = None) -> None:
        """Record an analyst turn. No-op on empty text (e.g. the opener turn
        carries no user message).

        ``author`` is the acting SOAR user's identity ({name, iri, ...}) supplied
        by the widget -- the connector runs under the service account and cannot
        derive it. Persisted in the payload so history rehydration renders the
        REAL sender (not the current viewer). None when the caller sent no actor
        (older widgets / opener turns) → history falls back to a neutral label."""
        if not text:
            return
        payload = {"text": text}
        if author:
            payload["author"] = author
        with self._write() as c:
            c.execute(
                "INSERT INTO session_transcript "
                "(session_id, role, payload_json, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (session_id, json.dumps(payload, default=str), time.time()),
            )

    def append_transcript_resume(self, session_id: str, choice_id: str,
                                 value: Any, label: str | None = None,
                                 author: dict | None = None) -> None:
        """Record the analyst's answer to a choice card, so history replay can
        show the card as answered. Without it the pick lived only in the
        browser: on reload the card came back unanswered, as if nothing had
        been chosen. Stored as its own role -- it is neither a chat message nor
        an assistant turn, and every role-filtered reader stays unaffected."""
        if not choice_id:
            return
        payload: dict = {"choice_id": str(choice_id), "value": value}
        if label:
            payload["label"] = str(label)
        if author:
            payload["author"] = author
        with self._write() as c:
            c.execute(
                "INSERT INTO session_transcript "
                "(session_id, role, payload_json, created_at) "
                "VALUES (?, 'resume', ?, ?)",
                (session_id, json.dumps(payload, default=str), time.time()),
            )

    def set_session_initiator(self, session_id: str, author: dict) -> None:
        """Record who STARTED this session -- first writer wins (INSERT OR IGNORE),
        so the opener stays the initiator even as others post to the session.
        No-op on empty author. Best-effort; never raises to the caller."""
        if not author:
            return
        with self._write() as c:
            c.execute(
                "INSERT OR IGNORE INTO session_initiator "
                "(session_id, author_json, created_at) VALUES (?, ?, ?)",
                (session_id, json.dumps(author, default=str), time.time()),
            )

    def claim_session(self, session_id: str, author: dict) -> bool:
        """Start a session as `author` only if nobody has started it yet. True
        when this call claimed it, False when it already had an initiator. The
        auto-triage trigger uses it so one record is triaged once, even when two
        fires race."""
        with self._write() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO session_initiator "
                "(session_id, author_json, created_at) VALUES (?, ?, ?)",
                (session_id, json.dumps(author, default=str), time.time()),
            )
            return cur.rowcount == 1

    def route_triage(self, record_iri: str, session_id: str, keys: list[str],
                     window_s: float, cap: int, cap_window_s: float,
                     now: float) -> dict:
        """Decide, atomically, what auto-triage does with a new record: a
        recent lead sharing one of `keys` makes it a member of that lead's
        group; otherwise it leads a new group unless `cap` leads (0 = no cap)
        already started within `cap_window_s`, then it is deferred. Returns
        {role, lead_session, lead_record, members} (members counts the group
        including this record). A record seen before keeps its role, except a
        deferred one, which is decided again."""
        with self._write() as c:
            row = c.execute("SELECT role, session_id, lead_session FROM "
                            "triage_log WHERE record_iri=?",
                            (record_iri,)).fetchone()
            if row and row[0] != "deferred":
                lead = row[2] if row[0] == "member" else row[1]
                return self._group_view(c, row[0], lead, repeat=True)
            c.execute("DELETE FROM triage_log WHERE record_iri=?", (record_iri,))
            for key in keys:
                hit = c.execute(
                    "SELECT lead_session FROM triage_group WHERE group_key=? "
                    "AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                    (key, now - window_s)).fetchone()
                if hit:
                    c.execute("INSERT INTO triage_log VALUES (?,?,?,?,?)",
                              (record_iri, "member", None, hit[0], now))
                    return self._group_view(c, "member", hit[0])
            if cap > 0:
                n = c.execute("SELECT COUNT(*) FROM triage_log WHERE "
                              "role='lead' AND created_at>=?",
                              (now - cap_window_s,)).fetchone()[0]
                if n >= cap:
                    c.execute("INSERT INTO triage_log VALUES (?,?,?,?,?)",
                              (record_iri, "deferred", None, None, now))
                    return {"role": "deferred", "lead_session": None,
                            "lead_record": None, "members": 0}
            c.execute("INSERT INTO triage_log VALUES (?,?,?,?,?)",
                      (record_iri, "lead", session_id, None, now))
            for key in keys:
                c.execute("INSERT OR REPLACE INTO triage_group VALUES (?,?,?,?)",
                          (key, session_id, record_iri, now))
            return self._group_view(c, "lead", session_id)

    @staticmethod
    def _group_view(c, role: str, lead_session: str, repeat: bool = False) -> dict:
        lead = c.execute("SELECT record_iri FROM triage_log WHERE role='lead' "
                         "AND session_id=?", (lead_session,)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM triage_log WHERE lead_session=?",
                      (lead_session,)).fetchone()[0]
        return {"role": role, "lead_session": lead_session,
                "lead_record": lead[0] if lead else None, "members": n + 1,
                "repeat": repeat}

    def get_session_initiator(self, session_id: str) -> dict | None:
        """The session's initiator author dict, or None when unknown (pre-actor
        sessions / opener-only)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT author_json FROM session_initiator WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def set_session_rating(self, session_id: str, rating: str | None,
                           note: str | None = None) -> None:
        """Record an analyst's thumbs up/down on a session so the history
        browser can surface the ones worth investigating. `rating` is
        'up' | 'down' | None (clear). Last write wins (UPSERT)."""
        r = (rating or "").strip().lower() or None
        if r not in (None, "up", "down"):
            raise ValueError("rating must be 'up', 'down', or None")
        with self._write() as c:
            c.execute(
                "INSERT INTO session_rating (session_id, rating, note, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "rating=excluded.rating, note=excluded.note, "
                "updated_at=excluded.updated_at",
                (session_id, r, (note or None), time.time()),
            )

    def get_session_rating(self, session_id: str) -> dict | None:
        """The session's {rating, note} or None when unrated."""
        with self._conn() as c:
            row = c.execute(
                "SELECT rating, note FROM session_rating WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row or not row[0]:
            return None
        return {"rating": row[0], "note": row[1]}

    def append_transcript_assistant(
        self, session_id: str, transcript: list[dict],
    ) -> None:
        """Record an assistant turn as the FULL wire_transcript event array
        (the exact list the widget renders live)."""
        if not transcript:
            return
        with self._write() as c:
            c.execute(
                "INSERT INTO session_transcript "
                "(session_id, role, payload_json, created_at) "
                "VALUES (?, 'assistant', ?, ?)",
                (session_id, json.dumps({"transcript": transcript}, default=str),
                 time.time()),
            )

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """Return stored conversations newest-first, one row per session, for a
        history browser. Each row: {session_id, title, entries, last_activity,
        started_at}. `title` is the session's FIRST user message (trimmed) -- a
        human-readable label -- or a placeholder when the session opened with no
        analyst text (e.g. an opener-only turn)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT session_id, COUNT(*) AS entries, "
                "       MIN(created_at) AS started_at, MAX(created_at) AS last_activity "
                "FROM session_transcript GROUP BY session_id "
                "ORDER BY last_activity DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out: list[dict] = []
            for session_id, entries, started_at, last_activity in rows:
                out.append(self._session_summary_row(
                    c, session_id, entries, started_at, last_activity))
        return out

    def _session_summary_row(self, c, session_id: str, entries, started_at,
                             last_activity) -> dict:
        """One history-browser row for a session (shared by list_sessions and
        search_sessions so the two can't drift). `title` = the first user message."""
        first = c.execute(
            "SELECT payload_json FROM session_transcript "
            "WHERE session_id=? AND role='user' ORDER BY seq LIMIT 1",
            (session_id,),
        ).fetchone()
        title = ""
        if first:
            try:
                title = (json.loads(first[0]).get("text") or "").strip()
            except Exception:
                title = ""
        return {
            "session_id": session_id,
            "title": title or "(no message)",
            "entries": entries,
            "started_at": started_at,
            "last_activity": last_activity,
            # Who started it (real initiator), so the history browser can
            # label each row by owner. None on pre-actor sessions.
            "initiator": self.get_session_initiator(session_id),
            # Analyst 👍/👎 so the browser can filter flagged sessions.
            # None when unrated; else {'rating': 'up'|'down', 'note': ...}.
            "rating": self.get_session_rating(session_id),
        }

    @staticmethod
    def _payload_plain_text(payload_json: str) -> str:
        """The human-readable text of a transcript row (user text, or the assistant
        turn's event texts joined) -- for content search + snippets, without the
        JSON scaffolding matching on keys."""
        try:
            payload = json.loads(payload_json)
        except Exception:
            return payload_json or ""
        if not isinstance(payload, dict):
            return str(payload)
        if payload.get("text"):
            return str(payload["text"])
        parts: list[str] = []
        for ev in (payload.get("transcript") or []):
            if not isinstance(ev, dict):
                continue
            for k in ("text", "display", "question", "title", "summary",
                      "name", "missing", "error"):
                v = ev.get(k)
                if isinstance(v, str) and v:
                    parts.append(v)
        return " ".join(parts)

    @staticmethod
    def _match_snippet(text: str, query: str, width: int = 60) -> str:
        """A short excerpt of `text` centred on the first (case-insensitive) match
        of `query`, with ellipses -- so a search hit shows WHY it matched."""
        if not text or not query:
            return ""
        low = text.lower()
        i = low.find(query.lower())
        if i < 0:
            return ""
        start = max(0, i - width)
        end = min(len(text), i + len(query) + width)
        snip = text[start:end].strip()
        if start > 0:
            snip = "… " + snip
        if end < len(text):
            snip = snip + " …"
        return snip

    def search_sessions(self, query: str, limit: int = 50) -> list[dict]:
        """Sessions whose transcript CONTENT contains `query` (case-insensitive
        substring), newest-first -- the history browser's "search WITHIN sessions"
        (find a keyword like ``FIXME`` across conversations, not just titles). Each
        row is the :meth:`list_sessions` shape PLUS ``snippet``: an excerpt of the
        first matching message. Blank query → ``[]`` (caller shows the recent list).

        Matches against the raw stored payload (so a hit is never missed), but the
        title/snippet are derived from the plain message text so keys/JSON noise
        don't leak into what the analyst sees."""
        q = (query or "").strip()
        if not q:
            return []
        # Escape LIKE wildcards so a literal % / _ in the keyword matches itself.
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        with self._conn() as c:
            rows = c.execute(
                "SELECT session_id, COUNT(*) AS entries, "
                "       MIN(created_at) AS started_at, MAX(created_at) AS last_activity "
                "FROM session_transcript GROUP BY session_id "
                "HAVING SUM(CASE WHEN payload_json LIKE ? ESCAPE '\\' "
                "                 COLLATE NOCASE THEN 1 ELSE 0 END) > 0 "
                "ORDER BY last_activity DESC LIMIT ?",
                (like, limit),
            ).fetchall()
            out: list[dict] = []
            for session_id, entries, started_at, last_activity in rows:
                row = self._session_summary_row(
                    c, session_id, entries, started_at, last_activity)
                # First matching row's plain text → snippet.
                hit = c.execute(
                    "SELECT payload_json FROM session_transcript "
                    "WHERE session_id=? AND payload_json LIKE ? ESCAPE '\\' "
                    "COLLATE NOCASE ORDER BY seq LIMIT 1",
                    (session_id, like),
                ).fetchone()
                snippet = ""
                if hit:
                    snippet = self._match_snippet(
                        self._payload_plain_text(hit[0]), q)
                row["snippet"] = snippet
                out.append(row)
        return out

    def list_transcript(self, session_id: str, limit: int = 200) -> list[dict]:
        """Return the session's visible exchanges in CHRONOLOGICAL order,
        capped to the most recent `limit` rows. Each entry is
        `{user: str, ts: float}` or `{transcript: [...], ts: float}` -- plus
        `author` on a user row that was written with one. This is the shape the
        widget's rehydrate loop consumes (it appends a user message and/or an
        assistant message per entry, in order)."""
        with self._conn() as c:
            cur = c.execute(
                "SELECT role, payload_json, created_at FROM session_transcript "
                "WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                (session_id, limit),
            )
            rows = cur.fetchall()
        turns: list[dict] = []
        for role, payload_json, created_at in reversed(rows):  # back to chronological
            payload = json.loads(payload_json)
            if role == "user":
                _check_row("TranscriptUserPayload", payload)
                # `ts` is the ORIGINAL send time (epoch seconds) so rehydration
                # shows when the chat actually happened, not the refresh time;
                # `author` is the real sender, absent on pre-actor rows.
                entry = {"user": payload.get("text", ""), "ts": created_at}
                if payload.get("author"):
                    entry["author"] = payload["author"]
                turns.append(entry)
            elif role == "resume":
                _check_row("TranscriptResumePayload", payload)
                entry = {"resume": {k: payload[k] for k in
                                    ("choice_id", "value", "label")
                                    if k in payload},
                         "ts": created_at}
                if payload.get("author"):
                    entry["author"] = payload["author"]
                turns.append(entry)
            else:
                _check_row("TranscriptAssistantPayload", payload)
                turns.append({"transcript": payload.get("transcript", []),
                              "ts": created_at})
        return turns

    def list_turns(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT turn, model, input_tokens, output_tokens, "
                " stop_reason, tags_json, tool_calls_json, created_at "
                "FROM chat_turns WHERE session_id=? "
                "ORDER BY turn DESC LIMIT ?",
                (session_id, limit),
            )
            cols = [d[0] for d in cur.description]
            out = [
                {**dict(zip(cols, row)),
                 "tags": json.loads(row[cols.index("tags_json")] or "{}"),
                 "tool_calls": json.loads(row[cols.index("tool_calls_json")] or "[]")}
                for row in cur.fetchall()
            ]
        # `session_id` is not SELECTed (it is the query key, identical on every
        # row), so it is supplied here rather than widening the projection --
        # ChatTurnRow describes the stored ROW, and dropping it from the model
        # would make the model describe the query instead of the table.
        for row_dict in out:
            _check_row("ChatTurnRow", {"session_id": session_id, **row_dict})
        return out

    # ----- Scheduled tasks -------------------------------------------------

    def upsert_scheduled_task(self, task: dict) -> None:
        """Insert or replace a scheduled-task definition row keyed by `name`."""
        now = time.time()
        with self._write() as c:
            c.execute(
                "INSERT INTO scheduled_tasks "
                "(name, cron, intent, prompt, allow_mutating, agent_config, "
                " wf_iri, wf_uuid, collection_uuid, timezone, enabled, "
                " notify_target, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                " cron=excluded.cron, intent=excluded.intent, "
                " prompt=excluded.prompt, "
                " allow_mutating=excluded.allow_mutating, "
                " agent_config=excluded.agent_config, "
                " wf_iri=excluded.wf_iri, wf_uuid=excluded.wf_uuid, "
                " collection_uuid=excluded.collection_uuid, "
                " timezone=excluded.timezone, enabled=excluded.enabled, "
                " notify_target=excluded.notify_target, "
                " updated_at=excluded.updated_at",
                (
                    task["name"], task["cron"], task["intent"], task["prompt"],
                    int(bool(task.get("allow_mutating"))),
                    task.get("agent_config"),
                    task.get("wf_iri"), task.get("wf_uuid"),
                    task.get("collection_uuid"),
                    task.get("timezone", "UTC"),
                    int(bool(task.get("enabled", True))),
                    task.get("notify_target"),
                    task.get("created_at", now), now,
                ),
            )

    def get_scheduled_task(self, name: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT name, cron, intent, prompt, allow_mutating, "
                " agent_config, wf_iri, wf_uuid, collection_uuid, timezone, "
                " enabled, notify_target, created_at, updated_at "
                "FROM scheduled_tasks WHERE name=?",
                (name,),
            ).fetchone()
        if not row:
            return None
        cols = ["name", "cron", "intent", "prompt", "allow_mutating",
                "agent_config", "wf_iri", "wf_uuid", "collection_uuid",
                "timezone", "enabled", "notify_target", "created_at", "updated_at"]
        out = dict(zip(cols, row))
        out["allow_mutating"] = bool(out["allow_mutating"])
        out["enabled"] = bool(out["enabled"])
        return out

    def list_scheduled_tasks(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, cron, intent, prompt, allow_mutating, "
                " agent_config, wf_iri, wf_uuid, collection_uuid, timezone, "
                " enabled, notify_target, created_at, updated_at "
                "FROM scheduled_tasks ORDER BY updated_at DESC",
            ).fetchall()
        cols = ["name", "cron", "intent", "prompt", "allow_mutating",
                "agent_config", "wf_iri", "wf_uuid", "collection_uuid",
                "timezone", "enabled", "notify_target", "created_at", "updated_at"]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["allow_mutating"] = bool(d["allow_mutating"])
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def delete_scheduled_task(self, name: str) -> bool:
        """Delete a task def row; return True if a row was removed."""
        with self._write() as c:
            cur = c.execute(
                "DELETE FROM scheduled_tasks WHERE name=?", (name,),
            )
            return cur.rowcount > 0

    def list_scheduled_task_runs(self, name: str, limit: int = 25) -> list[dict]:
        """Return the runs (one per cron fire) of a scheduled task, newest-first.
        Each fire creates a ``chat_turn`` with ``session_id`` ``sched:<name>:<uuid>``
        (see operations._compile_runner_playbook), so a task's runs are the stored
        sessions whose id has that prefix. Each run row is the :meth:`list_sessions`
        summary shape PLUS ``stop_reason`` (the last turn's terminal), ``verdict``
        (the plain text of the last assistant transcript), and ``parked`` (a HITL
        gate suspended this session and it is resumable in chat).

        Pure read from the session tables (no scheduler transport), so it works
        hermetically -- the widget's history drawer renders run transcripts and a
        'Resume' handoff for parked runs without touching the live box."""
        prefix = f"sched:{name}:"
        with self._conn() as c:
            rows = c.execute(
                "SELECT session_id, COUNT(*) AS entries, "
                "       MIN(created_at) AS started_at, "
                "       MAX(created_at) AS last_activity "
                "FROM session_transcript GROUP BY session_id "
                "ORDER BY last_activity DESC",
            ).fetchall()
            runs: list[dict] = []
            for session_id, entries, started_at, last_activity in rows:
                if not session_id or not session_id.startswith(prefix):
                    continue
                base = self._session_summary_row(
                    c, session_id, entries, started_at, last_activity)
                sr = c.execute(
                    "SELECT stop_reason FROM chat_turns WHERE session_id=? "
                    "ORDER BY turn DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                base["stop_reason"] = sr[0] if sr else None
                verdict = ""
                last_asst = c.execute(
                    "SELECT payload_json FROM session_transcript "
                    "WHERE session_id=? AND role='assistant' "
                    "ORDER BY seq DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if last_asst:
                    verdict = self._payload_plain_text(last_asst[0]).strip()
                base["verdict"] = verdict
                parked = c.execute(
                    "SELECT 1 FROM suspended_sessions WHERE session_id=? LIMIT 1",
                    (session_id,),
                ).fetchone() is not None
                base["parked"] = parked
                runs.append(base)
                if len(runs) >= limit:
                    break
        return runs

    def last_assistant_frames(self, session_id: str) -> list[dict]:
        """The structured frames (verdict card, approvals, ...) of the session's
        last assistant row, so a caller can decide from fields, not prose."""
        with self._conn() as c:
            row = c.execute(
                "SELECT payload_json FROM session_transcript "
                "WHERE session_id=? AND role='assistant' "
                "ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
        if not row:
            return []
        try:
            payload = json.loads(row[0])
        except Exception:  # noqa: BLE001
            return []
        frames = payload.get("transcript") if isinstance(payload, dict) else None
        return [f for f in (frames or []) if isinstance(f, dict)]

    # ----- Scheduled hunts (X6) --------------------------------------------

    _HUNT_COLS = ("name", "question", "cron", "timezone", "module", "enabled",
                  "agent_config", "wf_iri", "wf_uuid", "collection_uuid",
                  "case_iri", "created_by", "created_at", "updated_at")
    _HUNT_RUN_COLS = ("id", "hunt_name", "session_id", "started_at", "finished_at",
                      "outcome", "disposition", "severity", "confidence", "summary",
                      "case_iri", "case_action", "waiting", "error")

    def upsert_hunt(self, hunt: dict) -> None:
        """Insert or replace a hunt definition keyed by `name`. The case link
        and creation stamp survive an edit."""
        now = time.time()
        with self._write() as c:
            c.execute(
                "INSERT INTO hunts (name, question, cron, timezone, module, enabled, "
                " agent_config, wf_iri, wf_uuid, collection_uuid, case_iri, "
                " created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                " question=excluded.question, cron=excluded.cron, "
                " timezone=excluded.timezone, module=excluded.module, "
                " enabled=excluded.enabled, agent_config=excluded.agent_config, "
                " wf_iri=excluded.wf_iri, wf_uuid=excluded.wf_uuid, "
                " collection_uuid=excluded.collection_uuid, "
                " updated_at=excluded.updated_at",
                (hunt["name"], hunt["question"], hunt["cron"],
                 hunt.get("timezone", "UTC"), hunt.get("module", "alerts"),
                 int(bool(hunt.get("enabled", True))), hunt.get("agent_config"),
                 hunt.get("wf_iri"), hunt.get("wf_uuid"), hunt.get("collection_uuid"),
                 hunt.get("case_iri"), hunt.get("created_by"),
                 hunt.get("created_at", now), now))

    def _hunt_row(self, row) -> dict:
        d = dict(zip(self._HUNT_COLS, row))
        d["enabled"] = bool(d["enabled"])
        return d

    def get_hunt(self, name: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(f"SELECT {', '.join(self._HUNT_COLS)} FROM hunts "
                            "WHERE name=?", (name,)).fetchone()
        return self._hunt_row(row) if row else None

    def list_hunts(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(f"SELECT {', '.join(self._HUNT_COLS)} FROM hunts "
                             "ORDER BY name COLLATE NOCASE").fetchall()
        return [self._hunt_row(r) for r in rows]

    def set_hunt_case(self, name: str, case_iri: str | None) -> None:
        with self._write() as c:
            c.execute("UPDATE hunts SET case_iri=? WHERE name=?", (case_iri, name))

    def delete_hunt(self, name: str) -> bool:
        """Delete a hunt and its run history; True if the definition existed."""
        with self._write() as c:
            c.execute("DELETE FROM hunt_runs WHERE hunt_name=?", (name,))
            return c.execute("DELETE FROM hunts WHERE name=?",
                             (name,)).rowcount > 0

    def record_hunt_run(self, run: dict) -> int:
        with self._write() as c:
            cur = c.execute(
                "INSERT INTO hunt_runs (hunt_name, session_id, started_at, "
                " finished_at, outcome, disposition, severity, confidence, summary, "
                " case_iri, case_action, waiting, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run["hunt_name"], run["session_id"], run["started_at"],
                 run["finished_at"], run["outcome"], run.get("disposition"),
                 run.get("severity"), run.get("confidence"), run.get("summary"),
                 run.get("case_iri"), run.get("case_action") or "none",
                 int(run.get("waiting") or 0), run.get("error")))
            return int(cur.lastrowid)

    def list_hunt_runs(self, name: str | None = None, limit: int = 25,
                       since: float | None = None,
                       until: float | None = None) -> list[dict]:
        """Hunt runs newest first, optionally one hunt's and/or inside a window
        (`started_at` in [since, until))."""
        where, args = [], []
        if name is not None:
            where.append("hunt_name=?")
            args.append(name)
        if since is not None:
            where.append("started_at>=?")
            args.append(since)
        if until is not None:
            where.append("started_at<?")
            args.append(until)
        sql = f"SELECT {', '.join(self._HUNT_RUN_COLS)} FROM hunt_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(sql, (*args, int(limit))).fetchall()
        return [dict(zip(self._HUNT_RUN_COLS, r)) for r in rows]


    # ----- Outbound notifications ------------------------------------------

    def claim_notification(self, kind: str, ref: str, trigger_code: str,
                           subject: str) -> int | None:
        """Claim the one notification for (kind, ref). Returns its row id, or
        None when it was already claimed (so the caller sends nothing)."""
        with self._write() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO notifications "
                "(kind, ref, trigger_code, subject, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (kind, ref, trigger_code, subject, time.time()))
            return int(cur.lastrowid) if cur.rowcount else None

    def finish_notification(self, nid: int, status: str, channel: str | None,
                            detail: str | None) -> None:
        with self._write() as c:
            c.execute("UPDATE notifications SET status=?, channel=?, detail=? "
                      "WHERE id=?", (status, channel, detail, nid))

    def list_notifications(self, since: float | None = None,
                           until: float | None = None,
                           limit: int = 200) -> list[dict]:
        cols = ("id", "kind", "ref", "trigger_code", "subject", "status",
                "channel", "detail", "created_at")
        where, args = [], []
        if since is not None:
            where.append("created_at>=?")
            args.append(since)
        if until is not None:
            where.append("created_at<?")
            args.append(until)
        sql = f"SELECT {', '.join(cols)} FROM notifications"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(sql, (*args, int(limit))).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ----- Agent usage telemetry ------------------------------------------

    def record_agent_usage(self, record: dict) -> int | None:
        """Append one LLM-turn telemetry row. Best-effort -- swallow errors so
        telemetry never breaks chat. `record` mirrors the UsageEvent shape
        (provider.py:106) plus the audit prompt/response. Returns the inserted
        row id (so the caller can link per-tool-call rows to it), or None on
        failure."""
        try:
            with self._write() as c:
                cur = c.execute(
                    "INSERT INTO agent_usage "
                    "(ts, session_id, user_iri, user_name, intent, model, "
                    " turn, input_tokens, output_tokens, cache_read, "
                    " cache_write, cost_usd, latency_ms, stop_reason, "
                    " status, error, tool_calls_json, tags_json, prompt, "
                    " response, agent_thinking, approvals_count, "
                    " activity_log_iri, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    " ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.get("ts"), record.get("session_id"),
                        record.get("user_iri"), record.get("user_name"),
                        record.get("intent"), record.get("model"),
                        record.get("turn"),
                        record.get("input_tokens"), record.get("output_tokens"),
                        record.get("cache_read", 0), record.get("cache_write", 0),
                        record.get("cost_usd"), record.get("latency_ms"),
                        record.get("stop_reason"),
                        record.get("status", "success"), record.get("error"),
                        json.dumps(record.get("tool_calls") or []),
                        json.dumps(record.get("tags") or {}),
                        record.get("prompt"), record.get("response"),
                        record.get("agent_thinking"),
                        record.get("approvals_count", 0),
                        record.get("activity_log_iri"),
                        time.time(),
                    ),
                )
                return cur.lastrowid
        except Exception as e:  # telemetry must never break a turn
            _log_store_error("record_agent_usage", e)
            return None

    def record_agent_tool_calls(self, usage_id: int | None,
                                turn: int | None,
                                tool_calls: list[dict]) -> None:
        """Append per-tool-call detail rows linked to one agent_usage row
        (agentic-assistant-monitor §10.1). `usage_id` is the parent row id from
        `record_agent_usage`; `turn` is the connector turn_idx. Each entry:
        {tool_name, params, result_status, result_summary, latency_ms}.
        Best-effort -- telemetry never breaks chat. No-op when usage_id is
        None (the parent row failed to insert)."""
        if not usage_id or not tool_calls:
            return
        try:
            now = time.time()
            rows = []
            for i, tc in enumerate(tool_calls):
                rows.append((
                    usage_id, turn, i,
                    tc.get("tool_name") or tc.get("name") or "",
                    json.dumps(tc.get("params"), default=str)
                        if tc.get("params") is not None else None,
                    tc.get("result_status") or "success",
                    tc.get("result_summary"),
                    tc.get("latency_ms"),
                    now,
                ))
            with self._write() as c:
                c.executemany(
                    "INSERT INTO agent_tool_calls "
                    "(usage_id, turn, tool_call_index, tool_name, params_json, "
                    " result_status, result_summary, latency_ms, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except Exception as e:  # telemetry must never break a turn
            _log_store_error("record_agent_tool_calls", e)

    def list_agent_usage(self, filters: dict | None = None,
                         limit: int = 200) -> list[dict]:
        """Read usage rows newest-first. `filters` may carry from/to (ISO
        8601), user_iri, model, intent, session_id, status."""
        filters = filters or {}
        where: list[str] = []
        args: list = []
        for col in ("session_id", "user_iri", "model", "intent", "status"):
            if filters.get(col):
                where.append(f"{col} = ?")
                args.append(filters[col])
        if filters.get("from"):
            where.append("ts >= ?")
            args.append(filters["from"])
        if filters.get("to"):
            where.append("ts <= ?")
            args.append(filters["to"])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as c:
            cur = c.execute(
                "SELECT id, ts, session_id, user_iri, user_name, intent, model, "
                " turn, input_tokens, output_tokens, cache_read, cache_write, "
                " cost_usd, latency_ms, stop_reason, status, error, "
                " tool_calls_json, tags_json, prompt, response, "
                " agent_thinking, approvals_count, activity_log_iri "
                f"FROM agent_usage{clause} ORDER BY created_at DESC LIMIT ?",
                (*args, limit),
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            out = []
            usage_ids = []
            for row in rows:
                d = dict(zip(cols, row))
                usage_ids.append(d.pop("id"))
                d["tool_calls"] = json.loads(d.pop("tool_calls_json") or "[]")
                d["tags"] = json.loads(d.pop("tags_json") or "{}")
                out.append(d)
            # Nest the per-tool-call detail rows (§10.1) -- one batched fetch
            # for all returned usage rows, grouped by usage_id. Cheap (a few
            # rows per turn) and lets the audit trail render the expandable
            # tool chain without a second round-trip per row.
            if usage_ids:
                placeholders = ",".join("?" * len(usage_ids))
                tc_rows = c.execute(
                    "SELECT usage_id, tool_name, params_json, result_status, "
                    "       result_summary, latency_ms, tool_call_index, turn "
                    f"FROM agent_tool_calls WHERE usage_id IN ({placeholders}) "
                    "ORDER BY usage_id, tool_call_index",
                    usage_ids,
                ).fetchall()
                by_usage: dict[int, list[dict]] = {}
                for (uid, name, pj, st, summ, lat, idx, trn) in tc_rows:
                    by_usage.setdefault(uid, []).append({
                        "tool_name": name,
                        "params": json.loads(pj) if pj else None,
                        "result_status": st,
                        "result_summary": summ,
                        "latency_ms": lat,
                        "index": idx,
                        "turn": trn,
                    })
                for i, uid in enumerate(usage_ids):
                    out[i]["tool_call_detail"] = by_usage.get(uid, [])
            else:
                for d in out:
                    d["tool_call_detail"] = []
            return out

    def usage_summary(self, bucket: str = "day",
                      metric: str = "tokens",
                      group_by: str | None = None,
                      from_ts: str | None = None,
                      to_ts: str | None = None) -> list[dict]:
        """Aggregate usage into time buckets. `bucket` = hour|day,
        `metric` = tokens|cost|turns, `group_by` = model|intent|user_iri|None.
        Returns [{bucket, group, value}]."""
        if bucket == "hour":
            fmt = "%Y-%m-%dT%H:00:00"
        elif bucket == "day":
            fmt = "%Y-%m-%d"
        else:
            fmt = "%Y-%m-%d"
        metric_sql = {
            "tokens": "SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0))",
            "cost": "SUM(COALESCE(cost_usd,0))",
            "turns": "COUNT(*)",
        }.get(metric, "COUNT(*)")
        group_col = {
            "model": "model", "intent": "intent", "user_iri": "user_iri",
        }.get(group_by, "") if group_by else ""
        select = (f"strftime('{fmt}', ts) AS bucket, "
                  f"{metric_sql} AS value")
        if group_col:
            select += f", {group_col} AS grp"
        where: list[str] = []
        args: list = []
        if from_ts:
            where.append("ts >= ?")
            args.append(from_ts)
        if to_ts:
            where.append("ts <= ?")
            args.append(to_ts)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        group_clause = f" GROUP BY bucket{', grp' if group_col else ''} ORDER BY bucket"
        sql = f"SELECT {select} FROM agent_usage{clause}{group_clause}"
        with self._conn() as c:
            cur = c.execute(sql, args)
            out = []
            for row in cur.fetchall():
                d = {"bucket": row[0], "value": row[1]}
                if group_col:
                    d["group"] = row[2]
                out.append(d)
            return out

    # ----- Live sessions (agentic-assistant-monitor §10.2) ---------------------

    def upsert_chat_session(self, session_id: str, record_uuid: str | None = None,
                            record_module: str | None = None,
                            current_prompt: str | None = None) -> None:
        """Record that a session was touched on a chat_turn. `started_at` is
        first-writer-wins (the session opener owns it); the descriptive fields
        (record_uuid, record_module, current_prompt) and `updated_at` are
        last-writer-wins. Best-effort -- never raises to the turn."""
        try:
            now = time.time()
            with self._write() as c:
                c.execute(
                    "INSERT INTO chat_sessions "
                    "(session_id, record_uuid, record_module, current_prompt, "
                    " started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "record_uuid=excluded.record_uuid, "
                    "record_module=excluded.record_module, "
                    "current_prompt=excluded.current_prompt, "
                    "updated_at=excluded.updated_at",
                    (session_id, record_uuid, record_module,
                     current_prompt, now, now),
                )
        except Exception as e:  # telemetry must never break a turn
            _log_store_error("upsert_chat_session", e)

    def get_chat_session(self, session_id: str) -> dict | None:
        """One chat_sessions row (record_uuid, record_module, current_prompt,
        started_at, updated_at) or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT record_uuid, record_module, current_prompt, "
                "started_at, updated_at FROM chat_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "record_uuid": row[0],
            "record_module": row[1],
            "current_prompt": row[2],
            "started_at": row[3],
            "updated_at": row[4],
        }

    def transcript_author(self, session_id: str) -> dict | None:
        """Derive the acting user from the first user message in the session
        transcript. Falls back to this when session_initiator is empty (the
        chat widget sent no actor on the opening turn)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT payload_json FROM session_transcript "
                "WHERE session_id=? AND role='user' "
                "ORDER BY seq ASC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            payload = json.loads(row[0])
            return payload.get("author") or None
        except Exception:
            return None

    def list_active_sessions(self, limit: int = 50,  # noqa: C901
                             status: str | None = None,
                             user_iri: str | None = None) -> list[dict]:
        """Live agentic sessions with a DERIVED status, newest-first. One row
        per session in `chat_sessions`, enriched with the acting user (initiator),
        the mounted record, turn count, and last activity. Status is derived --
        never stored -- so it can't drift:

          active            -- a turn is currently streaming: the session's
                              highest turn in `turn_progress` has NO terminal
                              frame yet.
          waiting_approval -- parked at a HITL gate: the session is in
                              `suspended_sessions`.
          idle              -- completed / not currently streaming and not parked.

        `waiting_approval` wins over `active` (a parked session is not also
        "active" even if a stale non-terminal frame lingers). Returns one row:
        {session_id, status, user, record_uuid, record_module, current_prompt,
        turn_count, started_at, last_activity}.

        Optional `status` / `user_iri` filter the result (the monitor widget's
        drill-downs). Because status is DERIVED after the query, filtering
        happens post-derivation and `limit` is applied last -- otherwise a
        SQL-side LIMIT would slice the candidate set before the predicate ran
        and a filtered view could miss matching sessions."""
        # Bound to a local: the row loop below rebinds `status` to each
        # session's DERIVED status, which would otherwise clobber the filter.
        status_filter = status
        filtering = bool(status_filter or user_iri)
        # Widen the candidate window when filtering so `limit` still yields up
        # to `limit` MATCHING rows rather than matches within the first N.
        scan = max(limit, 500) if filtering else limit
        with self._snapshot() as c:
            rows = c.execute(
                "SELECT cs.session_id, cs.record_uuid, cs.record_module, "
                "       cs.current_prompt, cs.started_at, cs.updated_at, "
                "       si.author_json "
                "FROM chat_sessions cs "
                "LEFT JOIN session_initiator si ON si.session_id = cs.session_id "
                "ORDER BY cs.updated_at DESC LIMIT ?",
                (scan,),
            ).fetchall()
            # In-flight sessions: highest turn has no terminal frame. Joined to
            # each session's last turn, not a per-row correlated MAX(turn):
            # that form re-scanned the session for every frame and took 9.6s
            # over 167k frames on a lab box (the Monitor's request timed out
            # and its Sessions panel read empty); the join takes 0.15s.
            inflight = {r[0] for r in c.execute(
                "SELECT tp.session_id FROM turn_progress tp "
                "JOIN (SELECT session_id, MAX(turn) AS turn FROM turn_progress "
                "      GROUP BY session_id) last "
                "  ON last.session_id = tp.session_id AND last.turn = tp.turn "
                "GROUP BY tp.session_id "
                "HAVING MAX(tp.is_terminal) = 0"
            ).fetchall()}
            # HITL-parked sessions, with the age of the NEWEST gate per session.
            #
            # A suspended row is only removed when the approval is actually
            # answered (`SuspendedStore.pop`) or when it ages out at TTL. An
            # analyst who ignores the gate and simply types the next message
            # leaves the row behind -- so a bare `SELECT DISTINCT session_id`
            # reports "waiting_approval" for a conversation that demonstrably
            # moved on, which is exactly how the monitor came to show pending
            # sessions with nothing pending in them.
            #
            # The gate is therefore only live if NOTHING has happened in the
            # session since it was raised. Any newer turn_progress frame means
            # the agent ran again past the gate, which only happens once the
            # analyst has moved on; the stale row is ignored for status (its
            # deletion is left to the TTL gc, which owns that lifecycle).
            parked_at = {
                r[0]: r[1] for r in c.execute(
                    "SELECT session_id, MAX(created_at) FROM suspended_sessions "
                    "GROUP BY session_id"
                ).fetchall()
            }
            # Per-session turn count + last activity from the streaming feed.
            stats = {
                r[0]: {"turn_count": r[1], "last_activity": r[2]}
                for r in c.execute(
                    "SELECT session_id, MAX(turn), MAX(created_at) "
                    "FROM turn_progress GROUP BY session_id"
                ).fetchall()
            }
        out: list[dict] = []
        for (sid, rec_uuid, rec_mod, prompt, started_at, updated_at,
             author_json) in rows:
            st = stats.get(sid, {})
            if _gate_is_live(parked_at.get(sid), st.get("last_activity")):
                status = "waiting_approval"
            elif sid in inflight:
                status = "active"
            else:
                status = "idle"
            user = None
            if author_json:
                try:
                    user = json.loads(author_json)
                except Exception:
                    user = None
            # Fallback: when no initiator row exists (the chat widget sent
            # no actor on the opening turn), try to derive the user from
            # the first user message in the session transcript.
            if user is None:
                try:
                    row = c.execute(
                        "SELECT payload_json FROM session_transcript "
                        "WHERE session_id=? AND role='user' "
                        "ORDER BY seq ASC LIMIT 1",
                        (sid,),
                    ).fetchone()
                    if row and row[0]:
                        payload = json.loads(row[0])
                        user = payload.get("author") or None
                except Exception:
                    pass
            if status_filter and status != status_filter:
                continue
            if user_iri and (user or {}).get("iri") != user_iri:
                continue
            out.append({
                "session_id": sid,
                "status": status,
                "user": user,
                "record_uuid": rec_uuid,
                "record_module": rec_mod,
                "current_prompt": prompt,
                "turn_count": st.get("turn_count") or 0,
                "started_at": started_at,
                # Prefer the streaming-feed watermark (covers resume turns,
                # which write frames but skip the chat_turn hook); fall back to
                # the chat_turn touch time for sessions with no feed rows yet.
                "last_activity": st.get("last_activity") or updated_at,
            })
            if len(out) >= limit:
                break
        return out


    # ----- suspended HITL sessions (raw store; the gateway pickles) -------

    def suspend_put(self, approval_id: str, session_id: str, payload: bytes,
                    created_at: float) -> None:
        with self._write() as c:
            c.execute(
                "INSERT OR REPLACE INTO suspended_sessions "
                "(approval_id, session_id, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (approval_id, session_id, payload, created_at))

    def suspend_get(self, approval_id: str) -> bytes | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT payload FROM suspended_sessions WHERE approval_id=?",
                (approval_id,)).fetchone()
        return bytes(row[0]) if row else None

    def suspend_take(self, approval_id: str) -> bytes | None:
        """Read AND delete one suspended session in one write transaction.
        Single-use: of N processes asking for the same approval, exactly one
        gets the payload; the rest get None."""
        with self._write() as c:
            row = c.execute(
                "SELECT payload FROM suspended_sessions WHERE approval_id=?",
                (approval_id,)).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM suspended_sessions WHERE approval_id=?",
                      (approval_id,))
        return bytes(row[0])

    def suspend_expire(self, cutoff: float) -> None:
        with self._write() as c:
            c.execute("DELETE FROM suspended_sessions WHERE created_at < ?",
                      (cutoff,))

    def suspend_clear(self) -> None:
        with self._write() as c:
            c.execute("DELETE FROM suspended_sessions")

    def suspended_gates(self) -> list[tuple[str, str, float]]:
        """(approval_id, session_id, created_at) for every parked gate."""
        with self._conn() as c:
            return [(r[0], r[1], r[2]) for r in c.execute(
                "SELECT approval_id, session_id, created_at "
                "FROM suspended_sessions").fetchall()]

    # ----- reads the approvals reviewer and the monitor need ---------------

    def list_cards_created_since(self, since: float) -> list[tuple[str, str, str, float]]:
        """(session_id, card_id, card_json, created_at) for stored cards newer
        than `since`."""
        with self._conn() as c:
            return [(r[0], r[1], r[2], r[3]) for r in c.execute(
                "SELECT session_id, card_id, card_json, created_at "
                "FROM session_cards WHERE created_at >= ?", (since,)).fetchall()]

    def last_activity_by_session(self) -> dict[str, float]:
        """Newest turn_progress frame time per session."""
        with self._conn() as c:
            return {r[0]: r[1] for r in c.execute(
                "SELECT session_id, MAX(created_at) FROM turn_progress "
                "GROUP BY session_id").fetchall()}

class PersistedApprovalGateway:
    """sqlite-backed ApprovalGateway. Paused turns survive worker
    restarts. Single-use semantics + TTL match
    fsr_playbooks.llm.approvals.InMemoryApprovalGateway exactly.

    Holds only the store: every read and write is a `Storage` call, so a
    different backend needs no gateway change."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def stash(self, session) -> None:
        self._gc()
        self._storage.suspend_put(session.approval_id, session.session_id,
                                  pickle.dumps(session), session.created_at)

    def peek(self, approval_id: str):
        self._gc()
        blob = self._storage.suspend_get(approval_id)
        if blob is None:
            return None
        s = pickle.loads(blob)  # noqa: S301 -- see module SECURITY note
        return s if self._verified(s) else None

    def pop(self, approval_id: str):
        self._gc()
        # Read-and-delete is one transaction: concurrent resumes of the same
        # approval cannot both receive the session.
        blob = self._storage.suspend_take(approval_id)
        if blob is None:
            return None
        s = pickle.loads(blob)  # noqa: S301 -- see module SECURITY note
        if s.expired():
            return None
        return s if self._verified(s) else None

    @staticmethod
    def _verified(s) -> bool:
        """S2 -- fail closed on a tampered/unbound suspended session.

        A store leak or writable sqlite file lets an attacker swap the
        stored args before the human approves. The HMAC binding
        (approval_id + tool + args_hash + created_at under the server
        secret) is recomputed here; a mismatch means the record was
        altered or minted under a different secret, so we refuse to hand
        the session back to any caller (tier-3 resume OR action-card
        execute) rather than re-dispatching substituted args."""
        try:
            from fsr_playbooks.llm import approvals as _approvals
            return _approvals.verify(s)
        except Exception:
            # No token field / import failure → cannot prove integrity.
            return False

    def clear(self) -> None:
        self._storage.suspend_clear()

    def _gc(self) -> None:
        self._storage.suspend_expire(time.time() - _TTL_SECONDS)


# Convenience: lazily-constructed default storage, one per process. A forked
# worker builds its own (the parent's object is shared state it must not reuse
# implicitly). Building it sweeps turns whose owner process is gone, so a worker
# restart cannot leave a turn "running" for the widget to poll forever.
_DEFAULT_STORAGE: Storage | None = None
_DEFAULT_PID: int | None = None


def default_storage() -> Storage:
    global _DEFAULT_STORAGE, _DEFAULT_PID
    pid = os.getpid()
    # A recorded pid that differs means this object was built in another
    # (forked-from) process. An instance installed without a pid (tests inject
    # one) is accepted as-is.
    if _DEFAULT_STORAGE is None or (_DEFAULT_PID is not None and _DEFAULT_PID != pid):
        st = Storage()
        _DEFAULT_STORAGE, _DEFAULT_PID = st, pid
        try:
            st.sweep_orphaned_turns()
        except sqlite3.Error as e:
            _log_store_error("startup sweep", e)
    return _DEFAULT_STORAGE
