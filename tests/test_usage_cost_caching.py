"""Cost accounting with prompt-cache discounts.

The trap these tests exist for: OpenAI reports cached tokens as a SUBSET of
`input_tokens`, Anthropic reports them DISJOINT from it. Pricing both the same
way is silently wrong — no error, just a bill that's off by a large factor —
so each provider shape gets an exact-arithmetic assertion rather than a
"roughly cheaper than uncached" smoke check.
"""
import pytest

from connector_fsr_agentic_assistant.operations import (  # noqa: E402
    _PRICE_CACHED_IN_PER_MTOK,
    _PRICE_IN_PER_MTOK,
    _PRICE_OUT_PER_MTOK,
    _usage_cost_usd,
)


M = "gpt-4.1-mini"  # in 0.40 / cached 0.10 / out 1.60 per Mtok


def test_openai_cached_tokens_are_subtracted_from_input():
    """cache_read is INSIDE input_tokens — pricing both at full rate double-bills."""
    # 11886 prompt tokens of which 11776 were cache hits (a real measurement).
    got = _usage_cost_usd(M, 11886, 196, cache_read=11776, cache_write=0)
    want = (110 * 0.40 + 11776 * 0.10 + 196 * 1.60) / 1_000_000
    # abs tolerance, not rel: the impl rounds to 6dp (sub-microdollar).
    assert got == pytest.approx(want, abs=5e-7)


def test_openai_cache_hit_is_cheaper_than_the_same_turn_cold():
    cold = _usage_cost_usd(M, 11886, 196)
    warm = _usage_cost_usd(M, 11886, 196, cache_read=11776)
    assert warm < cold
    # 4.1-mini caches at 0.25x input, so a ~99% hit should land near a third of
    # cold cost once the (unchanged) output charge is included.
    assert warm == pytest.approx(cold / 3.3, rel=0.05)


def test_no_cache_read_matches_the_old_uncached_formula():
    """Back-compat: the 2-arg call path must price exactly as it did before."""
    got = _usage_cost_usd(M, 5000, 500)
    want = (5000 * _PRICE_IN_PER_MTOK[M] + 500 * _PRICE_OUT_PER_MTOK[M]) / 1_000_000
    assert got == pytest.approx(want, abs=5e-7)


def test_anthropic_counters_are_disjoint_and_not_subtracted():
    """A non-zero cache_write selects the Anthropic shape: nothing is subtracted."""
    a = "claude-haiku-4-5-20251001"  # in 0.80 / out 4.00
    got = _usage_cost_usd(a, 50, 200, cache_read=100_000, cache_write=248)
    want = (50 * 0.80 + 100_000 * 0.80 * 0.1 + 248 * 0.80 * 1.25
            + 200 * 4.00) / 1_000_000
    assert got == pytest.approx(want, abs=5e-7)


def test_anthropic_pure_cache_hit_does_not_subtract_the_read():
    """Regression: a Claude turn with cache_read but cache_write == 0.

    Observed live on the second turn of a session: `input_tokens=3,
    cache_read=15202, cache_write=0`. Dispatching on `cache_write > 0` sent this
    down the OpenAI branch, which subtracts cache_read from input_tokens and
    zeroed a genuinely billable input. Provider must be decided by model name.
    """
    a = "claude-haiku-4-5-20251001"  # in 0.80 / out 4.00
    got = _usage_cost_usd(a, 1000, 100, cache_read=900, cache_write=0)
    # Disjoint: all 1000 input tokens are billable, the 900 read at 0.1x.
    want = (1000 * 0.80 + 900 * 0.80 * 0.1 + 100 * 4.00) / 1_000_000
    assert got == pytest.approx(want, abs=5e-7)
    # And specifically NOT the subtracting form.
    wrong = (100 * 0.80 + 900 * 0.80 * 0.1 + 100 * 4.00) / 1_000_000
    assert got != pytest.approx(wrong, abs=5e-7)


@pytest.mark.parametrize("model", [
    "claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-7",
    "anthropic.claude-sonnet-4-6-v1:0", "us.anthropic.claude-opus-4-7-v1:0",
])
def test_anthropic_ids_are_recognized_in_all_deployment_forms(model):
    from connector_fsr_agentic_assistant.operations import _is_anthropic_model
    assert _is_anthropic_model(model)


@pytest.mark.parametrize("model", [
    "gpt-4.1-mini", "gpt-5.4-nano", "o4-mini",
    "coding-b200/glm-5.2-nvfp4-highthroughput", "",
])
def test_non_anthropic_ids_are_not_misclassified(model):
    from connector_fsr_agentic_assistant.operations import _is_anthropic_model
    assert not _is_anthropic_model(model)


def test_unknown_model_degrades_to_zero_not_an_exception():
    assert _usage_cost_usd("some-future-model", 1000, 100, cache_read=900) == 0.0


def test_negative_and_none_cache_counters_are_clamped():
    base = _usage_cost_usd(M, 1000, 100)
    assert _usage_cost_usd(M, 1000, 100, cache_read=None) == base
    assert _usage_cost_usd(M, 1000, 100, cache_read=-5) == base


def test_cache_read_exceeding_input_never_goes_negative():
    """Defensive: a provider bug reporting cache_read > input must not credit us."""
    got = _usage_cost_usd(M, 100, 10, cache_read=99999)
    assert got > 0


@pytest.mark.parametrize("model", sorted(_PRICE_CACHED_IN_PER_MTOK))
def test_every_cached_price_has_an_input_and_output_price(model):
    """A cached rate without its base rates would price the uncached remainder
    at 0.0 — a partial table is worse than no table."""
    assert model in _PRICE_IN_PER_MTOK, f"{model} missing input price"
    assert model in _PRICE_OUT_PER_MTOK, f"{model} missing output price"


@pytest.mark.parametrize("model", sorted(_PRICE_CACHED_IN_PER_MTOK))
def test_cached_rate_is_cheaper_than_uncached_input(model):
    assert _PRICE_CACHED_IN_PER_MTOK[model] < _PRICE_IN_PER_MTOK[model]
