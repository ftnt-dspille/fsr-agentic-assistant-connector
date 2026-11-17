"""notify_finding: the post-turn notification step that makes "alert me" real.

The runner playbook calls chat_turn (the agent investigates) then notify_finding
(this op) to push the verdict to the analyst via a configured notification channel
(SMTP email / Slack / Teams / SMS). The agent stays read-only; the notify is a
deterministic post-turn step -- no HITL gate parks the run on the notify.

These tests pin the contract:
  - channel discovery: only CONFIGURED connectors are dispatched to
  - priority: smtp beats slack when both are configured
  - no_finding skip: a clean run ("all clear" / empty verdict) does not spam
  - no channels: a box with none configured skips gracefully (the finding lives
    in the history drawer)
  - parked run: the message tells the analyst to resume in chat
  - dispatch: /api/integration/execute is called with the right connector + op
  - target: notify_target fills the email `to` / slack `channel`
"""
import json
import sys
import time
import importlib.util
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(sys.modules["connectors.core.connector"], "ConnectorError"):
        sys.modules["connectors.core.connector"].ConnectorError = type(
            "ConnectorError", (Exception,), {})
    pkg = types.ModuleType("_fsrpb_notify")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_notify"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_notify.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_notify.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_notify.operations"] = mod
    spec.loader.exec_module(mod)
    importlib.import_module("_fsrpb_notify.storage")
    return mod


import storage as S  # noqa: E402


@pytest.fixture()
def store(operations, tmp_path, monkeypatch):
    st = S.Storage(tmp_path / "notify.db")
    pkg_storage = sys.modules.get("_fsrpb_notify.storage") or S
    monkeypatch.setattr(pkg_storage, "_DEFAULT_STORAGE", st)
    return st


def _def(store, name, **kw):
    store.upsert_scheduled_task({
        "name": name, "cron": kw.get("cron", "0 9 * * 1"),
        "intent": kw.get("intent", "triage"),
        "prompt": kw.get("prompt", "check bandwidth"),
        "agent_config": "fsrpb-anthropic",
        "allow_mutating": False, "wf_iri": "/api/3/workflows/x",
        "timezone": "UTC", "enabled": True,
    })


def _run(store, name, uid, *, verdict="spike found", stop_reason="end_turn",
         ts=None, disposition=None):
    """Seed one cron-fire session with a verdict. `disposition` adds the
    structured verdict card the notify decision reads."""
    frames = [{"type": "text", "text": verdict}]
    if disposition:
        frames.append({"type": "verdict_card", "disposition": disposition,
                       "summary": "s"})
    sid = f"sched:{name}:{uid}"
    t = ts if ts is not None else time.time()
    with store._write() as c:
        c.execute(
            "INSERT INTO session_transcript "
            "(session_id, role, payload_json, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (sid, json.dumps({"text": "run me"}), t),
        )
        c.execute(
            "INSERT INTO session_transcript "
            "(session_id, role, payload_json, created_at) "
            "VALUES (?, 'assistant', ?, ?)",
            (sid, json.dumps({"transcript": frames}),
             t + 1),
        )
        c.execute(
            "INSERT OR REPLACE INTO chat_turns "
            "(session_id, turn, model, input_tokens, output_tokens, "
            " cache_read, cache_write, stop_reason, tags_json, "
            " tool_calls_json, created_at) "
            "VALUES (?, 1, 'claude', 1, 2, 0, 0, ?, '{}', '[]', ?)",
            (sid, stop_reason, t + 1),
        )
    return sid


class _MockMR:
    """Stands in for the crudhub transport. Records calls + returns canned
    responses for the connector listing + the execute dispatch."""
    def __init__(self, configured=("smtp",), execute_status="Success",
                 execute_resp=None):
        self._configured = set(configured)
        self._execute_status = execute_status
        self._execute_resp = execute_resp
        self.calls = []

    def __call__(self, path, method, body=None, params=None):
        self.calls.append({"path": path, "method": method, "body": body})
        if "/api/integration/connectors" in path:
            members = [{"name": n} for n in self._configured]
            return {"data": members}
        if path == "/api/integration/execute/":
            if self._execute_resp is not None:
                return self._execute_resp
            return {"status": self._execute_status, "message": "ok",
                    "data": {"id": "msg-1"}}
        return {}


@pytest.fixture()
def transport(operations, monkeypatch, request):
    """Patch _require_transport to return a mock crudhub."""
    mr = _MockMR(**(getattr(request, "param", {}) or {}))

    def _fake_require(message=None):
        return mr, None
    monkeypatch.setattr(sys.modules["_fsrpb_notify.ops_autonomy"], "_require_transport", _fake_require)
    monkeypatch.setattr(sys.modules["_fsrpb_notify.ops_playbooks"], "_require_transport", _fake_require)
    monkeypatch.setattr(sys.modules["_fsrpb_notify.ops_playbooks_persist"], "_require_transport", _fake_require)
    monkeypatch.setattr(sys.modules["_fsrpb_notify.ops_scheduling"], "_require_transport", _fake_require)
    monkeypatch.setattr(sys.modules["_fsrpb_notify.ops_usage"], "_require_transport", _fake_require)
    monkeypatch.setattr(sys.modules["_fsrpb_notify.ops_warmup"], "_require_transport", _fake_require)
    return mr


def test_dispatches_via_smtp_when_configured(operations, store, transport):
    _def(store, "Bandwidth watch")
    sid = _run(store, "Bandwidth watch", "u1", verdict="port3 spiked 500%")

    out = operations.notify_finding({}, {"name": "Bandwidth watch",
                                         "session_id": sid,
                                         "notify_target": "soc@example.com"})

    assert out["ok"] is True
    assert out["channel"] == "smtp"
    # The execute call targeted smtp.send_email.
    exec_calls = [c for c in transport.calls
                  if c["path"] == "/api/integration/execute/"]
    assert len(exec_calls) == 1
    assert exec_calls[0]["body"]["connector"] == "smtp"
    assert exec_calls[0]["body"]["operation"] == "send_email"
    # The subject carries the task name.
    assert "Bandwidth watch" in exec_calls[0]["body"]["params"]["subject"]
    assert "port3 spiked" in exec_calls[0]["body"]["params"]["body"]


def test_priority_smtp_beats_slack_when_both_configured(operations, store, transport):
    transport._configured = {"smtp", "slack"}
    _def(store, "Daily")
    _run(store, "Daily", "u1", verdict="IOC found")

    out = operations.notify_finding({}, {"name": "Daily", "session_id": "sched:Daily:u1",
                                         "notify_target": "soc@example.com"})

    assert out["channel"] == "smtp"  # smtp wins by priority


def test_slack_when_smtp_not_configured(operations, store, transport):
    transport._configured = {"slack"}
    _def(store, "Slack only")
    _run(store, "Slack only", "u1", verdict="finding")

    out = operations.notify_finding({}, {"name": "Slack only",
                                          "session_id": "sched:Slack only:u1",
                                          "notify_target": "#soc"})
    assert out["channel"] == "slack"
    exec_calls = [c for c in transport.calls
                  if c["path"] == "/api/integration/execute/"]
    assert exec_calls[0]["body"]["operation"] == "post_message"


def test_no_finding_skips_notify(operations, store, transport):
    _def(store, "Clean sweep")
    sid = _run(store, "Clean sweep", "u1", verdict="all clear",
               disposition="benign")

    out = operations.notify_finding({}, {"name": "Clean sweep",
                                         "session_id": sid})
    assert out["ok"] is True
    assert out["skipped"] == "no_finding"
    # No execute call was made.
    assert not [c for c in transport.calls
                if c["path"] == "/api/integration/execute/"]


def test_decision_ignores_verdict_wording(operations, store, transport):
    """A French benign verdict skips; an English "no findings" text on a
    malicious disposition notifies. The text is never read for the decision."""
    _def(store, "Fr")
    sid = _run(store, "Fr", "u1", verdict="Rien a signaler, tout est normal",
               disposition="benign")
    out = operations.notify_finding({}, {"name": "Fr", "session_id": sid})
    assert out["skipped"] == "no_finding"

    _def(store, "En")
    sid = _run(store, "En", "u1", verdict="no findings",
               disposition="true_positive")
    out = operations.notify_finding({}, {"name": "En", "session_id": sid,
                                         "notify_target": "soc@example.com"})
    assert out.get("channel") == "smtp"
    assert "skipped" not in out


def test_empty_verdict_skips_notify(operations, store, transport):
    _def(store, "Empty")
    sid = _run(store, "Empty", "u1", verdict="")

    out = operations.notify_finding({}, {"name": "Empty", "session_id": sid})
    assert out["skipped"] == "no_finding"


def test_only_on_finding_false_sends_even_on_clean_run(operations, store, transport):
    _def(store, "Always notify")
    sid = _run(store, "Always notify", "u1", verdict="all clear")

    out = operations.notify_finding({}, {"name": "Always notify",
                                         "session_id": sid,
                                         "only_on_finding": False,
                                         "notify_target": "soc@example.com"})
    assert out.get("channel") == "smtp"
    assert "skipped" not in out


def test_no_channels_graceful_skip(operations, store, transport):
    transport._configured = set()  # no notification connectors configured
    _def(store, "No channels")
    _run(store, "No channels", "u1", verdict="spike found")

    out = operations.notify_finding({}, {"name": "No channels",
                                         "session_id": "sched:No channels:u1"})
    assert out["ok"] is True
    assert out["skipped"] == "no_channels"
    assert not [c for c in transport.calls
                if c["path"] == "/api/integration/execute/"]


def test_parked_run_message_tells_analyst_to_resume(operations, store, transport):
    _def(store, "Gated")
    sid = _run(store, "Gated", "u1", verdict="need to block IP",
              stop_reason="awaiting_action_card")

    out = operations.notify_finding({}, {"name": "Gated", "session_id": sid,
                                         "notify_target": "soc@example.com"})
    assert out["ok"] is True
    exec_calls = [c for c in transport.calls
                  if c["path"] == "/api/integration/execute/"]
    body = exec_calls[0]["body"]["params"]["body"]
    assert "parked" in body.lower() or "resume" in body.lower()
    assert sid in body


def test_notify_target_fills_email_to(operations, store, transport):
    _def(store, "Targeted")
    sid = _run(store, "Targeted", "u1", verdict="spike")
    operations.notify_finding({}, {"name": "Targeted", "session_id": sid,
                                   "notify_target": "soc@example.com"})
    exec_calls = [c for c in transport.calls
                  if c["path"] == "/api/integration/execute/"]
    assert exec_calls[0]["body"]["params"]["to"] == "soc@example.com"


def test_verdict_override_used_over_storage(operations, store, transport):
    _def(store, "Override")
    _run(store, "Override", "u1", verdict="storage verdict")
    operations.notify_finding({}, {"name": "Override",
                                   "session_id": "sched:Override:u1",
                                   "verdict": "override verdict",
                                   "notify_target": "soc@example.com"})
    exec_calls = [c for c in transport.calls
                  if c["path"] == "/api/integration/execute/"]
    assert "override verdict" in exec_calls[0]["body"]["params"]["body"]


def test_not_found_for_unknown_task(operations, store, transport):
    out = operations.notify_finding({}, {"name": "ghost"})
    assert out.get("ok") is False
    assert out["error"]["code"] == "not_found"


def test_send_failure_surfaces_error(operations, store, transport):
    transport._execute_resp = {"status": "Failed", "message": "SMTP refused"}
    _def(store, "Fail send")
    _run(store, "Fail send", "u1", verdict="spike")
    out = operations.notify_finding({}, {"name": "Fail send",
                                         "session_id": "sched:Fail send:u1",
                                         "notify_target": "soc@example.com"})
    assert out.get("ok") is False
    assert out["error"]["code"] == "send_failed"


def test_no_target_skips_gracefully(operations, store, transport):
    """SMTP needs a recipient; without notify_target, the op skips instead of
    dispatching a guaranteed-to-400 send."""
    _def(store, "No target")
    _run(store, "No target", "u1", verdict="spike")
    out = operations.notify_finding({}, {"name": "No target",
                                         "session_id": "sched:No target:u1"})
    assert out["ok"] is True
    assert out["skipped"] == "no_target"
    assert not [c for c in transport.calls
                if c["path"] == "/api/integration/execute/"]
