"""T0 preflight gate (offline-testing plan, Task B).

Runs `scripts/preflight.py`'s whole scenario registry in CI. A red test here
means a **discovery/catalog defect** — the scenario's ask names something the
tools cannot resolve — not a model defect. Catching that here costs
milliseconds; catching it in a live S-slice costs a release → ship → box eval.

Proven against the real regression it exists for: pinning the pre-`8ea4ec2`
`find_connector` back in (whole-phrase LIKE, broadening on `words[0]` only)
turns `s3` red with the true diagnostic —
`did not return 'cyops_utilities' — got ['fortisoar-ml-engine']`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import preflight  # noqa: E402


@pytest.mark.parametrize("name", sorted(preflight.SCENARIOS))
def test_scenario_preflight_resolves(name):
    """Every scenario's ask must resolve through the discovery tools.

    Parametrized per scenario deliberately: one failing slice names itself in
    the test id instead of hiding inside an aggregate.
    """
    res = preflight.check(name, preflight.SCENARIOS[name])
    assert res.ok, "\n".join(res.failures)


def test_connector_lookup_uses_the_asks_own_words():
    """The registry must preflight the HUMAN phrasing, not the canonical name.

    Preflighting `find_connector('cyops_utilities')` would pass trivially and
    prove nothing — the model never types the canonical name, it types what the
    analyst said. This guards the oracle against being defanged by a later
    'simplification'.
    """
    queries = [q for spec in preflight.SCENARIOS.values()
               for q, expect in spec.connectors]
    expects = [e for spec in preflight.SCENARIOS.values()
               for q, e in spec.connectors]
    assert queries, "no connector preflight left in the registry"
    assert not (set(queries) & set(expects)), (
        "a connector query equals its own expected canonical name — that is a "
        "tautological preflight")


def test_unresolvable_target_fails_loudly():
    """A bogus target must FAIL, not pass quietly. Without this, a preflight
    that silently resolves nothing reads exactly like a clean one."""
    bogus = preflight.Spec(
        why="guard", connectors=(("definitely not a real connector", "nope"),),
        step_types=("not_a_step_type",), tools=("not_a_tool",))
    res = preflight.check("bogus", bogus)
    assert not res.ok
    assert len(res.failures) == 3, res.failures
