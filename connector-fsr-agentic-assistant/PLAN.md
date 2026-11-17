# PLAN -- Broaden pydantic coverage across `connector-fsr-agentic-assistant`

Status: **Phases 1-6 are DONE.** Phase 5 was run as the plan specified -- pilot,
measure, then decide -- and the measurement changed the fix: see below.
Written 2026-07-22; header corrected 2026-07-31 (it still read "spec only,
nothing implemented" long after the work landed).

| phase | state |
|---|---|
| 1 -- outbound frames | ✅ `8cee211`. Every outbound frame constructed through its model via `_frame()`/`_as_frame()`, `exclude_unset=True` so each conversion is byte-identical to the literal it replaced. OUTBOUND-permissive: a mismatch logs and falls back to the plain dict, never raising mid-turn. `tests/test_outbound_frame_models.py` includes a source-grep gate so a raw frame literal cannot reappear (RED-proven). One plan correction: `{"type": "workflow_collections", …}` is FortiSOAR's import/export envelope, **not** a wire frame -- it got `WorkflowCollectionsEnvelope` and stays **out** of `PollFrame`. |
| 2 -- op responses | ✅ Wired at the dispatch seam (`_RESPONSE_MODELS` + `_check_response`), not at each `return` -- `update_playbook` has ten returns, `dry_run_playbook` nine, mostly `_err(...)` envelopes. Warns in production, raises under `FSRPB_STRICT_MODELS=1`: raising on our own output would fail a turn that succeeded. Live-checked against 8.0 -- seven real payloads validate clean, so the models were right and simply unwired. |
| 3 -- op params | ✅ `2d96610`. 10 ops validate inbound params, `extra="allow"` kept **on purpose**: widget and connector ship independently, so an older connector must not reject a newer widget's key. |
| 4 -- LLM tool-arg gate | ✅ `2d96610`. Arg models generated from the real `inspect.signature` -- never hand-guessed, because `_model_covers_signature` silently DISABLES the gate for any model missing a real parameter, so a guessed field list looks exactly like success. Two findings that contradicted this plan: (a) `run_op` is registered by the **framework**, so no connector registration site ever wrapped it and the planned "one line into `TOOL_ARG_MODELS`" would have been a pure no-op -- added `_gate_framework_tools()` re-wrapping the REGISTRY entry; (b) `RunOpArgs` itself was wrong (declared `operation`, but the parameter is `op`), so the gate would have found 4 uncovered params and disabled itself. |
| 5 -- tool result envelopes | ✅ **but not as specified** -- the pilot's measurement inverted the plan. The two halves are nothing alike: of 110 `{"ok": ...}` returns, the 94 ERROR envelopes already shared exactly `ok`/`code`/`message` (93 of 94), while the SUCCESS envelopes share only `ok` across 34 distinct key sets. Retyping 78 individually would have pinned the consistent half and been unable to pin the other. Landed instead: a `ToolErr`/`ToolOk` pair + a `tool_err()` constructor, a pilot conversion of `tools_records.py` (12) and `tools_noc.py` (1), and `tests/test_tool_envelope_vocabulary.py` -- a STATIC scan asserting every error envelope in the package declares both keys (RED-proven). One real defect found: `arg_validation` returned `{"ok": False, "error": ...}`, the only dialect on the surface, on the very envelope the model reads to correct itself. |
| 6 -- parity enforcement | ✅ `2d96610`. Contract parity is now a test, and it found real drift: `enhancement_offer` + `patch_proposal` were declared by the widget with no `PollFrame` member. |

**Not a phase of this plan, but landed in this connector** (2026-07-31, `fb72474`):
the **HARDEN-1 pre-write gate** on `update_playbook` -- refuses a save that
silently deletes part of the live playbook. Tracked in
`fsr_all_widgets/docs/plans/playbook-compiler-fidelity-and-agent-surface.md`
§3.4. Related: `ec625df` fixed a test module that had never run once (a
collection error is not a failure) and added a suite-collectability guard.

Suite today: **478 passed / 2 skipped**, zero collection errors.

---

## 0. The finding that reframes this work

This is **not** a "write more models" job. The survey says the models are
largely already written -- they were just never wired to production code.

| measure | today |
|---|---|
| models defined in `pydantic_models.py` | **79** |
| …referenced anywhere outside `pydantic_models.py` + tests | **29** |
| …**defined, unit-tested, and used by nothing** | **50** |
| ops (of 25) validating their **params** | **7** |
| ops validating their **response** | **3** |
| registered triage tools (of 17) with a strict arg gate | **6** |
| hand-built `{"type": ...}` wire-frame dicts in `operations.py` | **~30** |
| `return {"ok": ...}` tool envelopes across `fsr_soc_triage/` | **78** |

`tests/test_pydantic_stage3.py` already contains **27 passing tests** asserting
those 50 models accept the real shapes. The module docstring states the
intent plainly: *"(Wiring those at the dispatch boundary is staged.)"*

**So the bulk of this plan is wiring, not authoring.** That makes it far cheaper
than it looks, and it means most steps are provably safe -- the models were
written against the real shapes and already pass tests.

### Why it matters (a defect this class already caused)

On 2026-07-22 a hand-rolled `stream_end` dict in `_resume_conversation` silently
omitted `last_assistant_yaml`. That frame is the **only** transcript delivery for
a polling consumer, so a build-mode resume producing YAML would have lost it --
no error, no test failure. `StreamEndFrame` existed the whole time. Building
through the model fixed it. There are ~30 more hand-built frame dicts.

---

## 1. Goal and scope

### Done means

1. Every operation in `info.json` validates its **params** through a model.
2. Every operation returns a response either **constructed through** a model or
   validated against one.
3. Every wire frame the connector emits is **constructed through** its frame
   model -- no hand-written `{"type": ...}` dict literals in `operations.py`.
4. The LLM-facing tool surface (`fsr_soc_triage`) has a strict arg gate on every
   registered tool, not 6 of 17.
5. `contract.d.ts` ↔ `pydantic_models.py` parity is **enforced by a test**, not
   by the docstring asking nicely.

### Explicit non-goals

- **Do not** make OUTBOUND validation fatal. The documented policy is
  INBOUND-strict / OUTBOUND-permissive, and it is correct: a strict validator on
  our own streamed output would break live `chat_poll` on any field evolution.
  Outbound work means *construct through the model*, never *validate-and-raise*.
- **Do not** retype `fsr_playbooks` (the framework). It has its own
  `llm/tool_models.py`; this plan stops at the connector boundary.
- **Do not** touch `storage.py`'s SQLite schema. Row models describe what comes
  out; they do not become an ORM.
- **Do not** convert the 78 tool-result envelopes in one pass -- see §5, this is
  the one genuinely large and genuinely risky area.

---

## 2. Affected files

| file | role in this work |
|---|---|
| `pydantic_models.py` | 79 models; add ~6 missing, no restructuring |
| `operations.py` (7276 ln) | the wiring surface: 25 op entry points + ~30 frame literals |
| `fsr_soc_triage/arg_validation.py` | the strict tool-arg gate -- extend from 6 → 17 tools |
| `fsr_soc_triage/registry.py` | already wraps every tool via `_validating`; no change expected |
| `fsr_soc_triage/tools_*.py` | source of the 78 `{"ok": ...}` envelopes (Phase 5 only) |
| `storage.py` | read boundary for `ChatTurnRow` / `TurnProgressRow` / `ChatMessageRow` |
| `tests/test_pydantic_contract.py`, `tests/test_pydantic_stage3.py` | existing; extend |
| `widgets-src/…/widgetAssets/js/contract.d.ts` | parity target (read-only here) |

---

## 3. Ordered implementation steps

Sequenced **safest-first**: every step through Phase 3 is non-breaking by
construction, so the risky work lands on a proven base.

### Phase 1 -- Outbound frames (no behaviour change, highest defect-prevention)

> These cannot break a caller: we build the same dict, just through a model.

- **1.1** Add a `_frame(Model, **kwargs) -> dict` helper in `operations.py`
  (`Model(**kwargs).model_dump(exclude_none=True)`). One place to add logging
  or a debug-mode strict check later.
- **1.2** Convert the 3 `{"type": "stream_end"}` literals to `StreamEndFrame`.
  *One is already done (`_resume_conversation`); do `chat_turn`'s two.* This is
  where the known defect lived -- highest value, smallest diff.
- **1.3** Convert `{"type": "turn_start"}` → `TurnStartFrame` (in `storage.py`'s
  `reserve_next_turn`, note -- not `operations.py`).
- **1.4** Convert the card frames: `info_card` ×4, `status_card` ×2,
  `approval_request`, `workflow_collections` → their models.
  ⚠️ `workflow_collections` has **no model** -- author `WorkflowCollectionsFrame`
  and add it to the `PollFrame` union.
- **1.5** Convert `text`/`tool_use`/`tool_result`/`usage`/`error`/`unknown`
  literals (8+3+3+1+3+1) -- mostly inside `_event_to_wire`.
- **1.6** Add a test asserting **no `{"type": "` literal remains** in
  `operations.py` (a simple source grep), so this cannot regress.

### Phase 2 -- Op responses (non-breaking: models are `extra="allow"`)

- **2.1** Wire the 8 already-written, already-tested response models to their
  ops: `HealthCheckResponse`, `WarmupResponse`, `ResolveYamlResponse`,
  `PushPlaybookResponse`, `RenderJinjaResponse`, `QueryStoreResponse`,
  `DryRunPlaybookResponse`, `GetSessionTraceResponse`.
- **2.2** Same for `ListSessionsResponse`, `ListMcpToolsResponse`,
  `RespondManualInputResponse`, `DecompilePlaybookResponse`.
- **2.3** Fill response-model gaps for ops that have none:
  `list_models` (`ModelChoice` exists -- needs a `ListModelsResponse` wrapper),
  `compile_yaml`, `validate_yaml`, `call_mcp_tool`, `probe_native_mcp`,
  `resolve_persona`, `list_agent_tools`.

### Phase 3 -- Op params, permissive first

- **3.1** Wire the written `extra="allow"` params models:
  `HealthCheckParams`, `WarmupParams`, `ListSessionsParams`,
  `RespondManualInputParams`, `PushPlaybookParams`, `QueryStoreParams`,
  `GetSessionTraceParams`, `DryRunPlaybookParams`, `RenderJinjaParams`,
  `CallMcpToolParams`.
- **3.2** Author + wire params models for the remaining ops: `list_models`,
  `resolve_persona`, `list_mcp_tools`, `probe_native_mcp`, `echo_param_types`.
- **3.3** ⚠️ **Measure before wiring the strict ones.** `ValidateYamlParams`,
  `CompileYamlParams`, `ResolveYamlParams` are `extra="forbid"`. Wiring them
  turns any extra key a caller sends into a `ConnectorError`. Before wiring:
  grep the widget + `fsr_all_widgets/scripts/` for every call site and diff the
  payload keys against the model. Land as `extra="allow"` + a **warning log** on
  unexpected keys; tighten to `forbid` only after a clean live sweep.

### Phase 4 -- LLM tool-arg gate (the untrusted-input boundary)

> Highest *security* value: these args are model-generated.

- **4.1** Wire the already-written, already-strict **`RunOpArgs`** into
  `TOOL_ARG_MODELS`. `run_op` is the highest-traffic tool and the one that
  reaches real connectors -- it should have been first.
  ⚠️ Confirm `_model_covers_signature` passes, or the self-protecting gate
  silently skips and the step is a no-op. **Assert coverage in a test.**
- **4.2** Author arg models for the 15 ungated SIEM/FAZ/FMG tools:
  `siem_search_ip|host|user`, `siem_events_for_incident`, `siem_raw_query`,
  `faz_get_alerts`, `faz_search_ip`, `faz_raw_query`, `faz_search_device_events`,
  `faz_search_by_serial`, `faz_event_summary`, `fmg_get_device_list|status`,
  `fmg_get_ha_status`, `fmg_get_policy_package_status`.
  Generate each from the real `inspect.signature`, don't hand-guess.
- **4.3** Add a test that **every** name in `_TRIAGE_TOOL_NAMES` has an entry in
  `TOOL_ARG_MODELS` and that its model covers the function signature. This is
  the anti-drift assertion -- cf. the parallel-name-list bug class that produced
  four defects on 2026-07-22.

### Phase 5 -- Tool result envelopes -- DONE, and the measurement changed the fix

Run exactly as 5.1-5.3 specified. The pilot's finding is worth recording,
because the plan's own framing ("78 envelopes, retype them") was wrong:

| half | count | shared vocabulary |
|---|---|---|
| `ok=False` | 94 | `ok` + `code` + `message` -- **93 of 94 already identical** |
| `ok=True` | 12 literal (+ computed) | `ok`, and nothing else. 34 distinct key sets overall |

The success envelopes carry `op`, `query`, `recording`, `calls`, `guidance`,
`how_to_drive`, `by_event_type`, `first_ts`/`last_ts` ... they are free-form
context the model reads as prose. A `ToolOk` that pins anything real would be 78
per-tool models; a `ToolOk` that pins only `ok` is what shipped, and it is
honest about being thin.

So the value was never in retyping. It was in (a) making the error vocabulary
impossible to break and (b) finding where it was already broken.

- **5.1** ✅ `ToolOk` / `ToolErr` in `pydantic_models.py`, plus `tool_err()`,
  which builds the envelope THROUGH the model. Same argument as `_frame()`: the
  win is not the type check, it is that a hand-written literal can silently omit
  a key and nothing notices.
- **5.2** ✅ Pilot: `tools_records.py` (12 envelopes) and `tools_noc.py` (1).
  **Deviation from the plan's named modules:** `tools_agent.py` was specified,
  but it contains *no* literal error envelopes, so converting it would have
  measured nothing. `tools_records.py` was substituted as the smallest module
  with a representative error surface. Diff cost: the conversion SHORTENED every
  site. One behavioural delta worth knowing -- `model_dump()` emits declared
  fields first, so an envelope that listed `op`/`query` before `code` comes out
  reordered. Same JSON semantically; the LLM reads a dict.
- **5.3** ✅ Stopped and re-evaluated, and took the plan's own alternative: the
  remaining ~80 conversions are NOT worth the churn, because a static test buys
  the same guarantee at no runtime risk. `tests/test_tool_envelope_vocabulary.py`
  AST-scans the whole package and asserts every `{"ok": False}` literal declares
  `code` and `message`. It is a source scan on purpose: these tools mostly fail
  against a live appliance, so their error paths are exactly the ones a unit
  suite never executes. RED-proven by injecting a bad envelope.

**The defect this found.** `arg_validation.validating()` returned
`{"ok": False, "error": "invalid args for ..."}` -- the single envelope on the
whole surface not speaking `code`/`message`, and the one the model reads in
order to fix its own tool call. Now `code="invalid_tool_args"` + `message` +
`tool`. Emitting both shapes for a release was considered and rejected: two
dialects is what caused this.

### Phase 6 -- Parity enforcement

- **6.1** Extend `tests/test_pydantic_contract.py` to parse `contract.d.ts` and
  assert every `FsrPbChat` wire-frame `type` literal has a `PollFrame` member,
  and vice versa. Today parity is a docstring request; make it a test.

---

## 4. Risks and edge cases

| # | risk | mitigation |
|---|---|---|
| R1 | **A validation error becomes a broken op on the box.** `_validate_model` raises `ConnectorError`. | Phases 1-3 use `extra="allow"` models only. Nothing becomes newly-rejecting before §3.3's measure step. |
| R2 | **Strict outbound would break live `chat_poll`.** | Non-goal, stated in §1. Outbound = construct-through-model only. Never call `_validate_model` on our own frames. |
| R3 | **The tool-arg gate silently no-ops.** `validating()` skips the gate when the model misses a real parameter -- a wrong model looks like success. | §4.1/4.3 assert `_model_covers_signature(...) == set()` in a test rather than trusting the runtime log. |
| R4 | **`model_dump(exclude_none=True)` changes the wire.** A field currently emitted as explicit `null` would disappear. | Diff a real `chat_poll` payload before/after Phase 1 on a live session. The widget's `contract.d.ts` marks most fields optional, but *verify*, don't assume. |
| R5 | **Ship cadence.** Every connector change is a release + ship to a box; the GA demo is the active priority. | Land Phase 1 alone first (highest value, smallest risk), ship with the next demo fix rather than as its own release. |
| R6 | Field-order / `Literal` mismatch silently reorders the `PollFrame` union and changes which member matches. | `UnknownEvent` must stay last (already documented). Add a test asserting it. |
| R7 | Stage 3 models were written against shapes as of ~16 days ago; `operations.py` has changed since. | The 27 existing tests pass, but they assert *models accept shapes*, not *production emits them*. Wiring is what proves it -- expect 1-2 genuine mismatches and treat each as a real finding. |

### Open questions (need a decision before Phase 3.3 / Phase 5)

1. **Should INBOUND op params ever be `forbid`?** The widget and connector ship
   independently; a newer widget sending a new key would be rejected by an older
   connector. Recommendation: **keep op params `allow` + warn**, reserve `forbid`
   for LLM tool args (untrusted generator, no version-skew problem).
2. **Is Phase 5 worth it at all?** 78 envelopes, agent-visible text, modest
   type benefit. Recommendation: pilot then decide; default to *no*.
3. Should `_frame()` validate strictly when an env flag (`FSRPB_DEV=1`) is set --
   loud in dev, permissive on the box? Cheap to add, catches drift early.

---

## 5. How to verify

```bash
# connector unit suite (the -p no:randomly + ignore are required: one module
# imports the on-box `connectors` package and cannot collect locally)
cd connector-fsr-agentic-assistant
FSRPB_DEV=1 ../.venv/bin/python -m pytest tests -q -p no:randomly \
    --ignore=tests/test_warmup_instance_rewarm.py     # baseline today: 173 passed

# the two contract suites specifically
FSRPB_DEV=1 ../.venv/bin/python -m pytest \
    tests/test_pydantic_contract.py tests/test_pydantic_stage3.py -q
```

Per-phase gates:

- **Phase 1** -- R4 check: capture a real `chat_poll` payload before and after,
  diff the JSON keys. Must be byte-identical apart from intentionally-dropped
  `null`s.
- **Phase 3.3** -- run the live sweep before tightening anything to `forbid`.
- **Phase 4** -- after wiring, drive one real triage turn and confirm no tool
  returns an arg-validation error that it did not return before.
- **Whole change** -- `make ship ENV=…` then re-run
  `scratchpad/ga_beat5_probe.py "The-Flame" "ok, isolate that host now"`; beat 5
  must still stage `fortinet-fortiedr.isolate_collector`.

---

## 6. Recommended first slice

If this is time-boxed, do **Phase 1 only** (~30 frame literals, one helper, one
anti-regression test). It is non-breaking, it directly prevents the class of
defect that already shipped, and it is independently valuable if the rest is
never done. Phase 4.1 (`RunOpArgs`, one line + one test) is the best
value-per-diff after that.
