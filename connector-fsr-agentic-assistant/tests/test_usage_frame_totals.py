"""The usage frame must carry a turn-correct input total, and the logged cost
must price cache writes.

Two provider shapes report the same usage differently:

* Anthropic -- ``input_tokens`` EXCLUDES the cache counters. A cache-hit round
  reads ``input_tokens=3, cache_read=15202``; the real input is 15,205.
* OpenAI -- ``cached_tokens`` is a SUBSET of ``input_tokens``, so
  ``input_tokens`` already is the total.

The widget header used to add ``input_tokens + output_tokens`` for both, so an
Anthropic cache hit showed roughly 1.7k tokens instead of roughly 17k. The
connector knows the model family, so it normalizes once here (``total_input``
and ``total_tokens`` on the frame) and the widget does not re-derive it.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)
if CONNECTOR_DIR not in sys.path:
    sys.path.insert(0, CONNECTOR_DIR)

provider = pytest.importorskip("fsr_playbooks.llm.provider")

ANTHROPIC = "claude-haiku-4-5-20251001"  # in 0.80 / out 4.00 per Mtok
OPENAI = "gpt-4.1-mini"                  # in 0.40 / cached 0.10 / out 1.60


def _ops():
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    return importlib.import_module("fsrpb_connector.operations")


def _ops_usage():
    _ops()
    return importlib.import_module("fsrpb_connector.ops_usage")


def _usage(model, input_tokens, output_tokens, cache_read=0, cache_write=0):
    return provider.UsageEvent(
        session_id="s", turn=1, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens, cache_read=cache_read,
        cache_write=cache_write, history_chars=0, stop_reason="end_turn",
        tool_calls=[])


def test_anthropic_cache_hit_frame_totals_the_disjoint_input():
    frame = _ops()._event_to_wire(_usage(ANTHROPIC, 3, 1745, cache_read=15202))
    assert frame["total_input"] == 3 + 15202
    assert frame["total_tokens"] == 3 + 15202 + 1745
    # The raw counters stay on the frame for anyone who wants the split.
    assert frame["input_tokens"] == 3
    assert frame["cache_read"] == 15202


def test_anthropic_cache_write_is_part_of_the_input_total():
    frame = _ops()._event_to_wire(_usage(ANTHROPIC, 50, 200, cache_write=248))
    assert frame["total_input"] == 50 + 248
    assert frame["total_tokens"] == 50 + 248 + 200


def test_openai_input_already_contains_the_cached_tokens():
    frame = _ops()._event_to_wire(_usage(OPENAI, 11886, 196, cache_read=11776))
    assert frame["total_input"] == 11886
    assert frame["total_tokens"] == 11886 + 196


def test_frame_cost_still_prices_the_cache():
    frame = _ops()._event_to_wire(_usage(ANTHROPIC, 3, 1745, cache_read=15202))
    want = _ops_usage()._usage_cost_usd(ANTHROPIC, 3, 1745, 15202, 0)
    assert frame["cost_usd"] == pytest.approx(want, abs=5e-7)


def test_turn_cost_helper_includes_cache_write():
    """The chat-turn log row (agent_usage / llm activity) used to pass only
    cache_read to the cost function, so a round that wrote the cache was
    logged at a lower cost than the same round on the usage frame."""
    ev = _usage(ANTHROPIC, 50, 200, cache_write=248)
    got = _ops_usage()._usage_event_cost_usd(ev)
    want = (50 * 0.80 + 248 * 0.80 * 1.25 + 200 * 4.00) / 1_000_000
    assert got == pytest.approx(want, abs=5e-7)
    assert got == pytest.approx(
        _ops_usage()._usage_cost_usd(ANTHROPIC, 50, 200, 0, 248), abs=5e-7)


def test_turn_cost_helper_matches_the_frame_cost():
    for ev in (_usage(ANTHROPIC, 3, 1745, cache_read=15202),
               _usage(ANTHROPIC, 50, 200, cache_write=248),
               _usage(OPENAI, 11886, 196, cache_read=11776)):
        frame = _ops()._event_to_wire(ev)
        assert _ops_usage()._usage_event_cost_usd(ev) == pytest.approx(
            frame["cost_usd"], abs=5e-7)


def test_haiku_5_5_is_priced_not_zero():
    """A model switch to claude-haiku-5-5 must not read back as $0 cost -- the
    id has to carry a price entry (0.10 in / 0.50 out per Mtok)."""
    ou = _ops_usage()
    cost = ou._usage_cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000, 0, 0)
    assert cost == pytest.approx(0.1 + 0.5, abs=5e-7)
    assert cost > 0


def test_unpriced_paid_model_warns_once_but_free_model_is_silent(monkeypatch):
    """An unknown PAID model priced at $0 silently zeroes the usage ledger, so
    warn once per id. A FREE gateway model priced at $0 is correct -- stay
    silent, so the warning stays a real signal."""
    ou = _ops_usage()
    ou._WARNED_UNPRICED.clear()
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(ou, "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    ou._usage_cost_usd("claude-made-up-9", 100, 100, 0, 0)
    ou._usage_cost_usd("claude-made-up-9", 100, 100, 0, 0)  # deduped
    assert sum(1 for _, msg in seen if "claude-made-up-9" in msg) == 1
    assert seen[0][0] == "warning"
    ou._usage_cost_usd("qwen3.8-27b-nvfp4", 100, 100, 0, 0)  # free -> silent
    assert not any("qwen" in msg for _, msg in seen)
