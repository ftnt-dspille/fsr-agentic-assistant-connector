---
title: FSR Agentic Assistant Connector Plan
category: plans
status: reference
source: hand-written
topics:
- connector-plan
- playbook-builder
- agent-loop
- entity-grounding
- deployment
summary: 'Phased plan (P0-P7) for agent-driven playbook builder connector: provider,
  chat ops, HITL, mock parity, triage intent, entity context, contract versioning,
  demo recipes. SUPERSEDED -- reference only; work is tracked on the agentic-assistant-tracker board.'
---

> **SUPERSEDED.** This plan predates the rename and is no longer maintained. The roadmap is `ROADMAP.md` in the private agentic-assistant-tracker repo.

# connector-fsr-agentic-assistant -- Connector Plan

> ⚠️ **REFERENCE ONLY -- do not plan from this file.** Written against `fsr_core`
> and connector `v0.3.24` (phases P0-P7); both are long superseded. The library
> is `fsr_playbooks`, the connector is `0.5.66`, and live work is tracked on the
> board: https://github.com/users/ftnt-dspille/projects/1 (issues in
> `ftnt-dspille/agentic-assistant-tracker`). Kept for the phase rationale and the
> architecture notes below, which are still accurate.

The connector is the **brain**: agent loop, MCP tool registry, platform calls, session storage, HITL pause/resume. The widget is a thin chat front-end. The wire shape between us is owned by [`FSR_PLAYBOOK_BUILDER_CONNECTOR_CONTRACT.md`](/home/user/WebstormProjects/fsr_all_widgets/fortisoar-widget-harness/FSR_PLAYBOOK_BUILDER_CONNECTOR_CONTRACT.md) v2.0.0.

Companion: `fortisoar-widget-harness/FSR_PLAYBOOK_BUILDER_WIDGET_PLAN.md`.

> **Architecture review (2026-05-30).** A full multi-agent review of the agentic IR&R system lives
> in the canonical repo: `fsr-playbook-framework/docs/AGENTIC_IR_ARCHITECTURE_REVIEW.md` (durable
> assessment) and `fsr-playbook-framework/docs/plans/AGENT_HARDENING_PLAN.md` (ordered backlog: argument
> validation, structural mutating-op gate, `is_error` tool feedback, investigation-quality evals).
> **Reminder:** `fsr_core` is **vendored from `fsr-playbook-framework/fsr_core` via `scripts/build.sh`** --
> edit there, not the copy under `connector-fsr-agentic-assistant/fsr_core` (the build `rm -rf`s it).
>
> **Landed 2026-05-30:** op-name existence validation (`_validate_op_exists` offline +
> `_validate_op_live` live fallback + `emit_action_card` guard + triage prompt "never guess an op
> name"); a hallucinated op now returns `unknown_operation` (with near-matches) **before** the
> approval prompt, and the agent self-corrects within the turn. Tests:
> `python/tests/test_op_existence.py`. Next: validate op *arguments*, not just the name (plan §1.1).

---

## Persona spine -- the one-spine model (2026-07-16)

The connector used to run a *privileged hardcoded `triage`/`build` intent system*
next to a *subordinate Key Store persona system*. These are now collapsed into
**one resolution spine**; `triage` and `build` are just built-in personas, and
every power triage once had (grounding, preflight, CaseState continuation) is a
**capability a persona opts into**, not a privilege welded to the string
`"triage"`. Full phased plan (PL→P6) lives (local-only) in
`fsr-playbook-framework/docs/plans/PERSONA_SPINE_UNIFICATION_PLAN.md`.

- **One resolution point.** `ResolvedPersona(intent, profile)` +
  `_resolve_turn_persona(intent_hint, module, session)` resolve a turn once
  (built-in base intent + the module's Key Store persona, fail-open). Both the
  prompt seam and the tool-slice seam read from it -- no scattered
  `!= "triage"` catch-all (deleted in P1; `_tools_for_intent`/`_tools_for_turn`
  remain as thin back-compat shims over `_tools_for_persona`).
- **Grounding is a capability, not an intent (P3).**
  `ResolvedPersona.capabilities.grounding` gates the preflight → CaseState →
  continuation spine. True for built-in triage and for any persona that declares
  `jSONValue.capabilities.grounding: true`; a persona that declares nothing
  stays prompt-only (grounding is never silently inherited). A grounding persona
  grounds on its OWN prompt -- the triage scenario prompt never overrides it.
- **Tool-output envelope contract (P4, framework).**
  `fsr_playbooks/llm/tool_result.py` -- every dispatched tool's output is a dict
  or a list-of-dicts; `dispatch` validates (fail-open, strict under
  `FSRPB_STRICT_TOOL_OUTPUT`). All 56 registered tools conform.
- **Authoring (P5).** Personas are authored as one Key Store record, no
  connector code. Schema + capability flags + lint:
  [`connector-fsr-agentic-assistant/docs/PERSONA_AUTHORING.md`](connector-fsr-agentic-assistant/docs/PERSONA_AUTHORING.md);
  `profiles.validate_persona_record()` lints a record before save.

This supersedes the `triage`/`build` divergence findings in
`TRIAGE_BUILD_AUDIT_PLAN.md`.

---

## Outstanding TODOs (resume here after /clear) -- updated 2026-05-29

Phase status: P0 provider ✅ · P1 chat ops ✅ · P2 HITL ✅ · P3 mock parity ✅ ·
P4 triage intent ✅ · **P5 entity context ◑** (grounding done; fetch-path +
persist open) · P6 contract version ✅ (`CONTRACT_VERSION = "2.1.0"`) ·
P7 demo recipes ☐.

Working tree: `info.json` at **0.3.24**. This session's entity-grounding work is
**uncommitted + undeployed**. Ordered by value:

1. **Deploy entity grounding** (user picked as next). Bump `info.json`
   0.3.24 → **0.3.25** (sed all version fields), run `scripts/build.sh`
   (re-vendors fsr_core incl. updated `system_prompt_triage.md`), then
   `scripts/install_to_fsr.py`. `$replace` install = no service restart. Re-run
   the live action-card loop to confirm the widget sees the RECORD CONTEXT block.
2. **Timeline fetch-path gap** (task item #3, NOT closed). The triage prompt now
   tells the model to fetch correlated alerts / indicators / raw event rows via
   `entity.iri`, but there is **no first-class `get_record`/`get_related` tool** --
   only `run_op` against a configured connector that can GET records. Decide:
   (a) add `get_record(iri | module+uuid, relationships=True)` wrapping crudhub
   GET (`/api/3/<module>/<uuid>?$relationships=true`) into `fsr_core`
   `SAFE_TOOLS` (tier 1, read-only) -- cleanest; or (b) rely on `run_op` and
   document the op. Without it, "attack timeline" grounds on the base record but
   can't pull per-event netflow rows.
3. **Persist `entity` across resume turns** (P5.1). `chat_resume` carries no
   `entity`; today only the first-turn user message is grounded. Persist on first
   `chat_turn` + re-inject on resume. Helpers `_entity_context_block` /
   `_inject_entity_context` already exist in `operations.py`. **Prerequisite for
   item 6** (the re-triage regression fix reuses the persisted grounding).
4. **Optional server-side re-fetch** (P5.3) -- GET the record via crudhub when the
   widget didn't carry a field. Low priority now `fields` are guaranteed clean.
5. **Phase 7 -- live demo recipes** (`tests/recipes/*.json`): `block_ip_fortigate`,
   `quarantine_host_fortiedr`, `hunt_ioc_playbook` vs dev SOAR (192.0.2.205).
   Release-tag gate.
6. **✅ BUILT (2026-07-02, case-state spine P1+P2, offline-green, needs re-vendor+bump to ship) -- Re-triage regression / state continuity** -- live-observed
   on 8.0.0 (session `sess-cfm5107y…`): clicking a `capability_gap` card action
   re-ran the whole triage, and a follow-up "block ip 1.1.1.1" re-triaged instead
   of staging containment. Root cause is in the vendored framework, tracked as
   **`fsr-playbook-framework/docs/plans/AGENT_HARDENING_PLAN.md` §4.6 + §4.7**:
   resume runs the static `_load_prompt` instead of the grounded
   `_resolve_system_prompt` (§4.6), and every turn re-preflights a fresh "triage
   this alert" frame with no "act/contain" path (§4.7). **Depends on item 3**
   (persist first-turn grounding) -- that's the prerequisite both fixes reuse.
   Land in `fsr-playbook-framework/fsr_playbooks` + `system_prompt_triage.md`, then
   re-vendor via `scripts/build.sh`, bump `info.json`, `scripts/install_to_fsr.py`.
   NOTE: the widget pins `uiIntent='triage'` on record-mounted drawers, so the fix
   MUST live in the framework -- it can't assume the widget disambiguates.
7. **✅ BUILT (2026-07-02, offline-green, needs re-vendor+bump to ship) -- No Deploy button after a successful build** -- same live session
   (`sess-cfm5107y…`, v0.4.18): the build turn produced valid YAML + passed
   `verify_playbook`, then **narrated** "call `push_playbook`" instead of emitting
   a `playbook_offer` card. Zero offers emitted, `currentYaml` empty
   (`finalYaml:false`), so the widget rendered no Deploy affordance on the alert
   page. Fix = build flow must emit `playbook_offer` (carrying the final YAML) as
   the terminal action when verify passes. Tracked as **AGENT_HARDENING_PLAN.md
   Live-session §A**; companion widget backstop = surface Deploy when a compiled
   YAML exists.
8. **Tool-output bloat** -- same session: `verify_playbook` = **46,913 chars** but
   only 4 warnings (1 unique) + `ready_to_push:true`; the bulk is a 19KB `evidence`
   field + pretty-printing. Drop `evidence` from the model result, dedupe warnings,
   don't pretty-print. `find_enrichment_actions`/`run_op` enrichment re-fetched
   near-identically 3×/2× (~40KB dup). Tracked as **AGENT_HARDENING_PLAN.md
   Live-session §B/§C** (§C ties to item 6).
9. **Error-triage from the same session (root causes)** -- 8/45 calls "errored" but
   only 2 are real: (a) **4 are guardrails** (`call_once_guard` + `hunt_floor_guard`)
   surfaced as `status=error` -- give guard redirects a non-error status so the widget
   + evals don't treat steering as failure (**§D**); (b) the `hunt_floor_guard` counter
   is **turn-scoped**, forcing a re-hunt on the "block ip" turn -- make it session-scoped
   (**§4.7-#3**, the load-bearing re-triage fix); (c) agent **guessed `whois-rdap`**
   (unconfigured) twice -- gate `run_op` behind `list_configured_connectors` + carry an
   "unavailable" note (**§E**); (d) build authoring gaps -- no schema for playbook-level
   `parameters`, and the validator auto-rewrites the set_variable-namespace gotcha but
   still hard-fails (**§F**).
10. **✅ BUILT (2026-07-02, offline-green, needs re-vendor+bump to ship) -- FALSE-POSITIVE `unknown_jinja_filter`, filter catalog incomplete**
    -- CRITICAL. `json_query` + `ternary` are **valid** FSR filters (FSR runs Ansible's
    Jinja, so all Ansible filters are supported), but `jinja_filters.json` (81 entries)
    is generated only from the widget's `jinjaFilters.constants.js` and omits most of
    the Ansible namespace (`json_query`/jmespath, `ternary`, `to_json`, `combine`, `zip`
    …). So `validate_yaml`/`verify_playbook` warn on CORRECT playbooks and
    `find_jinja_filter` returns `[]` for real filters -- the agent gets punished for a
    right answer and could rewrite good YAML to satisfy a phantom warning. Fix: rebuild
    the catalog from the Ansible filter registry ∪ widget list ∪ jinja2 builtins; until
    then make `unknown_jinja_filter` advisory-only. Tracked as **AGENT_HARDENING_PLAN.md
    Live-session §G**.
> **Workflow coherence review (2026-07-02):** items 6-11 below are symptoms of one root -- no
> persistent per-session case state. Fresh-eyes design review:
> `fsr-playbook-framework/docs/plans/AGENTIC_WORKFLOW_COHERENCE_REVIEW.md`. Read before the state work.

11. **Silent dead-ends + recommendation quality** -- `search_module_records q="a OR b OR c"`
    silently returned `total:0` (`$search` is literal, not boolean) so the agent read a
    broken query as "no related activity"; `find_jinja_filter` returned `[]` with no
    near-match. **Principle:** every `find_*`/`get_*`/`*_search`/`validate_yaml` must
    return a near-match/`suggestion`/`query_syntax` on a miss (like `find_operation`
    already does), never a bare empty/zero -- plus an eval flagging silent-zero-on-
    malformed-query + empty-discovery. Tracked as **§H**.

Cache note: entity grounding is injected into the **first user message**, NOT the
system prompt, to keep the cached (tools + system) prefix
(`anthropic_provider.py:182`) stable across records. Don't move it back.

---

## Status (2026-05-28)

Version `0.2.0`. All 12 operations declared in `info.json` implemented end-to-end:

- **Pure-compute**: `health_check`, `list_models`, `warmup`, `compile_yaml`, `validate_yaml`, `resolve_yaml`
- **Agent loop**: `chat_turn`, `chat_resume`, `chat_history` (sqlite history, `PersistedApprovalGateway` survives worker restart)
- **Platform-side via `integrations.crudhub`**: `push_playbook`, `dry_run_playbook`, `render_jinja`
- **Wire mapping**: `_event_to_wire` + `_wire_transcript` correctly splice `emit_choice_card` / `emit_action_card` / `emit_manual_input` `tool_use`+`tool_result` pairs into single `choice_card` / `action_card` / `manual_input` events with the right `stop_reason`
- **fsr_core vendored** by `scripts/build.sh` into the tarball; reference DB shipped with it

---

## Phased work

### Phase 1 -- Validation (do first, no new features)

**LIVE INTEGRATION HARNESS -- ✅ 9/10 paths green on real FSR (2026-05-29).**
`scripts/fsr_live.py` (driver) + `scripts/live_integration.py` (CLI, golden
capture, self-cleaning) + `tests/test_live_integration.py` (pytest wrapper,
gated by `FSR_LIVE=1`) drive the INSTALLED connector remotely through
`/api/integration/execute/` -- the real on-platform path where `crudhub`
works. Structural + steered-semantic assertions (never exact LLM text).

Status against the dev box (192.0.2.205, connector 0.3.8, config `fsrpb-live`
on haiku): **health, warmup, compile, validate_ok, validate_bad, resolve,
push, dry_run, chat_build all PASS**; the platform-write loop (compile → push →
dry_run→finished) runs end-to-end and self-cleans.

Real bugs the harness surfaced and we FIXED (none were visible to mocks):
- `resolve_yaml` called `Resolver()` without the required `db_path`
- `warmup` 500'd: duplicate connector names (dedupe by name), non-scalar cells
  (coerce), composite-key dupes (`INSERT OR REPLACE`)
- crudhub calls used `json=` but `make_request(url, method, body=…)` wants
  `body=` → push/dry_run posted empty bodies (null-name 409/violation); also
  fixed `render_jinja`/trigger
- vendored top-level `fsr_core` cached across `$replace` upgrades → stale code;
  `_ensure_fsr_core_path` evicts a sibling-version copy so a version bump needs
  NO service restart (validated live)
- install/runtime prereqs: `anthropic` SDK pip-installed into the integrations
  venv from public PyPI (private index lacks it); `mcp` SDK absent on py3.9 →
  `_shared.py` FastMCP no-op shim

KNOWN / open:
- `render_jinja` -- `"Can't compile non template nodes"` via the `localhost`
  loopback (same body works with a user Bearer token on the public URL);
  service-token jinja-editor behaves differently. Defer + investigate.
- `chat_action_card` / `chat_resume` -- SKIP until the Phase-4 triage prompt +
  a configured remediation connector steer a containment intent to an
  action_card.
- `respond_manual_input` -- SKIP; live paused-run leg covered by
  `scripts/probe_fsr.py` lifecycle (`wfinput_resume`).

Install loop (repeatable): `scripts/install_to_fsr.py` ($replace install +
idempotent `fsrpb-live` config) -- bump `info.json` version each rebuild so the
worker picks up new code without a restart. Root for services: `ssh fsr-root`.

---

Goal: prove every operation works against a live SOAR + Anthropic key. Ship as `scripts/validate_connector.py` runnable from CI and the demo-prep checklist. Mirror the 12 self-tests in §9 of the contract:

| Test | Op | Asserts |
|---|---|---|
| T1 | `health_check` | `ok=true`, all sub-flags true |
| T2 | `list_models` | non-empty, configured model included |
| T3 | `chat_turn` (triage, no tool use) | `stop_reason=end_turn` |
| T4 | `chat_turn` (forces `run_op` intel) | paired `tool_use`+`tool_result` in transcript |
| T5 | `chat_turn` (forces action_card) | `stop_reason=awaiting_action_card`, last event is `action_card` |
| T6 | `chat_resume(approve)` | continues, executes, follow-up `tool_result` with real exec result |
| T7 | `chat_resume(reject)` | re-enters loop, rejection acknowledged |
| T8 | `chat_history(session_id)` | turns returned in order |
| T9 | `mode=mock, mock_scenario=incident_smtp_intrusion` | full happy path, no Anthropic call |
| T10 | worker-kill between T5 and T6 | T6 still succeeds (PersistedApprovalGateway durability) |
| T11 | concurrent two sessions paused at action_card, approve reverse order | both resolve, no cross-talk |
| T12 | Anthropic 5xx mid-turn | `stop_reason=error` with structured `error` event, no crash |

`scripts/validate_connector.py` exits non-zero on any failure. Run before every release tag.

### Phase 2 -- HITL foundation hardening

**DONE so far (2026-05-29):** `respond_manual_input` op now exists + is registered
(operations.py + info.json), forwards to the paused workflow run best-effort
(`_forward_manual_input`, degrades to conversational-only off-SOAR) and resumes
the conversation Model-A style. Unit tests in `tests/test_chat_operations.py`
(approve continues, reject skips forward, bad decision, registration). Also: live
FSR contract harness landed -- `scripts/probe_fsr.py` captures golden responses for
every FSR-touching endpoint, `tests/fsr_contract.py` declares the key-paths the
connector reads, `tests/test_fsr_contract.py` asserts them (skips until goldens
captured). Fixed a real bug surfaced by this work: `compile_yaml` read
`result.workflow_json` (→ AttributeError); attribute is `result.fsr_json`.
**Still TODO below** + reconcile the guessed manual_input resume endpoint against
`tests/golden/manual_input_resume.json` once the probe runs against live FSR.

**DONE (2026-05-29):** `tests/test_hitl_durability.py` (7 cases, all green) covers
every failure mode below:

- T10 -- `SuspendedSession` survives a worker kill: stash → drop the `default_storage`
  singleton (simulates restart; sqlite file is the only durable state) → `chat_resume`
  by `approval_id` still resumes.
- T11 -- two sessions paused at an `action_card`, resumed in REVERSE order; asserts each
  resume re-enters its own conversation (session-specific IP) with no cross-talk.
- T12 -- provider raises mid-stream → `ok=False`, `stop_reason=error`, trailing `error`
  event, no raise out of `chat_turn`.
- `max_turns` ceiling → envelope preserves `stop_reason=max_turns` (hand-rolled provider,
  since `FakeProvider` always closes with `DoneEvent(end_turn)`).
- unknown `approval_id` (no card/choice fallback) → structured `approval_not_found`.
- double `chat_resume` for the same `approval_id` (double-click) → first resumes, second
  returns `approval_not_found` (gateway already popped it) -- no duplicate exec.
- `respond_manual_input` when the run is unreachable (no matching pending input, e.g. the
  run already completed) → degrades cleanly: `ok=True`, chat continues,
  `manual_input_forward.reason="no_matching_pending_input"` so the widget sees the miss.
  (Faithful to the connector's never-dead-end design rather than a hard error envelope.)

Total connector suite: 62 passed, 6 skipped.

Already have `PersistedApprovalGateway` (sqlite).

### Phase 3 -- Mock-mode parity (server-side) -- ✅ DONE (2026-05-29)

The connector replays the SAME fixtures the widget's harness uses, so demo day can fall back to mock without a widget code change:

1. **Vendor the fixtures** ✅ -- `scripts/build.sh` rsyncs the widget's `widgetAssets/fixtures/*.json` into `connector-fsr-agentic-assistant/fixtures/` (override src via `WIDGET_FIXTURES_SRC`; warns, doesn't fail, when absent).
2. **`connector-fsr-agentic-assistant/mock_replay.py`** ✅ -- loads fixture JSON by `mock_scenario` (env `FSR_MOCK_FIXTURES_DIR` override → pkg `fixtures/`), `match`-based picker (subset-match against the resume payload, first unconsumed wins), per-session cursor in the `mock_cursor` sqlite table (added to `storage.py`). Honors per-action `delays` unless `mockSpeed=="fast"`. Stamps `ok`/`session_id`/`contract_version` so the wire shape matches a live turn. No-match → `error.code=="mock_no_match"` (fails loud, never hangs). **Faithful to the REAL fixture schema** (`action`/`match`/`delays`), not the plan's earlier guessed `on`/`delay_ms` keys.
3. **Dispatch-layer branch** ✅ -- `operations._mock_aware` wraps every replayable op in the dispatch table; `params["mode"]=="mock"` routes to `mock_replay.respond(...)` and bypasses `_check_health` (no Anthropic key needed). `health_check`/`list_models`/`warmup` always run live.
4. **Acceptance** ✅ -- self-test **T9 PASSES offline** (`incident_smtp_intrusion`, no Anthropic call, `stop_reason=awaiting_action_card`). 8 unit tests in `tests/test_mock_replay.py` (cursor advance, match discrimination, full happy path, independent session cursors, unknown scenario, real-widget-fixture smoke). Total connector suite: **70 passed, 6 skipped.**

### Phase 4 -- Triage intent

The widget will send `intent: 'triage'` from drawer mounts (per contract §2). Today both intents share the same system prompt. Add:

1. `fsr_core/agent/system_prompt_triage.md` -- biased toward `find_connector` → `find_operation` → `run_op(confirm=true)` for containment. Constrains the model to emit `emit_action_card` for any mutating op rather than silently running it.
2. `_wire_messages(prior, intent)` branches on intent to select the prompt.
3. Optional: a smaller tool slice for triage -- drop `push_playbook` / `dry_run_playbook` / `validate_yaml` / `compile_yaml` since you're not building YAML. Keeps the tool count down and steers the model.
4. **Acceptance**: a triage turn against an alert about `192.168.77.30 → 12.62.213.134 SMTP` produces an `action_card` for `fortigate.add_to_address_group` (or whichever connector is configured) within 2 turns. Hard-code in `tests/test_triage_intent.py`.

### Phase 5 -- Entity context ◑ (grounding ✅ 2026-05-29; fetch-path + persist open)

The widget sends `entity: {iri, module, uuid, id, fields, tags, summary_seed}`
(contract §2; `fields` are pre-cleaned -- no Jinja/plumbing, picklists
flattened, dates ISO).

**Done this session:**
- `operations._entity_context_block(entity)` renders a `RECORD CONTEXT` block
  (lead fields name/severity/status/phase first, then rest, tags, summary_seed,
  and the iri/module/uuid **lookup keys** flagged as the handle for fetching
  child/event rows the `fields` don't carry).
- `operations._inject_entity_context(wire_msgs, entity)` folds it into the
  **first user message** every turn (NOT system -- cache-friendly, see note above).
- Triage prompt (`fsr_core/agent/system_prompt_triage.md`, re-synced to vendored
  copy) gained a "Record context (always provided)" section + tightened
  quick-action wording: answer from `fields` directly, fetch event-level rows
  via lookup keys.
- Tests: 3 new in `tests/test_chat_operations.py` (block render, empty→None,
  injection-into-first-user-message + not-in-system). Suite 80 pass / 6 skip.

**Outstanding** (see Outstanding TODOs §2-4 at top): no first-class
`get_record`/`get_related` tool for the timeline fetch; entity not persisted
across resume turns; optional server-side re-fetch.

### Phase 6 -- Contract version stamping

Every envelope MUST include `contract_version: "2.0.0"` (contract §10). Add to `_envelope_stop_reason` or wherever the response dict is finalized. Bump on any contract change.

### Phase 7 -- Live demo recipes

Pick the connectors confirmed installed on the demo SOAR. For each, write a recipe in `tests/recipes/`:

- `recipes/block_ip_fortigate.json` -- ask: "Block 1.2.3.4 at the edge" → expected: `action_card(fortigate, add_to_address_group)` → approve → success result
- `recipes/quarantine_host_fortiedr.json` -- ask: "Isolate HR-WS-04" → expected: `action_card(fortiedr, isolate_host)`
- `recipes/hunt_ioc_playbook.json` -- ask: "Build a playbook to hunt IOC 1.2.3.4 across all hosts in the last 72h" → expected: compiled YAML in transcript, `push_playbook` succeeds

Each recipe is a `chat_turn` request + the assertion against the transcript shape. Run in CI nightly against the dev SOAR.

---

## Mock-mode design (Phase 3 detail)

```
connector handlers
   │
   ├─ if params.mode == "mock":
   │     return mock_replay.respond(op_name, session_id, payload)
   │
   └─ else: existing live agent loop
```

`mock_replay.respond(op, session_id, payload)`:
1. Load fixture by `params.mock_scenario`.
2. Get session cursor from sqlite (`mock_cursor` table: `(session_id, scenario, consumed_indices[])`).
3. Find first unconsumed response whose `on == op` AND `match` is subset of `payload`.
4. Mark index consumed.
5. Honor `delay_ms` via `asyncio.sleep` (skip if `params.mockSpeed == "fast"`).
6. Return `response` block from fixture verbatim (already in contract shape).

If no response matches, return `stop_reason=error, error.code="mock_no_match"` so the test fails loudly.

---

## Acceptance -- release-ready

Cut a release tag (`v0.3.0` for these additions) when:

1. `scripts/validate_connector.py` passes against live SOAR + live Anthropic (all 12 self-tests).
2. `scripts/validate_connector.py` passes with `mode=mock` (no Anthropic key required) -- T9 alone but exhaustive across all 6 reference fixtures.
3. The 3 live demo recipes (Phase 7) pass in CI against the dev SOAR.
4. Widget at compatible version (≥ 1.0.25) walks all 4 critical paths in `FSR_PLAYBOOK_BUILDER_WIDGET_PLAN.md` switch-mock-to-real section with no console errors.

---

## Reference

- Contract: `/home/user/WebstormProjects/fsr_all_widgets/fortisoar-widget-harness/FSR_PLAYBOOK_BUILDER_CONNECTOR_CONTRACT.md`
- Widget plan: `/home/user/WebstormProjects/fsr_all_widgets/fortisoar-widget-harness/FSR_PLAYBOOK_BUILDER_WIDGET_PLAN.md`
- fsr_core source: `/home/user/PycharmProjects/fsr-playbook-framework`
- MCP tools doc: `fsr-playbook-framework/store/MCP_TOOLS.md`
- Widget repo: `/home/user/WebstormProjects/fsr_all_widgets/fortisoar-widget-harness/widgets-src/fsrAgenticAssistant`
