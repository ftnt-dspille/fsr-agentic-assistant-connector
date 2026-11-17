# Authoring skills -- learned house rules

A **skill** is one granular, editable rule that shapes *how* the assistant uses
its generic tools for a task on a module -- without a connector code change. It
is the fine-grained companion to a [persona](PERSONA_AUTHORING.md): the persona
says *who the assistant is* on a module; a skill is one *correction* to how that
role behaves.

    "When listing playbooks, do not offer any whose name contains 'deprecated'."
    "'run the pending steps' means run the playbook 'Create Run Group and Execute Steps'."
    "Sort run groups by createDate descending; sort steps by stepNumber ascending."

Every one of those started as an analyst *fixing the same thing every session*.
Storing them as records means the tools stay generic (the same
`list_module_playbooks` serves every deployment), the rule is editable in a grid
with RBAC + history for free, and **the agent can capture one for itself**.

## Why a skill and not a tool change / a persona edit

- **Not a tool change** -- baking "hide deprecated-named playbooks" into
  `list_module_playbooks` would hardcode one site's preference into a generic
  primitive and require a connector release to change. A skill is data.
- **Not (usually) a persona edit** -- a persona is the coarse role; you don't
  rewrite the whole system prompt to fix one recurring detail. Skills let you
  accumulate many small corrections independently, each toggleable.

Reach for a skill when the persona is right but the assistant keeps getting one
*detail* wrong that a human has to correct. If the assistant is doing the wrong
*job* entirely (wrong tools, wrong write scope), that's a persona, not a skill.

## The `assistant_skills` module

Skills live in a custom module named `assistant_skills` -- one record per rule.
The connector reads it by name (`/api/3/assistant_skills`); the field **API
keys must match exactly** (FortiSOAR camel-cases custom fields, so the stored
name is `scopeModule`, not `scope_module`):

| field API key | type | meaning |
|---|---|---|
| `trigger` | Text | short human name for the rule ("list pending steps") |
| `instruction` | **Long Text** | the rule text injected into the system prompt |
| `scopeModule` | Text | module the rule applies to (e.g. `ztpf_devices`); **blank = global** |
| `enabled` | Checkbox | a disabled rule is kept for history but not applied |
| `priority` | Integer | higher sorts earlier in the injected block |
| `source` | Text | `analyst` \| `agent` -- provenance |

> **`instruction` must be a Long Text (textarea) field, not single-line.** A
> single-line text field is a `varchar(255)` column and the longer rules
> overflow it with a `value too long for type character varying(255)` error.
> `scripts/setup_assistant_skills.py` creates it correctly and **self-heals** an
> already-created short column (widens it via `set_field_type` + publish).

## Read path (how a skill reaches the model)

`fsr_soc_triage/skills.py`:

- `load_skills(client, module)` scans the module, keeps the **enabled** records
  whose `scopeModule` equals the mounted module **or is blank** (global), and
  sorts highest `priority` first.
- `render_skills_block(...)` renders them as a `## Learned house rules` prompt
  section, framed as **overrides** ("they OVERRIDE your defaults -- follow them
  exactly").
- `operations._resolve_profile → _apply_skills` appends that block to the
  resolved persona's prompt **once**, so every downstream path (ungrounded,
  grounded, resume) inherits it.

Two consequences worth knowing:

1. **A skill only fires when a persona resolves for its module.**
   `_apply_skills` runs after a persona is found; a skill scoped to a module
   with no persona is inert until that persona exists. Create the persona first.
2. **The profile cache is process-life.** A newly added or edited skill needs a
   worker recycle to take effect (same property personas have). A connector
   publish/release recycles workers.

Everything is **fail-open**: no `assistant_skills` module (404), a transient
read error, or a garbled record all yield "no skills this turn" -- the persona
behaves normally. A malformed record is dropped, never fatal.

## Writing a skill

A skill is an ordinary record, so there are three ways in -- no new tool:

- **The setup script** (bulk seed) -- `scripts/setup_assistant_skills.py` creates
  the module, upserts a persona, and seeds a table of skills idempotently
  (matched on `scopeModule` + `trigger`). This is the reproducible path.
- **The FortiSOAR grid** (a human) -- add/edit a record in the `assistant_skills`
  module like any other.
- **The agent itself** -- the assistant captures a skill through the **same
  persona-gated, tier-3 approval-carded** `create_record` / `update_record` it
  uses for any write (module = `assistant_skills`). For that to work the
  module's persona must list `assistant_skills` in `may_write` and include the
  write tools in `tools.allow`. So a recurring correction can *capture itself*
  for next session, behind one approval card.

### `pyfsr` example (a single skill, idempotent)

```python
from pyfsr.config import EnvConfig

client = EnvConfig.from_env().client()
skill, created = client.records("assistant_skills").get_or_create(
    {
        "scopeModule": "ztpf_devices",
        "trigger": "hide deprecated playbooks",
        "instruction": 'When listing playbooks, do not offer any whose name contains "deprecated".',
        "enabled": True,
        "priority": 50,
        "source": "analyst",
    },
    key=["scopeModule", "trigger"],   # natural key: don't duplicate the same rule
    raw=True,
)
print("created" if created else "already existed", skill.get("uuid"))
```

## Worked example

`scripts/setup_assistant_skills.py` is the end-to-end reference: it creates the
`assistant_skills` module with the exact field API keys above, upserts the
`ztpf_automation_actions` authoring persona, grants skill-capture on the ztpf
personas (adds `assistant_skills` to `may_write` and the write tools to
`tools.allow`), and seeds a table of real, FIXME-derived skills. It is
idempotent and box-agnostic -- point it at a box by sourcing that box's env
first, and run `--dry-run` to see the plan without writing.
