"""Every outbound wire frame is constructed through its pydantic model.

Why this file exists: a hand-rolled `stream_end` dict once silently omitted
`last_assistant_yaml` while `StreamEndFrame` existed the whole time. That frame
is the only transcript delivery for a polling consumer, so the loss was
invisible -- the widget renders whatever arrives, and a missing key is a dead
Deploy button rather than an error. There were ~30 more hand-built frame dicts.

Two guards:
  1. a **source grep** so a new raw `{"type": ...}` literal cannot reappear
     (the regression gate -- the models only help if they are actually used);
  2. `_frame`'s **identity property**: `exclude_unset=True` means the dump
     carries exactly the keys passed in, explicit `None`s and `extra="allow"`
     extras included. That is what makes the whole conversion non-breaking, so
     it is asserted rather than assumed.
"""
import importlib.util
import pathlib
import re
import sys
import types

import pytest
from pydantic import TypeAdapter

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pydantic_models as M  # noqa: E402

PollFrame, StreamEndFrame, TextEvent = M.PollFrame, M.StreamEndFrame, M.TextEvent


def _load(name):
    """Load a connector module under a synthetic package -- the connector dir
    name has a hyphen, so `operations` is not importable normally. Same loader
    the other operations-level tests use."""
    for n in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(n, types.ModuleType(n))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    if "_fsrpb_conn" not in sys.modules:
        pkg = types.ModuleType("_fsrpb_conn")
        pkg.__path__ = [str(ROOT)]
        sys.modules["_fsrpb_conn"] = pkg
    key = f"_fsrpb_conn.{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def operations():
    return _load("operations")


@pytest.fixture(scope="module")
def storage():
    return _load("storage")


_SRC = ROOT


def _literals(path: pathlib.Path) -> list[str]:
    """Raw `{"type": "..."` frame literals, with the allowed exceptions.

    Allowed: dicts that are *inputs* to a model rather than emitted frames --
    i.e. the payload argument of `_as_frame(...)`, which is routed through the
    model within a few lines. Those are matched by an `_as_frame(` in the
    surrounding window.

    The pattern matches a VARIABLE type (`{"type": card_type,`) as well as a
    quoted one. It used to require a quote right after `"type": `, and that
    blind spot is exactly how the emit_* card splice kept a raw literal while
    this gate stayed green -- its type came from `_CARD_EMITTER_TO_TYPE`, so
    there was no quote to match. A regression gate that cannot see half the
    shape it guards reports absence as coverage."""
    lines = path.read_text().splitlines()
    hits = []
    for i, line in enumerate(lines):
        # Comments and docstrings discuss these literals at length; only code
        # can BE one.
        if line.lstrip().startswith(("#", "*", '"""')):
            continue
        # Both quote styles, and whitespace around the colon. The single-quoted
        # spelling `{'type': ...}` is equally legal Python and was invisible --
        # the same class of blind spot as the variable-type one this pattern was
        # widened for once already (#56).
        if not re.search(r"""\{\s*['"]type['"]\s*: """, line):
            continue
        # Look both directions: the payload dict may precede or follow its
        # `_as_frame(...)` call depending on whether it is inlined or named.
        window = "\n".join(lines[max(0, i - 3):i + 5])
        if "_as_frame(" in window or "_frame(" in window:
            continue
        hits.append(f"{path.name}:{i + 1}: {line.strip()}")
    return hits


def test_the_literal_scanner_sees_every_spelling(tmp_path):
    """Self-proof (#56): this gate has already been green over a shape it could
    not express -- the `_CARD_EMITTER_TO_TYPE` splice built `{"type": card_type,`
    and the pattern required a quote after the colon, so a raw frame literal sat
    on the path all seven emit_* cards take while this file reported clean.

    So the matcher now carries fixtures of the spellings it forbids. A gate that
    only asserts "the codebase is clean" cannot tell that apart from "the
    detector is broken"; these assert the detector works.
    """
    forbidden = {
        "quoted type": '    return {"type": "text", "text": msg}',
        "variable type": '    return {"type": card_type, "payload": p}',
        "single quotes": "    return {'type': 'text', 'text': msg}",
        "space before colon": '    return {"type" : "text"}',
        "extra space inside brace": '    return {  "type": "text"}',
    }
    for label, line in forbidden.items():
        f = tmp_path / "sample.py"
        f.write_text(line + "\n")
        assert _literals(f), f"scanner missed the {label} spelling: {line!r}"

    allowed = {
        "routed through _frame": '    return _frame(TextFrame, type="text", text=msg)',
        "payload arg of _as_frame": (
            '    return _as_frame(InfoCard, {"type": "info_card", "title": t})'),
        "a comment discussing one": '    # emits {"type": "text", ...} downstream',
    }
    for label, line in allowed.items():
        f = tmp_path / "sample.py"
        f.write_text(line + "\n")
        assert not _literals(f), f"scanner false-positives on the {label} case"


@pytest.mark.parametrize("name", ["operations.py", "storage.py"]
                         + sorted(p.name for p in _SRC.glob("ops_*.py")))
def test_no_raw_frame_literals(name):
    """No hand-built `{"type": ...}` wire frame outside `_frame`/`_as_frame`."""
    offenders = _literals(_SRC / name)
    assert not offenders, (
        "hand-built wire-frame literal(s) -- build these through their model "
        "via operations._frame() / _as_frame():\n  " + "\n  ".join(offenders))


def test_frame_output_is_identical_to_the_literal_it_replaced(operations):
    """`_frame` is a drop-in for the dict literal: same keys, same values.

    This is the property the whole conversion rests on. `exclude_none` would
    NOT satisfy it -- an explicit `None` is a key the widget may branch on."""
    got = operations._frame(TextEvent, type="text", text=None, custom=7)
    assert got == {"type": "text", "text": None, "custom": 7}


def test_frame_keeps_the_key_that_was_once_dropped(operations):
    """The actual historical defect: `stream_end` without `last_assistant_yaml`."""
    got = operations._frame(
        StreamEndFrame, type="stream_end", stop_reason="end_turn", ok=True,
        transcript=[], last_assistant_yaml="name: pb\n")
    assert got["last_assistant_yaml"] == "name: pb\n"


def test_frame_never_raises_on_bad_output(operations):
    """OUTBOUND-permissive: a model mismatch must not take down a live turn --
    it logs and falls back to the plain dict."""
    got = operations._frame(StreamEndFrame, type="WRONG", ok=True)
    assert got == {"type": "WRONG", "ok": True}


def test_turn_start_frame_shape_unchanged(storage):
    assert storage._turn_start_frame(3) == {"type": "turn_start", "turn": 3}


def test_workflow_collections_is_an_envelope_not_a_poll_frame():
    """`{"type": "workflow_collections", ...}` is FortiSOAR's import/export
    envelope for the decompiler -- it only *looks* like a wire frame because it
    carries a `type` key, and it never reaches the poll stream. It gets its own
    model and must stay OUT of `PollFrame`, or a poll-frame consumer would be
    told to expect something the connector never sends."""
    env = M.WorkflowCollectionsEnvelope(
        type="workflow_collections", data=[{"name": "c"}])
    assert env.model_dump(exclude_unset=True) == {
        "type": "workflow_collections", "data": [{"name": "c"}]}
    # In the poll union it must fall through to the catch-all, not resolve.
    frame = TypeAdapter(PollFrame).validate_python(
        {"type": "workflow_collections", "data": []})
    assert type(frame).__name__ == "UnknownEvent"


# ---------------------------------------------------------------------------
# The AUTHORITATIVE transcript is typed too.
#
# `ChatPollResponse.frames` was `list[PollFrame]` and validated on every poll,
# while `ChatTurnResponse.transcript` and `StreamEndFrame.transcript` -- the
# transcript the widget actually renders and rehydrates from -- were
# `list[dict[str, Any]]`. chat_poll's feed is explicitly a PREVIEW (tool results
# are truncated at `_FEED_RESULT_CAP`); the transcript is the source of truth.
# Checking the preview and waving through the load-bearing path is the asymmetry
# backwards, and it is the same defect class as the dropped `last_assistant_yaml`
# above: the widget switches on `type`, so a frame that arrives without one is a
# dead render, not an error.
# ---------------------------------------------------------------------------

_ENVELOPE = dict(ok=True, session_id="s", turn=1, turn_id="t",
                 stop_reason="end_turn", contract_version="2.6.0")


@pytest.mark.parametrize("build", [
    pytest.param(lambda tr: M.ChatTurnResponse(**_ENVELOPE, transcript=tr),
                 id="chat_turn"),
    pytest.param(lambda tr: M.StreamEndFrame(type="stream_end", transcript=tr),
                 id="stream_end"),
])
class TestTranscriptIsTyped:
    def test_a_frame_with_no_type_is_rejected(self, build):
        """The dead-render case. Under `list[dict[str, Any]]` this validated
        happily on both paths."""
        with pytest.raises(Exception):
            build([{"text": "hi"}])

    def test_a_non_string_type_is_rejected(self, build):
        with pytest.raises(Exception):
            build([{"type": 42}])

    def test_an_unknown_type_still_passes(self, build):
        """Forward compatibility is the whole reason `UnknownEvent` pins
        `type: str` and sits last in the union: a newer framework emitting a
        card this connector has never heard of must not fail the turn."""
        assert build([{"type": "some_future_card", "x": 1}]) is not None

    def test_a_real_spliced_card_literal_passes(self, build):
        """The `_CARD_EMITTER_TO_TYPE` splice builds its card as a raw dict
        literal rather than through `_as_frame`, so it is the shape most likely
        to drift -- and now the one this typing covers."""
        assert build([{"type": "choice_card", "preceding_tool_use_id": None,
                       "prompt": "?", "options": []}]) is not None


def test_a_bad_stream_end_transcript_never_raises_through_frame(operations):
    """OUTBOUND-permissive still holds where it was already the policy: the
    detached path builds its terminal through `_frame`, which logs and falls
    back to the plain dict. Widening what is CHECKED must not widen what can
    kill a live turn."""
    got = operations._frame(StreamEndFrame, type="stream_end",
                            transcript=[{"no": "type"}])
    assert got == {"type": "stream_end", "transcript": [{"no": "type"}]}


# ---------------------------------------------------------------------------
# The card splice goes through its model too.
#
# `_CARD_EMITTER_TO_TYPE` built its card as a raw `{"type": ...}` literal --
# the last one on a wire path, and the one all seven emit_* card types take.
# The model is DERIVED from the PollFrame union (`_frame_model_for`) rather
# than hand-listed: a `{card_type: Model}` dict next to _CARD_EMITTER_TO_TYPE
# would be a third parallel name list of the kind that produced #33 and #49.
# ---------------------------------------------------------------------------

def test_every_card_emitter_resolves_to_a_model(operations):
    """The derivation has to COVER the emitters, or the splice quietly falls
    back to its literal for the ones it misses and nothing says so."""
    unresolved = {tool: ctype
                  for tool, ctype in operations._CARD_EMITTER_TO_TYPE.items()
                  if operations._frame_model_for(ctype) is None}
    assert not unresolved, (
        f"card emitters whose frame type has no PollFrame member: "
        f"{unresolved}. Add the member to the union -- do not add a lookup "
        f"table next to _CARD_EMITTER_TO_TYPE.")


def test_the_derivation_reads_discriminants_not_class_names(operations):
    """`ioc_card` -> IocCardEvent proves it matches on the `type` Literal.
    A name-mangling implementation (card_type.title() + 'Event') would also
    pass that, so an unknown type must return None rather than raise."""
    # Compared by NAME, not identity: `operations` loads the models as
    # `_fsrpb_conn.pydantic_models` while this file imports them top-level, so
    # the two are distinct module objects with distinct class objects.
    assert operations._frame_model_for("ioc_card").__name__ == "IocCardEvent"
    assert operations._frame_model_for("stream_end").__name__ == "StreamEndFrame"
    assert operations._frame_model_for("not_a_real_frame_type") is None


@pytest.mark.parametrize("card_type,args", [
    ("choice_card", {"prompt": "?", "options": [{"value": "a"}]}),
    ("action_card", {"connector": "fortigate", "operation": "block_ip",
                     "args": {"ip": "1.2.3.4"}, "nested": {"deep": [1, None]}}),
    ("manual_input", {"fields": []}),
    ("capability_gap", {"missing": "x"}),
    ("playbook_offer", {"yaml": "name: x", "ops_summary": []}),
    ("enhancement_offer", {}),
])
def test_routing_the_card_through_its_model_is_identity(operations, card_type, args):
    """Same property `_frame` is built on: `exclude_unset=True` means the dump
    carries exactly the keys passed in -- explicit `None`s (the splice always
    sets `preceding_tool_use_id`, often to None) and extra="allow" extras
    included. That is what makes the swap provably non-breaking rather than
    merely tested-and-seemed-fine."""
    literal = {"type": card_type, "preceding_tool_use_id": None, **args}
    routed = operations._as_frame(operations._frame_model_for(card_type), literal)
    assert routed == literal


def test_no_raw_card_literal_remains_at_the_splice(operations):
    """Regression gate, mirroring the source grep above: the splice must build
    `card_payload` and route it, not hand a bare literal to `pending_card`."""
    src = "\n".join(p.read_text() for p in sorted(ROOT.glob("*.py"))
                   if p.name == "operations.py" or p.name.startswith("ops_"))
    body = src.split("card_type, input_args = emitted", 1)[1][:1500]
    assert "_as_frame(card_model" in body, (
        "the emit_* card splice stopped routing through its model")


# ---------------------------------------------------------------------------
# `ChatTurnResponse` is validated with the RAISING helper (`_validate_model`),
# and that is deliberate -- but only because nothing untrusted can trip it.
#
# The check runs inside `_finalize`, BEFORE the stream_end frame carrying the
# transcript is written, so a raise there discards a successful turn's entire
# output and shows the analyst an error. That is only an acceptable trade while
# every reachable failure is a connector bug. The probe below is what makes
# that a checked property instead of an assumption: if a future required field
# lets model / tool / MCP output fail the envelope, this file goes red and the
# raise has to be reconsidered (switch that site to `_check_response`).
# ---------------------------------------------------------------------------

_ENVELOPE_MIN = dict(ok=True, session_id="s", turn=1, turn_id="abc",
                     stop_reason="end_turn", contract_version="2.6.0")


@pytest.mark.parametrize("label,transcript", [
    ("an MCP card type this connector has never heard of",
     [{"type": "mcp_some_future_card", "x": 1}]),
    ("a tool result whose content is bytes",
     [{"type": "tool_result", "tool_use_id": "t", "content": b"\x00\x01"}]),
    ("a deeply nested tool result",
     [{"type": "tool_result", "content": {"a": {"b": [1, {"c": None}]}}}]),
    ("an empty-string type", [{"type": ""}]),
    ("no transcript at all", None),
    ("an empty transcript", []),
])
def test_untrusted_output_can_never_fail_the_envelope(label, transcript):
    """Model output, tool output and MCP output all flow into `transcript`.
    None of it may fail validation -- a stochastic generator must not be able
    to destroy a turn that otherwise succeeded."""
    assert M.ChatTurnResponse(**_ENVELOPE_MIN, transcript=transcript) is not None


@pytest.mark.parametrize("field,bad", [
    ("stop_reason", None),      # _envelope_stop_reason is `-> str`, `or "end_turn"`
    ("tags", ["not", "a", "dict"]),   # tags = dict(result.tags or {})
    ("last_assistant_yaml", {"not": "a string"}),  # only ever extract_yaml_block()
])
def test_the_envelope_still_rejects_connector_side_drift(field, bad):
    """The other half: these DO fail, and each is unreachable from live data
    today. They are the reason the raise is tolerable -- if one ever becomes
    reachable, that is the signal to soften the check, not to relax the model.

    `last_assistant_yaml` is the one to watch: it crosses a package boundary
    from `fsr_playbooks.TurnResult`, which is a plain @dataclass with no
    runtime type enforcement, so a version-skewed framework could hand back
    something else and nothing local would catch it first."""
    with pytest.raises(Exception):
        M.ChatTurnResponse(**{**_ENVELOPE_MIN, field: bad})
