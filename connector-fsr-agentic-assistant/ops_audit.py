"""Audit export (X6): the agent audit for a time window as CSV and JSON.

Sessions, model turns, tool calls and approval decisions (what was proposed,
who decided, what ran) plus the notifications sent, read from the store and
nothing else. The JSON keeps the structure; the CSV is one flat timeline of the
same events so it opens in a spreadsheet.

Redaction follows the `redaction_mode` field: with it on, every exported value
passes through the same redactor, loaded with the session's own value map, that
masks text before it reaches the model provider, so an exported placeholder
means what it meant in the conversation. With it off, the export is the data as
stored. Read only.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import time
from typing import Any

from .ops_common import _check_params, _err
from .ops_monitor import _hours
from .ops_redaction import _free_text_fields, _load_state, _save_state, redaction_mode
from .pydantic_models import ExportAuditParams

_DEFAULT_HOURS = 24
_MAX_HOURS = 24 * 93
_MAX_TURNS = 20000
FORMATS = ("both", "csv", "json")
CSV_COLUMNS = ("at", "kind", "session_id", "actor", "name", "status", "detail")


def _iso(ts: float | None) -> str:
    return "" if ts is None else _dt.datetime.fromtimestamp(
        ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str) -> float:
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class _Masker:
    """Masks exported values with each session's own redactor, or does nothing
    when redaction is off."""

    def __init__(self, config: Any, storage: Any):
        self.mode = redaction_mode(config)
        self._storage, self._config = storage, config
        self._by_session: dict[str, Any] = {}

    def _redactor(self, session_id: str):
        if session_id not in self._by_session:
            from fsr_playbooks.llm import redaction as fw
            self._by_session[session_id] = fw.Redactor(
                self.mode, state=_load_state(self._storage, session_id) if session_id else None,
                free_text_fields=_free_text_fields(self._config))
        return self._by_session[session_id]

    def __call__(self, session_id: str | None, value: Any) -> Any:
        if self.mode == "off":
            return value
        return self._redactor(session_id or "").mask_result(value)

    def learn(self, session_id: str | None, value: Any) -> None:
        """Teach the session's redactor the typed values in `value` without
        masking it, so a value seen in any row is masked in every row."""
        if self.mode != "off":
            self._redactor(session_id or "").learn_record(
                value if isinstance(value, dict) else {"_": value})

    def share_known(self) -> None:
        """Every host, user and email any session of the export knows is masked
        in all of them: the same entity can sit as plain text in a session that
        never saw it in a typed field."""
        if self.mode == "off":
            return
        pool: list[tuple[str, str]] = []
        for r in self._by_session.values():
            pool.extend(r.shareable())
        for r in self._by_session.values():
            r.adopt(pool)

    def persist(self) -> None:
        for sid, r in self._by_session.items():
            if sid and r.dirty:
                try:
                    _save_state(self._storage, sid, r)
                except Exception:  # noqa: BLE001 - the export is still masked
                    pass


def _cell(v: Any) -> str:
    """One CSV cell. A value a spreadsheet would run as a formula is prefixed so
    it opens as text."""
    if v is None:
        return ""
    text = v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _timeline(data: dict) -> list[dict]:
    rows = []
    for t in data["turns"]:
        rows.append({"at": t["at"], "kind": "turn", "session_id": t["session_id"],
                     "actor": t.get("user"), "name": t.get("intent"),
                     "status": t.get("status"),
                     "detail": {"model": t.get("model"), "stop_reason": t.get("stop_reason"),
                                "input_tokens": t.get("input_tokens"),
                                "output_tokens": t.get("output_tokens"),
                                "cost_usd": t.get("cost_usd"),
                                "prompt": t.get("prompt"), "response": t.get("response"),
                                "error": t.get("error")}})
    for c in data["tool_calls"]:
        rows.append({"at": c["at"], "kind": "tool_call", "session_id": c["session_id"],
                     "actor": None, "name": c["tool"], "status": c.get("status"),
                     "detail": {"params": c.get("params"), "result": c.get("result"),
                                "latency_ms": c.get("latency_ms")}})
    for d in data["decisions"]:
        rows.append({"at": d["at"], "kind": "decision", "session_id": d["session_id"],
                     "actor": d.get("actor"), "name": d.get("tool") or d.get("card_type"),
                     "status": d.get("decision"),
                     "detail": {"card_id": d["card_id"], "executed": d["executed"],
                                "connector": d.get("connector"),
                                "operation": d.get("operation"), "args": d.get("args"),
                                "ok": d.get("ok")}})
    for n in data["notifications"]:
        rows.append({"at": n["at"], "kind": "notification",
                     "session_id": n.get("session_id"), "actor": None,
                     "name": n.get("subject"), "status": n.get("status"),
                     "detail": {"channel": n.get("channel"), "reason": n.get("detail"),
                                "trigger": n.get("trigger")}})
    rows.sort(key=lambda r: r["at"])
    return rows


def render_csv(data: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in _timeline(data):
        w.writerow([_cell(r[c]) for c in CSV_COLUMNS])
    return out.getvalue()


def build_audit(config: dict, since: float, until: float) -> dict:
    """The audit for [since, until) as plain data, masked per the config."""
    from .storage import default_storage
    storage = default_storage()
    mask = _Masker(config, storage)

    usage = storage.list_agent_usage(
        filters={"from": _iso(since), "to": _iso(until)}, limit=_MAX_TURNS)
    usage = [u for u in usage if since <= _epoch(u["ts"]) < until]
    usage.sort(key=lambda u: u["ts"])
    # Pass 1 gathers the raw rows; pass 2 masks them. Every row is learned
    # first, so a host seen in one row is masked inside names in all of them.
    raw: list[tuple[str, str | None, dict]] = []
    for u in usage:
        sid = u["session_id"]
        raw.append(("turn", sid, {
            "at": u["ts"], "session_id": sid, "user": u.get("user_name") or u.get("user_iri"),
            "intent": u.get("intent"), "model": u.get("model"), "turn": u.get("turn"),
            "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
            "cost_usd": u.get("cost_usd"), "stop_reason": u.get("stop_reason"),
            "status": u.get("status"), "error": u.get("error"),
            "approvals": u.get("approvals_count"), "prompt": u.get("prompt"),
            "response": u.get("response")}))
        for tc in u.get("tool_call_detail") or []:
            raw.append(("call", sid, {
                "at": u["ts"], "session_id": sid, "turn": tc.get("turn"),
                "index": tc.get("index"), "tool": tc["tool_name"],
                "status": tc.get("result_status"), "params": tc.get("params"),
                "result": tc.get("result_summary"), "latency_ms": tc.get("latency_ms")}))
    for d in storage.audit_decisions(since, until):
        raw.append(("decision", d["session_id"], {**d, "at": _iso(d["decided_at"])}))
    for s_ in storage.audit_sessions(since, until):
        raw.append(("session", s_["session_id"], {
            **s_, "started_at": _iso(s_["started_at"]), "updated_at": _iso(s_["updated_at"])}))
    for n in storage.list_notifications(since, until, limit=_MAX_TURNS):
        raw.append(("note", n["ref"], {
            "at": _iso(n["created_at"]), "kind": n["kind"], "session_id": n["ref"],
            "trigger": n["trigger_code"], "subject": n["subject"], "status": n["status"],
            "channel": n["channel"], "detail": n["detail"]}))
    for _kind, sid, row in raw:
        mask.learn(sid, row)
    mask.share_known()
    by_kind: dict[str, list] = {"turn": [], "call": [], "decision": [],
                                "session": [], "note": []}
    for kind, sid, row in raw:
        by_kind[kind].append(mask(sid, row))
    turns, calls, decisions = by_kind["turn"], by_kind["call"], by_kind["decision"]
    sessions, notes = by_kind["session"], by_kind["note"]
    mask.persist()
    return {"from": _iso(since), "to": _iso(until), "redaction": mask.mode,
            "sessions": sessions, "turns": turns, "tool_calls": calls,
            "decisions": decisions, "notifications": notes}


def export_audit(config: dict, params: dict) -> dict:
    """Export the agent audit for the last `hours` (default 24), ending at
    `until` (epoch seconds; default now). `format` is csv, json or both
    (default). The reply carries the content as strings (`csv`, `json`), a
    suggested file stem and the row counts."""
    _check_params(ExportAuditParams, params)
    fmt = str(params.get("format") or "both").lower()
    if fmt not in FORMATS:
        return _err("bad_format", f"format must be one of {', '.join(FORMATS)}, got {fmt!r}")
    hours = _hours(params, _DEFAULT_HOURS)
    if hours > _MAX_HOURS:
        return _err("bad_window", f"hours must be at most {_MAX_HOURS}, got {hours}")
    until: Any = params.get("until")
    try:
        until = float(until) if until else time.time()
    except (TypeError, ValueError):
        return _err("bad_window", f"until must be epoch seconds, got {until!r}")
    try:
        data = build_audit(config, until - hours * 3600.0, until)
    except ImportError:
        return _err("redaction_unavailable",
                    "Redaction is on, but the installed framework cannot apply it, "
                    "so nothing was exported.")
    out: dict[str, Any] = {
        "ok": True, "from": data["from"], "to": data["to"],
        "redaction": data["redaction"],
        "file_stem": "agent-audit-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(until)),
        "counts": {k: len(data[k]) for k in
                   ("sessions", "turns", "tool_calls", "decisions", "notifications")}}
    if fmt in ("json", "both"):
        out["json"] = json.dumps(data, indent=2, sort_keys=True, default=str)
    if fmt in ("csv", "both"):
        out["csv"] = render_csv(data)
    return out
