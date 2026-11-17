"""Every tool ERROR envelope speaks `code` + `message` (PLAN.md Phase 5.3).

The Phase 5 pilot measured the two halves of the `{"ok": ...}` surface and they
came out completely different:

  * ERROR envelopes are near-uniform -- 93 of 94 already carried exactly
    `ok`/`code`/`message` plus tool-specific context. The one exception was the
    arg-validation gate, returning `{"ok": False, "error": "..."}`, which is the
    envelope the model reads in order to CORRECT itself.
  * SUCCESS envelopes share nothing but `ok`: `op`, `query`, `recording`,
    `calls`, `guidance`, `how_to_drive`, `by_event_type`, ... 34 distinct key
    sets across 110 returns.

So retyping all 78 individually was the wrong shape of fix -- it would have
pinned the half that is already consistent and could not usefully pin the half
that is not. This file is the cheap alternative the plan itself proposed: a
STATIC assertion over the source, which catches a dropped key in a module
nobody converted, and in a code path no test happens to execute.

It is a source grep on purpose. A runtime test only sees the envelopes some
test drives; these tools mostly fail against a live appliance, so their error
paths are exactly the ones a unit suite does not reach.
"""
import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "fsr_soc_triage"

# `code` values are computed at runtime in a few places (an upstream status is
# passed through). Those still DECLARE both keys, which is all this asserts.
REQUIRED = {"code", "message"}


def _error_envelopes():
    """Every `return {"ok": False, ...}` dict literal in the tool package."""
    found = []
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Dict)):
                continue
            keys = {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "ok" not in keys:
                continue
            literal_false = any(
                isinstance(k, ast.Constant) and k.value == "ok"
                and isinstance(v, ast.Constant) and v.value is False
                for k, v in zip(node.value.keys, node.value.values))
            if literal_false:
                found.append((f"{path.name}:{node.lineno}", keys))
    return found


ENVELOPES = _error_envelopes()


def test_the_scan_found_envelopes():
    """A walker that silently matches nothing makes the whole file vacuous --
    the failure mode this repo has hit more than once. (The record, hunt and
    playbook-run tools moved to the framework with their own copy of this
    scan, fsr_playbooks/tests/test_record_hunt_error_envelopes.py.)"""
    assert len(ENVELOPES) > 5, f"only found {len(ENVELOPES)}"


@pytest.mark.parametrize("where,keys", ENVELOPES, ids=[e[0] for e in ENVELOPES])
def test_error_envelope_declares_code_and_message(where, keys):
    missing = REQUIRED - keys
    assert not missing, (
        f"{where} returns an error envelope without {sorted(missing)}. The "
        f"agent branches on `code` and reads `message`; an envelope missing "
        f"either leaves it re-parsing prose. Use `pydantic_models.tool_err()`.")


def test_tool_err_builds_the_canonical_shape():
    """The constructor the message above points at has to produce what this
    file demands, or the advice is wrong."""
    import sys
    sys.path.insert(0, str(PKG.parent))
    from pydantic_models import tool_err

    out = tool_err("some_code", "some message", op="an_op", query={"a": 1})
    assert out["ok"] is False
    assert out["code"] == "some_code" and out["message"] == "some message"
    # Tool-specific context rides along untouched -- the payload is not the
    # thing being constrained here.
    assert out["op"] == "an_op" and out["query"] == {"a": 1}


def test_tool_err_refuses_to_build_a_broken_envelope():
    """The construction path has to actually reject, or it is just a dict
    literal with extra steps."""
    import sys
    sys.path.insert(0, str(PKG.parent))
    from pydantic import ValidationError
    from pydantic_models import tool_err

    with pytest.raises(ValidationError):
        tool_err("some_code", None)  # message is not optional
