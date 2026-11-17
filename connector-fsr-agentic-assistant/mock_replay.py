"""Server-side mock-mode replay (PLAN Phase 3).

Lets the connector replay the SAME fixture JSON the widget's harness uses,
so a demo can fall back to mock without any widget code change. When a
handler is called with `params["mode"] == "mock"`, the dispatch layer
routes here instead of running the live agent loop.

Fixture schema (authoritative — matches the widget's
`widgetAssets/fixtures/*.json`):

    {
      "scenario": "incident_smtp_intrusion",
      "contract_version": "2.0.0",          # optional
      "default_session_id": "demo-...",      # optional
      "delays": { "chat_turn": 1500, ... },  # per-action ms, optional
      "responses": [
        { "action": "chat_turn", "response": {...} },
        { "action": "chat_resume",
          "match": { "decision": "approve", "card_id": "card-block-c2" },
          "response": {...} }
      ]
    }

`respond(op, config, params)` finds the first UNCONSUMED response whose
`action == op` AND whose `match` (if present) is a subset of `params`,
marks it consumed for this (session, scenario), honors the per-action
`delay` (skipped when `mockSpeed == "fast"`), and returns the fixture's
`response` verbatim — stamped with `ok` / `session_id` / `contract_version`
so the wire shape matches the live agent.

If no response matches, returns an error envelope with
`error.code == "mock_no_match"` so a test fails loudly rather than hanging.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# Filled lazily; keeps import cheap and avoids a hard dep when unused.
_FIXTURE_CACHE: dict[str, dict] = {}


def _fixtures_dir() -> Path | None:
    """Resolve the fixtures directory. Search order:
    1. FSR_MOCK_FIXTURES_DIR env override (used by tests + dev).
    2. <connector-pkg>/fixtures/ (where scripts/build.sh rsyncs them).
    Returns None when neither exists."""
    if env := os.environ.get("FSR_MOCK_FIXTURES_DIR"):
        p = Path(env).expanduser()
        if p.is_dir():
            return p
    sibling = Path(__file__).resolve().parent / "fixtures"
    if sibling.is_dir():
        return sibling
    return None


def _load_fixture(scenario: str) -> dict | None:
    if scenario in _FIXTURE_CACHE:
        return _FIXTURE_CACHE[scenario]
    fdir = _fixtures_dir()
    if fdir is None:
        return None
    path = fdir / f"{scenario}.json"
    if not path.is_file():
        return None
    with path.open() as fh:
        data = json.load(fh)
    _FIXTURE_CACHE[scenario] = data
    return data


def _is_subset(match: dict, payload: dict) -> bool:
    """True when every key/value in `match` is present and equal in
    `payload`. Shallow — fixture matches key on scalar resume fields
    (decision, card_id, choice_id, value)."""
    for k, v in (match or {}).items():
        if payload.get(k) != v:
            return False
    return True


def clear_cache() -> None:
    """Drop the in-process fixture cache (tests that swap fixture dirs)."""
    _FIXTURE_CACHE.clear()


def respond(op: str, config: dict, params: dict, *, storage=None) -> dict:
    """Serve the next matching fixture response for `op`. Pure function of
    the fixture + per-session cursor; never touches Anthropic or SOAR."""
    scenario = params.get("mock_scenario") or params.get("scenario")
    if not scenario:
        return _mock_err("mock_no_scenario",
                         "mode=mock requires a mock_scenario parameter")

    fixture = _load_fixture(scenario)
    if fixture is None:
        return _mock_err(
            "mock_fixture_not_found",
            f"no fixture for scenario {scenario!r}; set FSR_MOCK_FIXTURES_DIR "
            "or vendor fixtures/ via scripts/build.sh")

    session_id = (params.get("session_id")
                  or fixture.get("default_session_id")
                  or scenario)

    if storage is None:
        from .storage import default_storage
        storage = default_storage()

    consumed = set(storage.mock_consumed(session_id, scenario))
    responses = fixture.get("responses") or []

    chosen_idx = None
    for i, r in enumerate(responses):
        if i in consumed:
            continue
        if r.get("action") != op:
            continue
        if not _is_subset(r.get("match") or {}, params):
            continue
        chosen_idx = i
        break

    if chosen_idx is None:
        return _mock_err(
            "mock_no_match",
            f"no unconsumed fixture response for action {op!r} "
            f"(scenario {scenario!r}, session {session_id!r}) matching "
            f"the supplied params",
            scenario=scenario, action=op)

    storage.mock_mark(session_id, scenario, chosen_idx)

    # Honor the scripted delay unless the caller asked for fast replay.
    fast = params.get("mockSpeed") == "fast" or params.get("mock_fast")
    delays = fixture.get("delays") or {}
    delay_ms = delays.get(op)
    if delay_ms and not fast:
        time.sleep(min(delay_ms, 5000) / 1000.0)

    response = dict(responses[chosen_idx].get("response") or {})

    # Stamp the envelope fields the live handlers always set, so the wire
    # shape is identical to a real turn. Fixture values win when present.
    response.setdefault("session_id", session_id)
    if op != "chat_history":  # chat_history is a bare {turns:[...]} read
        response.setdefault("ok", True)
    cv = fixture.get("contract_version")
    if cv:
        response.setdefault("contract_version", cv)
    return response


def _mock_err(code: str, message: str, **extra: Any) -> dict:
    return {"ok": False, "stop_reason": "error",
            "error": {"code": code, "message": message, **extra}}
