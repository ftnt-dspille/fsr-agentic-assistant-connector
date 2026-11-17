# connector-fsr-agentic-assistant

FortiSOAR connector that exposes the fsr-playbook-framework compiler + agent
loop as platform operations. Backs the chat widget that authors
playbooks via LLM-driven conversation.

## Operations

All declared operations are implemented end-to-end. (The shipped `version`
lives in `connector-fsr-agentic-assistant/info.json`; see `release_notes.md` for the
changelog.)

- **Pure-compute** (no platform): `health_check`, `compile_yaml`,
  `validate_yaml`, `resolve_yaml`, `list_models`.
- **Agent loop / chat**: `chat_turn`, `chat_poll`, `chat_resume`,
  `chat_history`, `respond_manual_input`, `list_sessions`, `rate_session`
  (analyst thumbs up/down on a session), `resolve_persona` (the persona
  UI-framing for a module -- a widget fallback when its in-browser Key Store read
  is 403'd) -- sqlite
  history + `PersistedApprovalGateway` so paused HITL turns survive worker
  restart.
- **Platform-side** (require `integrations.crudhub`): `push_playbook`,
  `update_playbook` (write-back to an existing playbook on its `@id`),
  `decompile_playbook` (platform playbook → YAML), `dry_run_playbook` (push +
  trigger + poll + optional hard-purge), `render_jinja` (proxies
  `/api/wf/api/jinja-editor/`), `warmup` (repopulate the reference catalog --
  connectors, ops, modules, picklists -- from this instance), `query_store`
  (read the bundled reference DB).
- **MCP / diagnostics**: `list_mcp_tools`, `call_mcp_tool`, `list_agent_tools`
  (the LLM-facing tool slice per intent/persona), `list_mcp_servers` (enumerate
  materialized **external** MCP servers + tools, annotated with each module's
  default surface), `probe_native_mcp` (health-check the on-box **native**-MCP
  gateway with this config's creds -- auth mode + tools + errors),
  `get_session_trace`, `echo_param_types`.

Off-platform, the platform-side ops return a `crudhub_unavailable`
envelope so the contract still type-checks during development.

### Two independent tool surfaces

A connector-owned tool can be reachable on **two separate surfaces** -- an audit
for "is this tool still used?" must check **both**, or it will wrongly flag a
live tool as dead:

1. **Agent loop (LLM tool-calling)** -- the library `REGISTRY`. The record
   reads and hunt tools (`get_record`, `search_module_records`, `siem_search`,
   `faz_search`, `fmg_device`, `edr_host`) are the framework's own
   (`fsr_playbooks.mcp_server.tools_triage` / `tools_noc` / `tools_edr`, names
   in `fsr_playbooks.llm.hunt_tools`), and so are the record writes
   (`create_record` / `update_record`, `fsr_playbooks.mcp_server.tools_records`)
   and the playbook runs (`run_playbook` / `resume_playbook` /
   `list_module_playbooks`, `fsr_playbooks.mcp_server.tools_playbook_run`).
   `fsr_soc_triage/registry.py` adds the persona-gated ztpf tool, registers the
   persona as the policy for the writes (`may_write`) and the runs (run gate,
   `run_playbook.auto`, the `unlisted` flag), and wires the connector's context
   into the hunt tools (which connector backs each family, its own
   name for the build trace).
2. **Direct invocation** (`call_mcp_tool` op, playbooks, action panel) -- the
   registry built by `operations.py::_get_mcp_registry()`, which enumerates
   **every** `@mcp.tool()`-decorated function via `mcp._tool_manager._tools`
   plus a `dir()` scan of the read-only tool modules, and is listed by
   `list_mcp_tools`. A tool can be absent from surface (1) but exposed via (2)
   purely from its `@mcp.tool()` decorator (e.g. `list_tags`,
   `verification_status`, `test_find_record`).

The retired per-axis hunt helpers (`siem_search_ip`, `fmg_get_device_status`,
...) stay callable on surface (2): playbooks built from traces call them through
`call_mcp_tool`. Grep the sibling `fsr-playbook-framework` before deleting any
`fsr_soc_triage` symbol that looks unreferenced in this repo.

## Personas & Skills -- shaping the assistant without a connector release

The connector ships **one** generic tool catalog and **one** built-in
SOC-triage/build flow. Two data layers let you retune the assistant per module
-- *what it is* and *how it works* -- as **records you own**, with **no connector
code change and no release**. Both are read at turn time and **fail-open**: a
missing or malformed record never breaks a turn, it just falls back to built-in
behavior.

| Layer | Stored as | Answers | Who edits it |
|---|---|---|---|
| **Persona** | one Key Store (`keys`) record, `fsr_assistant_profile:<module>` | *Who am I on this module?* -- system prompt, tool subset, write scope, turn-shaping (grounding), widget framing | a human (UI or `pyfsr`) |
| **Skill** | one record in the custom `assistant_skills` module | *One granular house rule* -- a correction to how the generic tools are used for a task | a human **and the agent itself** |

### How they relate

Skills are **not** linked to a persona by any reference -- they meet on the
**module name**. A persona is keyed `fsr_assistant_profile:<module>`; a skill
carries `scopeModule = <module>` (or blank = global). At turn time
`_resolve_profile(module)` resolves the persona, then `_apply_skills` appends
every skill whose `scopeModule` matches (plus globals), highest `priority`
first, framed as **overrides** the model must honor.

> **A skill only fires when a persona resolves for its module.** `_apply_skills`
> runs only after a persona is found, so a skill scoped to a module with no
> persona is inert until that persona exists. Seed the persona first (or use a
> global skill on a module the built-in flow already personifies).

### When would you want each?

- **Reach for a *persona*** when the assistant should do a *different job* on a
  module than the built-in SOC triage/build flow: author records for a domain
  (templates, automation actions), run a bespoke investigation runbook, or
  expose a different tool subset / write scope. It is the coarse "what is this
  assistant" switch. Absence of the right persona is what lets the wrong tools
  leak in (e.g. a "Build a playbook" offer on an authoring page).
- **Reach for a *skill*** when the persona is right but the assistant keeps
  getting one *detail* wrong that an analyst has to correct every time -- a site
  convention ("don't offer playbooks named 'deprecated'"), a term-of-art
  mapping ("'pending steps' means the ungrouped ones"), a field-name or sort
  rule. A skill is a single line of learned behavior; you grow a library of
  them without ever touching a tool. The agent can even capture one for itself
  (via the persona-gated, approval-carded `create_record` on `assistant_skills`).

Rule of thumb: **persona = the role; skills = the corrections that role
accumulates.** A new persona is a deliberate design act; a new skill is cheap
and reversible (toggle `enabled`).

Authoring detail: [docs/PERSONA_AUTHORING.md](connector-fsr-agentic-assistant/docs/PERSONA_AUTHORING.md)
and [docs/SKILLS_AUTHORING.md](connector-fsr-agentic-assistant/docs/SKILLS_AUTHORING.md).
To stand the whole thing up on a box (module + a persona + seed skills) in one
idempotent, `pyfsr`-typed pass, see `scripts/setup_assistant_skills.py`.

## Repository layout

```
connector-fsr-agentic-assistant/                  <-- this repo
├── connector-fsr-agentic-assistant/              <-- the connector package (per FSR convention)
│   ├── connector.py                   <-- Connector subclass + execute / check_health
│   ├── operations.py                  <-- operation handlers (the op dispatch surface)
│   ├── storage.py                     <-- sqlite chat-session history + approval state
│   ├── pydantic_models.py             <-- typed op params / envelope models
│   ├── mock_replay.py                 <-- recorded-response replay for hermetic tests
│   ├── fsr_soc_triage/                <-- connector-owned triage/authoring tools
│   │   ├── registry.py                <-- persona-gated tools + framework policy/context hooks
│   │   ├── tools_records.py           <-- persona write policy (the writes are the framework's)
│   │   ├── tools_playbook.py          <-- persona run policy (the runs are the framework's)
│   │   ├── tools_ztpf.py / tools_agent.py  <-- @mcp.tool() defs (record/hunt/run tools: framework)
│   │   ├── _common.py                 <-- shared low-level helpers (live_request, …)
│   │   ├── _live_crudhub.py / _live_mcp.py   <-- live transport bridges
│   │   ├── profiles.py                <-- per-module persona (Key Store) resolution
│   │   ├── skills.py                  <-- learned house rules (assistant_skills module) → prompt
│   │   └── triage_*.py                <-- normalize / preflight / scenarios / prompt
│   ├── docs/PERSONA_AUTHORING.md      <-- authoring a per-module persona
│   ├── docs/SKILLS_AUTHORING.md       <-- authoring / seeding skills (house rules)
│   ├── info.json                      <-- declared operations + config fields
│   ├── requirements.txt               <-- the fsr-playbooks== pin = version source of truth
│   └── release_notes.md
├── scripts/                           <-- build/deploy + eval harnesses (see Makefile);
│   │                                      gitignored `_*.py` are one-off probes
├── tests/                             <-- pytest, stubs `connectors.core.connector`
├── Makefile                           <-- ship / bump-framework / eval targets
├── pyproject.toml                     <-- dev/test only; build is via RDK
└── README.md
```

## Consumer of `fsr_playbooks`

`fsr_playbooks` is **no longer vendored as source**. It is a normal pip
dependency, pinned in `connector-fsr-agentic-assistant/requirements.txt`
(`fsr-playbooks==<version>`), and its reference catalog ships inside the wheel
(`fsr_playbooks/_data/`) -- there is no separate `store/` vendor step.
`scripts/build.sh` materializes the pinned wheel into the bundle so the box can
install air-gapped, and self-tests the import to catch producer/consumer skew.

Because the pin is published to PyPI, **ordering matters**: the release must be
on the index before the connector ships, or FortiSOAR's install-time
`pip install -r requirements.txt` fails and every op dies on the version guard.
Use `make deploy FW=next` -- never the steps by hand.

For local dev, an editable install also works:

```bash
pip install -e ../../fsr-playbook-framework
pip install -r connector-fsr-agentic-assistant/requirements.txt
```

## Build / install on a FortiSOAR instance

Prefer the unified `scripts/deploy.sh` (bump → vendor → build → install →
warmup → rollout gate) for a live push. To build a tarball by hand:

```bash
# Build the connector tarball. The reference DB rides inside the fsr_playbooks
# wheel; `warmup` fills the FortiSOAR-derivable tables (connectors, ops,
# op_safety, modules' fields) from the target instance on install.
FSR_SRC=../../fsr-playbook-framework ./scripts/build.sh
# → dist/connector-fsr-agentic-assistant-<version>.tgz
```

The tarball bundles the pinned `fsr_playbooks` wheel (catalog included). The FortiSOAR
installer `pip install -r`'s the `requirements.txt` at connector-install
time -- no manual vendoring of `anthropic` / `httpx` needed. Import via the
Connector Store UI (or CLI). See
[CONNECTOR_BUILDING_GUIDE.md](../../Miscellaneous/fortisoar/fsr_connector_toolkit/tests/CONNECTOR_BUILDING_GUIDE.md)
for the platform-side packaging contract.

## Configuration

The single required field is `llm_provider`; it reveals the credential/model
inputs for the chosen backend. The rest are optional and enable the native-MCP
and demo surfaces. All secrets are `password` fields, encrypted at rest and
decrypted into `config` on call.

| Field | Type | Required | Notes |
|---|---|---|---|
| `llm_provider` | select | ✓ | `anthropic` or `openai` -- which LLM backend drives the agent. Picks the sub-fields below. |
| ↳ `anthropic_api_key` / `model` / `anthropic_base_url` | password / text / text | when `anthropic` | Claude API key, model (default `claude-sonnet-4-5-20250929`), optional base URL. |
| ↳ `openai_api_key` / `openai_model` / `openai_base_url` | password / text / text | when `openai` | Key + model for any OpenAI Chat-Completions-compatible endpoint (incl. local gateways). |
| `simulation_mode` | checkbox | | Serve the live FortiSOAR tools (`run_op`, connector healthchecks, …) from built-in simulated data instead of the platform -- for demos / offline. |
| `mcp_allowlist` | textarea | | JSON mapping a FortiSOAR 8.0 native-MCP server (`soc`/`modules`/`playbooks`/`utility`, or `connector:<name>`) to `{"tools": [...] \| "*", "tier": "read_only" \| "mutating"}`. Off unless set. |
| `soar_api_key` | password | | **Preferred** native-MCP credential -- a FortiSOAR API key sent as `Authorization: API-KEY <key>` (not URI-bound, so it survives the gateway). |
| `soar_username` / `soar_password` | text / password | | Alternative to the API key: a service-account login used to mint a short-lived bearer token for native-MCP calls. |

### Configuring the product connectors a live run needs

The demo and live-autonomy paths need real product connectors on the target box
(FortiGate, VirusTotal, FortiEDR, ...). One script owns this, registry-driven:

```bash
.venv/bin/python scripts/configure_connector.py --list   # the registry + env-var -> config-field map
.venv/bin/python scripts/configure_connector.py --all    # configure every connector
.venv/bin/python scripts/configure_connector.py virustotal   # just one
```

`--list` is the source of truth for which env vars each connector reads. The
creds themselves are operator secrets, never committed: each connector reads its
own gitignored `.env.<key>` file (`.env.virustotal`, `.env.fortigate`,
`.env.fortiedr`, `.env.fortimanager`, `.env.fortisiem`, `.env.jira`, `.env.aws`)
from the checkout the operator runs the script in. Source the target box env
first, then the connector files, then run:

```bash
set -a; . <box env file>; for c in aws fortiedr fortigate fortimanager fortisiem jira virustotal; do [ -f .env.$c ] && . .env.$c; done; set +a
.venv/bin/python scripts/configure_connector.py --all
```

`fortinet-fortiguard-ioc` is built into FortiSOAR and needs no config here.

## Tests

```bash
pytest tests/
```

The test suite stubs `connectors.core.connector` so it runs outside a
live FortiSOAR.
