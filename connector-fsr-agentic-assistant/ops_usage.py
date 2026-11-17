"""usage domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

from typing import Any
from .pydantic_models import (
    ListUsageParams, GetUsageSummaryParams,
)

from .ops_common import _check_params, _require_transport, _log



# Per-model output token pricing (USD per 1M tokens). Input prices in
# the matching dict below. Sourced from public Anthropic pricing as of
# 2026-05-28; missing models default to 0.0 so cost_usd degrades to a
# floor rather than 500ing the wire shape.
_PRICE_IN_PER_MTOK: dict[str, float] = {
    # Adaptive-thinking family -- the only Anthropic models the agent loop
    # should run (see the model policy in AGENT_DESIGN_AND_TEST_UNIFICATION.md).
    "claude-opus-5-5": 4.0,
    "claude-sonnet-5-5": 2.0,
    "claude-haiku-5-5": 0.1,
    "claude-opus-5": 5.0,
    "claude-sonnet-5": 3.0,
    "claude-opus-4-7": 15.0,
    "claude-sonnet-4-6": 3.0,
    "claude-sonnet-4-5-20250929": 3.0,
    "claude-haiku-4-5-20251001": 0.8,
    # OpenAI (public list pricing, USD/1M input tokens, as of 2026-06).
    "gpt-4o": 2.5,
    "gpt-4o-mini": 0.15,
    "gpt-4.1": 2.0,
    "gpt-4.1-mini": 0.4,
    "gpt-4.1-nano": 0.1,
    "o3": 2.0,
    "o4-mini": 1.1,
    # OpenAI GPT-5 family (USD/1M input tokens).
    "gpt-5.4": 2.5,
    "gpt-5.4-mini": 0.75,
    "gpt-5.4-nano": 0.2,
    "gpt-5.5": 5.0,
    "gpt-5.6-sol": 5.0,
    "gpt-5.6-terra": 2.5,
    "gpt-5.6-luna": 1.0,
}
_PRICE_OUT_PER_MTOK: dict[str, float] = {
    "claude-opus-5-5": 20.0,
    "claude-sonnet-5-5": 10.0,
    "claude-haiku-5-5": 0.5,
    "claude-opus-5": 25.0,
    "claude-sonnet-5": 15.0,
    "claude-opus-4-7": 75.0,
    "claude-sonnet-4-6": 15.0,
    "claude-sonnet-4-5-20250929": 15.0,
    "claude-haiku-4-5-20251001": 4.0,
    # OpenAI (USD/1M output tokens).
    "gpt-4o": 10.0,
    "gpt-4o-mini": 0.6,
    "gpt-4.1": 8.0,
    "gpt-4.1-mini": 1.6,
    "gpt-4.1-nano": 0.4,
    "o3": 8.0,
    "o4-mini": 4.4,
    # OpenAI GPT-5 family (USD/1M output tokens).
    "gpt-5.4": 15.0,
    "gpt-5.4-mini": 4.5,
    "gpt-5.4-nano": 1.25,
    "gpt-5.5": 30.0,
    "gpt-5.6-sol": 30.0,
    "gpt-5.6-terra": 15.0,
    "gpt-5.6-luna": 6.0,
}

# Cached-input pricing (USD per 1M tokens read from the provider's prompt
# cache). OpenAI publishes an explicit per-model cached rate and the ratio is
# NOT uniform -- the 4.1 family discounts to 0.25x input while the 5.x family
# discounts to 0.1x -- so this must be a table, not a multiplier.
_PRICE_CACHED_IN_PER_MTOK: dict[str, float] = {
    "gpt-4o": 1.25,
    "gpt-4o-mini": 0.075,
    "gpt-4.1": 0.5,
    "gpt-4.1-mini": 0.1,
    "gpt-4.1-nano": 0.025,
    "o3": 0.5,
    "o4-mini": 0.275,
    "gpt-5.4": 0.25,
    "gpt-5.4-mini": 0.075,
    "gpt-5.4-nano": 0.02,
    "gpt-5.5": 0.5,
    "gpt-5.6-sol": 0.5,
    "gpt-5.6-terra": 0.25,
    "gpt-5.6-luna": 0.1,
}

# Anthropic prices caching as fixed multipliers on the model's base input rate
# rather than per-model line items: reads 0.1x, writes 1.25x at the default
# 5-minute TTL (2x for the 1h TTL, which we don't request).
_ANTHROPIC_CACHE_READ_MULT = 0.1
_ANTHROPIC_CACHE_WRITE_MULT = 1.25


def _is_anthropic_model(model: str) -> bool:
    """True for Claude model ids, which account cached tokens disjointly from
    ``input_tokens`` (OpenAI nests them inside it instead). Covers the bare ids
    and the Bedrock/Vertex-prefixed forms (``anthropic.claude-…``, ``us.anthropic…``)."""
    m = (model or "").lower()
    return "claude" in m or m.startswith("anthropic")


#: Paid-provider model ids we have already warned were missing a price entry.
#: A model switch (e.g. to ``claude-haiku-5-5``) that lands on an id absent from
#: the tables above reads back as $0 cost and silently zeroes the usage ledger.
#: A FREE gateway model (Frank/qwen) priced at 0 is correct, so we warn only for
#: the paid providers, and once per id so the connector log is not flooded.
_WARNED_UNPRICED: set[str] = set()


def _warn_if_unpriced(model: str, pin: float, pout: float) -> None:
    if pin or pout:
        return
    m = (model or "").lower()
    looks_paid = _is_anthropic_model(m) or m.startswith(("gpt", "o3", "o4"))
    if not looks_paid or model in _WARNED_UNPRICED:
        return
    _WARNED_UNPRICED.add(model)
    _log(f"usage: no price entry for model {model!r}; cost will read $0 until "
         "it is added to the ops_usage.py price tables", level="warning")


def _input_is_disjoint(model: str, cache_write: int = 0) -> bool:
    """True when ``input_tokens`` EXCLUDES the cache counters (Anthropic shape).

    Decided by model family. A non-zero ``cache_write`` also selects the
    disjoint shape, as the cost function always has. Both the cost and the
    normalized input total read this one predicate so they cannot disagree."""
    return _is_anthropic_model(model) or bool(cache_write)


def _total_input_tokens(model: str, input_tokens: int, cache_read: int = 0,
                        cache_write: int = 0) -> int:
    """Every input token the round was billed for, cached or not.

    Disjoint (Anthropic): input + cache_read + cache_write. Subset (OpenAI):
    ``input_tokens`` already contains the cached part, so it is the total."""
    input_tokens = int(input_tokens or 0)
    cache_read = max(0, int(cache_read or 0))
    cache_write = max(0, int(cache_write or 0))
    if _input_is_disjoint(model, cache_write):
        return input_tokens + cache_read + cache_write
    return input_tokens


def _usage_cost_usd(model: str, input_tokens: int, output_tokens: int,
                    cache_read: int = 0, cache_write: int = 0) -> float:
    """Cost of one turn in USD, honoring prompt-cache discounts.

    The two providers report cached tokens with OPPOSITE semantics, and getting
    this backwards silently mis-bills by a large factor in one direction or the
    other:

    * OpenAI -- ``cached_tokens`` is a SUBSET of ``prompt_tokens``. The uncached
      remainder is ``input_tokens - cache_read``, so we must subtract before
      pricing or we charge the cached portion at full rate as well. Cache writes
      are free and unreported on the models we support (GPT-5.6+ bills them at
      1.25x -- add handling here if we ever default to one).
    * Anthropic -- ``input_tokens`` EXCLUDES both cache figures; the three are
      disjoint and sum to the true input. So each is priced on its own and
      nothing is subtracted.

    We tell them apart by MODEL NAME, not by which counter is populated. An
    earlier version keyed off ``cache_write > 0`` and was wrong for a real case:
    an Anthropic turn that is a pure cache hit reports cache_read with
    cache_write == 0, and the OpenAI branch would then subtract a disjoint
    counter -- zeroing a genuinely billable input. Measured live: a second turn
    came back ``input_tokens=3, cache_read=15202, cache_write=0``.

    Unknown models price at 0.0, so cost degrades to a floor rather than raising
    into the chat path.
    """
    pin = _PRICE_IN_PER_MTOK.get(model, 0.0)
    pout = _PRICE_OUT_PER_MTOK.get(model, 0.0)
    _warn_if_unpriced(model, pin, pout)
    cache_read = max(0, int(cache_read or 0))
    cache_write = max(0, int(cache_write or 0))

    if _input_is_disjoint(model, cache_write):
        # Anthropic shape: disjoint counters, multiplier-priced.
        billable_in = input_tokens
        cached_cost = (cache_read * pin * _ANTHROPIC_CACHE_READ_MULT
                       + cache_write * pin * _ANTHROPIC_CACHE_WRITE_MULT)
    else:
        # OpenAI shape: cache_read is contained in input_tokens.
        billable_in = max(0, input_tokens - cache_read)
        p_cached = _PRICE_CACHED_IN_PER_MTOK.get(
            model, pin * _ANTHROPIC_CACHE_READ_MULT)
        cached_cost = cache_read * p_cached

    return round((billable_in * pin + cached_cost
                  + output_tokens * pout) / 1_000_000, 6)


def _usage_event_cost_usd(ev) -> float:
    """Cost of one LLM round (a framework UsageEvent), cache counters included.

    The single call every usage sink uses -- the wire frame, the chat-turn log
    row, the llm activity row and the resume log row -- so none of them can
    drop ``cache_write`` from the price."""
    return _usage_cost_usd(
        ev.model, ev.input_tokens, ev.output_tokens,
        getattr(ev, "cache_read", 0) or 0,
        getattr(ev, "cache_write", 0) or 0)


def _usage_log_path(config: dict | None) -> str:
    """Resolve the JSONL token-usage firehose path for the live token profile.

    In-platform connectors can't read process env vars -- the operator
    configures things through connector *configuration*. So honor an explicit
    `usage_log_path` connector-config field if present; otherwise co-locate the
    log beside the connector's storage DB, a known connector-writable path on
    the SOAR host. (fsr_playbooks's repo-root default doesn't exist in-platform, so
    live usage was being dropped.)"""
    configured = (config or {}).get("usage_log_path")
    if configured:
        return str(configured)
    from .storage import _default_db_path
    return str(_default_db_path().with_name(".connector_fsr_agentic_assistant_usage.jsonl"))


def _now_iso() -> str:
    """UTC now as an ISO 8601 string (matches usage_log's ts shape)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _log_usage_jsonl(ev: Any, path: str) -> None:
    """Mirror web/backend's `_persist_usage`: append one JSONL line per
    UsageEvent so the live token profile lands in the same firehose
    shape the Studio profiler consumes. `path` is connector-derived (no env).
    Best-effort -- never raises into the chat path (telemetry must not break a
    live turn)."""
    try:
        from fsr_playbooks.llm.usage_log import est_tokens, log_turn
        history_chars = getattr(ev, "history_chars", 0) or 0
        log_turn({
            "session": ev.session_id,
            "turn": ev.turn,
            "model": ev.model,
            "input_tokens": ev.input_tokens,
            "output_tokens": ev.output_tokens,
            "cache_read": ev.cache_read,
            "cache_write": ev.cache_write,
            "stop_reason": ev.stop_reason,
            "self_repair_turn": getattr(ev, "self_repair_turn", None),
            "history_chars": history_chars,
            "history_est_tokens": est_tokens(history_chars),
            "tool_calls": [
                {"name": t.name, "args_chars": t.args_chars,
                 "result_chars": t.result_chars,
                 "result_est_tokens": est_tokens(t.result_chars),
                 "duration_ms": getattr(t, "duration_ms", None)}
                for t in ev.tool_calls
            ],
            "tags": dict(ev.tags or {}),
        }, path)
    except Exception:
        pass


# ---------- Monitor: agent usage telemetry + audit (agentic-assistant-monitor) --
#
# Dual-write per LLM turn: POST /api/3/llm_activity_logs (the native platform
# audit module, live-verified writable on .206) + sqlite agent_usage (the
# richer fields the module lacks: tool_calls, cache stats, tags, session_id,
# intent). The monitor widget reads the platform module via FormEntityService
# for the audit trail + totals and the connector ops for drill-downs.

_LLM_ACTIVITY_LOG_ENDPOINT = "/api/3/llm_activity_logs"


def _llm_activity_log_payload(record: dict) -> dict:
    """Map the connector's snake_case telemetry to the platform module's
    camelCase fields (live-verified schema on .206)."""
    return {
        "title": record.get("title") or "Agentic Assistant",
        "provider": record.get("provider") or "anthropic",
        "modelName": record.get("model"),
        "prompt": record.get("prompt") or "[]",
        "response": record.get("response") or "",
        "inputTokens": int(record.get("input_tokens") or 0),
        "outputTokens": int(record.get("output_tokens") or 0),
        "totalTokens": int(record.get("total_tokens")
                           or ((record.get("input_tokens") or 0)
                               + (record.get("output_tokens") or 0))),
        "costUSD": float(record.get("cost_usd") or 0),
        "latencyMs": int(record.get("latency_ms") or 0),
        "status": record.get("status") or "success",
        "error": record.get("error") or "",
        "correlationID": record.get("correlation_id") or record.get("session_id") or "",
    }


def log_llm_activity(config: dict, record: dict) -> int | None:
    """Dual-write one LLM turn to /api/3/llm_activity_logs + sqlite
    agent_usage. Best-effort -- telemetry never breaks chat. Called from the
    existing per-turn telemetry path alongside usage_log.log_turn(). Returns
    the inserted sqlite agent_usage row id on success (so the caller can link
    per-tool-call rows to it), or None on failure."""
    activity_iri = None
    mr, _ = _require_transport()
    if mr is not None:
        try:
            resp = mr(_LLM_ACTIVITY_LOG_ENDPOINT, "POST",
                      body=_llm_activity_log_payload(record))
            if isinstance(resp, dict):
                activity_iri = resp.get("@id")
        except Exception:
            pass
    from .storage import default_storage
    try:
        return default_storage().record_agent_usage({**record,
                                                     "activity_log_iri": activity_iri})
    except Exception:
        return None


def list_usage(config: dict, params: dict) -> dict:
    """Per-turn usage rows (newest-first) from sqlite agent_usage. Filters:
    from/to (ISO 8601), user_iri, model, intent, session_id, status, limit."""
    _check_params(ListUsageParams, params)
    from .storage import default_storage
    storage = default_storage()
    filters = {k: params.get(k) for k in
               ("session_id", "user_iri", "model", "intent", "status",
                "from", "to") if params.get(k) is not None}
    limit = int(params.get("limit") or 200)
    return {"usage": storage.list_agent_usage(filters=filters, limit=limit)}


def get_usage_summary(config: dict, params: dict) -> dict:
    """Aggregated usage buckets for the charts. bucket=hour|day,
    metric=tokens|cost|turns, group_by=model|intent|user_iri, from?, to?."""
    _check_params(GetUsageSummaryParams, params)
    from .storage import default_storage
    storage = default_storage()
    return {"summary": storage.usage_summary(
        bucket=params.get("bucket") or "day",
        metric=params.get("metric") or "tokens",
        group_by=params.get("group_by"),
        from_ts=params.get("from"),
        to_ts=params.get("to"),
    )}
