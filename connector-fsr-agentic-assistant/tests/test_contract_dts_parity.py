"""`contract.d.ts` ↔ `pydantic_models.PollFrame` parity, enforced.

The widget and the connector live in **different repos** and ship
independently, and the wire contract is duplicated in both: a TypeScript
interface per frame in `contract.d.ts`, and a pydantic model per frame in
`pydantic_models.py`. Until now parity was a docstring asking nicely.

That is the parallel-name-list bug class in its most expensive form -- when the
two sides drift, the connector emits a frame the widget has no branch for (it
renders nothing, silently) or the widget waits for one the connector never
sends. Assert the *relationship*.

Cross-repo, so it SKIPS rather than fails when the widget checkout is not
present (CI for the connector alone must stay green). It is not skipped
silently on the developer machines that have both.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pydantic_models as M  # noqa: E402

# The widget repo sits beside the connector's containing repo. Try the known
# layout, then fall back to an env override so a non-standard checkout can opt in.
_CANDIDATES = [
    Path.home() / "WebstormProjects/fsr_all_widgets/widgets-src/"
    "agenticAssistant/widget/widgetAssets/js/contract.d.ts",
]


def _contract_dts() -> Path | None:
    import os
    env = os.environ.get("FSRPB_CONTRACT_DTS")
    if env and Path(env).is_file():
        return Path(env)
    for c in _CANDIDATES:
        if c.is_file():
            return c
    return None


def _ts_frame_types(text: str) -> set[str]:
    """Every `type: 'x'` discriminant declared by a WireEvent/PollFrame member.

    Not anchored to line start: several interfaces are declared inline on one
    line (`interface ActivityEvent { type: 'activity'; message?: string; }`), so
    a `^\\s*type:` anchor silently misses them and the parity check passes for
    the wrong reason.

    Both quote styles and both terminators are matched. The pattern used to
    require a single-quoted value AND a semicolon, so `type: "x";` or a
    comma-terminated `type: 'x',` (legal TypeScript, and what an inline object
    literal or a reformat produces) was invisible -- and invisible SHRINKS the
    declared set, which makes `test_every_widget_frame_type_has_a_connector_model`
    pass by seeing less. That is #56's failure mode: a matcher that cannot
    express part of its subject reporting the blindness as coverage.
    `test_the_matcher_sees_every_spelling` below is what keeps this honest."""
    return set(re.findall(r"""\btype:\s*['"]([a-z_]+)['"]\s*[;,}]""", text))


def test_the_matcher_sees_every_spelling():
    """Self-proof: the gate above is only as good as this pattern.

    Each fixture is a spelling that is legal in a `.d.ts` and has appeared, or
    could appear after a reformat. A gate that silently stops matching one of
    them under-reports, and under-reporting is what BOTH parity assertions read
    as agreement."""
    fixtures = {
        "single quotes, semicolon": "interface A { type: 'alpha'; }",
        "double quotes, semicolon": 'interface B { type: "bravo"; }',
        "comma terminated": "interface C { type: 'charlie', msg?: string }",
        "closing brace directly": "interface D { type: 'delta'}",
        "inline on one line": "interface E { type: 'echo'; message?: string; }",
        "extra whitespace": "interface F {  type:   'foxtrot' ; }",
    }
    for label, snippet in fixtures.items():
        found = _ts_frame_types(snippet)
        assert len(found) == 1, f"matcher missed the {label} spelling: {snippet!r}"

    # Comments are deliberately NOT excluded: this gate's risk is UNDER-matching
    # (a missed declaration reads as parity), and a `.d.ts` comment that spells
    # `type: 'x';` names a frame that exists. Over-matching here fails loudly on
    # the other side of the parity check instead of hiding.


def _py_frame_types() -> set[str]:
    """Every `type` Literal pinned by a PollFrame union member."""
    import typing
    types_ = set()
    for member in typing.get_args(typing.get_args(M.PollFrame)[0]):
        field = member.model_fields.get("type")
        args = typing.get_args(field.annotation) if field else ()
        if args:  # Literal["..."]; UnknownEvent's bare `str` has no args
            types_.update(a for a in args if isinstance(a, str))
    # UnknownEvent pins `type: str` rather than `Literal["unknown"]` precisely so
    # it can absorb ANY unrecognized frame -- but it is still the model for the
    # `unknown` frame `_event_to_wire` emits, and contract.d.ts declares it as
    # `UnknownEvent { type: 'unknown' }`. Without this it reads as a false gap.
    types_.add("unknown")
    return types_


@pytest.fixture(scope="module")
def dts():
    p = _contract_dts()
    if p is None:
        pytest.skip("widget contract.d.ts not present (set FSRPB_CONTRACT_DTS)")
    return p.read_text()


def test_every_widget_frame_type_has_a_connector_model(dts):
    """A frame the widget declares but the connector cannot construct."""
    missing = _ts_frame_types(dts) - _py_frame_types()
    assert missing == set(), (
        f"contract.d.ts declares {sorted(missing)} with no PollFrame member -- "
        f"the connector has no model to construct those frames through.")


def test_every_connector_frame_type_is_declared_by_the_widget(dts):
    """A frame the connector can emit that the widget has no branch for -- it
    would render as nothing at all, with no error anywhere."""
    missing = _py_frame_types() - _ts_frame_types(dts)
    assert missing == set(), (
        f"PollFrame has members {sorted(missing)} absent from contract.d.ts -- "
        f"the widget has no branch for those frames.")


def test_unknown_event_stays_last_in_the_union():
    """R6: `PollFrame` is `left_to_right`, and `UnknownEvent` pins `type: str`,
    which matches ANYTHING. If it is not last it swallows every frame after it
    and the specific models stop being reachable."""
    import typing
    members = typing.get_args(typing.get_args(M.PollFrame)[0])
    assert members[-1] is M.UnknownEvent, (
        "UnknownEvent must stay last in the PollFrame union -- its `type: str` "
        "matches any frame, so anything after it is unreachable.")
