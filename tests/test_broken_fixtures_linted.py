"""S7 negative tests: deliberately-broken eval fixtures the linter MUST flag.

The S5/S6/S7 troubleshooting cluster deploys broken playbooks and asks the
assistant to diagnose/fix them. S7's bar is narrower and mechanical: the linter
must flag the defect BEFORE a push. Each broken fixture under
`scripts/eval_fixtures/broken_*.yaml` therefore doubles as a negative test here --
if `validate_yaml` ever stops flagging one, this suite reddens.

First anchor: `broken_create_record_bad_args.yaml` -- a create_record whose record
body is written under a key the step does not take. It compiles clean and fails
only at runtime (`insert_data() takes at least 2 positional arguments`) unless
the linter checks arguments per handler; the resolver flags the dropped key as
`unknown_param`.

That check lives in the RESOLVER, not the parser: step arguments are hoisted to
the step's top level, so there is no leftover key for the parser to notice. Any
lint path that skips the resolver passes this file -- which is exactly what
`validate_yaml` did until it was wired to run the resolver pass.
"""
from __future__ import annotations

from pathlib import Path


_FIXTURES = Path(__file__).resolve().parent.parent / "scripts" / "eval_fixtures"


def _ops():
    from connector_fsr_agentic_assistant.operations import operations
    return operations


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _diagnostics(out: dict) -> list:
    """validate_yaml returns warnings under `errors` (each carries `severity`);
    a fully-broken input returns them under `errors` too. Merge both so a test
    doesn't care which bucket a given release routes a warning into."""
    return list(out.get("errors") or []) + list(out.get("warnings") or [])


def test_broken_create_record_bad_args_is_flagged(config):
    """The misnamed record-body key must surface as an unknown_param diagnostic
    naming the key -- the exact signal that was missing when this class of bug
    reached runtime."""
    out = _ops()["validate_yaml"](config, {"yaml": _load("broken_create_record_bad_args.yaml")})
    hits = [
        d for d in _diagnostics(out)
        if d.get("code") == "unknown_param"
        and "resource_fields" in str(d.get("message", ""))
    ]
    assert hits, f"linter did not flag the misnamed record-body key; got {out}"
    assert out.get("ok") is False


def test_correct_fixture_is_not_flagged(config):
    """The corrected shape (`marker_emitter.yaml`) must NOT trip the unknown-
    argument check -- otherwise the negative test above proves nothing (an
    always-firing linter is this plan's original bug one level up)."""
    out = _ops()["validate_yaml"](config, {"yaml": _load("marker_emitter.yaml")})
    bad = [d for d in _diagnostics(out) if d.get("code") == "unknown_param"]
    assert not bad, f"corrected fixture wrongly flagged: {bad}"
    assert out.get("ok") is True
