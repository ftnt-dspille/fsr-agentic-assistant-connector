"""Every authored model is REACHABLE from production code.

The third instance of one bug class. #33 found 50 of 79 models wired to
nothing; #49 found the op gate green over 17 ops it could not see; #50 found
six storage models checked by nothing but their own unit tests. Each time the
audit was a grep someone ran by hand, and each time the models it cleared
drifted again. So the audit itself becomes the gate.

WHAT "REACHABLE" MEANS -- and why it is transitive rather than a flat grep:

A flat "is this name mentioned outside tests?" sweep is wrong in both
directions. It reports base classes (`_WireBase`), nested row models
(`SessionSummary`), and union members as unused when they are load-bearing --
and a hand-run version of exactly that sweep called `PollFrame` dead when
`ChatPollResponse.frames` is `list[PollFrame]`, validated on every poll. So
reachability is a closure: seed with the models production code names
directly, then follow references THROUGH model definitions.

Two deliberate narrowings, both learned from being wrong:

* **Code only.** Docstrings and comments are stripped before matching. A model
  named in prose is documented, not wired -- and treating a `See FooEvent`
  mention as a reference is how a dead model looks alive.
* **Tests do not seed.** A model whose only caller is its own unit test is the
  precise state #50 was filed on. It must be reachable from something that
  runs in production, or be written down below.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODELS_FILE = ROOT / "pydantic_models.py"


# Models that production code legitimately never reaches. Same posture as
# test_op_params_gate's opt-outs: a reason someone wrote down beats a silence
# the suite reports as coverage.
UNREACHED_BY_DESIGN = {
    "ToolOk": "deliberately near-empty -- it exists so the success/error split "
              "is EXPRESSIBLE and so `ok` cannot be typed as a non-bool. "
              "`tool_err` builds the error half; the success half is "
              "constructed by the tools themselves, in the framework.",
}


def _code_only(text: str) -> str:
    """Source with every docstring blanked and comments dropped (ast.unparse
    does not emit comments). A mention in prose is not a reference."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0].value.value = ""
    return ast.unparse(tree)


def _definition_blocks(src: str) -> dict[str, str]:
    """Top-level model definitions, keyed by name. Covers `class Foo(...)` and
    the `Foo = Annotated[Union[...]]` union aliases (`PollFrame`), which are
    models for this purpose -- and were the exact thing a flat grep missed."""
    starts = [(m.start(), m.group(1) or m.group(2)) for m in
              re.finditer(r'^(?:class (\w+)\(|(\w+) = Annotated\[)', src, re.M)]
    blocks = {}
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(src)
        blocks[name] = src[pos:end]
    return blocks


def _production_sources() -> list[str]:
    """Every .py in the connector that is not a test and not the model file."""
    out = []
    for p in ROOT.rglob("*.py"):
        # Match path PARTS under ROOT, never a substring of the absolute path:
        # a checkout living under a directory named like "conn_tests" would
        # otherwise exclude every production file and report vacuous coverage.
        parts = p.relative_to(ROOT).parts
        if (".venv" in parts or any(part.startswith("test") for part in parts)
                or p == MODELS_FILE):
            continue
        out.append(_code_only(p.read_text()))
    return out


BLOCKS = _definition_blocks(_code_only(MODELS_FILE.read_text()))
NAMES = set(BLOCKS)
def _module_level_functions(src: str) -> str:
    """The bodies of `pydantic_models.py`'s own top-level functions.

    They are production code even though they live in the model file --
    `tool_err` is the constructor every tool error envelope goes through, so a
    model it builds is wired, not orphaned. Not blocks (only classes and union
    aliases are), so they are folded into the production seed instead."""
    tree = ast.parse(src)
    return "\n".join(ast.unparse(n) for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))


PRODUCTION = "\n".join(
    _production_sources()
    + [_module_level_functions(_code_only(MODELS_FILE.read_text()))])


def _reachable() -> set[str]:
    seed = {n for n in NAMES if re.search(rf'\b{n}\b', PRODUCTION)}
    reach, frontier = set(seed), set(seed)
    while frontier:
        nxt = set()
        for n in frontier:
            for other in NAMES:
                if (other != n and other not in reach
                        and re.search(rf'\b{other}\b', BLOCKS[n])):
                    reach.add(other)
                    nxt.add(other)
        frontier = nxt
    return reach


REACHABLE = _reachable()


def test_the_sweep_actually_parsed_something():
    """A regex that matched nothing would make the assertions below vacuous --
    the failure mode this whole file exists to prevent."""
    assert len(NAMES) > 100, len(NAMES)
    assert "PollFrame" in NAMES, "the union aliases must be swept too"
    assert len(PRODUCTION) > 100_000, "production sources did not load"
    assert len(REACHABLE) > 100, sorted(NAMES - REACHABLE)


def test_docstring_mentions_do_not_count_as_references():
    """Pins the narrowing itself. Without it a `See FooEvent` in prose makes a
    dead model look wired, which is how the hand-run audit that preceded this
    file cleared models it should have flagged."""
    stripped = _code_only('class A(B):\n    """See CapabilityGapEvent."""\n    x: int = 1\n')
    assert "CapabilityGapEvent" not in stripped


def test_every_model_is_reachable_from_production_code():
    orphans = sorted(NAMES - REACHABLE - set(UNREACHED_BY_DESIGN))
    assert not orphans, (
        f"authored but unreachable from production: {orphans}. Wire each one, "
        f"delete it, or add it to UNREACHED_BY_DESIGN with the reason. An "
        f"authored model that checks nothing is worse than no model, because "
        f"it reads as coverage -- #33, #49 and #50 were all this.")


@pytest.mark.parametrize("name", sorted(UNREACHED_BY_DESIGN))
def test_the_opt_outs_are_not_stale(name):
    """Two ways an opt-out rots: the model is deleted (dead permission that
    would silently excuse a NEW model of that name), or it gains a production
    caller and the entry now hides a real reference."""
    assert name in NAMES, f"{name} no longer exists -- drop the opt-out"
    assert name not in REACHABLE, (
        f"{name} IS reachable from production now -- remove it from "
        f"UNREACHED_BY_DESIGN so the sweep covers it.")
