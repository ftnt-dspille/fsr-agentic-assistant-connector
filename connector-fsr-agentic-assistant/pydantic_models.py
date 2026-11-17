"""Pydantic models for operation contract validation.

Validates operation input/output shapes at the boundary so malformed
connector/widget communication is caught early and surfaced loudly.
Validation errors propagate as ConnectorError; internal logic is unchanged.

CONTRACT PARITY: these models are the Python projection of the widget-side
TypeScript contract in
`widgets-src/agenticAssistant/widget/widgetAssets/js/contract.d.ts`
(namespace `FsrPbChat`). The two MUST describe the same wire items -- a change to
a frame type, envelope field, or the turn ack/result split belongs in BOTH.

STRICTNESS POLICY:
 - INBOUND tool args (the LLM produces them): strict. Untrusted generator, and
   no version skew to worry about, so a hallucinated key is rejected loudly.
   Not modelled here: each tool's input_schema (from its typed signature) is
   the contract, checked by the framework's dispatch gate (`arg_gate`).
 - INBOUND op params (the widget sends them): permissive (`_ALLOW_PARAMS`).
   The widget and connector ship independently, so an older connector rejecting
   a newer widget's key is a self-inflicted outage. Required fields still catch
   the defect that matters (a MISSING or MISNAMED field). Wired via
   `operations._check_params`.
 - OUTBOUND (frames/responses the connector itself builds): a typed union with an
   explicit `unknown` catch-all and permissive members. `_event_to_wire` has a
   `{"type": "unknown"}` fallback and frame fields evolve, so a strict validator
   on our OWN streamed output would break live chat_poll. Here the union documents
   the shape and lets drift surface in TESTS without being fatal at runtime.
   Responses are checked at the dispatch seam (`operations._RESPONSE_MODELS`),
   which warns by default and raises only under `FSRPB_STRICT_MODELS=1`.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _coerce_str_list(v):
    """Coerce a reach_servers input to a list[str]. Widgets pass a real JSON
    list; the connector op UI passes a text field (JSON array or comma/space
    separated). None/empty ⇒ None; anything unparseable ⇒ None (fail-open)."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            import json as _json
            parsed = _json.loads(s)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [p.strip() for p in s.replace(",", " ").split() if p.strip()]
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if str(x).strip()]
    return None


# Op-params config. Named (rather than inline) because the permissiveness is a
# POLICY, not an oversight: the widget and connector ship independently, so an
# older connector must not reject a newer widget's unknown key. See
# `operations._check_params`.
_ALLOW_PARAMS = ConfigDict(extra="allow")


# ======================================================================
# Wire events  (FsrPbChat.WireEvent -- the per-event streaming vocabulary)
# ----------------------------------------------------------------------
# These appear both as loose poll `frames[]` (bracketed by turn_start/stream_end)
# and as the authoritative `transcript[]`. Modeled from operations.py
# `_event_to_wire` + the card-injection path. All members keep extra="allow"
# and near-all-optional fields: this is the connector's OWN output, so the union
# is for structure/documentation and MUST NOT reject live frames.
# ======================================================================

class _WireBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class TextEvent(_WireBase):
    type: Literal["text"]
    text: Optional[str] = None


class ToolUseEvent(_WireBase):
    type: Literal["tool_use"]
    id: Optional[str] = None
    name: Optional[str] = None
    input: Optional[Any] = None
    tier: Optional[int] = None
    #: The analyst's name for the call (`tool_labels.TOOL_LABELS`); absent for
    #: a tool with none (native MCP tools, which the widget names itself).
    label: Optional[str] = None


class ToolResultEvent(_WireBase):
    type: Literal["tool_result"]
    tool_use_id: Optional[str] = None
    tool: Optional[str] = None
    content: Optional[Any] = None
    duration_ms: Optional[int] = None


class DroppedCall(_WireBase):
    name: Optional[str] = None
    arg_chars: Optional[int] = None
    tail: Optional[str] = None


class UsageEvent(_WireBase):
    type: Literal["usage"]
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    cache_read: Optional[int] = None
    cache_write: Optional[int] = None
    #: Every input token billed this round, cached ones included (Anthropic's
    #: input_tokens excludes them; OpenAI's already contains them).
    total_input: Optional[int] = None
    #: total_input + output_tokens. The widget's turn total sums this.
    total_tokens: Optional[int] = None
    #: Why the round ended (`tool_calls`, `length`, `build_progress_forced`...).
    stop_reason: Optional[str] = None
    #: Calls the round started but never ran -- cut off at the output cap.
    dropped_calls: list[DroppedCall] = Field(default_factory=list)
    #: The model's own reasoning summary for this round, when it emits one
    #: (Anthropic adaptive-thinking models with display="summarized"). Empty
    #: for standard models and the OpenAI/FortiAI proxy paths, and omitted from
    #: the frame entirely when empty. The widget folds it into a per-turn
    #: "How I worked this out" panel; never shown inline as prose.
    thinking: Optional[str] = None


class ApprovalRequestEvent(_WireBase):
    type: Literal["approval_request"]
    approval_id: Optional[str] = None
    tool_use_id: Optional[str] = None
    tool: Optional[str] = None
    tier: Optional[int] = None
    preview: Optional[dict[str, Any]] = None
    args_hash: Optional[str] = None
    summary: Optional[str] = None
    requires_step_up: Optional[bool] = None
    # Batched approval: further gated calls from the same model turn that this
    # one decision also covers, in run order ({tool_use_id, tool, args, tier,
    # summary, preview}). Absent for a single-call approval.
    batch: Optional[list[dict[str, Any]]] = None
    # The autonomy policy's shadow decision for this call ({rule, outcome,
    # failed, ...}); absent when no rule covers it. The call waits either way.
    policy: Optional[dict[str, Any]] = None


class ActivityEvent(_WireBase):
    type: Literal["activity"]
    message: Optional[str] = None
    phase: Optional[str] = None


class ErrorEvent(_WireBase):
    type: Literal["error"]
    message: Optional[str] = None


# Card events are handed to the widget's normalizeActionCard / normalizeInfoCard,
# which read a broad, connector-defined field set -- kept open (extra="allow",
# only the `type` discriminant pinned), mirroring the index signatures on the
# TS side.
class ActionCardEvent(_WireBase):
    type: Literal["action_card"]


class ChoiceCardEvent(_WireBase):
    type: Literal["choice_card"]


class CapabilityGapEvent(_WireBase):
    type: Literal["capability_gap"]


class ManualInputEvent(_WireBase):
    type: Literal["manual_input"]


class PlaybookOfferEvent(_WireBase):
    type: Literal["playbook_offer"]


class StatusCardEvent(_WireBase):
    type: Literal["status_card"]


class InfoCardEvent(_WireBase):
    type: Literal["info_card"]


class IocCardEvent(_WireBase):
    """The connector has NO producer for this frame -- checked, and recorded
    here so the next reader does not have to re-derive it.

    `wire_enrichment._ioc_cards` emits `{"type": "info_card", "variant":
    "ioc_enrichment", ...}`, not an `ioc_card`. The member stays because both
    sides already contract it and agree on the fold: the widget's renderer
    routes `status_card` / `info_card` / `ioc_card` through one path
    (`fsrPbRender.js`), and the harness's acceptance gate canonicalizes
    `ioc_card` to `info_card` (`canonCard`, matrixEval), so an expectation
    written either way is satisfied by either frame. Deleting it would be a
    contract break that buys nothing."""

    type: Literal["ioc_card"]


class EnhancementOfferEvent(_WireBase):
    """The verified-edit offer card (contract `EnhancementOfferEvent`). Open like
    the other cards -- the widget reads a broad field set (`final_yaml`,
    `diff_summary`, `steps_added/removed/modified`, `warnings`, …)."""
    type: Literal["enhancement_offer"]


class VerdictCardEvent(_WireBase):
    """Structured investigation verdict with disposition, severity, confidence,
    and cited findings. The wire shape mirrors the framework's verdict_card
    carrier (fsr_playbooks.mcp_server.tools_emit.emit_verdict), with type pinned
    and every field open (extra="allow") per the card event policy."""
    type: Literal["verdict_card"]

class TurnInputsFrame(_WireBase):
    """What the model was given this turn: tools offered, intent, model, prompt
    sizes (names and sizes, never prompt text). Emitted first by the framework
    loop; read with `fsr_playbooks.harness.frames.turn_inputs`.

    Deliberately NOT a `PollFrame` member: that union is held to the widget's
    `contract.d.ts` in both directions (`test_contract_dts_parity`), and this
    frame is for diagnosis, not display. The widget's renderer has no branch
    for it and ignores it (an unknown type falls through its if-chain), and
    `PollFrame` validators land it on `UnknownEvent`. Not to be confused with
    `TurnStartFrame`, the poll feed's turn boundary the widget latches on."""
    type: Literal["turn_inputs"]
    model: Optional[str] = None
    tools: list[str] = Field(default_factory=list)
    intent: Optional[str] = None
    system_chars: Optional[int] = None
    slices: list[dict[str, Any]] = Field(default_factory=list)


class UnknownEvent(_WireBase):
    """_event_to_wire's final fallback for an unrecognized fsr_playbooks event.
    Also the union's catch-all: any frame whose `type` matches no member above
    lands here, so a strict list validator never rejects live output."""
    type: str
    repr: Optional[str] = None


# ======================================================================
# Poll envelope frames (FsrPbChat.PollFrame)
# ======================================================================

class TurnStartFrame(_WireBase):
    type: Literal["turn_start"]
    turn: Optional[int] = None


class StreamEndFrame(_WireBase):
    type: Literal["stream_end"]
    ok: Optional[bool] = None
    stop_reason: Optional[str] = None
    turn_id: Optional[str] = None
    # Forward-referenced `PollFrame` (declared below, and it lists this class),
    # so the annotation is a string and the model is rebuilt after the union
    # exists. Self-recursive in principle -- a stream_end nested in a
    # transcript would validate -- which costs nothing and is cheaper than a
    # hand-maintained "union minus the terminals", i.e. another parallel name
    # list of the kind that caused #33/#49 in the first place.
    transcript: Optional[list["PollFrame"]] = None
    contract_version: Optional[str] = None
    last_assistant_yaml: Optional[str] = None
    tags: Optional[dict[str, Any]] = None
    error: Optional[Any] = None
    cursor: Optional[int] = None


# Left-to-right union: each known member pins its `type` Literal, so a frame
# resolves to its model; anything else falls through to UnknownEvent (type: str).
# UnknownEvent MUST stay last. Kept non-discriminated + left_to_right so the
# fallback works without a native discriminated-union default member.
PollFrame = Annotated[
    Union[
        TurnStartFrame, StreamEndFrame,
        TextEvent, ToolUseEvent, ToolResultEvent, UsageEvent,
        ApprovalRequestEvent, ActivityEvent, ErrorEvent,
        ActionCardEvent, ChoiceCardEvent, CapabilityGapEvent,
        ManualInputEvent, PlaybookOfferEvent,
        EnhancementOfferEvent,
        StatusCardEvent, InfoCardEvent, IocCardEvent, VerdictCardEvent,
        UnknownEvent,
    ],
    Field(union_mode="left_to_right"),
]

# Back-compat alias: operations.py imported ChatPollFrame (previously a bare
# {type: str}). Keep the name; point it at the catch-all so any stray import
# still resolves.
ChatPollFrame = UnknownEvent

# `StreamEndFrame.transcript` forward-references `PollFrame`, which lists
# StreamEndFrame -- so the annotation cannot resolve until here.
StreamEndFrame.model_rebuild()


# ======================================================================
# chat_turn
# ======================================================================

class ChatTurnParams(BaseModel):
    """Input shape for chat_turn (widget → connector)."""
    model_config = ConfigDict(extra="allow")

    session_id: str
    intent: Optional[str] = None
    text: Optional[str] = None
    messages: Optional[list[dict[str, Any]]] = None
    entity: Optional[dict[str, Any]] = None
    detached: Optional[bool] = None
    # The acting SOAR user ({name, iri, ...}) supplied by the widget so the
    # connector can persist the real chat author/initiator (it runs under the
    # service account and can't derive it). None from older widgets.
    actor: Optional[dict[str, Any]] = None
    # Track C1: the build quick-action chip the analyst clicked (e.g.
    # `explain`/`add_step`/`find_issues`/`add_error_handling`/`optimize`).
    # Threading it as a typed field (not extra-passthrough) documents the
    # contract; the build system prompt branches on it. None on free-typed
    # turns and on triage chips (the connector only acts on it for `build`).
    quick_action: Optional[str] = None
    # M2.1 on-ask reach: MCP server keys to UNION into this page's default MCP
    # surface for this turn, so a server dropped by per-page surfacing (M2) is
    # reachable when explicitly requested (e.g. the analyst enables `deepwiki`
    # docs lookup on an alerts page). Empty/None ⇒ the page's default surface.
    reach_servers: Optional[list[str]] = None
    # Phase 1: page FACTS the widget reports so the connector classifies the page
    # itself (one taxonomy). `page` is the FortiSOAR router state name (e.g.
    # `main.playbookDetail`); `in_playbook_editor` is the widget's own mount
    # signal. These seed a SOFT frontier prior when `intent` is absent -- `intent`
    # (a task discriminator) still wins when present (soft override). None from
    # older widgets ⇒ prior falls back to the module / triage default.
    page: Optional[str] = None
    in_playbook_editor: Optional[bool] = None
    # Enforcement mode: when True, the turn runs with read-only frontier (no
    # write tools). Used by scheduled tasks with allow_mutating=False to enforce
    # that auto-runs cannot authorize writes (they require analyst approval).
    read_only: Optional[bool] = None
    # Create-method strategy chosen in the widget settings (auto/guided/freeform).
    # `guided` removes the free-text YAML authoring surface so compose_playbook
    # (plan->compiler) is the only path to a deliverable; `freeform` keeps the
    # full authoring slice; `auto` (default) picks by the resolved model's
    # capability. Only consulted on the `build` intent. None from older widgets
    # is treated as `auto`.
    create_strategy: Optional[str] = None

    _coerce_reach = field_validator("reach_servers", mode="before")(_coerce_str_list)


class ChatTurnResponse(BaseModel):
    """Response from chat_turn -- either a detached {accepted} ack (no transcript;
    the transcript then rides the chat_poll stream_end terminal) or a blocking
    result carrying the transcript inline. Mirrors FsrPbChat.ChatTurnResponse
    (ChatTurnAck | ChatTurnResult). Kept as one permissive model (extra="allow")
    because operations.py validates a single dict here; `accepted` distinguishes
    the two shapes."""
    model_config = ConfigDict(extra="allow")

    ok: bool
    accepted: Optional[bool] = None
    session_id: str
    turn: int
    turn_id: str
    stop_reason: str
    contract_version: str
    # `list[PollFrame]`, not `list[dict]`. This is the AUTHORITATIVE transcript
    # -- the one the widget renders and rehydrates from -- while chat_poll's
    # `frames` is explicitly a preview (see `_FEED_RESULT_CAP`). Typing the
    # preview and not the source of truth is the asymmetry backwards: a frame
    # that reaches the widget with no `type` is a dead render, and it was
    # checked on the cheap path and waved through on the load-bearing one.
    transcript: Optional[list[PollFrame]] = None
    last_assistant_yaml: Optional[str] = None
    tags: Optional[dict[str, Any]] = None
    error: Optional[Any] = None


# ======================================================================
# chat_poll
# ======================================================================

class ChatPollParams(BaseModel):
    """Input shape for chat_poll."""
    model_config = ConfigDict(extra="allow")

    session_id: str
    cursor: Optional[int] = Field(default=0)
    since_turn: Optional[int] = Field(default=0)
    # Long-poll window in ms (capped server-side). 0 / absent = answer at once.
    long_poll_ms: Optional[int] = Field(default=0)


class ChatCancelParams(BaseModel):
    """Input shape for chat_cancel (C6): the session whose running turn to stop."""
    model_config = ConfigDict(extra="allow")

    session_id: str


class ChatCancelResponse(BaseModel):
    """Response from chat_cancel (C6). `cancelled` is False when no turn ran."""
    model_config = ConfigDict(extra="allow")

    ok: bool
    session_id: str
    turn: Optional[int] = None
    cancelled: bool


class ChatPollResponse(BaseModel):
    """Response from chat_poll. `frames` is now the typed PollFrame union (was
    list[dict]); the top-level `done` is the terminal signal (NOT a `status`
    field -- see FsrPbChat.ChatPollResponse)."""
    model_config = ConfigDict(extra="allow")

    ok: bool
    session_id: str
    contract_version: str
    turn: Optional[int] = None
    cursor: int
    frames: list[PollFrame] = Field(default_factory=list)
    done: bool
    stop_reason: Optional[str] = None
    error: Optional[Any] = None


# ======================================================================
# chat_resume
# ======================================================================

class ChatResumeParams(BaseModel):
    """Input shape for chat_resume."""
    model_config = ConfigDict(extra="allow")

    session_id: Optional[str] = None
    decision: str
    approval_id: Optional[str] = None
    card_id: Optional[str] = None
    choice_id: Optional[str] = None
    text: Optional[str] = None
    messages: Optional[list[dict[str, Any]]] = None
    # enhancement_offer accept: chat_resume carries no entity (contract §4),
    # so the widget names the open playbook the edit is applied onto.
    workflow_iri: Optional[str] = None
    workflow_uuid: Optional[str] = None
    # enhancement_offer accept: "canvas" = stage the edit into the open
    # designer as unsaved changes (the analyst clicks Save); absent = write it.
    apply_to: Optional[Literal["canvas"]] = None
    # approve on a run card that offered "Always allow": also add the playbook
    # to the persona's run_playbook allow + auto lists.
    remember: Optional[bool] = None
    # Who answered the card (the widget's acting user); recorded with the
    # decision so a second answer can be told who already gave one.
    actor: Optional[dict[str, Any]] = None


class ChatResumeResponse(BaseModel):
    """Response from chat_resume (same shape as a chat_turn result)."""
    model_config = ConfigDict(extra="allow")

    ok: bool
    session_id: Optional[str] = None
    turn: Optional[int] = None
    turn_id: Optional[str] = None
    stop_reason: str
    contract_version: str
    transcript: Optional[list[dict[str, Any]]] = None
    last_assistant_yaml: Optional[str] = None
    tags: Optional[dict[str, Any]] = None
    error: Optional[Any] = None


# ======================================================================
# chat_history
# ======================================================================

class ChatHistoryParams(BaseModel):
    """Input shape for chat_history."""
    model_config = ConfigDict(extra="allow")

    session_id: str
    limit: Optional[int] = Field(default=200)
    # Only the card decisions: what an open chat polls to learn that a card
    # it still shows as pending was answered elsewhere.
    decisions_only: bool = False


class HistoryTurn(BaseModel):
    model_config = ConfigDict(extra="allow")
    user: Optional[str] = None
    transcript: Optional[list[dict[str, Any]]] = None
    # The analyst's answer to a choice card: {choice_id, value, label?}. Lets
    # replay show the card as answered instead of open again.
    resume: Optional[dict[str, Any]] = None


class ChatHistoryResponse(BaseModel):
    """Response from chat_history."""
    model_config = ConfigDict(extra="allow")

    # Typed by ROW, not `list[dict]`: `HistoryTurn` existed and was checked by
    # nothing, so the element shape drifted freely. extra="allow" keeps it
    # documentation rather than a reject.
    turns: list[HistoryTurn] = Field(default_factory=list)
    # The session's real initiator (widget actor of the opening turn), or None
    # on pre-actor sessions -- lets the widget frame a reopened chat by its owner.
    initiator: Optional[dict[str, Any]] = None
    # Who answered which card ({card_id, decision, actor, decided_at}). A card
    # answered in another tab, by another analyst or from the Monitor queue
    # otherwise still showed its buttons until someone clicked one.
    card_decisions: list[dict[str, Any]] = Field(default_factory=list)


# ======================================================================
# Agent tool arguments are NOT modelled here: each tool's advertised
# input_schema, built from its typed signature, is the one contract, and the
# framework's dispatch gate checks every call against it (W1). A second copy
# here drifted from the signatures and was retired.
# The yaml operations receive `{yaml: ...}` from the widget service.
# ======================================================================

class ValidateYamlParams(BaseModel):
    """Input shape for the validate_yaml operation (widget sends {yaml: ...}).

    `extra="allow"`, like every other OP-params model: these were authored
    strict while nothing wired them, and wiring them strict would mean an older
    connector rejecting a newer widget's extra key -- the version-skew failure
    `_check_params` exists to avoid. Nothing is actually lost by relaxing: the
    defect these models were written for is the wrong field NAME (`yaml_text`
    for `yaml`), and `yaml` being REQUIRED still rejects that. Strictness stays
    where the caller is an untrusted generator -- the LLM tool-arg gate.
    """
    model_config = _ALLOW_PARAMS

    yaml: str


class CompileYamlParams(BaseModel):
    """Input shape for the compile_yaml operation. See ValidateYamlParams on
    why op params are permissive about unknown keys."""
    model_config = _ALLOW_PARAMS

    yaml: str
    name: Optional[str] = None
    collection: Optional[str] = None


# Back-compat aliases for the previous (incorrect) names, in case anything
# imported them. Point at the corrected params models.
ValidateYamlArgs = ValidateYamlParams
CompileYamlArgs = CompileYamlParams


# ======================================================================
# Stage 3: the remaining operations (params + response shapes) + storage.
# ----------------------------------------------------------------------
# These document the shapes of the utility / playbook-CRUD / MCP / trace ops
# so drift surfaces in tests. Params are semi-trusted (widget/agent callers) →
# extra="allow" with only genuinely-required fields pinned; responses are the
# connector's OWN output → extra="allow" (documentation, never a runtime reject),
# matching the OUTBOUND policy above. Mirrors the real signatures in
# operations.py (health_check/echo_param_types/resolve_yaml/list_sessions/
# respond_manual_input/push_playbook/render_jinja/list_mcp_tools/call_mcp_tool/
# query_store/dry_run_playbook/get_session_trace/list_models/warmup).
# ======================================================================

_ALLOW = ConfigDict(extra="allow")


# ── health_check ──────────────────────────────────────────────────────
class HealthCheckParams(BaseModel):
    model_config = _ALLOW
    skip_llm_probe: Optional[bool] = None


class HealthCheckResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    llm_provider: Optional[str] = None
    llm_key_configured: Optional[bool] = None
    llm_reachable: Optional[bool] = None
    llm_probe: Optional[str] = None
    llm_error: Optional[str] = None
    openai_reachable: Optional[bool] = None
    anthropic_reachable: Optional[bool] = None
    reference_db_present: Optional[bool] = None
    reference_db_path: Optional[str] = None
    fsr_playbooks_version: Optional[str] = None
    fsr_playbooks_module: Optional[str] = None
    connector_version: Optional[str] = None
    package: Optional[str] = None
    contract_version: Optional[str] = None
    pid: Optional[int] = None
    host: Optional[str] = None
    # Install faults (framework_missing / framework_mismatch): the connector
    # cannot run a turn until it is reinstalled.
    fault: Optional[str] = None
    message: Optional[str] = None
    dependencies: Optional[dict[str, Any]] = None
    warnings: Optional[list[str]] = None


# ── echo_param_types (diagnostic: echoes received param types) ─────────
class EchoParamTypesResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    received: dict[str, Any] = Field(default_factory=dict)


# ── resolve_yaml ──────────────────────────────────────────────────────
class ResolveYamlParams(BaseModel):
    # Permissive like every other op-params model -- see ValidateYamlParams.
    model_config = _ALLOW_PARAMS
    yaml: str


class ResolveYamlResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    ir: Optional[dict[str, Any]] = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


# ── list_sessions ─────────────────────────────────────────────────────
class ListSessionsParams(BaseModel):
    model_config = _ALLOW
    limit: Optional[int] = Field(default=50)
    # Non-empty ⇒ search WITHIN sessions (transcript content), not just list them.
    query: Optional[str] = Field(default=None)


class SessionSummary(BaseModel):
    model_config = _ALLOW
    session_id: str
    title: Optional[str] = None
    entries: Optional[int] = None
    last_activity: Optional[float] = None
    started_at: Optional[float] = None


class ListSessionsResponse(BaseModel):
    model_config = _ALLOW
    sessions: list[SessionSummary] = Field(default_factory=list)


# ── respond_manual_input ──────────────────────────────────────────────
class RespondManualInputParams(BaseModel):
    model_config = _ALLOW
    session_id: str
    # A manual-input id is numeric, and /api/integration/execute/ coerces a
    # numeric STRING back to an int on the way in -- so a `str`-only annotation
    # rejects the very ids the platform hands us ("input_id: Input should be a
    # valid string [input_value=141, input_type=int]", live on 8.0.0). The
    # handler normalises with `int(str(input_id).strip())`, so accept either.
    input_id: Union[str, int]
    decision: Optional[Literal["approve", "reject"]] = Field(default="approve")
    values: Optional[dict[str, Any]] = None
    # The button the analyst pressed, by label, on a gate with several.
    option: Optional[str] = None
    workflow_run_iri: Optional[str] = None


class RespondManualInputResponse(BaseModel):
    """Base resume envelope + manual-input forwarding metadata. Inherits the
    resume shape loosely (extra="allow") since it reuses _resume_conversation."""
    model_config = _ALLOW
    input_id: Optional[str] = None
    manual_input_forward: Optional[dict[str, Any]] = None


# ── push_playbook ─────────────────────────────────────────────────────
class PushPlaybookParams(BaseModel):
    model_config = _ALLOW
    workflow_json: Union[dict[str, Any], str]
    overwrite: Optional[bool] = Field(default=True)


class PushPlaybookResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    collection_uuid: Optional[str] = None
    collection_name: Optional[str] = None
    workflow_uuids: Optional[list[str]] = None
    raw: Optional[dict[str, Any]] = None
    renamed_from: Optional[str] = None
    uuid_regenerated: Optional[bool] = None
    error: Optional[dict[str, Any]] = None


# ── update_playbook ────────────────────────────────���────────────────────���─
# Track C2 -- modify-in-place counterpart of push_playbook (which is
# create-only). Targets the existing workflow IRI the widget seeds from
# main.playbookDetail. PUT /api/3/workflows/<uuid> primary (the designer's
# own Save path), import_jobs merge_replace fallback (verified upsert).
class UpdatePlaybookParams(BaseModel):
    model_config = _ALLOW
    workflow_json: Union[dict[str, Any], str]
    # Identity of the EXISTING playbook to update. Either form suffices;
    # workflow_iri (/api/3/workflows/<uuid>) is what the widget seeds.
    workflow_iri: Optional[str] = None
    workflow_uuid: Optional[str] = None
    # Labels the pre-edit restore point so a snapshot in the Versions tab is
    # attributable to the chat session that caused it. Optional: absent just
    # yields an unqualified "ai-pre-edit" note.
    session_id: Optional[str] = None
    # HARDEN-1 escape hatch. The pre-write guard refuses this PUT when a step,
    # route, argument or declared parameter that exists on the live playbook is
    # missing from the body. When the deletion IS what the analyst asked for,
    # the caller re-issues the save with the refusal's exact `dropped` paths
    # here. Absent = no drops permitted, which is the safe default.
    acknowledged_drops: Optional[list[str]] = None
    # Stage, don't write: take the restore point and run every guard, then
    # return the save body for the designer to load as UNSAVED changes. The
    # analyst's own Save in the designer is the write.
    stage: Optional[bool] = None


# ── decompile_playbook ─────────────────────────────────────────────────────
# The READ counterpart of compile_yaml. Closes the gap S2 found live: nothing in
# the build tool slice can fetch a live playbook, so the designer persona was
# being asked to edit something it could not see. The widget calls this at
# designer mount and seeds the YAML.
class WorkflowCollectionsEnvelope(BaseModel):
    """FortiSOAR's `{"type": "workflow_collections", "data": [...]}` import /
    export envelope -- what the decompiler consumes.

    NOT a widget wire frame, despite the `type` key: it never reaches the poll
    stream and must stay out of `PollFrame`. Modeled here so
    `_live_collection_envelope` builds it through a model like everything else.
    """
    model_config = ConfigDict(extra="allow")

    type: Literal["workflow_collections"]
    data: Optional[list[Any]] = None


class DecompilePlaybookParams(BaseModel):
    model_config = _ALLOW
    # Identity of the playbook to read. Either form suffices; workflow_iri
    # (/api/3/workflows/<uuid>) is what the widget seeds from the open playbook.
    workflow_iri: Optional[str] = None
    workflow_uuid: Optional[str] = None


class DecompilePlaybookResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    # Authored-style YAML -- the same dialect compile_yaml consumes, so the
    # read → edit → compile → update round-trip closes.
    yaml: Optional[str] = None
    workflow_iri: Optional[str] = None
    workflow_uuid: Optional[str] = None
    name: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class UpdatePlaybookResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    workflow_iri: Optional[str] = None
    workflow_uuid: Optional[str] = None
    collection_uuid: Optional[str] = None
    # Always "put" -- the designer's own Save, and now the only path. An
    # import_jobs merge_replace fallback used to sit behind it; live on 8.0 it
    # returned ok while applying nothing, so a failed edit reached the user as a
    # success. Kept as a field because callers/live-verify read it.
    method: Optional[str] = None
    # The pre-edit restore point. Always present on a successful update: the
    # edit is fail-closed on the snapshot, so ok=True implies one exists.
    version_uuid: Optional[str] = None
    raw: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None


# ── render_jinja ──────────────────────────────────────────────────────
class RenderJinjaParams(BaseModel):
    model_config = _ALLOW
    template: str
    context: Optional[dict[str, Any]] = None
    from_pb_execution: Optional[str] = None


class RenderJinjaResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    output: Optional[Any] = None
    error: Optional[dict[str, Any]] = None


# ── list_mcp_tools ────────────────────────────────────────────────────
class McpToolInfo(BaseModel):
    model_config = _ALLOW
    name: str
    description: Optional[str] = None


class ListMcpToolsResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    tools: list[McpToolInfo] = Field(default_factory=list)
    count: Optional[int] = None
    error: Optional[dict[str, Any]] = None


# ── call_mcp_tool ─────────────────────────────────────────────────────
class CallMcpToolParams(BaseModel):
    model_config = _ALLOW
    tool: str
    args: Optional[Union[dict[str, Any], str]] = None


# call_mcp_tool returns the tool's own (arbitrary) result dict or an error dict;
# no fixed response model -- it is passed through verbatim.


# ── query_store ───────────────────────────────────────────────────────
class QueryStoreParams(BaseModel):
    model_config = _ALLOW
    sql: str
    limit: Optional[int] = Field(default=200)


class QueryStoreResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    rows: list[dict[str, Any]] = Field(default_factory=list)
    count: Optional[int] = None
    columns: Optional[list[str]] = None
    db_path: Optional[str] = None
    error: Optional[dict[str, Any]] = None


# ── dry_run_playbook ──────────────────────────────────────────────────
class DryRunPlaybookParams(BaseModel):
    model_config = _ALLOW
    workflow_json: Union[dict[str, Any], str]
    playbook_name: Optional[str] = None
    inputs: Optional[dict[str, Any]] = None
    timeout_s: Optional[int] = Field(default=180)
    cleanup: Optional[bool] = Field(default=True)
    use_mock_output: Optional[bool] = Field(default=False)


class DryRunPlaybookResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    status: Optional[str] = None
    task_id: Optional[str] = None
    wf_uuid: Optional[str] = None
    wf_pk: Optional[str] = None
    collection_uuid: Optional[str] = None
    cleaned_up: Optional[bool] = None
    error_message: Optional[str] = None
    failed_steps: Optional[list[dict[str, Any]]] = None
    # Populated when status == "awaiting": the step(s) parking the run on a
    # human gate (e.g. a trace-built playbook's "Confirm Containment").
    awaiting_steps: Optional[list[Optional[str]]] = None


# ── get_session_trace ─────────────────────────────────────────────────
class GetSessionTraceParams(BaseModel):
    model_config = _ALLOW
    session_id: str
    name: Optional[str] = Field(default="Triage Playbook")


class GetSessionTraceResponse(BaseModel):
    model_config = _ALLOW
    ok: bool
    session_id: Optional[str] = None
    count: Optional[int] = None
    calls: Optional[list[dict[str, Any]]] = None
    compiled_ok: Optional[bool] = None
    yaml: Optional[str] = None
    steps: Optional[Any] = None
    workflow_json: Optional[dict[str, Any]] = None
    build_error: Optional[str] = None
    compile_errors: Optional[list[dict[str, Any]]] = None


# ── list_models (returns a list[dict], not a dict) ────────────────────
class ModelChoice(BaseModel):
    model_config = _ALLOW
    title: str
    value: str


# ── warmup ────────────────────────────────────────────────────────────
class WarmupParams(BaseModel):
    model_config = _ALLOW
    force: Optional[bool] = None


class WarmupResponse(BaseModel):
    model_config = _ALLOW
    ok: Optional[bool] = None
    ms: Optional[int] = None
    timings: Optional[dict[str, Any]] = None
    counts: Optional[dict[str, Any]] = None
    connector_health: Optional[str] = None
    skipped: Optional[str] = None
    error: Optional[dict[str, Any]] = None


# ======================================================================
# storage.py -- persisted session shapes (sqlite rows). Documentation models
# for the load/save shapes that were previously raw dicts. All extra="allow":
# these mirror table rows and the collapsed conversation/transcript payloads.
# ======================================================================

class ConversationMessage(BaseModel):
    """A row of session_conversation.messages_json -- the history replayed on
    resume. `content` is a string (an analyst turn, or any row an older
    connector wrote) or a block list (an assistant turn: text, tool_use,
    tool_result -- see history_replay.transcript_blocks)."""
    model_config = _ALLOW
    role: str
    content: Union[str, list[dict[str, Any]]]


class TranscriptUserPayload(BaseModel):
    """session_transcript.payload_json for a role=user row."""
    model_config = _ALLOW
    text: str


class TranscriptResumePayload(BaseModel):
    """session_transcript.payload_json for a role=resume row -- the answer to a
    choice card. `value` is an option's value, a list of them, or free text."""
    model_config = _ALLOW
    choice_id: str
    value: Any = None
    label: Optional[str] = None


class TranscriptAssistantPayload(BaseModel):
    """session_transcript.payload_json for a role=assistant row."""
    model_config = _ALLOW
    transcript: list[dict[str, Any]] = Field(default_factory=list)


class ChatTurnRow(BaseModel):
    """A chat_turns metadata row."""
    model_config = _ALLOW
    session_id: str
    turn: int
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read: Optional[int] = None
    cache_write: Optional[int] = None
    stop_reason: Optional[str] = None
    tags_json: Optional[str] = None
    tool_calls_json: Optional[str] = None
    created_at: Optional[float] = None


class ChatMessageRow(BaseModel):
    """A chat_messages per-event row."""
    model_config = _ALLOW
    session_id: str
    turn: int
    seq: int
    kind: str  # user | assistant_text | tool_use | tool_result
    name: Optional[str] = None
    content: Optional[str] = None
    created_at: Optional[float] = None


class TurnProgressRow(BaseModel):
    """A turn_progress streaming-feed row (contract 2.5.0)."""
    model_config = _ALLOW
    session_id: str
    turn: int
    frame_json: Optional[str] = None
    is_terminal: Optional[int] = None
    stop_reason: Optional[str] = None
    created_at: Optional[float] = None


# ======================================================================
# list_agent_tools -- introspection of the agentic tool surface
# ======================================================================


class ListAgentToolsParams(BaseModel):
    """Input shape for list_agent_tools."""
    model_config = ConfigDict(extra="allow")

    # "triage" | "build" | "both" (default). Anything else normalizes to build.
    intent: str = Field(default="both")
    # Optional mounted module -- applies its Key Store persona slice, exactly as a
    # real turn would (persona allowlist wins over the built-in intent slice).
    module: Optional[str] = None
    # Materialize the box's native-MCP-gateway tools (mcp_<server>__<tool>) from
    # the connector's mcp_allowlist so they appear in the surface. Default on.
    include_mcp: bool = Field(default=True)
    # Include each tool's input_schema (verbose); default names/tier only.
    verbose: bool = Field(default=False)
    # M2.1 on-ask reach: server keys to union into the module's default MCP
    # surface (mirrors ChatTurnParams.reach_servers so introspection matches a
    # reached turn). None ⇒ the page's default surface.
    reach_servers: Optional[list[str]] = None

    _coerce_reach = field_validator("reach_servers", mode="before")(_coerce_str_list)


class ListMcpServersParams(BaseModel):
    """Input shape for list_mcp_servers (M2.1 discovery)."""
    model_config = ConfigDict(extra="allow")

    # Optional mounted module -- annotate each server's on/off-surface status for
    # that page (via the same affordance/persona resolution a turn uses). None ⇒
    # every materialized server reports on_surface=True (unrestricted).
    module: Optional[str] = None
    # Union these server keys into the module's default surface before annotating
    # (preview an on-ask reach). None ⇒ the page's default surface.
    reach_servers: Optional[list[str]] = None

    _coerce_reach = field_validator("reach_servers", mode="before")(_coerce_str_list)


class McpServerRow(BaseModel):
    """One materialized MCP server in the enumeration."""
    model_config = _ALLOW

    server: str
    # True when this server is on ``module``'s default advertised surface (or the
    # page is unrestricted). False ⇒ dropped from the default surface but still
    # reachable on-ask (reach_servers / an explicit request).
    on_surface: bool
    tools: list[str] = Field(default_factory=list)


class ListMcpServersResult(BaseModel):
    """Response from list_mcp_servers."""
    model_config = _ALLOW

    ok: bool = True
    module: Optional[str] = None
    # The server keys on the page's default surface, or None when unrestricted.
    surfaced_servers: Optional[list[str]] = None
    # The mcp_allowlist keys configured on the connector config (what's connected).
    allowlist_keys: list[str] = Field(default_factory=list)
    servers: list[McpServerRow] = Field(default_factory=list)


class AgentToolRow(BaseModel):
    """One tool as the agentic LLM would see it for a given (intent, persona)."""
    model_config = _ALLOW

    name: str
    # Approval tier 0-4, or -1 for dynamic (resolved at call time). None if the
    # tool is not in the framework REGISTRY (e.g. a materialized MCP tool on an
    # older wheel that doesn't register a tier).
    tier: Optional[int] = None
    # auto | log | approve | step_up | dynamic
    confirm_mode: Optional[str] = None
    # Provenance: "safe" (curated SAFE_TOOLS), "mcp:<server>" (materialized), or
    # "widget" (emit_* card emitter).
    source: str = "safe"
    description: Optional[str] = None
    input_schema: Optional[dict[str, Any]] = None


class AgentToolSurface(BaseModel):
    """The resolved tool surface for a single intent."""
    model_config = _ALLOW

    intent: str
    count: int = 0
    tools: list[AgentToolRow] = Field(default_factory=list)


class ListAgentToolsResult(BaseModel):
    """Response from list_agent_tools."""
    model_config = _ALLOW

    ok: bool = True
    # The module whose persona was applied, or None (built-in intent slice).
    module: Optional[str] = None
    # True when a Key Store persona was resolved and its allowlist applied.
    persona_applied: bool = False
    # One entry per requested intent (both, unless a single intent was asked).
    surfaces: list[AgentToolSurface] = Field(default_factory=list)


# ======================================================================
# Scheduled agent tasks + monitor telemetry (#49)
# ----------------------------------------------------------------------
# These ops read `params` and had no model at all, so the derived op-params
# gate was green over them by NOT PAIRING them -- an op with no model is not a
# failing pair, it is not a pair. `test_op_params_gate` now asserts PRESENCE
# against an explicit opt-out list, so "this op takes no params" has to be
# written down rather than inferred from silence.
#
# The six `*_scheduled_task` ops are the ones that matter most: they are
# WRITES reached from the widget, so a wrong type lands in a real schedule row
# and a real playbook collection rather than in a validation error.
#
# Same policy as every other op-params model: `extra="allow"` for widget /
# connector version skew, with REQUIRED fields doing the actual catching.
# ======================================================================

# ── scheduled tasks ───────────────────────────────────────────────────
class CreateScheduledTaskParams(BaseModel):
    """Input shape for create_scheduled_task. Idempotent on `name`."""
    model_config = _ALLOW_PARAMS

    name: str
    # Standard 5-field cron expression; parsed into the scheduler's crontab.
    cron: str
    prompt: str
    # The Key Store agent-config NAME the runner turn resolves against.
    agent_config: str
    intent: Optional[str] = Field(default="triage")
    allow_mutating: Optional[bool] = Field(default=False)
    timezone: Optional[str] = Field(default="UTC")
    enabled: Optional[bool] = Field(default=True)
    # Where to send findings (email address / slack channel). Stored on the
    # task def so the runner's notify_finding step reads it without a second
    # widget round-trip.
    notify_target: Optional[str] = None


class CreateScheduledTaskResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    name: Optional[str] = None
    wf_iri: Optional[str] = None
    schedule_name: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class GetScheduledTaskParams(BaseModel):
    model_config = _ALLOW_PARAMS

    name: str


class GetScheduledTaskResponse(BaseModel):
    model_config = _ALLOW

    # Present on success; absent on the not_found error envelope.
    task: Optional[dict[str, Any]] = None
    ok: Optional[bool] = None
    error: Optional[dict[str, Any]] = None


class ListScheduledTasksResponse(BaseModel):
    model_config = _ALLOW

    tasks: list[dict[str, Any]] = Field(default_factory=list)


class UpdateScheduledTaskParams(BaseModel):
    """Input shape for update_scheduled_task. Only `name` is required --
    every omitted field keeps its current stored value."""
    model_config = _ALLOW_PARAMS

    name: str
    cron: Optional[str] = None
    prompt: Optional[str] = None
    agent_config: Optional[str] = None
    intent: Optional[str] = None
    allow_mutating: Optional[bool] = None
    timezone: Optional[str] = None
    enabled: Optional[bool] = None
    notify_target: Optional[str] = None


class UpdateScheduledTaskResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    name: Optional[str] = None
    wf_iri: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class DeleteScheduledTaskParams(BaseModel):
    model_config = _ALLOW_PARAMS

    name: str


class DeleteScheduledTaskResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    name: Optional[str] = None
    # False when there was no sqlite def to remove (the schedule/playbook
    # cleanup is best-effort either way).
    deleted: Optional[bool] = None
    error: Optional[dict[str, Any]] = None


class RunScheduledTaskNowParams(BaseModel):
    model_config = _ALLOW_PARAMS

    name: str


class RunScheduledTaskNowResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    name: Optional[str] = None
    # The scheduler's acceptance payload -- the trigger is async, so this
    # confirms acceptance, not completion.
    trigger: Optional[Any] = None
    error: Optional[dict[str, Any]] = None


class ListScheduledTaskRunsParams(BaseModel):
    """Input shape for list_scheduled_task_runs: the runs (one per cron fire)
    of one scheduled task, identified by its def name. Each fire creates a
    chat_turn with session_id ``sched:<name>:<uuid>`` (see _compile_runner_playbook),
    so the runs are the stored sessions whose id has that prefix."""
    model_config = _ALLOW_PARAMS

    name: str
    limit: Optional[int] = Field(default=25)


class ListScheduledTaskRunsResponse(BaseModel):
    model_config = _ALLOW

    # Present on success; absent on the not_found error envelope.
    runs: Optional[list[dict[str, Any]]] = Field(default_factory=list)
    ok: Optional[bool] = None
    error: Optional[dict[str, Any]] = None


class NotifyFindingParams(BaseModel):
    """Input shape for notify_finding: push a scheduled task's finding to the
    analyst via a configured notification channel (SMTP/Slack/Teams/SMS).
    Called by the runner playbook after chat_turn completes."""
    model_config = _ALLOW_PARAMS

    name: str
    # The run's session id (sched:<name>:<uuid>) — used to read the verdict from
    # storage when `verdict` is not passed directly.
    session_id: Optional[str] = None
    # When True (default), a clean run (no verdict / "no findings") skips the
    # notify so a daily sweep with nothing to report does not spam.
    only_on_finding: Optional[bool] = Field(default=True)
    # Explicit verdict text from the runner (the chat_turn result). When absent,
    # the op reads the last assistant transcript for the session.
    verdict: Optional[str] = None
    # The channel target (email address for SMTP, channel name for Slack).
    # When absent, the send uses the connector's default config.
    notify_target: Optional[str] = None


class NotifyFindingResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    name: Optional[str] = None
    session_id: Optional[str] = None
    # Which channel dispatched (smtp/slack/...); absent when skipped.
    channel: Optional[str] = None
    channel_label: Optional[str] = None
    subject: Optional[str] = None
    # Present when no notify was sent (no_finding / no_channels).
    skipped: Optional[str] = None
    message: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class AutoTriageParams(BaseModel):
    """Input shape for auto_triage: triage one record unattended. Called by the
    on-create playbook that set_auto_triage installs."""
    model_config = _ALLOW_PARAMS

    # The record IRI (/api/3/<module>/<uuid>).
    record: str
    # Triage only these severities (display text); None = High and Critical,
    # [] = any.
    severities: Optional[list[str]] = None
    # Triage only records whose `source` is one of these; empty = any.
    sources: Optional[list[str]] = None
    # Who is told when this record needs a human.
    notify_target: Optional[str] = None
    # Alert-storm grouping and rate cap: {group, window_minutes, keys,
    # same_name, max_sessions, cap_window_minutes}; omitted keys keep defaults.
    storm: Optional[dict[str, Any]] = None


class AutoTriageResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    record: Optional[str] = None
    session_id: Optional[str] = None
    # Why the record was not triaged: severity / source / already_triaged /
    # grouped (attached to a lead's session) / storm_cap (deferred).
    skipped: Optional[str] = None
    # For a grouped record: the lead's session and record, and the group size.
    lead_session: Optional[str] = None
    lead_record: Optional[str] = None
    group_size: Optional[int] = None
    stop_reason: Optional[str] = None
    # The delivered verdict card's disposition/severity/confidence/summary;
    # None when the turn ended without one.
    verdict: Optional[dict[str, Any]] = None
    # Cards waiting on a human (action cards, run approvals).
    waiting: Optional[int] = None
    needs_human: Optional[bool] = None
    # no_verdict / needs_review / awaiting_approval / awaiting_answer /
    # confirmed_threat / turn_error / action_unverified.
    why: Optional[list[str]] = None
    # Phase E: actions the autonomy policy ran itself
    # ({card_id, connector, operation, targets, rule, ok, verified}).
    acted: Optional[list[dict[str, Any]]] = None
    # Set when a human is needed: the queued escalation card, its priority
    # (P1..P4), and the channel the reviewer was told on (or why not).
    escalation_id: Optional[str] = None
    priority: Optional[str] = None
    notified: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class SetAutoTriageParams(BaseModel):
    """Input shape for set_auto_triage: install or replace the on-create
    auto-triage playbook for a module."""
    model_config = _ALLOW_PARAMS

    # The connector configuration the triage turn runs under.
    agent_config: str
    module: Optional[str] = None
    severities: Optional[list[str]] = None
    sources: Optional[list[str]] = None
    enabled: Optional[bool] = Field(default=True)
    # Who is told when a record needs a human (email address / channel).
    notify_target: Optional[str] = None


class SetAutoTriageResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    module: Optional[str] = None
    wf_iri: Optional[str] = None
    enabled: Optional[bool] = None
    severities: Optional[list[str]] = None
    sources: Optional[list[str]] = None
    notify_target: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class ResolveEscalationParams(BaseModel):
    """Input shape for resolve_escalation: take an auto-triage escalation off
    the queue."""
    model_config = _ALLOW_PARAMS

    session_id: str
    card_id: str
    # handled (default) / dismissed.
    outcome: Optional[str] = None
    # The reviewer ({name, iri}); the connector runs as a service account.
    actor: Optional[dict[str, Any]] = None


class ResolveEscalationResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    session_id: Optional[str] = None
    card_id: Optional[str] = None
    outcome: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class UndoActionParams(BaseModel):
    """Input shape for undo_action: reverse an approved action card that ran."""
    model_config = _ALLOW_PARAMS

    session_id: str
    card_id: str
    # Who is undoing ({name, iri}); the connector runs as a service account.
    actor: Optional[dict[str, Any]] = None


class UndoActionResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    session_id: Optional[str] = None
    card_id: Optional[str] = None
    # {connector, op, source, args}: the reverse call that ran.
    undo: Optional[dict[str, Any]] = None
    result: Optional[Any] = None
    error: Optional[dict[str, Any]] = None


# ── session rating ────────────────────────────────────────────────────
class RateSessionParams(BaseModel):
    """Input shape for rate_session. An empty/null `rating` CLEARS it, so the
    field is deliberately not a `Literal["up","down"]` -- the storage layer
    owns the vocabulary and returns a `bad_rating` envelope for anything else."""
    model_config = _ALLOW_PARAMS

    session_id: str
    rating: Optional[str] = None
    note: Optional[str] = None


class RateSessionResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    session_id: Optional[str] = None
    rating: Optional[Any] = None
    error: Optional[dict[str, Any]] = None


# ── native-MCP diagnostic ─────────────────────────────────────────────
class ProbeNativeMcpParams(BaseModel):
    """Input shape for probe_native_mcp. The cred params override config so a
    diagnostic can inject a key without a config write."""
    model_config = _ALLOW_PARAMS

    server: Optional[str] = Field(default="soc")
    tool: Optional[str] = None
    # A JSON string is accepted and parsed by the op (widget form fields).
    args: Optional[Union[dict[str, Any], str]] = None
    api_key: Optional[str] = None
    soar_username: Optional[str] = None
    soar_password: Optional[str] = None


class ProbeNativeMcpResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    # api_key | bearer | hmac -- which credential the gateway adapter used.
    auth_mode: Optional[str] = None
    server: Optional[str] = None
    tools: Optional[list[Any]] = None
    count: Optional[int] = None
    called: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[dict[str, Any]] = None


# ── usage telemetry (agentic-assistant-monitor) ───────────────────────────
class ListUsageParams(BaseModel):
    """Input shape for list_usage (and get_audit_trail, which delegates to it).

    `from` / `to` are ISO-8601 range bounds. `from` is a Python keyword, so the
    field is `from_` with the wire name as its alias; `populate_by_name` keeps
    both spellings working, and `extra="allow"` means neither is ever rejected."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    session_id: Optional[str] = None
    user_iri: Optional[str] = None
    model: Optional[str] = None
    intent: Optional[str] = None
    status: Optional[str] = None
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    limit: Optional[int] = Field(default=200)


class ListUsageResponse(BaseModel):
    model_config = _ALLOW

    usage: list[dict[str, Any]] = Field(default_factory=list)


class GetAuditTrailParams(ListUsageParams):
    """get_audit_trail is `return list_usage(config, params)` -- same shape by
    construction, named so the derived gate pairs it."""


class GetAuditTrailResponse(ListUsageResponse):
    """See GetAuditTrailParams."""


class GetUsageSummaryParams(BaseModel):
    """Input shape for get_usage_summary (the monitor's charts)."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # hour | day
    bucket: Optional[str] = Field(default="day")
    # tokens | cost | turns
    metric: Optional[str] = Field(default="tokens")
    # model | intent | user_iri
    group_by: Optional[str] = None
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None


class GetUsageSummaryResponse(BaseModel):
    model_config = _ALLOW

    summary: Any = None


class ListPendingResponse(BaseModel):
    model_config = _ALLOW

    pending: list[dict[str, Any]] = Field(default_factory=list)
    # Unresolved auto-triage escalations, most urgent first.
    escalations: list[dict[str, Any]] = Field(default_factory=list)


class ListShadowDecisionsParams(BaseModel):
    """Input shape for list_shadow_decisions (the Monitor's shadow table)."""
    model_config = _ALLOW_PARAMS

    # Default: the 2-week shadow period decision D3 requires before enforcing.
    since_hours: Optional[float] = Field(default=336, gt=0)


class ListShadowDecisionsResponse(BaseModel):
    model_config = _ALLOW

    rules: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    # Phase F: per detection rule, records the policy closed as false
    # positives and how many were reopened ({source, rule, closed, reopened, last_at}).
    tuning: list[dict[str, Any]] = Field(default_factory=list)
    policy: Optional[dict[str, Any]] = None
    # The full policy document, so the Monitor can show each rule's conditions
    # (the `policy` field above is only an enabled/mode/rule-ids summary).
    policy_document: Optional[dict[str, Any]] = None


class GetAutonomyPolicyResponse(BaseModel):
    """Output of get_autonomy_policy: the current policy document and whether it
    parses against the framework model (for the Monitor's policy editor)."""
    model_config = _ALLOW

    ok: bool = True
    policy: Optional[dict[str, Any]] = None
    # A read error is a plain string; a write refusal rides the `_err` envelope
    # ({code, message}), so this accepts either shape.
    error: Optional[Any] = None
    valid: bool = True
    problem: Optional[str] = None


class SetAutonomyPolicyParams(BaseModel):
    """Input shape for set_autonomy_policy. `policy` is the full policy document
    (a JSON object); a string is accepted and parsed."""
    model_config = _ALLOW_PARAMS

    policy: Any = None


class SetAutonomyPolicyResponse(BaseModel):
    model_config = _ALLOW

    ok: bool = False
    policy: Optional[dict[str, Any]] = None
    # Success: a plain string or None. Refusal: the `_err` envelope
    # {code, message[, required_roles]}.
    error: Optional[Any] = None
    valid: Optional[bool] = None


class ListActiveSessionsParams(BaseModel):
    """Input shape for list_active_sessions (the Live Sessions panel)."""
    model_config = _ALLOW_PARAMS

    limit: Optional[int] = Field(default=50)
    # active | waiting_approval | idle
    status: Optional[str] = None
    user_iri: Optional[str] = None


class ListActiveSessionsResponse(BaseModel):
    model_config = _ALLOW

    sessions: list[dict[str, Any]] = Field(default_factory=list)


# ── persona resolution ────────────────────────────────────────────────
class ResolvePersonaParams(BaseModel):
    """Input shape for resolve_persona (the widget's Key-Store fallback).

    `module` is optional at the model layer even though the op needs it: the op
    is deliberately FAIL-OPEN (a missing/non-string module returns
    `{found: False}` so the widget falls back to triage framing), and making it
    required here would convert that designed degradation into a raised
    ConnectorError."""
    model_config = _ALLOW_PARAMS

    module: Optional[str] = None


class ResolvePersonaResponse(BaseModel):
    model_config = _ALLOW

    # Optional, not required, even though the op always sets it: every wired
    # response model must also accept a bare `_err(...)` envelope (see
    # test_response_model_seam), and a required field is what breaks that.
    found: Optional[bool] = None
    module: Optional[str] = None
    label: Optional[str] = None
    # The persona's opaque jSONValue.ui block, or None.
    ui: Optional[dict[str, Any]] = None


# ── yaml compile / validate responses ─────────────────────────────────
class CompileYamlResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    workflow_json: Optional[Any] = None
    errors: list[Any] = Field(default_factory=list)


class ValidateYamlResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    errors: list[Any] = Field(default_factory=list)


# ======================================================================
# Tool result envelopes (PLAN.md Phase 5)
# ----------------------------------------------------------------------
# The `{"ok": ...}` dicts the tools return. These are what the LLM READS, so
# their shape is load-bearing for model behaviour in a way an operation
# response is not -- a tool error the model cannot parse is a retry it cannot
# make.
#
# The survey that motivated the split: across `fsr_soc_triage/` there are 110
# `{"ok": ...}` return literals. The ERROR half is near-uniform -- 93 of 94
# carry exactly `ok` + `code` + `message` plus tool-specific context. The
# SUCCESS half shares nothing but `ok` itself: `op`, `query`, `recording`,
# `calls`, `guidance`, `how_to_drive`, `by_event_type` ... 34 distinct key sets.
#
# So the two halves get very different treatment, and `ToolOk` is deliberately
# thin. Pinning success fields would mean 78 per-tool models for a payload the
# model reads as free-form context anyway.
# ======================================================================

class ToolOk(BaseModel):
    """A successful tool envelope.

    Intentionally near-empty: `ok` is the ONLY key the success envelopes share.
    It exists so `ok` cannot be typed as something other than a bool and so the
    success/error split is expressible, not to constrain the payload.
    """
    model_config = _ALLOW

    ok: Literal[True] = True


class ToolErr(BaseModel):
    """A failed tool envelope: the vocabulary the agent retries against.

    `code` is for the agent's control flow (and for our own grep-the-sessions
    analysis, which buckets by it); `message` is the human/model-readable
    reason. Both required -- an error envelope missing one is the defect this
    model exists to prevent, since the model then has nothing stable to branch
    on and falls back to re-reading prose.
    """
    model_config = _ALLOW

    ok: Literal[False] = False
    code: str
    message: str


def tool_err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build a tool error envelope THROUGH `ToolErr`.

    Same argument as `operations._frame`: the win is not the type check, it is
    that a hand-written literal can silently omit a key and nothing notices.
    Extra keys are the tool's own context (`op`, `query`, `module`,
    `suggestions`, ...) and pass through untouched.
    """
    return ToolErr(code=code, message=message, **extra).model_dump()


class PlaybookHealthParams(BaseModel):
    """Input shape for playbook_health (X4): how many bottled playbooks to list."""
    model_config = ConfigDict(extra="allow")

    limit: Optional[int] = Field(default=20)


class PlaybookHealthResponse(BaseModel):
    """Response from playbook_health (X4): one health row per bottled playbook."""
    model_config = ConfigDict(extra="allow")

    ok: bool
    count: int
    playbooks: list[dict]


# ── Monitor revamp (V2 M1-M5) ─────────────────────────────────────────
class MonitorCasesParams(BaseModel):
    """Input shape for monitor_cases. `view` picks list (default), kpis, policy,
    tuning or would_help. `bucket` (list only): all, handled, needs_you or reopened."""
    model_config = _ALLOW_PARAMS

    view: Optional[str] = Field(default="list")
    bucket: Optional[str] = Field(default="all")
    # Look-back window in hours: 24 for list and kpis, 336 for policy and tuning.
    hours: Optional[float] = None
    limit: Optional[int] = Field(default=50)
    # False skips the live read that spots a closed record that is open again.
    check_reopened: Optional[bool] = Field(default=True)
    # Whose feedback shows as "mine"; the verified caller wins when present.
    actor: Optional[Any] = None


class MonitorCasesResponse(BaseModel):
    """Response from monitor_cases. The fields present depend on `view`:
    list -> total, counts, rows; kpis -> kpis; policy -> mode, thresholds,
    rules; tuning -> thresholds, rules."""
    model_config = _ALLOW

    ok: bool
    view: Optional[str] = None
    hours: Optional[float] = None
    bucket: Optional[str] = None
    total: Optional[int] = None
    counts: Optional[dict[str, int]] = None
    rows: Optional[list[dict[str, Any]]] = None
    kpis: Optional[dict[str, Any]] = None
    mode: Optional[str] = None
    thresholds: Optional[dict[str, Any]] = None
    rules: Optional[list[dict[str, Any]]] = None
    error: Optional[dict[str, Any]] = None


# ── Scheduled hunts and the shift summary (X6) ────────────────────────
class SaveHuntParams(BaseModel):
    """Input shape for save_hunt: create or edit a hunt (idempotent on `name`;
    omitted fields keep their stored value)."""
    model_config = _ALLOW_PARAMS

    name: str
    # The hunt question, asked in plain words on every run.
    question: Optional[str] = None
    # Standard 5-field cron expression (not more often than every 15 minutes).
    cron: Optional[str] = None
    timezone: Optional[str] = None
    # Where a finding opens its case: alerts or incidents.
    module: Optional[str] = None
    # False pauses the schedule; the definition and history stay.
    enabled: Optional[bool] = None
    # The connector configuration the runner calls; the default one when absent.
    agent_config: Optional[str] = None
    actor: Optional[Any] = None


class ListHuntsParams(BaseModel):
    """Input shape for list_hunts (no parameters)."""
    model_config = _ALLOW_PARAMS


class DeleteHuntParams(BaseModel):
    """Input shape for delete_hunt."""
    model_config = _ALLOW_PARAMS

    name: str


class RunHuntParams(BaseModel):
    """Input shape for run_hunt. The runner playbook sends only `name`; the
    Monitor's Run now sends `trigger` so the scheduler fires it."""
    model_config = _ALLOW_PARAMS

    name: str
    trigger: Optional[bool] = Field(default=False)


class HuntResponse(BaseModel):
    """Response shared by the hunt ops: the fields present depend on the op
    (list_hunts -> hunts; save_hunt -> created, enabled; run_hunt -> outcome)."""
    model_config = _ALLOW

    ok: bool
    name: Optional[str] = None
    hunts: Optional[list[dict[str, Any]]] = None
    error: Optional[dict[str, Any]] = None


class ListHuntsResponse(HuntResponse):
    """Response from list_hunts."""


class SaveHuntResponse(HuntResponse):
    """Response from save_hunt."""


class DeleteHuntResponse(HuntResponse):
    """Response from delete_hunt."""


class RunHuntResponse(HuntResponse):
    """Response from run_hunt."""


class ShiftSummaryParams(BaseModel):
    """Input shape for shift_summary: the digest for the last `hours` (default
    8), ending at `until` (epoch seconds; default now)."""
    model_config = _ALLOW_PARAMS

    hours: Optional[float] = None
    until: Optional[float] = None
    # Send the digest through the configured notification channel (X6).
    notify: Optional[bool] = None
    actor: Optional[Any] = None


class ShiftSummaryResponse(BaseModel):
    """Response from shift_summary: the numbers by section plus the same digest
    as plain `text`."""
    model_config = _ALLOW

    ok: bool
    hours: Optional[float] = None
    handled: Optional[dict[str, Any]] = None
    waiting: Optional[dict[str, Any]] = None
    notable: Optional[dict[str, Any]] = None
    would_help: Optional[dict[str, Any]] = None
    hunts: Optional[dict[str, Any]] = None
    subject: Optional[str] = None
    text: Optional[str] = None
    notified: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class ExportAuditParams(BaseModel):
    """Input shape for export_audit: the audit for the last `hours` (default
    24), ending at `until` (epoch seconds; default now), as csv, json or both."""
    model_config = _ALLOW_PARAMS

    hours: Optional[float] = None
    until: Optional[float] = None
    format: Optional[str] = None
    actor: Optional[Any] = None


class ExportAuditResponse(BaseModel):
    """Response from export_audit: row counts, the redaction mode applied and
    the content as `csv` and `json` strings."""
    model_config = _ALLOW

    ok: bool
    redaction: Optional[str] = None
    file_stem: Optional[str] = None
    counts: Optional[dict[str, int]] = None
    csv: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class MonitorFeedbackParams(BaseModel):
    """Input shape for monitor_feedback: right or wrong on one decision."""
    model_config = _ALLOW_PARAMS

    # The record IRI the decision was made on (/api/3/<module>/<uuid>).
    record: str
    # right | wrong; empty clears this analyst's feedback.
    rating: Optional[str] = None
    note: Optional[str] = None
    actor: Optional[Any] = None


class MonitorFeedbackResponse(BaseModel):
    model_config = _ALLOW

    ok: bool
    record: Optional[str] = None
    session_id: Optional[str] = None
    feedback: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
