"""One bug class, five instances, all found reactively. This is the gate.

`chat_turn` binds per-turn state from the widget's payload -- persona, mounted
record IRI, open-playbook grounding, read-only chip, the
turn plan. `chat_resume` (contract 4: no entity, no quick_action) cannot
re-derive any of it. When the unbound default is the *permissive* value, a
resume silently reopens what the turn closed: persona lost, mounted record
lost, tier-3 tools run with no open playbook, declining a card hands back the
write frontier (#118), a suspended read-only chip loses its gate (#117).

Each used to be its own ContextVar with its own `_set_resume_*` helper, fixed
one at a time after a live failure. Now they are fields of the framework's
`SessionState`: the turn builds them with `_turn_session_state`, which also
stores them, and every resume reads them back with `_load_session_state`.

This file is the gate on both levels:

* every module-level ContextVar in shipped code carries a decision about the
  HITL gap (discovered by parsing, so a new one cannot slip past); and
* every SessionState field is either restored by `_load_session_state` or
  re-derived by the resume itself, with the reason -- and the turn and the
  resume produce the same state for the same session.
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

# ---------------------------------------------------------------------------
# ContextVars. Every discovered one must appear in exactly one dict.
# ---------------------------------------------------------------------------

#: The host-known turn state, as one object. Its FIELDS are adjudicated below.
SESSION_STATE = {"_STATE": "fsr_playbooks.llm.session_state.SessionState"}

# Restrictive when unbound => nothing to re-bind. Losing it across the gap
# fails CLOSED, which is safe. Each entry states why, because "safe" is a claim
# that must be re-checked whenever the default changes.
TURN_SCOPED_SAFE = {
    "_OP_RENAMES":
        "defaults None; set and reset around the one verify call inside "
        "edit_playbook. Unbound means no rename_step op vouches for a rename, "
        "so a rename is judged unrequested and blocked -- restrictive.",
    "_OP_TOUCHED":
        "defaults None; set and reset around the one verify call inside "
        "edit_playbook, like _OP_RENAMES. Unbound means no op named a step, so "
        "no modified step earns a write acknowledgement -- restrictive.",
    "_FORCE_OFFLINE":
        "defaults False; set and reset inside walk_paths (verify's offline "
        "walk). It only ever REMOVES capability (no box calls); unbound is "
        "the normal stepper, never a widened gate.",
    "_HITS":
        "defaults None; set and reset around one local Jinja render to collect "
        "empty reads. Diagnostics only -- gates nothing.",
    "_REDACTOR":
        "defaults None (no redaction). Unbound is NOT restrictive: it sends text "
        "unmasked. It guards privacy, not authorization, and is not a gate: "
        "every site that makes a model call (chat turn, both resume paths) "
        "re-derives it from the redaction_mode configuration and the stored "
        "session map via `ops_redaction.redaction_scope`, so a resume cannot "
        "lose it. Tool dispatch never reads it.",
    "_APPROVED_DISPATCH":
        "defaults False; scoped to its own context manager around an approved "
        "dispatch. Unbound means 'not approved' -- the safe answer.",
    "_turn_evidence":
        "defaults None. Unbound means citation lookups get a "
        "'no_turn_evidence' error and a claim citing it fails validation -- "
        "restrictive. Re-bound on resume anyway, from the suspended session's "
        "turn_evidence_state in run_turn.py.",
    "_turn_ready_sources":
        "defaults None. Only names unchecked sources in the evidence cap's "
        "refusal and unknowns (I3); unbound names none and changes no "
        "decision. Re-set by _advertised_tools when the resumed turn builds "
        "its tool list.",
    "_turn_ready_connectors":
        "defaults None. Only adds ticket and threat-intel connectors to the "
        "not-yet-queried note and the verdict card's sources list (I5); "
        "unbound lists none and changes no decision. Re-set by "
        "_advertised_tools when the resumed turn builds its tool list.",
    "_turn_connector_labels":
        "defaults None. Only supplies catalog display labels to the change "
        "offer card (U6); unbound adds no label and the widget humanizes the "
        "id, so no decision changes. Re-set by _advertised_tools when the "
        "resumed turn builds its tool list.",
    "_CALLER":
        "defaults None. Bound per operation in connector.py from the "
        "gateway's X-User header, not from the widget payload, so a resume "
        "binds its own caller. Unbound means the caller is unknown, and the "
        "reviewer check refuses the decision -- restrictive.",
    "_DECIDER":
        "defaults None; set and reset by chat_resume itself, from the reviewer "
        "check (or the enforcing policy), so every resume binds its own. It "
        "names who approved in the audit row and gates nothing; unbound reads "
        "as 'human'.",
    "_TURN_MCP_TOOLS":
        "defaults (); set by the connector's _advertised_tools, which the turn "
        "AND the resume both call, so a resume binds its own. It only adds a "
        "`native_mcp` note to discovery results and gates nothing; unbound, "
        "the discovery tools list connectors as before.",
    "_TURN_INTEL_MCP_TOOLS":
        "defaults None (no MCP tool is intel evidence); set by the connector's "
        "_advertised_tools, which the turn AND the resume both call. Unbound, the "
        "autonomy policy's evidence check fails closed on an MCP lookup -- "
        "restrictive.",
    "_mute_depth":
        "defaults 0; set and reset only inside `skill_trace.mute_recording()`, "
        "which never spans a turn or an approval gap. It gates trace "
        "recording, not permission.",
}

# ---------------------------------------------------------------------------
# SessionState fields. Every field must appear in exactly one.
# ---------------------------------------------------------------------------

#: Fields a resume re-derives itself, with no stored copy.
REDERIVED_ON_RESUME = {
    "ask_scope":
        "bound per chat turn from the analyst's message (ops_scope). A resume "
        "carries no new ask, so it runs unscoped: the full flow, which is "
        "today's behaviour. Unbound narrows nothing -- permissive only in "
        "that it withholds the answer-scope refusals, and an approval resume "
        "is by definition past a narrow question.",
    "user_message":
        "run_turn binds it from the message history (`latest_user_text`), and "
        "a resume replays that history. Unbound only denies the #132 intent "
        "exemption in verify_enhancement -- restrictive.",
    "autonomy_policy":
        "re-read from the Key Store on every resume (`_bind_autonomy_policy`), "
        "so the kill switch holds across the gap. Unbound = no policy = every "
        "action waits for a human.",
    "autonomy_counter":
        "set only alongside autonomy_policy. Unbound means the rate limits "
        "cannot be checked, which fails the rule (would_not_act).",
    "autonomy_subject":
        "re-bound by `_bind_autonomy_subject` after the record is restored. "
        "Unbound means a record rule cannot tell which record the turn is "
        "about and fails -- would_not_act.",
    "offered_tools":
        "re-bound by `_bind_offered_tools` from the tools the resume itself "
        "advertises. Unbound falls back to the persona allowlist, so `find` "
        "names fewer record tools -- restrictive.",
}

RESUME_ENTRY_POINTS = ("chat_resume",)


def _ops():
    from connector_fsr_agentic_assistant import operations as ops
    return ops


def _source_roots():
    # `__path__`, not `__file__`: the connector package is a namespace package
    # on the box and has no `__file__` at all.
    import connector_fsr_agentic_assistant
    import fsr_playbooks
    roots = []
    for m in (connector_fsr_agentic_assistant, fsr_playbooks):
        roots.extend(pathlib.Path(p) for p in getattr(m, "__path__", []))
    return roots


def _discover_context_vars():
    """Every module-level `X: ... = ContextVar(...)` in shipped (non-test)
    code. Parsed, not imported, so a module with a heavy import side effect is
    still covered."""
    found = {}
    for root in _source_roots():
        for path in root.rglob("*.py"):
            if "test" in path.parts or path.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in tree.body:
                target = None
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    target, value = node.target.id, node.value
                elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)):
                    target, value = node.targets[0].id, node.value
                if target is None or value is None:
                    continue
                fn = value.func if isinstance(value, ast.Call) else None
                if (getattr(fn, "attr", None) or getattr(fn, "id", None)) == "ContextVar":
                    found[target] = path
    return found


ADJUDICATED = set(SESSION_STATE) | set(TURN_SCOPED_SAFE)


def test_every_context_var_is_adjudicated():
    discovered = _discover_context_vars()
    assert discovered, (
        "discovered ZERO ContextVars -- the walk is broken, and a gate that "
        "selects nothing looks exactly like a passing one")
    unadjudicated = {n: str(p) for n, p in discovered.items() if n not in ADJUDICATED}
    assert not unadjudicated, (
        "New ContextVar(s) with no decision about the HITL gap:\n"
        + "\n".join(f"  {n}  ({p})" for n, p in sorted(unadjudicated.items()))
        + "\n\nHost-known turn state belongs in SessionState as a field (then "
        "restore it in _load_session_state, or list it in REDERIVED_ON_RESUME). "
        "Per-call scratch whose unbound default is RESTRICTIVE goes in "
        "TURN_SCOPED_SAFE with the reason.")


def test_no_adjudication_is_stale():
    stale = sorted(ADJUDICATED - set(_discover_context_vars()))
    assert not stale, (
        f"adjudicated ContextVar(s) no longer exist in the source: {stale} -- "
        "remove the entry, or fix the rename")


def test_every_session_state_field_is_adjudicated():
    from fsr_playbooks.llm.session_state import SessionState
    fields = {f.name for f in dataclasses.fields(SessionState)}
    restored = set(_ops().RESUME_FIELDS)
    rederived = set(REDERIVED_ON_RESUME)
    assert not (restored & rederived), restored & rederived
    missing = fields - restored - rederived
    assert not missing, (
        f"SessionState field(s) {sorted(missing)} have no decision about the "
        "HITL gap: restore them in _load_session_state (and store them in "
        "_turn_session_state), or list them in REDERIVED_ON_RESUME with why.")
    assert not (restored | rederived) - fields, "stale field name"


def test_turn_and_resume_build_the_same_fields():
    ops = _ops()
    keys = set(ops.RESUME_FIELDS)

    class _Empty:
        pass

    assert set(ops._load_session_state(_Empty(), "s")) == keys
    assert set(ops._load_session_state(_Empty(), None)) == keys


def _calls_in(fn_name: str, callee: str) -> bool:
    pkg = pathlib.Path(_ops().__file__).parent
    for path in sorted([*pkg.glob("ops_*.py"), pkg / "operations.py"]):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name == fn_name:
                if any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == callee
                       for n in ast.walk(node)):
                    return True
    return False


def test_every_resume_entry_point_loads_the_state():
    """A loader no resume route reaches is the exact shape of a dead gate."""
    for entry in RESUME_ENTRY_POINTS:
        assert _calls_in(entry, "_load_session_state"), entry
        assert _calls_in(entry, "_bind_session_state"), entry


@pytest.mark.parametrize("quick_action,read_only,entity", [
    (None, False, {"playbook_yaml": "playbooks: []\n", "module": "alerts",
                   "uuid": "11111111-2222-3333-4444-555555555555"}),
    ("explain", False, {"playbook_yaml": "playbooks: []\n"}),
    (None, True, {"module": "alerts", "uuid": "11111111-2222-3333-4444-555555555555"}),
    (None, False, {"playbook_yaml": "playbooks: []\n", "playbook_in_hand": "draft"}),
])
def test_a_resume_restores_what_the_turn_bound(tmp_path, quick_action, read_only, entity):
    """The equivalence the whole design rests on: for the same session, the
    resume binds what the turn bound."""
    from connector_fsr_agentic_assistant.storage import Storage
    ops = _ops()
    st = Storage(tmp_path / "s.db")
    sid = "sess-eq"
    st.upsert_chat_session(sid, record_uuid=entity.get("uuid"),
                           record_module=entity.get("module"))
    st.set_session_intent(sid, "build")
    from fsr_playbooks.llm.turn_plan import TurnContext, plan_turn
    plan = plan_turn("build", context=TurnContext(
        has_open_playbook=bool(entity.get("playbook_yaml"))))
    turn = ops._turn_session_state(
        st, sid, entity=entity, quick_action=quick_action,
        has_open_playbook=bool(entity.get("playbook_yaml")), read_only=read_only,
        profile=None, entity_module=entity.get("module"), plan=plan)
    resume = ops._load_session_state(st, sid)
    for key in ("record_iri", "grounded_yaml", "grounded_source",
                "playbook_scope", "read_only"):
        assert resume[key] == turn[key], key
    # The plan is re-derived, not stored: same gate, not the same object.
    assert resume["turn_plan"].intent == turn["turn_plan"].intent
    assert (resume["turn_plan"].context.has_open_playbook
            == turn["turn_plan"].context.has_open_playbook)
    assert getattr(resume["profile"], "may_write", None) == \
        getattr(turn["profile"], "may_write", None)


def test_a_turn_with_nothing_open_clears_the_stored_playbook(tmp_path):
    """Otherwise a resume grounds on an earlier turn's playbook."""
    from connector_fsr_agentic_assistant.storage import Storage
    ops = _ops()
    st = Storage(tmp_path / "s.db")
    for entity in ({"playbook_yaml": "playbooks: []\n"}, {}):
        ops._turn_session_state(st, "s", entity=entity, quick_action=None,
                                has_open_playbook=bool(entity), read_only=False,
                                profile=None, entity_module=None, plan=None)
    assert ops._load_session_state(st, "s")["grounded_yaml"] is None


def test_a_scheduled_read_only_task_is_refused_at_dispatch_too(tmp_path):
    """`allow_mutating=False` used to only hide the write tools; the flag
    dispatch reads came from the quick-action chip alone."""
    from connector_fsr_agentic_assistant.storage import Storage
    ops = _ops()
    st = Storage(tmp_path / "s.db")
    turn = ops._turn_session_state(st, "s", entity={}, quick_action=None,
                                   has_open_playbook=False, read_only=True,
                                   profile=None, entity_module=None, plan=None)
    assert turn["read_only"] is True
    assert ops._load_session_state(st, "s")["read_only"] is True
