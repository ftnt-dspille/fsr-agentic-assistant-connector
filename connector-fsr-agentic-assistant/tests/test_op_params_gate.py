"""Inbound op-params and outbound op-response validation: every op that HAS a
model uses it.

Deliberately NOT strict about unknown keys. The widget and the connector ship
independently, so a newer widget sending a key an older connector has never
heard of must keep working -- these models are all `extra="allow"` and the gate
only catches genuine type errors. Strictness belongs on the LLM tool-arg
boundary (untrusted generator, no version-skew problem), not here.

The op -> model map used to be hand-written here, which is the parallel-name-list
drift class: authoring a model and forgetting to wire it left the suite green,
and that is how 50 of 79 models ended up connected to nothing. It is now DERIVED
from `_LIVE_OPERATIONS` + the naming convention, so a newly authored
`FooParams` / `FooResponse` for an op named `foo` fails this file until
something wires it.
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pydantic_models as M  # noqa: E402

# The op bodies live in operations.py and the domain modules (ops_*.py) since the split.
SRC = "\n\n".join(
    "\n".join(l for l in p.read_text().splitlines() if not l.startswith("from __future__"))
    for p in sorted(ROOT.glob("ops_*.py")) + [ROOT / "operations.py"])


def _dict_keys(assign_name: str) -> list[str]:
    """String keys of a module-level dict literal, read with `ast`.

    Was a regex (`"(\\w+)":` inside a `^_LIVE_OPERATIONS = {...^}` block). Two
    ways that under-reports, and under-reporting here is invisible: an op keyed
    with SINGLE quotes, or a key that is a constant/variable rather than a
    literal, never becomes a pair -- so it is not a failing case, it is simply
    not a case, which is the #49 defect exactly. `ast` reads the dict the
    interpreter actually builds. (#56)"""
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if assign_name not in targets or not isinstance(node.value, ast.Dict):
            continue
        keys = [k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        non_literal = [k for k in node.value.keys
                       if not (isinstance(k, ast.Constant) and isinstance(k.value, str))]
        assert not non_literal, (
            f"{assign_name} has {len(non_literal)} non-literal key(s); this gate "
            f"derives its cases from the literals and would silently skip them")
        return keys
    raise AssertionError(f"{assign_name} not found -- the gate cannot derive anything")


def _op_def(op: str):
    """The top-level `def <op>` node, found by AST so reflowing or
    re-commenting an op's signature cannot hide its body from the gate."""
    for node in ast.parse(SRC).body:
        if isinstance(node, ast.FunctionDef) and node.name == op:
            return node
    return None


def _op_names() -> list[str]:
    return _dict_keys("_LIVE_OPERATIONS")


def _response_model_wiring() -> dict:
    """`op -> model name` from the `_RESPONSE_MODELS` dict, via `ast`."""
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if "_RESPONSE_MODELS" not in [t.id for t in node.targets if isinstance(t, ast.Name)]:
            continue
        out = {}
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and isinstance(value, ast.Name):
                out[key.value] = value.id
        return out
    raise AssertionError("_RESPONSE_MODELS not found")


def _model_for(op: str, suffix: str):
    """`get_session_trace` + 'Params' -> GetSessionTraceParams, if it exists.

    `Response` also accepts the `*Result` spelling: `list_agent_tools` and
    `list_mcp_servers` named theirs `ListAgentToolsResult` /
    `ListMcpServersResult`, and a naming inconsistency must not read as a
    missing model."""
    camel = "".join(part.title() for part in op.split("_"))
    found = getattr(M, camel + suffix, None)
    if found is None and suffix == "Response":
        found = getattr(M, camel + "Result", None)
    return found


OPS = _op_names()
PARAMS_PAIRS = [(op, _model_for(op, "Params")) for op in OPS
                if _model_for(op, "Params") is not None]
RESPONSE_PAIRS = [(op, _model_for(op, "Response")) for op in OPS
                  if _model_for(op, "Response") is not None]


# ---------------------------------------------------------------------------
# Presence opt-outs (#49)
#
# Deriving the op -> model map killed the parallel-name-list drift, but a
# derived pairing can only see an op whose model EXISTS. An op with no model is
# not a failing pair; it is not a pair at all -- so the gate was green over 17
# of 38 ops while checking nothing there. Same shape as the vacuous-grader and
# never-ran-test findings: a green result that disagrees with the running
# system.
#
# So presence is now asserted, and "this op needs no model" has to be WRITTEN
# DOWN with a reason instead of being inferred from silence. Adding an op with
# no model fails this file until someone either authors the model or lands here
# deliberately.
# ---------------------------------------------------------------------------

NO_PARAMS_MODEL = {
    "echo_param_types": "type-echo diagnostic; reflects whatever it is given, "
                        "so there is no shape to pin",
    "list_models": "reads config only -- feeds a picklist",
    "list_mcp_tools": "no params; enumerates the configured servers",
    "list_scheduled_tasks": "no params; returns every stored task def",
    "list_pending": "no params; returns every live HITL-parked session",
    "get_autonomy_policy": "no params; reads the single autonomy policy record",
}

NO_RESPONSE_MODEL = {
    "call_mcp_tool": "deliberate passthrough of the downstream tool's own "
                     "result -- modelling it would pin a shape we do not own",
    "list_models": "returns a bare list, so it is checked per-ROW via "
                   "_RESPONSE_ROW_MODELS (ModelChoice) instead",
}


def test_the_derivation_actually_found_ops():
    """A regex that silently matches nothing would make every test below vacuous."""
    assert len(OPS) > 30, OPS
    assert len(PARAMS_PAIRS) > 10
    assert len(RESPONSE_PAIRS) > 10


def test_every_op_has_a_params_model_or_a_written_down_reason():
    missing = [op for op in OPS
               if _model_for(op, "Params") is None and op not in NO_PARAMS_MODEL]
    assert not missing, (
        f"ops with no params model and no opt-out: {missing}. Author "
        f"`<CamelOp>Params` and wire it with `_check_params`, or add the op to "
        f"NO_PARAMS_MODEL with the reason it takes no params. A gate that is "
        f"silent about an op it cannot see reports absence as coverage.")


def test_every_op_has_a_response_model_or_a_written_down_reason():
    missing = [op for op in OPS
               if _model_for(op, "Response") is None and op not in NO_RESPONSE_MODEL]
    assert not missing, (
        f"ops with no response model and no opt-out: {missing}. Author "
        f"`<CamelOp>Response` and add it to `_RESPONSE_MODELS`, or add the op "
        f"to NO_RESPONSE_MODEL with the reason.")


@pytest.mark.parametrize("registry,label", [(NO_PARAMS_MODEL, "params"),
                                            (NO_RESPONSE_MODEL, "response")])
def test_the_opt_out_lists_do_not_name_ops_that_no_longer_exist(registry, label):
    """An opt-out for a deleted op is dead permission that would silently
    excuse a NEW op of the same name."""
    stale = sorted(set(registry) - set(OPS))
    assert not stale, f"stale {label} opt-outs: {stale}"


@pytest.mark.parametrize("registry,suffix", [(NO_PARAMS_MODEL, "Params"),
                                             (NO_RESPONSE_MODEL, "Response")])
def test_an_opt_out_op_did_not_quietly_gain_a_model(registry, suffix):
    """If someone authors the model, the opt-out must go -- otherwise the model
    is unwired and unchecked, which is the exact state #33 was filed on."""
    now_modelled = [op for op in registry if _model_for(op, suffix) is not None]
    assert not now_modelled, (
        f"these ops have a {suffix} model but are still opted out of the "
        f"presence gate: {now_modelled}. Remove the opt-out so the wiring "
        f"tests above apply to them.")


@pytest.mark.parametrize("op,model", PARAMS_PAIRS, ids=[p[0] for p in PARAMS_PAIRS])
def test_op_validates_its_params(op, model):
    """The gate must be INSIDE the op, not merely imported."""
    # Keyword-only arguments after (config, params) are internal callers' only
    # (chat_resume's _policy_decider); the connector runtime passes two.
    fn = _op_def(op)
    assert fn is not None, f"{op} is not defined in the connector source"
    assert [a.arg for a in fn.args.args][:2] == ["config", "params"], (
        f"{op} does not take (config, params)")
    body = ast.get_source_segment(SRC, fn) or ""
    name = model.__name__
    assert (f"_check_params({name}, params)" in body
            or f"_validate_model({name}, params" in body), (
        f"{op} does not validate its params through {name}. Either wire it or "
        f"delete the model -- an authored-but-unwired model is worse than none, "
        f"because it reads as coverage that does not exist.")


@pytest.mark.parametrize("op,model", RESPONSE_PAIRS, ids=[p[0] for p in RESPONSE_PAIRS])
def test_op_validates_its_response(op, model):
    """Wired at the dispatch seam (`_RESPONSE_MODELS`), inline, or by being
    CONSTRUCTED -- `list_agent_tools` / `list_mcp_servers` build their result
    through the model and dump it, which is the strongest form of the check
    (every field goes through it) even though nothing validates afterwards."""
    name = model.__name__
    # The dispatch-seam arm reads the PARSED `_RESPONSE_MODELS` dict rather than
    # searching the whole file for `"op": Model,`. A whole-file substring passes
    # on a match anywhere -- a comment, an unrelated dict, a docstring example --
    # so it could report wiring that does not exist. It also depended on a
    # trailing comma, which the last entry need not have. (#56)
    assert (_response_model_wiring().get(op) == name
            or f"_validate_model({name}," in SRC
            or f"return {name}(" in SRC), (
        f"{op} has a {name} model that nothing checks. Add it to "
        f"_RESPONSE_MODELS or delete the model.")


@pytest.mark.parametrize("op,model", PARAMS_PAIRS, ids=[p[0] for p in PARAMS_PAIRS])
def test_the_params_model_is_permissive(op, model):
    assert model.model_config.get("extra") != "forbid", (
        f"{model.__name__} is strict -- an older connector would reject a newer "
        f"widget's new key. Op params must stay permissive; a required field is "
        f"how you catch a missing or misnamed one.")


@pytest.mark.parametrize("op,model", RESPONSE_PAIRS, ids=[p[0] for p in RESPONSE_PAIRS])
def test_the_response_model_is_permissive(op, model):
    assert model.model_config.get("extra") != "forbid", (
        f"{model.__name__} is strict -- it validates our OWN output, so a new "
        f"key would fail a turn that otherwise succeeded.")


def test_unknown_keys_are_tolerated():
    """Version skew: a key this connector has never heard of must not error."""
    M.QueryStoreParams(sql="SELECT 1", a_key_from_a_newer_widget=True)
    M.ValidateYamlParams(yaml="name: x", a_key_from_a_newer_widget=True)


def test_a_genuine_type_error_is_rejected():
    """The gate has to actually catch something, or it is decoration."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        M.QueryStoreParams(sql={"not": "a string"})


def test_a_misnamed_required_field_is_still_rejected():
    """What the yaml params models were made strict FOR. Permissiveness about
    unknown keys does not cost this: `yaml` is required, so the old wrong name
    fails on the missing field rather than on the extra one."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        M.ValidateYamlParams(yaml_text="name: x")


# ---------------------------------------------------------------------------
# Self-proof (#56): a derivation gate is only as good as what it can SEE.
#
# Both defects this file was built for (#33, #49) were the same shape -- the
# gate could not observe part of its own subject and reported that blindness as
# coverage. So the derivation gets a fixture of the spellings it must handle.
# ---------------------------------------------------------------------------

def test_the_key_reader_sees_every_spelling():
    """`_dict_keys` must read the dict the interpreter builds, not one quote style."""
    import textwrap

    module = textwrap.dedent('''
        _SAMPLE = {
            "double": alpha,
            'single': bravo,   # the regex could not see this one
            "trailing_comma_absent": charlie
        }
    ''')
    global SRC
    original, SRC = SRC, module
    try:
        assert set(_dict_keys("_SAMPLE")) == {
            "double", "single", "trailing_comma_absent"}
    finally:
        SRC = original


def test_the_key_reader_refuses_to_skip_a_computed_key():
    """A non-literal key is not a pair, and 'not a pair' is how #49 hid. It must
    fail loudly rather than quietly narrow the case list."""
    import textwrap

    module = textwrap.dedent('''
        _SAMPLE = {
            "literal": alpha,
            SOME_CONSTANT: bravo,
        }
    ''')
    global SRC
    original, SRC = SRC, module
    try:
        with pytest.raises(AssertionError, match="non-literal"):
            _dict_keys("_SAMPLE")
    finally:
        SRC = original
