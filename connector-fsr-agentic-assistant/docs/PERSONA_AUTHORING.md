# Authoring a module-scoped assistant persona

A **persona** lets the agentic assistant carry a per-module system prompt,
tool subset, write scope, and turn-shaping capabilities — so, mounted on a
given module, it helps *author* records/playbooks for that domain (or run a
custom investigation runbook) instead of the built-in SOC-triage/build flow.

> A persona is the coarse "who is this assistant" switch. Its fine-grained
> companion is a **skill** — one learned house rule that corrects *how* the
> assistant uses its tools, without editing the whole prompt. See
> [SKILLS_AUTHORING.md](SKILLS_AUTHORING.md). Rule of thumb: **persona = the
> role; skills = the corrections that role accumulates.**

The whole point is **no connector code change**: the connector ships the full
tool catalog once; you define a persona by creating **one Key Store (`keys`)
record** in the FortiSOAR UI (or via `pyfsr`). The connector reads and parses
that record at turn time (fail-open — a malformed record never breaks a turn,
it just falls back to built-in behavior).

## The Key Store record

One `keys` entry per module:

| field       | value |
|-------------|-------|
| `key`       | `fsr_assistant_profile:<module>` (e.g. `fsr_assistant_profile:ztpf_templates`) |
| `value`     | the persona **system prompt** (plaintext; Key Store values need not be secret) |
| `jSONValue` | the JSON config block below |
| `notes`     | human description |

### `jSONValue` schema

```jsonc
{
  "version": 1,                       // contract version (int; defaults to 1)
  "label": "ZTPF Template Author",    // display label (optional)

  "tools": {                          // tool allowlist — WINS over the intent slice
    "allow": ["get_record", "search_module_records",
              "test_template", "create_record", "update_record"]
  },

  "may_write": ["ztpf_templates"],    // modules this persona may write to
                                      // (empty/absent ⇒ read-only persona)

  "bind_modules": [],                 // extra modules this ONE record serves
                                      // (a persona shared across several modules)

  "capabilities": {                   // opt-in turn-shaping (see below)
    "grounding": true
  },

  "ui": { /* widget framing — greeting, placeholder, quickActions, ... */ }
}
```

Every field is optional except an implicit one: `value` (the prompt) must be
present and non-empty, or the record does not resolve to a persona.

## Capability flags

By default a persona is **prompt-only**: its prompt and tool slice apply, but it
does not run the record preflight or carry case state across turns. Capabilities
are how a persona opts into the dynamic turn-shaping machinery that used to be
welded to the built-in `triage` intent.

| capability  | effect |
|-------------|--------|
| `grounding` | Runs the record **preflight** on turn 1, builds a **CaseState**, and appends a **continuation directive** on later turns so the agent continues the case instead of restarting. The persona grounds on **its own prompt** (the triage scenario prompt never overrides it). Turn `true` on for an investigation/runbook persona; leave off (the default) for an authoring persona. |

A persona that declares **no** `capabilities` block keeps prompt-only behavior —
grounding is never silently inherited from the built-in base intent.

## Tool allowlist

`tools.allow` is the persona's tool set, drawn from the connector's full
catalog. It **wins over** the built-in intent's tool slice (an authoring persona
may legitimately want a tool the triage/build slice would exclude). An
empty/absent allowlist leaves the built-in slice unchanged.

Writes (`create_record` / `update_record`) are additionally gated by
`may_write`: a write to a module not in `may_write` is refused even if the write
tool is in `allow`. Every write also passes the tier-3 approval card — the
persona never bypasses human approval.

## Lint before you save

`fsr_soc_triage.profiles.validate_persona_record(rec, known_tools=...)` returns
a list of actionable findings so you catch mistakes at authoring time (at
runtime they'd be silently dropped). Pass the connector's tool catalog as
`known_tools` to catch typo'd tool names:

```python
from fsr_soc_triage.profiles import validate_persona_record
from fsr_playbooks.llm.tools import anthropic_tools

known = {t["name"] for t in anthropic_tools()}
for issue in validate_persona_record(record, known_tools=known):
    print(f"[{issue.level}] {issue.field}: {issue.message}")
```

- **error** — the record won't produce a usable persona (missing prompt) or a
  tool name isn't in the catalog (the persona would be narrowed to nothing
  useful). Fix before saving.
- **warning** — the record parses, but a field is malformed and will be
  silently ignored/degraded (an unknown capability key, a non-list `may_write`,
  a non-boolean capability value). Probably not what you intended.

An empty list means every field the runtime honors is well-formed.

## Worked example

See `scripts/_upsert_ztpf_persona.py` (a local, box-specific one-shot) for a
complete `ztpf_templates` authoring persona: prompt, a scoped read+author tool
set, `may_write`, and the widget `ui` block. To make it a *grounding* runbook
persona instead, add `"capabilities": {"grounding": true}` to its `jSONValue`.
