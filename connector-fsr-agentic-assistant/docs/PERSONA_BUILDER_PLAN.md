# Persona builder -- let the assistant author a persona for you

Today a persona is authored by hand: one Key Store record (`value` = prompt,
`jSONValue` = tools/may_write/ui) or a row in the `assistant_personas` grid,
checked with `validate_persona_record`. The playbook `build` flow, by contrast,
*interviews* the analyst, drafts the artifact, shows a `playbook_offer` card, and
on **Save** compiles and pushes it. This plan gives personas the same guided
authoring -- the assistant drafts a good, task-relevant persona and offers to
save it -- reusing the offer/approve machinery rather than inventing a new one.

## What "a good persona" means (the quality bar the drafter targets)

The whole value of a persona is **durable knowledge that is expensive to relearn
every turn**. A hand-written gold standard is the ZTPF orchestration persona
(`scripts/_upsert_ztpf_persona.py` / the `ztpf_automation_profiles` fixture): its
prompt encodes the *data model* (which records link to which, which fields a step
reads), the *working procedure* (ground first, follow links, test before write),
and the *scope boundary* (what this assistant does NOT author). A good drafted
persona must carry the same four things, grounded in the real module:

1. **Role + scope** -- who this assistant is on this module, and what it leaves
   to other assistants (so it never wanders).
2. **Data model** -- the module's own fields and its key relations, read from the
   live schema and a few sample records, not guessed. This is the expensive part.
3. **Procedure** -- how to work the module (which tool to reach for first, when to
   test/validate before a write, how to report).
4. **Config** -- a scoped `tools.allow` (read tools + the authoring tools the task
   needs), `may_write` only for the modules it should write, `capabilities.grounding`
   on for a runbook persona / off for an authoring persona, a `ui` framing block,
   and optionally a stronger `models` entry when the task is harder than triage.

## Architecture -- mirror the playbook `build` flow, reuse everything

| playbook build | persona builder | reuse |
|---|---|---|
| `build` intent prompt | an **author** mode prompt that drafts the persona | `intents.py` loader |
| `emit_card(card_type='playbook_offer')` | `emit_card(card_type='persona_offer')` | `OFFER_CARD_TYPES` (framework) |
| accept -> compile + push playbook | accept -> `validate_persona_record` + write Key Store record | existing approval-gated `create_record` |
| compiler validates the YAML | `validate_persona_record(known_tools=...)` validates the draft | already shipped |

The artifact is *smaller* than a playbook (one record, no workflow graph), so the
builder is a smaller feature than the playbook flow -- the hard part is drafting
*quality*, not plumbing.

### The one safety rule

A persona grants the assistant tools, write scope, and playbook-run authority, so
drafting one is privilege-shaped. **Save MUST pass the normal tier-3 approval card**
-- a human confirms the `tools.allow` and `may_write` before the record lands,
exactly like a playbook save. The drafter never writes the Key Store itself; it
only proposes. The offer card surfaces the allowlist and write scope in plain
language so the approver sees what they are granting.

## Phases

- **P1 (this change): the drafting engine + CLI.** A self-contained, model-driven
  drafter that gathers the module's real knowledge (schema + sample records) and
  produces a validated persona for a stated task, plus a CLI to draft -> review ->
  save (approval-gated). This is the reusable core P2 calls, shippable on its own.
  - `fsr_soc_triage/persona_author.py`: `gather_module_knowledge(client, module)`,
    `draft_persona(provider, module, goal, knowledge)` -> `(record, lint)`.
  - `scripts/author_persona.py`: `--module --goal [--box | prompts] [--save]`.
  - Tests: fake-provider draft parses + validates; lint catches a typo'd tool;
    knowledge-gathering shapes the prompt; a bad draft is refused at save.
- **P2: the conversational offer flow.** Register `persona_offer` in the framework
  `OFFER_CARD_TYPES`; add the author-mode prompt and the accept handler
  (`_resume_persona_offer_accept`) that validates and writes. The assistant
  interviews the analyst and emits the offer in-chat.
- **P3: the widget card.** Render `persona_offer` (prompt preview, allowlist, write
  scope, Save/Edit) in `agenticAssistant`, mirroring the `playbook_offer` card.
- **P4: quality eval.** A known-answer set (module + goal -> expected tool scope /
  may_write / scope-boundary clauses) graded like the other agentic evals, so a
  prompt change can't silently regress persona quality.

## Open questions for the owner

- Native module vs Key Store as the save target. P1 writes the Key Store (works on
  every box, dev-push included). The `assistant_personas` grid is nicer to edit but
  needs the solution pack's module definitions; offer both once that module is
  standard (ties into Track PZ's "make one substrate primary").
- Should the drafter be allowed to pick a bigger `models` entry on its own, or only
  suggest it for the approver to confirm? (Leaning: suggest, don't auto-set.)
