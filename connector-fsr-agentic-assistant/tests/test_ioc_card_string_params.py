"""Regression: the IOC enrichment-card builder must tolerate a STRING `params`.

run_op accepts a stringified-JSON `params` (dispatch coerces it), but the
recorded transcript still carries the model's original string. `_ioc_extract`
then received a str and crashed `params.get(...)` with
`AttributeError: 'str' object has no attribute 'get'`, aborting the whole
chat_turn finalization (observed on a live enrich-then-block turn).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from wire_enrichment import _ioc_extract  # noqa: E402

# A minimal FortiGuard IOC-search result with a real risk signal.
_CONTENT = {"ok": True, "data": [{
    "risk_score": 90,
    "recordTags": ["botnet"],
    "geoip": {"country": "CN"},
}]}


def test_string_params_does_not_crash_and_resolves_indicator():
    rec = _ioc_extract("fortinet-fortiguard-ioc", "ioc_search",
                       '{"indicator": "116.12.57.43"}', _CONTENT)
    assert rec is not None
    assert rec["indicator"] == "116.12.57.43"
    assert rec["verdict"] == "malicious"


def test_dict_params_still_work():
    rec = _ioc_extract("fortinet-fortiguard-ioc", "ioc_search",
                       {"indicator": "116.12.57.43"}, _CONTENT)
    assert rec["indicator"] == "116.12.57.43"


def test_unparseable_or_nondict_params_degrade_gracefully():
    for bad in ("not json", "[1,2,3]", None, 42):
        rec = _ioc_extract("fortinet-fortiguard-ioc", "ioc_search", bad, _CONTENT)
        # Still builds a card (indicator falls back), never raises.
        assert rec is not None and rec["indicator"] == "indicator"
