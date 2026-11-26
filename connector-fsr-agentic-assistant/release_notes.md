# Release notes

## 0.6.27

- Version-only bump: the code merged since 0.6.26 is unchanged, so a box that already has 0.6.26 can take it.

## 0.6.26 (2026-11-25)

_TODO: describe this release._


## 0.6.25

- Version-only bump: the code merged since 0.6.24 is unchanged, so a box that already has 0.6.24 can take it.

## 0.6.24

- Version-only bump: the code merged since 0.6.23 is unchanged, so a box that already has 0.6.23 can take it.

## 0.6.23 (2026-11-20)

- Pins fsr-playbooks 0.12.2.
- A question scoped as a direct answer, with no record in hand, now looks the record up first instead of answering from nothing.
- A verdict of "needs more info" is refused once when the cited evidence already decides it and nothing further would help; the agent must commit to a verdict.
- Release workflow: installs ruff for the lint ratchet, builds without a local dev environment, and lists the tarball without a broken pipe.


## 0.6.22 (2026-11-19)

Includes 0.6.20 (below, which was only ever installed on a test box), plus:

- A follow-up to a staged block ("make it 6 hours") re-stages the card directly. It
  was held behind a three-lookup investigation because the message named no target.
- Shipping: a same-version re-install that the platform stores under the next version
  is verified by its build id, and a missing Anthropic key stops the ship before the
  version bump.

## 0.6.20 (2026-11-19)

The reply language and the size of the request now hold on every kind of turn, not
only a typed message. A quick-action chip, an answer to a choice card, and a short
follow-up ("make it 6 hours") used to skip that step: replies drifted to Spanish, and a
follow-up to a staged block re-ran a full investigation.

- A quick-action chip is checked for its language and keeps its own flow.
- A follow-up is read together with the assistant's last message, so an answer to a
  staged action stays on that action and re-stages it.
- A card answer resumes in the language of the session.
- A value only the analyst can choose, such as a block duration, is left on the card
  as a field to fill. The reply no longer asks for it.
- The "sources not yet queried" reminder appears only in investigations, no longer in
  playbook builds or blocks.


## 0.6.19 (2026-11-19)

A request to act stages the action and stops. "Block ip <addr>" used to run a full
investigation (SIEM, EDR, tickets, intel), get forced into a verdict, and never stage the
block; it now finds the action, stages its approval card, and asks for the target only when
none is named. Replies stay in the analyst's language: the scope call names the language
(English when the ask is too short to show one) and every reminder names it. Stored sessions
record the declared scope and language. The live golden gate now catches German and Russian
drift, reads card text, and checks that a block ask does not hunt.


## 0.6.18 (2026-11-16)

Stable-box install: the Import Wizard installs exactly the repo's versions; the framework wheel
built from main is bundled; the doctor and worker checks accept that bundled wheel.


## 0.6.15 (2026-11-16)

_TODO: describe this release._


## 0.6.14 (2026-11-16)

_TODO: describe this release._


## 0.6.13 (2026-11-16)

_TODO: describe this release._


## 0.6.12 (2026-11-16)

_TODO: describe this release._


## 0.6.11 (2026-11-16)

_TODO: describe this release._


## 0.6.10 (2026-11-16)

- Outbound notifications (off by default): with Notify On Unattended Runs on, an unattended hunt or auto-triage that ends on a true positive or suspicious verdict, or that needs a person, sends one notification through the first configured channel (email, Slack or Teams) to the Notification Recipient. Decided from the verdict disposition and the needs-a-person flag only, sent once per run, and every send, skip or failure is recorded.
- Shift summary: `notify` sends the digest through the same channel.
- New operation Export Audit: sessions, model turns, tool calls, approval decisions and notifications for a time window as JSON and as a CSV timeline. Model Provider Redaction applies to the exported content.


## 0.6.9 (2026-11-15)

_TODO: describe this release._


## 0.6.8 (2026-11-15)

_TODO: describe this release._


## 0.6.7 (2026-11-14)

_TODO: describe this release._


## 0.6.6 (2026-11-13)

_TODO: describe this release._


## 0.6.5 (2026-11-12)

_TODO: describe this release._


## 0.6.4 (2026-11-11)

Fixes the cold-worker transport failure that 503'd chat turns after an
in-place connector upgrade, and the MCP-configuration read noise that came
with it.

- A long-lived executor worker can hold `fsr_soc_triage` in `sys.modules`
  from a PREVIOUS deployment directory: every connector version unpacks to
  its own `.../connector-fsr-agentic-assistant_<ver>/` dir, and the import
  that cached the package ran before the current version existed. Python
  then resolves sibling imports through the CACHED parent's `__path__`, so
  `from fsr_soc_triage import _live_crudhub` failed with "cannot import
  name '_live_crudhub'" (the submodule is new in 0.6.3) no matter what
  `sys.path` said, and the probes bridge silently stayed unbound. Seen live
  after the 0.6.3 upgrade: cold uwsgi workers failed chat turns with "no
  FortiSOAR transport available" (HTTP 503) while warm workers that
  predated the upgrade kept working -- the bridge is the only consumer of
  the newest submodule, and the older submodules already existed, so warm
  workers ran an unnoticed version mix. `_ensure_probes_bridge` now evicts
  `fsr_soc_triage*` modules that do not come from its own directory before
  the sibling import (running frames keep their code objects; only new
  imports re-resolve, so this is safe under concurrent dispatches).
- `_make_request()` gains a last-resort transport: when the bridge cannot
  bind at all, the direct `integrations.crudhub` loopback is probed once per
  process (a real loopback raises on the probe path; the connectors SDK's
  off-platform stub returns None) and used when real. A broken bridge now
  degrades to the loopback instead of failing every live tool; the dev loop
  keeps its honest no-transport return.
- `_read_mcp_configurations` (and every other direct `fsr_soc_triage` import
  site) now evicts a stale shadow first, via the shared
  `ops_common._evict_stale_fsr_soc_triage` helper. The MCP read previously
  failed its direct sibling import under the same shadow and degraded every
  turn's MCP discovery to "no servers" (30 warnings/day seen live); the
  local turn harness's `_live_crudhub._make_request` seam is unchanged.

## 0.6.3 (2026-11-11)

Makes the 0.6.2 self-heal safe in the multi-threaded executor pool and the
FortiAI transport resilient to a transient resolution failure.

- The executor pool runs multi-threaded (uwsgi `enable-threads`, ~12 threads
  per worker). `importlib.invalidate_caches()` mutates finder state for the
  whole process, so the 0.6.2 heal calling it unconditionally could fail an
  unrelated thread's in-flight import for that instant -- seen live as a turn
  that lost both its Key Store read and its FortiAI transport while every
  worker was healthy again seconds later. The heal now evicts and re-imports
  without the global invalidate (FileFinder re-lists a directory whenever its
  mtime changed, which the in-place wheel upgrade already did), and only
  invalidates when a plain re-import is STILL stale.
- `_CrudhubAsyncClient.post` retries the transport resolution (bounded, ~0.5s
  total) before returning the 503. A None transport is pre-flight -- nothing
  was sent -- so the retry cannot double-fire a request.

## 0.6.2 (2026-11-11)

Self-heals a stale `fsr_playbooks` module cache in long-lived executor workers. A
worker that imported the framework before a connector/framework upgrade keeps the
old module in its process-global `sys.modules` forever: the wheel upgrades in
place (same path), and the connector package's version-named directory only
refreshes the connector's own modules. uwsgi workers recycle at `max-requests`
(5000) or a service restart, neither of which a remote operator can always
trigger, so the version assert alone left the connector failing
`framework_mismatch` until someone SSH'd in. On a mismatch the worker now evicts
every `fsr_playbooks*` module, invalidates the finders, and re-imports from disk;
only a genuinely stale wheel on disk still fails loud.

## 0.6.1 (2026-11-09)

Pins fsr-playbooks 0.11.0 (playbook compose fix: record-action step keys are described in the tool schema). Golden prompts regenerated.


## 0.6.0 (2026-11-08)

Renamed from FSR SOC Assistant to FSR Agentic Assistant, IDs included: the connector is now
`connector-fsr-agentic-assistant` (Python package `connector_fsr_agentic_assistant`), and the
Monitor widget is `agenticAssistantMonitor`. FortiSOAR installs it as a NEW connector beside the
old one: recreate its configurations (or copy them over), point any playbook step that calls the
old connector at the new name, then uninstall `connector-fsr-soc-assistant`. Earlier entries below
keep the old name.

## 0.5.194 (2026-11-06)

Native MCP record lookups work on boxes whose configs use Built-in HMAC: under
HMAC the modules server advertises only get_modules and get_module_schema
(record reads go through search_module_records / get_record), the MCP client
waits 30s to connect and 300s to read so a cold gateway's first call succeeds,
and a failed gateway call reports its real (leaf) error. The probe_native_mcp
operation runs again. Warmup now stores each connector's config schema in the
catalog, not its saved configurations, so connectors that need setup are
recognised as such.


## 0.5.192 (2026-11-04)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.190 (2026-10-27)

Register the agentic toolkit's seven workflows into the library tool registry
(withheld from the model; dispatchable through the connector's own slice). Pins
fsr-agentic-toolkit==0.1.6 and fsr-playbooks==0.8.7.


## 0.5.189 (2026-10-27)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.188 (2026-10-27)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.186 (2026-10-26)

Pin `fsr-playbooks` 0.8.3: grounded-reference fix for the threat-feed recipe
template, lint/type hygiene, and the autonomy machinery fixes (forced-containment
guard, evidence-registry crediting, machinery-correctness gate). No connector
behavior change beyond the framework pin. Live-proven 7/7 autonomy on 8.0.0.


## 0.5.185 (2026-10-24)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.184 (2026-10-23)

Requires fsr-playbooks 0.7.6, which carries the autonomy containment fix: a
true-positive block whose IP argument arrives as a JSON-encoded list string is
now unwrapped before the target check, so a legitimate containment is no longer
silently declined as "not an IP address."

Live-autonomy harness: the required product connectors are now verified with a
live health check, so a connector the platform reports under an install status
(rather than a health status) is no longer falsely skipped. Docs point operators
to configure_connector.py and the per-connector credential files.


## 0.5.183 (2026-10-19)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.182 (2026-10-19)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.181 (2026-10-16)

Pin fsr-playbooks to 0.7.4, which carries: a verify-time rejection of a decision
branch whose condition is not boolean (a bare `else` no longer passes verify and
crashes at runtime), the enhancement step-scope fix (a step an edit op names is in
scope whatever the summary says), the effect inventory (what a playbook writes and
where each value comes from), provider rate-limit / exhausted-credit handling, and
the duplicate-offer-card guard (one offer per turn).


## 0.5.180 (2026-10-15)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.179 (2026-10-14)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.178 (2026-10-11)

- Requires fsr-playbooks 0.7.2. Developer tooling only: the test and evaluation scripts share one conversation driver from the framework. No change to what analysts see.


## 0.5.177 (2026-10-10)

- Requires fsr-playbooks 0.7.1 (was 0.6.63) and pyfsr 0.23.0 or later, which child-playbook creation needs.
- The assistant reaches every MCP server on the platform by default. A new setting, MCP access, is "All servers" unless an administrator selects servers, and a configuration that already names servers keeps its selection. Under the built-in HMAC identity the SOC and playbook servers are left out, because their calls are refused; an API key or service account brings them back, and a server named explicitly stays in.
- Write tools on the MCP gateway always ask for approval: block_indicator, update_alert_ai_analysis and execute_playbook, and FortiSIEM's clear, resolve, severity and comment tools. Before, FortiSIEM's ran with no approval card under a read-only rule, and an undeclared block_indicator ran without one.
- A malware verdict from FortiSIEM's reputation-by-entity lookup counts as threat intelligence for the autonomy policy, so it can satisfy an intel rule. A feed that gave no answer does not count.
- The prompt names only the MCP tools of the configuration in use. Before, a turn could be told to reach for tools that belong to another configuration on the same worker.
- Approvals and undo record who approved. A decision made in the widget is recorded with the name the widget supplies, marked unverified, unless the reviewer check confirmed it. An MCP call made from a playbook or the action panel is authorized and audited the way a chat call is.
- The triage prompt no longer tells the assistant to skip threat-intel enrichment of internal addresses.
- List-page quick actions (attention, find and summary) are read-only turns. Before, they were offered write tools that the widget already withheld from them.
- Build turns are no longer offered investigation tools that the turn would refuse, and a turn with nothing open is no longer offered edit_playbook or verify_enhancement.
- Apply on a build: an edit that adds a child playbook saves the child to the playbook list, the parent references it, and the Apply summary says so. A child that cannot be checked is not written, and a re-applied child keeps its collection. The staged card carries the YAML now on the canvas, so the next edit starts from it. An edit that would leave more than one playbook in the designer is refused, and the playbooks are named. Declining an enhancement offer no longer reads as abandoning the build. The playbook scope choice is kept for the session.
- Resuming a paused action restores the same session state a turn had. If that state cannot be restored, the turn stops instead of running with its gates at their defaults. A resume with nothing open no longer picks up the playbook from the previous turn. A scheduled task that may not write is refused writes at dispatch, not only hidden from the prompt.
- A chat turn whose worker died, or went silent past the time limit, now ends with an error in the transcript instead of staying open. Several workers can share the connector's database safely, and a paused approval can be resumed only once.
- Developer tooling: the test suite runs shuffled with a per-test timeout, and the local loop can drive native MCP against a box (--mcp).


## 0.5.176 (2026-10-08)

- Unattended triage can close a false positive by itself when an administrator's autonomy policy has an enforced close rule. The rule may only set the alert's status and closure reason, only on the alert being triaged, and only when the verdict cites a lookup that succeeded, so text inside an alert can never trigger a close. The close goes through the same approval path an analyst's click uses, and the alert is read back afterwards; if it did not close, the alert goes to a person. Nothing changes until such a rule is written into the policy.
- Framework 0.6.63: record-update rules in the autonomy policy could never match before (their changes were compared by the wrong names); they now do.


## 0.5.175 (2026-10-07)

- Fixes a fresh install where every assistant turn failed with `cannot import name 'sentinel' from 'typing_extensions'`. A new release of `anyio` (a dependency of the OpenAI and MCP libraries) needs a newer `typing_extensions` than FortiSOAR's connector worker provides; the connector now holds `anyio` below 4.15. Appliances that installed earlier were not affected.
- Install notes for the SOC Assistant pack: PyPI access for the connector installer, the Import Wizard route, re-running a failed requirements install, and entering the LLM key by hand.


## 0.5.174 (2026-10-07)

- An appliance that installed the connector but could not install its `fsr-playbooks` library (most often because it cannot reach PyPI) now gets a health check that says so and how to fix it, instead of an error the assistant treated as a temporary failure. The health check also lists the connector's libraries and warns when `pyfsr` is missing, naming the features that stop working.
- Ships as a solution pack: `make pack`.


## 0.5.173 (2026-10-07)

- Earlier turns are replayed to the model as native, id-paired tool calls and results instead of call-shaped text, so the model no longer writes tool calls as prose.
- On a build turn with nothing open, the playbook this conversation offered or saved is the one being edited: refinements go through edit_playbook, and Apply updates the saved playbook in place.
- Framework pin 0.6.62: validate_yaml's auto-fix renames only real step names (record fields were being rewritten), find_record date filters compile as datetimes.


## 0.5.172 (2026-10-05)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.171 (2026-10-05)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.170 (2026-10-05)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.169 (2026-10-05)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.168 (2026-10-05)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.167 (2026-10-02)

- List Active Sessions answers in a fraction of a second on a busy box (was
  about 10s over 167k progress frames, long enough for the Monitor's
  Sessions request to time out).


## 0.5.166 (2026-10-02)

- New: when auto-triage needs a human it queues an escalation (record,
  verdict, why it stopped, priority P1..P4) and notifies the reviewer only
  then. The approval queue (List Pending) returns open escalations, most
  urgent first. Resolve Escalation marks one handled or dismissed, once.


## 0.5.165 (2026-10-02)

- New: Set Auto-Triage installs an on-create playbook that triages every new
  record of a module (alerts by default, High and Critical only). Auto-Triage
  Record runs the analyst's triage turn unattended, once per record; proposed
  actions still wait for approval in the queue.
- run_playbook passes a record-action playbook's form inputs, typed as the
  playbook declares them.
- Approving a card no longer loses the turn after it: its cards and history are
  stored.
- get_record refuses an id that is not a record path instead of fetching it.


## 0.5.164 (2026-09-29)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.163 (2026-09-27)

`siem_search(by="incident")` refuses a value that isn't a FortiSIEM incident id
(those are integers) with a message the model can act on: read the alert's
`sourceId`, or search by IP or host. It used to pass a SOAR record's uuid through
and get back "Expecting value: line 1 column 1". With a source IP, it falls back
to an IP event search.


## 0.5.162 (2026-09-27)

`siem_search(by="incident")` now finds the incident's time window. The lookup
sent a status filter ("Cleared", "Open") that neither FortiSIEM connector
accepts, so `run_op` refused the call. It also searched a ~6-row digest of 500
incidents. Every live by-incident search failed `no_time_window`; on FortiSIEM
v2 it now returns the incident's triggering events.


## 0.5.161 (2026-09-27)

The `siem_*` helpers run on `fortinet-fortisiemv2` (preferred) or
`fortinet-fortisiem`, whichever this box has ready. v2 6.1.0 carries every v1
operation. The source gate picks the first healthy candidate and reports the most
fixable one when none is. Warmup now resolves side-by-side connector versions
(left after an upgrade) to the newest, operation list included, and compares
versions numerically.


## 0.5.160 (2026-09-27)

Framework 0.6.56. The on-platform modules warm now marks the catalog as this
box's own schema. Without that marker, 0.5.159's field and picklist checks stayed
warnings on the appliance, because only a dev warm stamps a base URL. A
find_record on a field the box does not have, or on a wrong-list picklist value,
now blocks `ready_to_push`.


## 0.5.159 (2026-09-27)

Framework 0.6.55. `verify_playbook` now checks record fields and picklist values
wherever a step names them. find_record filters, sort and projection fields are
validated against the module, as trigger filters already were. A field the module
does not have, or a picklist value from the wrong list (`state: Open`, where `Open`
is a `status` value), is an error on a catalog warmed from this box. On a generic
snapshot it is a warning. Implicit system fields (`createDate`, `recordTags`,
`owners`, ...) no longer read as unknown. A `tags` filter points at `recordTags`.


## 0.5.158 (2026-09-27)

- Pins fsr-playbooks 0.6.54.
- Dates are shown to the assistant already converted (UTC), and every turn
  states today's date. Before, it converted record timestamps itself and got
  about half of them wrong; offline on Frank it went from 18 of 33 wrong to 0
  of 34.
- The assistant no longer ends a turn saying "please approve the card" when it
  never raised one: it is sent back once to make the call or say plainly that
  it is not doing it.
- The tool-round limit now stops turns that repeat themselves or fail on every
  call, instead of cutting off at 16 rounds. The hard ceiling rises to 40.
  Long builds that used to stop mid-authoring can now finish.


## 0.5.157 (2026-09-26)

- Pins fsr-playbooks 0.6.53: a refused card now names the fields it is
  missing. An action card sent without `args` says so, and explains that
  `args` holds the operation's parameter values, instead of listing every
  accepted field and leaving the model to guess.


## 0.5.156 (2026-09-26)

- Pins fsr-playbooks 0.6.52. One verb per job: the log-source helpers are now
  axes of `siem_search` (`by=incident|raw`) and `faz_search`
  (`by=alerts|raw|summary`), and overlapping framework tools fold into `find`,
  `validate_yaml`, `analyze_playbook`, `why_did_playbook_fail`, `emit_card` and
  `list_configured_connectors`. The triage tool list drops from 41 to 24
  (-35% of its size). Retired names still dispatch for older sessions.
- Fixes the triage prompt, source routing and scenario hints recommending
  tools that no longer existed (`siem_search_ip`, `fmg_get_device_status`, ...);
  they now name the calls the model actually has.


## 0.5.155 (2026-09-26)

- Pins fsr-playbooks 0.6.51: a failed lookup no longer counts as evidence, so
  a containment request whose record lookup errored is not forced into a
  verdict in place of the block the analyst asked for.

## 0.5.154 (2026-09-26)

- Pins fsr-playbooks 0.6.50: a refused verdict card lists every problem at
  once, with the payload contract and the valid evidence ids, so it is fixed in
  one retry (live: nine attempts before, one after).
- The "enable this source" guidance names a sibling connector for the same
  product (e.g. `fortinet-fortisiemv2`) and points the assistant at querying it
  directly.

## 0.5.153 (2026-09-26)

- Pins fsr-playbooks 0.6.49: an address in a documentation range (e.g. the
  demos' 203.0.113.42) is treated as external, not internal -- so the external
  C2 indicator is correlation-searched and enrichment is suggested for it.

## 0.5.152 (2026-09-26)

- The "enable this source" backstop reads the record from the turn itself, so
  it fires on the investigation turn whichever worker answers it.

## 0.5.151 (2026-09-26)

- When the alert under investigation came from a log source this box can't
  reach, the turn ends with the "enable this source" card even if the model
  didn't send it -- once per session.

## 0.5.150 (2026-09-26)

- The "enable this source" card for an unreachable log source is pre-built, so
  it always renders with concrete install / configure / repair steps and a
  working Re-check button (the model's own attempts failed validation).

## 0.5.149 (2026-09-26)

- The log-source helpers (`siem_*`, `faz_*`, `fmg_*`) are advertised only when
  their connector is installed, configured, and passes its health check. The
  assistant is told which sources it can't reach and how to enable them, so it
  says so instead of calling tools that fail every time.
- FortiManager tools resolve the configured FMG config again (a broken import
  had made them always use the default).

## 0.5.148 (2026-09-26)

- Pins fsr-playbooks 0.6.48: the forced verdict round now states the full
  payload contract (the validator's own disposition and severity lists, with
  `needs_more_info` for "could not conclude"), and a refused verdict gets one
  repair attempt showing the model the refusal. Live on 0.6.47 the forced
  verdict was refused for `disposition: "inconclusive"` with no second try.

## 0.5.147 (2026-09-26)

- Pins fsr-playbooks 0.6.47, which carries:
  - **Investigations now end in a verdict card.** The verdict guard never
    fired in production: triage and build are offered the same tools, so it
    read every turn as a build, and its evidence list used tool names retired
    by the consolidation (`siem_search`, `faz_search` and native MCP tools
    counted as nothing). It now judges by what the turn did, and shares one
    evidence rule with the hunt floor.
  - **Triage turns are no longer told to write a playbook.** The same
    confusion let the build-progress nudge fire on a triage turn that had
    only pulled the record and searched.
  - The Anthropic loop forces a verdict too (it had no verdict guard), and the
    forced round's output cap is raised so a multi-finding verdict is not cut off.

## 0.5.146 (2026-09-26)

- Pins fsr-playbooks 0.6.46, which carries:
  - **A round cut off at the output cap no longer kills the turn.** A tool
    call the model was still writing when it hit the token limit was never
    run, but it stayed in the conversation history, and the next request
    was rejected with a 400. The OpenAI, Anthropic and LM Studio providers
    now drop such calls and tell the model the call was not made.
  - **A build that was drafted but never verified now gets a Create button.**
    A build turn that checked its YAML with validate/compile and then stopped
    ended with the playbook in prose and nothing to save. It is now nudged
    once to verify the YAML and offer it.
  - Explicit record-write conflict handling for `create_record`:
    `on_conflict:` (`fail`, `keep_existing`, `update_all`, `update_listed`)
    and `update_fields:`, plus warnings for tag operations that replace the
    tag list and for upserts missing the module's unique key.
- Usage frames now carry `stop_reason` and any dropped tool calls (name, size,
  last 200 characters), so a cut-off round can be diagnosed from
  `chat_history`. `chat_turns` records the dropped calls too.

## 0.5.145

- Pins fsr-playbooks 0.6.45, which carries:
  - **A malformed tool call no longer kills the turn.** When the model streamed
    tool-call arguments that were not valid JSON, the call was bounced back
    for repair, but the raw string was replayed in the next request. An
    OpenAI-compatible endpoint rejects that whole request with a 400. The
    OpenAI and LM Studio providers now replay it as valid JSON, so the model
    repairs the call and the turn continues.
  - The LM Studio provider no longer raises NameError on its first tool result.
  - The model catalog lists `coding-b200/max` as tested.
- The post-ship card smoke covers all 8 card types, including the verdict card.

## 0.5.144

- Pins fsr-playbooks 0.6.44, which carries:
  - **Verdict cards.** A triage verdict now arrives as a structured
    `verdict_card` event: disposition, confidence, findings, recommended
    actions. Each finding cites the tool calls it rests on, and a finding that
    cites no evidence from the turn is refused. This connector routes the event
    through to the widget.
  - **Delivery guards on `emit_card`.** The guards that make a build turn end
    on a card rather than a bare YAML fence were keyed on the retired
    `emit_*` names. After the consolidation they had stopped firing, and they
    fire again now.
  - **An example library.** Friendly-YAML step examples harvested from the
    reference corpus, served by `find(kind=example)`.
  - Trigger filters are normalised the same way as `when:` expressions.
- Scheduled tasks: `allow_mutating` is now enforced. A task with it off runs
  read-only, and stays read-only when it is resumed after parking at an
  approval gate. `create_scheduled_task` also gains the `notify_target`
  parameter the runner already read.
- `resume_playbook` counts an approved card as authorisation, the same way
  `run_playbook` does.
- The health check fails closed when the model catalog cannot be imported.
  It also screens the FortiAI proxy's feature model.
  `list_models` labels each model with its catalog tier and hides the
  discouraged ones.

## 0.5.143

- Built from an uncommitted working tree (pin fsr-playbooks 0.6.43) and
  installed on the lab box only. It was never committed, and 0.5.144
  supersedes it.

## 0.5.142 (2026-08-23)

- Pins fsr-playbooks 0.6.40, which carries the turn plan's capability ask (the
  native primitives stop being inert), the `attribute` eval lane, and the
  tested-model catalog.
- The health check now refuses a model measured failing on the routing slice
  and warns on an unscreened one. Live-verified on 8.0.0: `gpt-4.1-mini` is
  refused with its evidence, `gpt-5.4-mini` passes, and an unlisted id passes
  with a warning so a customer's own endpoint is never dead-ended.


## 0.5.141 (2026-08-23)

- Pins fsr-playbooks 0.6.39, which carries the provider capability seam and the
  last of its four native Anthropic primitives: reasoning depth, task budgets,
  deferred tool loading, and now context editing (`clear_tool_uses_20250919`)
  in place of the host-side `shrink_history`.
- All four are ASK-gated and model-gated, so this connector's wire is unchanged
  until it passes the corresponding request: nothing here starts sending
  `thinking`, `output_config`, a tool-search tool, or `context_management`
  on its own.
- Also picks up the framework's undeclared-`httpx` fix: the FortiAI proxy
  provider imported it at module level without it being in any dependency
  list, which would have failed at import time on a box installing the
  published extras rather than at first use.


## 0.5.140 (2026-08-23)

- Prompt-cache prefix: the advertised tool array no longer varies within a
  session. Both `_afforded_drop_set` state drops (`has_trace`,
  `has_open_playbook`) are gone, and per-page MCP server surfacing became a
  ranking stated in the prompt prior instead of a subtraction (A1.2/A1.3).
  Each of these used to discard the whole cached prefix -- system prompt
  included -- mid-conversation.
- A failed TurnPlan derivation now installs `TurnPlan.failsafe(...)` instead
  of no plan, so the affordance gate can no longer silently vanish; the
  frontier refuses with `state_unresolved`.
- Authorization is unchanged: per-config server scoping, persona server
  scopes, tier-3 gating and the affordance gates all still subtract/refuse.
  Discovery got broader; execution did not.
- Anthropic model catalog moves to the adaptive-thinking family (Sonnet 5
  default, Opus 5 offered); the provider tooltip no longer claims the stock
  FortiAI path is egress-free unconditionally.


## 0.5.139 (2026-08-19)

- #149: async containment handoffs enriched at the native-MCP funnel; failed/pending queued playbooks now narrate honestly on every dispatch path (live-verified via T1).


## 0.5.138 (2026-08-19)

- #149: approved-card exec path chases a task_id-only result to a real outcome; failed/pending runs narrate honestly.


## 0.5.137 (2026-08-18)

- #148 closed: write persona bound around approved-card execution; probe T2 PASS live (approved record-update writes the row).


## 0.5.136 (2026-08-18)

- #148: approved record-write cards rebuild {module, uuid, fields} from target+args at execution.


## 0.5.135 (2026-08-18)

- #148: ContextVar bindings shared across the package's two import names; resume paths bind the host-record write fallback. Record-write cards stage from a plain triage mount again.


## 0.5.134 (2026-08-18)

- fsr-playbooks 0.6.29: #128 dispatch-shaped budget levers + emit_card union-fold fixes; T-probe runner hardening.
- First ship gated by the post-ship card smoke (#147): all 7 card types emitted live, canonical + dialect.


## 0.5.133 (2026-08-18)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.132 (2026-08-18)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.131 (2026-08-18)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.130 (2026-08-18)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.129 (2026-08-17)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.128 (2026-08-17)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.127 (2026-08-17)

- Framework pin -> **fsr-playbooks 0.6.24**, the released form of the fixes
  dev-patched on 2026-08-17: approved tier-3 actions dispatch off the event
  loop on resume (no more `asyncio.run() cannot be called from a running
  event loop`); resume re-enters with the suspended turn's tool slice, so the
  post-approval turn can act instead of stranding; zero-hit `find_operation`
  retries with domain synonyms and names the product's term; eval
  calibrations resolve the real triage prompt or fail loud.


## 0.5.126 (2026-08-17)

- **IOC card zero-score fix**: a lone zero from an unknown field renders no
  score (never "Risk 0/100 / clean"); scoring takes the max across populated
  fields. (Dev-only install; superseded by 0.5.127.)


## 0.5.125 (2026-08-17)

- **#132 - an analyst's own deletion is no longer an error.** Pins
  `fsr-playbooks==0.6.23`, which stops `verify_enhancement` from flagging a
  deletion the analyst explicitly asked for, and carries the analyst's intent
  through to the gate that needed it. The connector side forwards the action
  card's `acknowledged_drops` into the write, so an approved edit that removes
  steps now applies instead of being refused as data loss.
- **Triage prompt - tighter hunt budget.** Three new anti-patterns in the
  budget-discipline section: the opening correlation is **two** searches (one
  `alerts`, one `incidents`) rather than a pair per indicator; enrichment runs
  against **external** indicators only and stops at three sources (RFC1918
  addresses and internal hostnames have no threat-intel verdict); and record
  writes resolve via a single `find_record_actions` call instead of a
  `find_connector` / `find_operation` hunt that had been burning seven calls on
  a one-call turn.


## 0.5.124 (2026-08-15)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.123 (2026-08-15)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.119 (2026-08-15)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.118 (2026-08-15)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.117 (2026-08-15)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.116 (2026-08-14)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.115 (2026-08-13)

Ships **fsr-playbooks 0.6.20**. No connector-code changes; everything below
comes from the framework bump.

- **A rename the analyst ASKED for is no longer reported as a silent rename.**
  `verify_enhancement` blocked the analyst's own rename request, so the edit
  they explicitly asked for came back as a collateral-damage warning.
- **The enhancement diff carries what changed, not just which steps** (#126) --
  the offer card can lead with a step diff instead of raw YAML.
- **Sibling Jinja filters are no longer folded into one chain** by the
  compiler, which mis-emitted the expression.
- Offline-only, no runtime effect on a box: the simulated client gained the
  typed `.connectors` API the discovery tools read, and can now serve the
  record surface from a fixture bundle.


## 0.5.114 (2026-08-12)

- Fixes a NameError shipped in 0.5.113: `faz_search`'s `device` and `serial`
  axes delegate to functions in `tools_noc` that were never imported, so two of
  the merged tool's three axes crashed -- while the per-axis tools that used to
  serve them had just been removed from the model surface. Import is
  function-local (tools_noc imports back out of tools_triage).
- New `test_merged_hunt_tool_axes.py`: every declared axis of `siem_search` /
  `faz_search` / `fmg_device` must reach an implementation, transport stubbed.


## 0.5.113 (2026-08-12)

- Merged hunt tools (#69): `siem_search` / `faz_search` / `fmg_device` replace
  the fifteen per-axis tools on the model surface -- 15 names collapse to 8.
  The old functions stay callable and the merged tools delegate to them, so no
  caller breaks; they are simply no longer advertised. Routing verified
  unchanged at 5/5 on the tool-gate.
- On fsr-playbooks 0.6.19, which carries the one-change-one-approval gate fix.


## 0.5.112 (2026-08-12)

- `context_match` on record-write approval cards is real (#120). It read
  "unknown" on every card because the turn's mounted record was never threaded
  into `_approval_to_action_card`; an analyst could not tell from the card that
  the uuid was NOT the record they had open. Unknown still means unknown -- with
  nothing mounted the card claims nothing.
- Ship-time guard: `deploy.sh` refuses to build a connector version the target
  box already runs, before info.json is touched, and names the next safe number.
  The framework pin may stay the same -- it is a dependency, not an artifact.

## 0.5.110 (2026-08-11)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.109 (2026-08-11)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.108 (2026-08-11)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.107 (2026-08-11)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.106 (2026-08-10)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.105 (2026-08-10)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.104 (2026-08-05)

**A directive told the model to call a tool that has never existed.**

Live on .206 (0.5.103), one turn after the analyst submitted the manual-input
form, the assistant reported this to them:

    "What I tried - I attempted to fetch the workflow/run record (pk 9335)...
     /wf/api/workflows/9335 -> not_found
     /api/3/wf/workflow-runs/9335 -> http_400 (internal server error) ..."

The resume directive ended "...using `get_playbook_run` if you need its current
status", and **`get_playbook_run` is not a tool in this connector** -- the
registry has `run_playbook`, `resume_playbook` and `list_module_playbooks`, and
no run-status tool at all. Told to use a tool it did not have, the model
improvised REST paths through `get_record`, failed four times, and narrated the
failure. Instructing a capability we do not have manufactures the dead-end the
honesty rules exist to prevent, in the most expensive place: in front of the
analyst, immediately after a successful action.

The forwarded branch now says to confirm the submission from the outcome already
in hand and ask what's next, instead of sending the model hunting.

New gate: `tests/test_directives_name_real_tools.py` cross-references every tool
a synthesized directive tells the model to call/use against the live registry,
across every forward branch and both decisions. Nothing compared prose to the
registry before -- the string and the tool set were separate worlds, and each was
individually fine.

## 0.5.103 (2026-08-05)

**The turn after a successful submit asked the analyst for a run pk we were
holding.**

Found by re-running the live arc on .206 after 0.5.102. The strand fix worked --
`chat_history` shows the unrelated follow-up question getting a proper answer
with a `get_record` call -- but the RESUMED turn itself was still poor: it came
back "I need the playbook run identifier (the workflow run PK / @id) to query
its current status. Please paste the run_pk...", one turn after the analyst
watched that same run get resumed.

Cause: the SUCCESS path of `_forward_manual_input` was the only branch that did
not return `run`. Every failure branch has carried it for a while. So the resume
message read `manual_input_id=545` with no run pk, while the turn directive told
the model to report what the run does next "using get_playbook_run" -- and with
nothing to pass, it asked the human. Asking a human for an id we are holding is
the `agent_asks_for_data_it_holds` shape; the fix is to hand it over, not to
word the ask better.

Also in this release: the live arc spec (`approvalToManualInput.live.test.js`)
no longer measures the wrong window. It anchored the follow-up assertion on
transcript LENGTH, so a previous answer still streaming into the DOM made the
"new" text the tail of the PREVIOUS turn -- reporting the shipped, working
strand fix as broken. It now settles the prior turn, requires a brand-new
message element, and asserts the negative (no run/resume talk) rather than
requiring the word "phishing", which a correct answer need never use.

## 0.5.102 (2026-08-05)

**The session stranded after a resume: a turn-scoped instruction was being
persisted as something the analyst said.**

Live on the demo box (0.5.100), with the approval -> park -> form -> submit arc
finally working end to end, the very next question stranded. "How do I triage a
phishing report?" came back as *"I will not resume the run again. I don't yet
have the playbook run identifier (run_pk) needed to fetch its current workflow
status."* That turn ran **zero tools** -- it never tried and failed, it was
obeying an instruction that was still live.

`respond_manual_input` appends the manual-input outcome to the conversation as a
**user message**, and that message ended with "Do NOT call resume_playbook for
this gate again. Report what the run does next, using get_playbook_run if you
need its current status." Correct for the resumed turn -- every clause was added
to fix a real bug -- but it was persisted verbatim, so on every later turn the
model read a standing order from the analyst about a run that had already
finished, competing with whatever they actually asked. The stranded reply is
close to a literal readback of it.

The message is now split: **facts persist, imperatives do not.** The turn-scoped
directive reaches the model on the resumed turn only (via a new
`turn_directive` argument to `_resume_conversation`) and never enters the
canonical conversation. Care was taken that the failure branches keep their
FACTS in history -- "these values were NOT applied", "the run was not
reachable" -- because those rode inside the directive, and dropping them would
have left the conversation asserting a success that never happened.

Same class as the record-referent drift fixed in 0.5.90: stale context
outliving the turn it was written for.

Mutation-proved: re-persisting the directive (what 0.5.100 did) reds all four
history tests while the "the directive still reaches the resumed turn" tests
stay green -- so the gate cannot be satisfied by simply deleting the
instruction, which would regress the double-resume bug it was written for.

## 0.5.101 (2026-08-05)

**The same parked-run defect, one branch over: `follow=True` down the MANUAL
route also lost the form.**

0.5.100 fixed the unfollowed trigger. It did not fix the followed one, because
the branch it fixed was the branch its author was looking at. pyfsr's
`trigger(follow=True)` raises `TimeoutError` for a run parked on a manual-input
gate exactly as it does for a run that is merely slow -- its terminal set
excludes `awaiting` -- and the handler could not tell them apart. So a manual
playbook that parked came back as `not_finished_awaiting_or_slow`, "it may be
paused on a manual-input step or still running", carrying no `run_pk` and no
form. The gate really was open, its form really was ready, and the analyst had
no way to answer it.

That branch now re-reads the run (`_awaiting_or_slow`, the same shaping the
record-action path already used), so a park surfaces `awaiting_input` with the
form's fields and a resumable `run_pk` whatever route it was triggered down and
whatever the model passed for `follow`.

**Found by a property test, not by a person.** `tests/test_tools_playbook_
properties.py` parametrizes `run_playbook` over the cross product of its
model-selectable arguments (2 trigger routes x `follow` x record-present x
parked/slow/terminal) and asserts an invariant rather than a return shape: *if a
run was triggered and it parked, the caller always gets the form.* The tool had
45 tests. Several covered `follow=False`. Several covered a parked run. None
covered both -- which is how 0.5.100's gap survived its own fix. The 24-cell
matrix runs offline in 0.13s.

Also in this release, all test-only:

* Three test suites in this repo, not two. `connector-fsr-soc-assistant/
  fsr_soc_triage/tests` (527 tests, including the entire `run_playbook` surface)
  was outside `testpaths` and had never run in any gate. It had rotted exactly
  as you would expect -- stale intent-slice assertions, a fake provider raising
  `TypeError` on a keyword the real provider contract grew, and one test
  reaching a real LLM endpoint over the network. The `testpaths` guard that
  existed for this asserted only about its own directory; it is now
  discovery-based, so a fourth suite cannot hide.
* Four process-global leaks fixed at the isolation layer, all found by running
  the suite in randomized order: the FSR live-client seam `local_turn` rebinds
  and never restores, the mounted-record IRI ContextVar, the approval-grant
  table (a dispatch AUTHORIZATION table -- a gating test could pass on an
  earlier test's grant), and one global tier resolver read through two module
  identities of `fsr_soc_triage`.

## 0.5.100 (2026-08-05)

**`run_playbook(follow=False)` could hide a parked run -- the whole manual-input
arc hung on one model-supplied argument.**

`follow` defaults to `True`, but the agent may pass `follow=False`, and live on
a demo box it did exactly that for the *Manual Input Demo - Collect Note* arc.
The unfollowed branch returned `{"triggered": True}` and stopped, so the turn
never learned the run had parked on its manual-input gate: no `awaiting_input`,
no `manual_input` card, and an approval card that truthfully reported "Approved
and executed" because `ok` really was `true`. Every layer behaved as written and
the analyst still got no form -- the arc degraded from "a form appears in chat"
to "a chatbot asks you to describe the form", and which one you got was a coin
flip. It also quietly stranded one gate per approval on the box.

An unfollowed trigger now runs a short bounded probe (`_PARK_PROBE_SECONDS`,
10s) and shapes the resumable `awaiting_input` seam -- with the form's fields --
if the run parks. A parked run is not a run you can decline to wait for: nobody
else is coming to answer its gate. `follow=False` still means what it says for
every other outcome; a merely slow run is reported exactly as before.

Fixed in dispatch rather than in the tool description on purpose: a docstring
asking the model not to pass `follow:false` is a suggestion, not a gate.


## 0.5.99 (2026-08-04)

**The approve → parked run → manual-input arc (#88 / #90 / #91).**

Three bugs, one root cause: `ok:false` doing double duty for "this call failed"
AND "this call is parked awaiting a human". The sweep's matrix counted a park as
a refusal; the approval card told the analyst "Approved -- but the action did not
run" for a run that was live and parked; and the resume transcript was discarded
wholesale on the first already-committed `tool_use` id, so the manual-input form
never reached the analyst and the run stayed parked forever. Shipped with widget
1.2.52.

(Notes written retroactively in 0.5.100 -- 0.5.98 and 0.5.99 both shipped with a
placeholder.)


## 0.5.98 (2026-08-04)

Rolled up into the 0.5.99 work above; shipped inside it and never separately
verified on a box.


## 0.5.97 (2026-08-04)

**A personaless triage turn can run a deployed playbook.**

`_afforded_drop_set` -- not `tools_for_intent` -- decides the deployed tool
surface, and it reads the connector-LOCAL `_BUILD_ONLY_TOOLS`, which listed
`run_playbook`, `resume_playbook` and `search_playbooks`. So a triage turn with
no persona allowlist had no way to run a deployed playbook at all. The surface
only ever worked from a persona-gated module, because an allowlist WINS over the
intent slice and quietly re-added everything the drop set had removed.

This subtracts the framework's `RUN_VERB_TOOLS` from BOTH branches of
`_afforded_drop_set`: the run verb stays whole in every intent. Fail-open via
defensive import, matching the `TRIAGE_ONLY_TOOLS` convention -- an older pinned
wheel without the symbol subtracts nothing and behaves exactly as before.

> **Pin requirement:** this is inert until `fsr-playbooks` ships `RUN_VERB_TOOLS`
> and the pin is bumped to it. Release the framework FIRST, then bump, then ship
> the connector (`make deploy FW=next`). Shipping the connector against an
> older pin looks successful and changes nothing on-box.

The existing parity test could not catch the gap: it asserts the connector's set
is a SUPERSET of the framework's -- deliberately, so the connector may know about
tools the framework doesn't -- which makes an EXTRA entry here invisible to it.
The same one-directional blind spot as a gate that selects zero files. Three new
tests assert the invariant on the LIVE path (both intents, and the personaless
triage slice end-to-end), plus one that every exempted name is actually
REGISTERED -- a name exempted but never registered is exempted from nothing.

**The op catalog self-heals on a confirmed stale-catalog miss.**

`register_catalog_probe` now also registers an auto-rewarm callback. When the
on-miss probe confirms the box has a connector the catalog doesn't, the callback
force-warms the reference DB and invalidates the probe cache, so the next lookup
sees it without a manual `warmup`. A module-level flag keeps concurrent misses
from triggering multiple warms. Best-effort: failures are swallowed and the
`stale_catalog` hint is still returned.

**Graders no longer read an IP address as a verdict value.**

A sentence like "I won't state a risk score for <an IP>" contains a digit and a
verdict term, but the digit is not a verdict value. IP-like patterns are
stripped before the digit check, so a cited address can't score as a verdict.
Affects the eval harness only, not runtime behaviour.


## 0.5.96 (2026-08-03)

**An approved `run_playbook` now sees the record it was mounted on.**

`chat_turn` binds the mounted record from the widget's `entity` block, so a
record-action playbook fired via `run_playbook` defaults to THIS record when the
model omits `record`. `chat_resume` carries no entity, so on the resume path
that binding was never established: the default resolved to `""` and the
ALREADY-APPROVED call refused `record_required`.

The arc that produced: approve -> `record_required` -> the model corrects itself
with the record IRI -> that is a NEW tool_use, so it raises a SECOND approval
card -> nothing runs. One analyst approval, zero playbook runs, no results, no
closing message.

Third instance of one shape -- state `chat_turn` establishes per worker that a
resume landing on another worker does not inherit, after the persisted persona
(0.5.95) and the MCP allow-list. The re-bind reads the `chat_sessions` row
`chat_turn` already persists and composes the IRI through the same helper as the
turn path, so the two cannot drift. Fail-open: any storage problem leaves the
tool behaving exactly as before.


## 0.5.95 (2026-08-03)

**A human-approved run is no longer vetoed by a missing persona.**

The no-persona branch of the `run_playbook` gate was documented "normally
unreachable" on the premise that `run_playbook` only surfaces via a persona's
`tools.allow`. That premise is false: `run_playbook` is in the *build* intent's
tool slice, which binds no persona.

Live on 8.0 that produced the worst possible arc -- a correct approval card
(right playbook, right run history, working deep link), the analyst clicks
approve, and the run is refused `run_not_permitted` with nothing run. The
module's own policy already says human approval is the real gate and "reaching
execution == authorized"; the persona check contradicted it.

The guard now applies only to UNATTENDED runs. `_execute_approved_card` wraps
its dispatch in `approved_dispatch()`, and the gate honours that. A ContextVar
rather than a tool parameter: FastMCP rejects any parameter starting with `_`,
and a public `approved=` argument would be model-settable -- the model could
then approve its own runs, which is exactly what the HITL gate exists to
prevent. Only the resume path can set it, scoped to the single dispatch.

## 0.5.94 (2026-08-03)

**Picks up framework `fsr-playbooks` 0.6.15**, and lands the HITL approval
fixes: an approved card now actually executes.

- An approved non-`run_op` card was carded and then dropped -- the approval was
  recorded but never dispatched. An approval with nothing dispatchable is now a
  loud, audited failure instead of a silent conversational fallback.
- A `run_playbook` approval was not persisted across the resume, so even a
  correct dispatch had nothing to act on. Together these were the
  "carded, never ran" failure.
- A `run_playbook` approval card now says what it would run: playbook name,
  last-run date and 24h run count, plus a `playbook_url` deep link to the
  editor route on the configured appliance. Fail-open throughout -- any
  trouble and the card renders as before.
- Prompt hard-rules for absent products and unconfirmed connectors (#72), so
  the model stops writing unconfirmed connectors into YAML.

Needs widget >= 1.2.49 to render the playbook link; older widgets ignore the
field and render the card as before.

## 0.5.93 (2026-08-03)

**Picks up framework `fsr-playbooks` 0.6.14.** No connector-side behavior change
of its own; the pin bump carries:

- The picklist precheck no longer writes to the packaged catalog, so a live
  lookup can't mutate what ships in the wheel.
- A build never delivers a playbook that references an unconfigured connector.
- Gating fires on the change *transition*, not on the analyst's wording.
- The compiler parameterizes values nested inside JSON-blob record fields.


## 0.5.92 (2026-08-03)

**Gating -- a change is proposed only when the analyst reached for one.**
An analyst who asked, in prose, only for an explanation of their open playbook
was handed an applyable edit: the model explained, noticed a real defect while
explaining, then ran on for five more round-trips and ended on an enhancement
offer. The composer stayed locked behind the typing indicator the whole time,
because the turn really was still running.

- The read-only quick-action chips (`explain`, `find_issues`) no longer carry
  the write/offer frontier in their tool slice at all.
- A free-typed turn against an open playbook carries no change affordance, so
  the write frontier escalates to an approval card: "you didn't ask me to
  change anything -- want me to draft the edit?" The analysis tools stay;
  noticing a defect while explaining is the point, and only the unasked-for
  edit is withheld. Requires `fsr-playbooks` with the affordance gate; against
  an older wheel this degrades to the previous behavior rather than failing.

**Manual input -- an expired gate is never narrated as accepted.**
The bridge message was corrected previously, but the lead sentence still read
"I approved the gate with these values" on the branches where nothing reached
the run, handing the model an assertion and its retraction in the same turn.

**Triage -- an analyst naming a product does not make it available** (#72), and
a build turn now salvages its offer from the tool calls when the model ends
without a YAML fence (#1).


## 0.5.91 (2026-08-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.90 (2026-08-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.89 (2026-08-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.88 (2026-08-02)

- **An approved playbook run on resume executed nothing.** `_resume_conversation`
  -- the path every card approval lands on -- recovered the session persona by
  re-resolving it from the mounted module via the Key Store, and nothing else. A
  cold resume worker fails that read, so the agent loop re-entered with no
  persona and `_run_playbook_gate` refused with `run_not_permitted`: the analyst
  approved, the approval was consumed, and no playbook ran. `_resume_suspended`
  has carried the guard for this (prefer the persona persisted at turn time via
  `_store_turn_profile`) since the tier-3 write fix; the conversational resume
  path never got it. It does now. Live on 8.0.0: an approved
  `run_playbook` executes to `finished` and its run group appears.

- **`write_not_permitted` no longer dead-ends the analyst.** A read-only persona
  is usually read-only *by design* -- ZTPF devices mutate through approval-gated
  playbooks, not raw record writes -- so the refusal was a true statement of
  policy that still left the turn having done nothing, with the playbook that
  does the job one discovery call away. The refusal now names the open route
  (`list_module_playbooks` → run the one that fits) and tells the model not to
  retry the write or ask for a permission grant.

- `search_module_records` accepts `page`. `limit` caps a page, not the answer;
  without paging, "list every X" could not reach past the first 25 records.

- 0.5.86 and 0.5.87 were intermediate dev ships of the above; no separate
  behaviour.


## 0.5.85 (2026-08-02)

- No behaviour change. 0.5.83 and 0.5.84 were two measured prompt experiments
  against the intermittent `chat_action_card` staging failure (tracker #60);
  neither beat the 4/6 baseline on the box model (0.5.83 scored 1/6, 0.5.84
  3/6), so this restores the 0.5.82 prompt exactly. Findings are on the card.


## 0.5.82 (2026-08-02)

- **Bounds the mcp SDK pin (`mcp>=1.0,<2`).** The bare `>=1.0` now resolves to
  2.0.0, which renamed `streamablehttp_client` to `streamable_http_client`, so
  `build_client` raised on import, logged "mcp SDK not installed", and returned
  None -- native MCP would go dark on any FRESH install (a new box, or a deploy
  that reinstalls dependencies) with one warning and no other signal. Existing
  boxes were unaffected only because they already had a 1.x. The import now
  accepts either name, so lifting the bound is a decision rather than an outage.


## 0.5.81 (2026-08-02)

- Pins fsr-playbooks **0.6.13**, which fixes an allowlisted MCP server silently
  vanishing from ~30% of turns (tracker #66). Every worker minted its own OAuth2
  token and a ship restarts them together, so FortiSIEM's token endpoint --
  which tolerates roughly one grant per 3-4s per client -- rejected the burst;
  the failure then latched for the life of the worker. The framework now mints
  once per box and shares it, and retries a server that failed. Live on .159:
  10/10 calls report all three servers, where it was 7/10 before.


## 0.5.80 (2026-08-02)

- **A cold worker's first turn advertised half the toolset.** The connector's own
  triage/NOC tools (`get_record`, `search_module_records`, `siem_*`, `faz_*`,
  `fmg_*`) register on `import fsr_soc_triage`, which nothing did before the
  first tool slice was computed -- so the first turn on a fresh worker had no
  record lookup and no SIEM/FAZ/FMG at all, and which of the workers answered
  decided it. Silent: a smaller toolset is not an error, just a worse
  investigation. `_tools_for_persona` now registers before slicing.


## 0.5.79 (2026-08-02)

- **The advertised turn surface is now scoped to the config's own MCP allowlist**
  (tracker #63). It was sliced from the process-global materialized surface every
  config on the worker shares, so a worker that had served one config carried
  that config's MCP servers into the next config's turn -- an authorization gap,
  not a cosmetic one (`mcp_soc__block_indicator` was in that set). A
  misconfigured allowlist now fails closed, and scoping runs before per-page
  surfacing so an on-ask `reach_servers` cannot re-authorize a server the config
  never named.


## 0.5.78 (2026-08-02)

- `list_mcp_servers` and `list_agent_tools` report the calling configuration's
  MCP servers instead of the worker's accumulated union. Both read the same
  process-global materialized surface, so a config whose `mcp_allowlist` was
  empty could still be told it had three servers -- and which ones depended on
  which worker answered, not on the configuration. `list_mcp_servers`
  contradicted its own `allowlist_keys`; `list_agent_tools` returned an
  identical 66-tool surface for a three-server config and an empty one.
  Scoped on what the introspection ops report, so no turn's advertised toolset
  changes.


## 0.5.77 (2026-08-02)

- Native MCP tools are scoped to the calling configuration's `mcp_allowlist`.
  `materializer.SERVER_MAP` is a process-global that accumulates every server
  any config on the worker has materialized, so every caller was handed the
  union -- a configuration with an empty allowlist still listed 29 `mcp_*`
  tools, and which tools you got tracked worker warmth rather than the config.
  A misconfigured allowlist now yields no native tools, matching the turn
  path's fail-closed behavior.


## 0.5.76 (2026-08-02)

- Operations that read config now declare `is_config_required: true`.
  FortiSOAR hands the handler an **empty** config dict when an operation
  declares `false` -- it does not merely skip validation. Seven ops
  (`call_mcp_tool`, `dry_run_playbook`, `get_audit_trail`, `get_session_trace`,
  `list_agent_tools`, `list_mcp_servers`, `list_mcp_tools`) therefore saw `{}`
  on every call, making the allowlist filter a guaranteed no-op: no native-MCP
  tool was reachable from a playbook, and `list_mcp_servers` reported an empty
  allowlist on a box whose stored configuration held three servers.


## 0.5.75 (2026-08-02)

- `dry_run_playbook` returns `status="awaiting"` (with `awaiting_steps`) when a
  run parks on a manual-input gate, instead of polling to timeout. A gated
  playbook waits for a human who is not coming.
- `respond_manual_input` accepts a numeric `input_id` as an int: the execute
  endpoint coerces numeric string params in flight, so the `str`-only
  annotation rejected every real gate id.


## 0.5.74 (2026-08-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.73 (2026-08-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.72 (2026-08-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.71 (2026-08-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.70 (2026-08-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.69 (2026-08-02)

Pins `fsr-playbooks==0.6.9`.

Model-coverage hardening -- three gates that were green over things they
could not see (#49, #50, and the audit itself):

- **The op gate asserts PRESENCE, not just wiring (#49).** Deriving the
  op -> model map killed the name-list drift, but an op with *no* model was
  not a failing pair -- it was not a pair, so the gate was green over 17 of
  38 ops. Adds the 12 missing params models and 16 response models (the six
  `*_scheduled_task` ops matter most -- they are widget-reached WRITES), and
  makes "this op takes no params" an explicit, reasoned opt-out instead of a
  silence.
- **Storage row models are wired (#50).** Six models described the sqlite row
  and payload shapes and were checked by nothing but their own unit tests.
  Now validated on the load path (or the write path, for the two tables
  nothing reads back), permissive and side-effect only, warn in production /
  raise under `FSRPB_STRICT_MODELS=1`.
- **The authoritative transcript is typed.** `chat_poll`'s frames were
  `list[PollFrame]` and validated on every poll, while `ChatTurnResponse.
  transcript` and `StreamEndFrame.transcript` -- what the widget actually
  renders and rehydrates from -- were `list[dict]`. The preview was checked
  and the source of truth waved through. Both are now typed: a frame with no
  `type`, or a non-string one, is rejected; unknown-but-string types still
  pass, so a newer framework cannot fail a turn.
- **The emit_* card splice builds through its model** like every other
  outbound frame, with the model derived from the `PollFrame` union rather
  than a third hand-maintained name list.
- **New reachability gate**: every authored model must be reachable from
  production code, by AST closure rather than grep, so docstring mentions
  cannot make a dead model look wired.

Also: live-integration reports unmeasured checks as BLOCKED rather than SKIP,
and the P2 scenario asserts the gate rather than how eagerly the agent
reaches it.


## 0.5.68 (2026-08-01)

- Pins `fsr-playbooks==0.6.8`, which prunes connector params that the
  chosen discriminator value hides. A recorded trace spanning two branches
  of a conditional (fortigate `block_ip_new` with both Policy-Based and
  Quarantine-Based params) compiled to nothing; it now builds.


## 0.5.67 (2026-08-01)

- Pins `fsr-playbooks==0.6.7`: a structured tool argument sent as a JSON
  string is decoded instead of rejected, and the forced wrap-up round now
  delivers the playbook when the turn ran out of tool budget before
  authoring one.
- Persona/skills read paths, `may_write` refusing before carding,
  `claims_backed` halt-key fix, and search naming its relationship fields.


## 0.5.66 (2026-08-01)

**Picks up `fsr-playbooks` 0.6.6.** No connector-side change; the pin in
`requirements.txt` moves 0.6.5 -> 0.6.6 so the shipped build carries the
framework work that landed since:

* **HARDEN-1's pre-write gate.** A save that would delete live data is now
  refused on both write paths, and an unexpanded live pull refuses the write
  rather than approving a wipe -- the live fetch must use `$relationships=true`.
  A refusal names its own class and a remedy that exists.
* **The model no longer echoes the playbook to read it.** `yaml_text` is
  omitted where the open playbook is already in context, so a read costs no
  round-trip through the model's output.
* **Typed at the wire seam.** The playbook JSON the appliance hands back, and
  the four live fields the compiler actually reads, are typed at the boundary
  instead of duck-checked downstream. A live crudhub GET returning dict-keyed
  `steps`/`routes` is accepted rather than crashing the round trip.
* **Decompiler:** a decision step's unlabeled route stays unlabeled.


## 0.5.65 (2026-07-31)

**Native-MCP configuration becomes a set of pickers, and a bad allowlist stops
failing silently.** Configuring native MCP meant hand-writing a JSON blob into
`mcp_allowlist` naming servers the operator had to already know existed, and
every mistake was invisible: the string was parsed independently in three
places, each swallowing errors into an empty result, so a typo'd allowlist and
an unset one were indistinguishable -- no signal from the UI, the health check,
or the logs. Three changes:

* **Pickers instead of JSON.** `mcp_servers` is a `dynamic`-renderer multiselect
  populated live from `/api/3/mcp_configurations`, so only servers that actually
  exist on the box are offered; `mcp_tier` sets their approval tier
  (`read_only` = tier 1 auto-run, `mutating` = tier 3 approval card).
  `mcp_allowlist` survives as an Advanced JSON escape hatch for per-tool rules
  and `connector:<name>` servers, and still wins when set -- so configs written
  before the pickers existed keep working unchanged.
* **One parser, loud failures.** `_parse_mcp_allowlist` is now the single
  parser. It keeps `{}` for "native MCP not configured" (a legitimate default,
  stays silent) and raises the new `AllowlistError` for "configured, but wrong".
  The turn path swallows it -- a bad allowlist must never kill a chat turn --
  while `_check_health` surfaces it, so the operator finds out at Save time.
  `_mcp_server_key` maps an MCP Configuration URL (`https://…/mcp/soc/`) down to
  the bare key (`soc`) the materializer wants.
* **Identity as a branch, not three loose fields.** `mcp_auth` selects
  Built-in (HMAC) / API key / Service account and reveals only that choice's
  fields via `onchange`. The built-in HMAC identity is signed for the `/mcp/`
  URI and is not forwardable, so servers that fetch records or trigger playbooks
  downstream 401 with it -- the picker now says so instead of leaving the
  operator to discover it.

Every config field also declares `visible`/`editable`: an omitted `visible` key
renders the field INVISIBLE in the UI, which is how `simulation_mode` had been
unreachable. 30 regression tests (`test_mcp_allowlist_config.py`) pin the
parser contract. Also adds an `aws-extended` entry to `configure_connector.py`
with an IAM Role / Access Credentials branch.

## 0.5.64 (2026-07-31)

Intermediate ship iteration of the 0.5.65 native-MCP config work; superseded by
0.5.65.

## 0.5.63 (2026-07-30)

Intermediate ship iteration of the 0.5.65 native-MCP config work; superseded by
0.5.65.


## 0.5.62 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.61 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.60 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.59 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.58 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.57 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.56 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.55 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.54 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.53 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.52 (2026-07-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.51 (2026-07-29)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.50 (2026-07-29)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.49 (2026-07-29)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.48 (2026-07-29)

**Triage deliverable card now lands when the agent enriches via native MCP.**
Native MCP tools (`mcp_soc__*`, `mcp_fortisiem__*`) wrap output as
`{status:"success", result, error:null}` -- no `ok` field, empty connector/op --
so the run_op-shaped `_ioc_extract` was blind to them. On a box with the
soc/fortisiem MCP surface enabled (.159) the model prefers the shorter native
path, so a correct investigation produced NO `info_card`/`ioc_card` and every
triage row failed with "no deliverable". Two fixes: (A) `_ioc_cards` reads
native-MCP results (`_native_mcp_ioc_records`, reusing the generic
score/verdict/tags scanner) into per-indicator ioc_cards; (B) `_wire_transcript`
appends a `triage_summary` `info_card` from the final verdict text when a
native-MCP turn still produced no deliverable card (scoped to native MCP so it
never fires on build turns). 8 regression tests.


## 0.5.47 (2026-07-28)

State-derived-intent Phase 1 + persona substrate P0 (shipped to .206). Same code
as 0.5.46; deploy bump for the .206 box.


## 0.5.46 (2026-07-28)

**Persona substrate (P0):** personas get a first-class, grid-editable
`assistant_personas` module mirroring the skills module. `resolve_profile_status`
reads the module FIRST, falling back to the Key Store, so existing personas
resolve unchanged. `setup_assistant_skills.py` gains the module create +
`--migrate-keystore-personas` pass (reuses the read-path (de)serializers).

**Page as a soft prior (Phase 1):** one `classify_page` affordance taxonomy in
`triage_sources.py` -- the frontier prior, the MCP surface, and the default
persona all key off ONE class. `_resolve_intent` now treats `intent` as a soft
override: an explicit task intent wins, an absent intent derives the prior from
page facts (`page` + `in_playbook_editor`, added to `ChatTurnParams`). Authoring
page -> build (C5 wall intact), else triage. Shipped to .159.


## 0.5.45 (2026-07-28)

Deploy bump (no code change): shipped the 0.5.43 block-beat approval-card fix to
the .206 GA box.


## 0.5.44 (2026-07-28)

Deploy bump (no code change): shipped the 0.5.43 block-beat approval-card fix to
the .159 box.


## 0.5.43 (2026-07-28)

Mutating MCP tools now surface a tier-3 approval card instead of auto-running.
The dynamic-tool materializer tiers MCP tools per SERVER, so a gateway like `soc`
that mixes reads (get_alert) and mutations (block_indicator) is marked read_only
and every tool - INCLUDING block_indicator - lands at tier-1 auto-run: containment
executed with NO approval card, defeating the tier-3 gate. `_gate_mutating_mcp_tools`
now elevates the operator-declared mutating tools to tier-3 in TOOL_TIERS so
dispatch routes them through the approval card, while read tools stay auto (smooth
triage/hunt). Classification is EXPLICIT config - each `mcp_allowlist[server]` rule
may carry `"mutating_tools": ["block_indicator", ...]` (bare names) - never a name
heuristic (the FortiSOAR MCP gateway exposes no read-only/destructive annotations).
Fail-open, idempotent, only ever raises a tier. 5 tests.

## 0.5.42 (2026-07-28)

NOC / FortiManager hardening (GA-demo C1 fixes). Two production defects in the
FMG device-posture tools are closed:

- Multi-config `run_op`: when the FMG connector has several configurations and
  none is flagged default (GA `fortinet-fortimanager-json-rpc` has 3), a call
  with no explicit config sent the framework's `get_default_config` sentinel and
  400'd (`INTEGRATION-12: Could not find a connector configuration`). `_fmg_config()`
  now resolves a usable config id up front (default/sole, else first configured),
  cached per-process; "" in sim/offline so `run_op`/fixtures still drive.
- Device-type category error: the agent applied FMG device-posture tools
  (device-status / HA-status / policy-package status) to an EDR endpoint host FMG
  does not manage. `_fmg_require_managed()` now gates the device-scoped tools on
  the FMG managed-device inventory (`{name, serial, ip}`), so a non-managed host
  is a CLEAN SKIP (`ok:True`, "use EDR/SIEM tools for an endpoint") instead of a
  live call that 400s/misses and burns the turn's error budget. Fails OPEN when
  the inventory can't be read, so a transient FMG hiccup never hides a real device.

17 noc tests green.

## 0.5.41 (2026-07-28)

SOC Assistant Monitor v2 Phase 1: per-tool-call audit detail. The audit trail now
shows what the agent actually did, not just aggregate per-turn token counts. New
`agent_tool_calls` table (FK to `agent_usage.id` ON DELETE CASCADE; turn,
tool_call_index, tool_name, params_json, result_status, result_summary,
latency_ms). `record_agent_usage` returns the row id; `record_agent_tool_calls`
inserts the linked rows; `list_agent_usage` nests them per row as
`tool_call_detail` via one batched fetch (no N+1). Per-call tokens/cost are
intentionally omitted - the framework reports totals per round-trip, not per call,
so the round-trip totals stay on the parent `agent_usage` row.

## 0.5.40 (2026-07-28)

SOC Assistant Monitor v2 Phase 2: live agentic sessions panel. New
`list_active_sessions` op gives visibility into what is running right now. New
`chat_sessions` table (descriptive metadata only - record_uuid, record_module,
current_prompt, started_at, updated_at) plus `upsert_chat_session`. Status is
DERIVED at read time from `turn_progress` (in-flight turn -> active) and
`suspended_sessions` (parked -> waiting_approval, wins over active), else idle -
no stored status column, so it cannot drift. Best-effort upsert hooked at the top
of `chat_turn`; op registered in `_LIVE_OPERATIONS` + `_MOCKABLE` + info.json.


## 0.5.39 (2026-07-28)

Scheduled Agent Tasks + SOC Assistant Monitor -- 10 new connector operations for recurring agent runs and usage/audit monitoring. Includes `storage.scheduled_tasks` and `storage.agent_usage` tables, runner-playbook YAML compiler, and `log_llm_activity()` dual-write (POST `/api/3/llm_activity_logs` + sqlite) wired into the `chat_turn` telemetry hook. Live-verified on .159: 3 turns seeded, 16.6k tokens tracked, 1 scheduled task created and run via `run_scheduled_task_now`.


## 0.5.38 (2026-07-28)

New `fortiai-proxy` LLM provider -- drive the agent loop against the
on-appliance FortiAI gateway. No external API key, no egress.

- **Feature (`llm_provider`):** a third option in the LLM Provider dropdown.
  `fortiai-proxy` uses the on-appliance `fortinet-fortiai-proxy` connector's
  `agent_chat_completions` operation (reached via the crudhub loopback at
  `/api/integration/execute/`). No sub-fields to configure -- the proxy ignores
  the reasoning-profile `config` param and the `model` param is cosmetic
  (echoed, not routed), so the appliance's default LLM entitlement is the
  sole backend. Suitable for air-gapped or egress-restricted boxes.
- **Non-streaming:** the proxy has no SSE support, so the provider makes one
  HTTP call per LLM round-trip. The user sees no text until the full response
  arrives (sub-second for short answers; 10-20s dead air for long triage
  narratives). Tool calls are singular (one per response) and round-trip via
  flattened-text messages -- the proxy rejects structured `tool`-role blocks.
- **HITL parity:** identical tier-3+ approval gating, self-repair, and
  forced-assessment machinery as the `anthropic` and `openai` providers. The
  provider protocol is the loop boundary; only the wire format differs.
- **Health check:** `health_check` reports `llm_provider: "fortiai-proxy"`
  with `llm_key_configured: true` (no key needed -- on-platform) and skips the
  external endpoint probe (the crudhub loopback is always available to a
  worker on the same appliance).
- Framework: new `fsr_playbooks/llm/fortiai_proxy_provider.py` +
  factory registration (`get_provider("fortiai-proxy")`). 23 new tests in
  `web/tests/test_fortiai_proxy_provider.py`; 0 regressions across the
  992-test framework suite.
- See `docs/plans/FORTIAI_PROXY_PROVIDER_PLAN.md` (Phase A findings) for the
  full wire contract and the live-confirmed shapes.

## 0.5.37 (2026-07-27)

Manual-input form now renders on the tier-3 approval → resume path.

- **Fix (`_wire_transcript`):** when a record-action `run_playbook` is tier-3
  approval-gated, the `run_playbook` `tool_use` is in the PRIOR turn (before the
  gate), so the resume turn's transcript opens with an ORPHAN `tool_result` whose
  tool name can't be resolved from the current turn. The manual-input card
  synthesis was gated on that name and skipped the orphan -- so a paused run only
  ever produced a "what value?" narration, never the fillable `manual_input`
  card. An orphan (unknown-name) `tool_result` is now allowed through to
  `_manual_input_card_from_awaiting`, which self-validates on
  `code==awaiting_input` + fillable fields. Tests: two in `test_manual_input_card.py`
  (orphan awaiting → cards + truncates; orphan non-awaiting → no card).
- **Robustness (`_awaiting_or_slow`):** briefly poll `_pending_input` when a run
  is `awaiting` so the form is reliably attached even if the `manual-wf-input`
  record lags the pause; a ready form still settles at once. Tests in
  `test_tools_playbook.py`.
- Live-verified on the widget: run_playbook → approval → `manual_input` form
  (fillable `note`/`approver` fields, wired to the run IRI) for 1-gate and 2-gate
  playbooks. Repro playbooks seeded via `scripts/seed_manual_input_playbook.py`.

## 0.5.36 (2026-07-27)

Intermediate build; superseded by 0.5.37.


## 0.5.35 (2026-07-27)

Intermediate same-day ship iteration between the Assistant Skills build (0.5.34)
and the scheduled-tasks + monitor build (0.5.36); no standalone behaviour change.
Superseded by 0.5.36/0.5.37.


## 0.5.34 (2026-07-27)

Cumulative build (supersedes the 0.5.30-0.5.33 same-day ship iterations below).

- **Assistant Skills read-path.** New `fsr_soc_triage/skills.py` loads enabled
  house-rule records from a custom `assistant_skills` module (module-scoped plus
  globals, highest-priority first) and renders them as a `## Learned house rules`
  prompt section framed as overrides. `operations._resolve_profile` →
  `_apply_skills` folds the block onto the resolved persona prompt once, so every
  downstream path (ungrounded, grounded, resume) inherits it. Fail-open: a missing
  `assistant_skills` module or any read error leaves the prompt unchanged. Skills
  shape HOW the generic tools are used; the tools stay generic (see the
  `list_module_playbooks` docstring note). Tests: `tests/test_skills.py` (15).
- **Lever 2 -- run-vs-author routing.** On a build turn with no open playbook, a
  provider-agnostic meaning classifier (`_run_mode_active` → `classify_run_or_author`)
  narrows the advertised tools to the run-mode slice so the authoring surface
  (verify/compile/emit) is not offered when the analyst wants to RUN an existing
  playbook. Gated to directive build turns; fail-open to normal build.
- Pins `fsr-playbooks==0.5.7`.

## 0.5.33 (2026-07-27)

Intermediate same-day ship iteration; superseded by 0.5.34.


## 0.5.32 (2026-07-27)

Intermediate same-day ship iteration; superseded by 0.5.34.


## 0.5.31 (2026-07-27)

Intermediate same-day ship iteration; superseded by 0.5.34.


## 0.5.30 (2026-07-27)

Intermediate same-day ship iteration; superseded by 0.5.34.


## 0.5.29 (2026-07-27)

First ship after 0.5.28, carrying four triage/authoring fixes (commits
`4455282`, `d1c6684`, `7f59335`, `c490bbe`). These accumulated through the
same-day 0.5.30-0.5.33 iterations into the 0.5.34 cumulative build.

- **Harden SIEM/enrichment tools vs model arg shapes; steer FortiGate block
  method** (`4455282`). Three live-found defects on the FortiGate IP-block arc:
  `siem_search_*`/`siem_events_for_incident` crashed on a string `limit`
  (`'<' not supported between int and str` at `min()`) -- coerced via `_as_int`;
  the IOC-enrichment card builder crashed on stringified `params`
  (`'str' has no attribute 'get'`) aborting turn finalization -- now tolerated;
  and an anti-confabulation hard-rule prefers FortiGate's self-contained
  Quarantine-Based block mode over an invented Policy-Based policy name.
  Regression tests each; verified 2/2 live on 8.0.
- **Refuse un-runnable playbooks pre-card; deliverable sort; field naming**
  (`d1c6684`). `run_playbook` gains a pre-card validator so an invented name is
  refused (`unknown_playbook` + real candidate list, or `playbook_inactive`)
  BEFORE the tier-3 gate turns it into an approval card -- no human is asked to
  approve a no-op; strictly fail-open. `search_module_records` retries a
  query-API-rejected sort client-side (`sorted_client_side`) instead of falsely
  claiming the field is unsortable.
- **No unprompted playbook offer off an alert/case/war-room page** (`7f59335`).
  `uiIntent` defaults to `triage` on every non-editor mount, so intent alone
  couldn't distinguish triaging an alert from viewing a device record. Adds
  `TRIAGE_RECORD_MODULES` (alerts/incidents/warrooms) and a prompt directive
  suppressing the UNPROMPTED "save this as a playbook" offer everywhere else.
  Narrow by design: an explicit "build a playbook from this" still works on
  every module.
- **Commit-safe FortiEDR + FortiGate config helper scripts** (`c490bbe`).
  Mirror `configure_fortisiem.py`: creds from env (`EDR_*`/`FG_*`), idempotent
  upsert + healthcheck, enabling the demo containment beats (FortiEDR isolate
  host, FortiGate block IP).


## 0.5.28 (2026-07-26)

Fix: register the `list_mcp_servers` op in the `_LIVE_OPERATIONS` dispatch table
(0.5.27 added it to info.json + operations.py but not the registry, so the box
returned `unknown operation: list_mcp_servers` at call time). Adds a drift guard
(`test_every_info_json_operation_has_a_handler`) asserting every info.json
operation resolves to a registry handler, so this class can't ship again.


## 0.5.27 (2026-07-26)

M2.1 fast-follow -- on-ask reach + MCP-server discovery.

- **On-ask reach for off-surface servers.** `chat_turn` accepts a
  `reach_servers` list -- MCP server keys to UNION into the page's default MCP
  surface for that turn -- so a server dropped by M2 per-page surfacing (e.g.
  `deepwiki` on an alerts page) is reachable when explicitly requested, without
  loosening the default lean surface. Deterministic (state→set): `reach_servers`
  is unioned in `effective_surfaced_servers`/`apply_server_surfacing`; moot on an
  unrestricted page. Threaded through `_advertised_tools` and mirrored on
  `list_agent_tools` for introspection.
- **`list_mcp_servers` op -- discovery.** Enumerates the materialized external
  MCP servers and their tools, annotated with whether each is on a given
  `module`'s default surface (`on_surface`), plus the connector's configured
  `allowlist_keys`. Answers "what MCP is connected, and what does this page
  surface?" for operators wiring `mcp_allowlist` and for verifying the per-page
  surface -- no real turn needed. Read-only, fail-open.
- Tests: +11 (`test_surfacing_oracle.py` reach/inventory, `test_list_agent_tools.py`
  op-level reach + discovery + param coercion).


## 0.5.26 (2026-07-26)

M2 deterministic per-page MCP surfacing + capability-dedup (state-derived-intent
plan, Workstream M2). The advertised MCP surface is now a pure function of the
page: an affordance-class map (`AFFORDANCE_SERVERS`/`MODULE_AFFORDANCE`) surfaces
only the servers a page needs, and an MCP tool that duplicates a curated
first-class helper is dropped in favor of the curated one (`CAPABILITY_DEDUP`,
curated wins). Live-verified on 8.0.0: `alerts`→{fortisiem,soc},
`workflows`→{deepwiki}; pre-M2 every server appeared everywhere.


## 0.5.25 (2026-07-26)

M2 step 4 -- persona MCP block. A per-module Key Store persona may carry an `mcp`
block (`servers`, `capability_dedup`) that overrides the affordance default; the
persona allowlist now COMPOSES with the afforded MCP surface (union) instead of
replacing it, so a persona needn't re-list every materialized MCP tool by name.


## 0.5.24 (2026-07-26)

Pin `fsr-playbooks==0.5.3` -- external-MCP OAuth2 client_credentials
auto-refresh. An `mcp_allowlist` rule can now carry
`auth: {oauth2: {token_url, client_id, client_secret, verify}}`; the
materializer mints, caches, and refreshes the bearer itself (re-mints within
60s of expiry), so a short-lived token (e.g. FortiSIEM's) never goes stale
mid-session. Proven live on 159: `mcp_fortisiem__get_incidents_by_entity`
authenticated via the self-refreshing bearer with no static token in the config.


## 0.5.23 (2026-07-26)

State-derived tool frontier (state-derived-intent-and-tool-slicing plan,
Phase 2) + the MCP dispatch-seam unification (Workstream M3 follow-up).

- **Intent is now a PRIOR, not a one-way latch.** `_afforded_drop_set(prior, …)`
  computes the advertised tool slice from session state each turn: a
  triage-prior session reaches the authoring/enhance surface -- without a page
  flip -- once its state affords it (`authoring_active` = has_trace ∨
  has_open_playbook un-drops the authoring core; `has_trace` un-drops
  `build_playbook_from_trace`; `has_open_playbook` un-drops the enhance
  frontier). Bare/introspection calls default `authoring_active=False`, so the
  slice is byte-identical to the pre-Phase-2 per-intent slice. The build prior
  is unchanged (the C5 authoring wall holds until the Phase 3 disposition
  prompt). Safety never came from tool absence -- writes stay tier-3 /
  `confirm_mode="approve"` behind `_write_gate`.
- **One dispatch seam for the tool surface.** `_advertised_tools()` is now the
  single path that applies `mcp_allowlist` then slices, shared by the chat
  turn, resume, and `list_agent_tools` introspection -- so materialized `mcp_*`
  tools can't drift between paths.
- Test honesty: regenerated the turn-context goldens and reworked the
  write-tool slice assertion to the `4435be4` invariant (writes are
  triage-reachable / build-excluded, gated by tier-3+approve, not by absence).
- Added the Phase-0 `interleave_triage_author_enhance` scenario + oracle.


## 0.5.22 (2026-07-25)

Pin `fsr-playbooks==0.5.2` -- external MCP server support (`list_tools_at` /
`call_tool_at`). Enables an admin `mcp_allowlist` rule to carry a `url`+`auth`
for an off-appliance MCP server. (Proven live on GA 159: gpt-4.1-mini called
DeepWiki's public external MCP tool over the internet.)


## 0.5.21 (2026-07-25)

On-box adapter external-server path -- `_CrudhubMCP.list_tools_at` /
`call_tool_at` with a shared `_session_call` core (external path bypasses
CS-HMAC, uses the rule's own headers) + `follow_redirects=True` (external
FastMCP servers 307 on a trailing slash).


## 0.5.20 (2026-07-25)

Drop the enhance write path (`ENHANCE_ONLY_TOOLS`) from a from-scratch CREATE
slice when there is no open playbook -- a drifting model was ending a create at
`awaiting_enhancement_offer` instead of delivering a new playbook via
`emit_playbook_offer`.


## 0.5.19 (2026-07-23)

Manual-input resume robustness -- two fixes for the "approved the gate, then
nothing happens" report, both the same imperative-hijack class as the T2
containment drift (the fix is to remove what invites the wrong call, not to add
a guard downstream).

- **Resume-loop stopped.** After a gate was forwarded, the bridge message told
  the model to "continue and report what happens next" -- which it read as an
  instruction to continue the *run*, calling `resume_playbook` a second time and
  reaching for the nearest id in context (the gate id, not the run pk). Live on
  206: `resume_playbook(run='388')` where 388 was the `manual_input_id` and the
  run was 6839 → 400 "Select a valid choice", surfacing to the analyst as
  "approved, then nothing". The bridge now states the resume leg is complete and
  forbids a second `resume_playbook` for that gate, pointing at
  `get_playbook_run` for status instead.
- **Wrong-id error re-mapped.** A pk that isn't a workflow run reaches the
  platform as `manual-wf-input/?workflow=<pk>` and is rejected as an invalid form
  *choice* -- a message that describes a field, so it read as "the analyst picked
  something invalid" when the real fault was a wrong identifier. `resume_playbook`
  now detects that signature and returns `bad_run_reference`, naming the confusion
  (top-level `run_pk` vs nested `awaiting.manual_input_id`). Detected from the
  platform's own rejection, not a pre-flight existence check -- `get_execution`
  returning nothing means "no status snapshot", not "no such run", so pre-checking
  would refuse valid resumes on a transient read.


## 0.5.18 (2026-07-23)

Tool-contract fixes found by grepping stored chat sessions for analyst `FIXME:`
notes and failed outcomes. Common theme: a tool succeeded while handing the
model less than it needed, and the model papered over the gap.

- `list_module_playbooks` no longer offers **disabled** playbooks as runnable.
  Discovery filters `isActive` server-side; `include_disabled=True` gives an
  audit view. The scan cap is now disclosed rather than reading as a complete
  menu.
- `search_module_records` gained `fields=` (echo fields onto each hit -- kills
  the `get_record` fan-out a summarize-these-records turn needed) and `sort=`
  (query-API ordering; every spelling normalizes to one shape). A sort on a
  nonexistent field now names the `createDate`/`modifyDate` convention instead
  of returning an opaque 500.
- Every search result reports `total` + `showing`, and flags `truncated` with an
  instruction to disclose it -- a 59-record match summarized from its first 5 was
  being presented as complete.
- `run_playbook` refuses a **recordless record-action run** (`record_required`)
  instead of firing one that dies on `input.records[0]`, and a followed run that
  didn't finish now carries `failure: {failing_step, error_message, pk}` from the
  step-detail fetch (the run list always reports `error_message: null`).


## 0.5.17 (2026-07-23)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.16 (2026-07-23)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.15 (2026-07-23)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.14 (2026-07-23)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.13 (2026-07-23)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.12 (2026-07-23)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.11 (2026-07-23)

Pins fsr-playbooks==0.4.46: `verify_playbook` no longer flags
`connector_config_missing` for config-less built-ins (`cyops_utilities`, the
FortiSOAR Utilities connector that also backs `no_op`/`stop`/`end`). Its ops run
with an empty `config: ''` and it carries no saved configuration, so a step that
pins no config binds fine -- the check previously read its zero `connector_configs`
rows as "unconfigured" and fired a required fix. Observed live: a build turn
burned ~6 tool calls fighting the false gate before abandoning verify for
`dry_run`. Removing this friction serves model-agnostic authoring robustness.


## 0.5.10 (2026-07-23)

Pins fsr-playbooks==0.4.45: `get_op_schema` now teaches connector-output
binding, not just input params. A weak authoring model bound a connector op's
whole envelope (`vars.steps.X.data`) into a record field -- rendering as
`Array`/`[object Object]` instead of the scalar -- because the `.data.<field>`
envelope rule lived only in `get_step_type("connector")`, which the build
sequence skips. `get_op_schema` now surfaces the op's output field names plus an
explicit binding hint (e.g. `convert_periodic_time_to_minutes` → `minutes`), so
the author writes `vars.steps.<name>.data.minutes`. Verified live on GA: the S3
connector-action authoring eval (haiku arm) went 0/5 → passing.


## 0.5.9 (2026-07-22)

Pins fsr-playbooks==0.4.44, which forces enhance-mode delivery: when the model
verifies an edit to the open playbook but ends the turn without calling
emit_enhancement_offer (narrating "call emit_enhancement_offer …" instead of
calling it), the loop now forces the offer via a tool_choice-pinned round and
applies the verified bytes. Closes the narrated-not-delivered gap on the
branch-rewire case (1/4 → 5/5 delivered live).


## 0.5.8 (2026-07-22)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.7 (2026-07-22)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.6 (2026-07-22)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.5 (2026-07-22)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.4 (2026-07-22)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.3 (2026-07-22)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.2 (2026-07-22)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.1 (2026-07-22)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.5.0 (2026-07-21)

Framework pin → **fsr-playbooks 0.4.38**, which carries two fixes:

- **A ```yaml fence is a WRITE.** The build prompt used to demand the complete
  revised playbook as the last fence on *every* turn with a playbook open, with
  nothing scoping it to a change request. Since the widget saves the last fence
  over the open record, that made an explain/question turn ("Don't change
  anything") emit an unrequested WRITE. Read-only turns now answer in prose.
- **`for_each` survives a decompile.** Pulling a looping playbook back from a
  box dropped the loop, so a one-field edit silently rewrote "run this for every
  record" into "run it once".


## 0.4.94 (2026-07-21)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.93 (2026-07-21)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.92 (2026-07-21)

Connector-wide ground-truth hardening against confabulation (applies to every
intent and every persona, not one persona):

- Record-context injection now fences and labels `example*`/`sample*` fields
  (e.g. `exampleOutput`) as "ILLUSTRATIVE EXAMPLE -- not live", so the model
  can't report sample data as a real result.
- Every turn's system prompt gets connector-wide invariant rules: never present
  example/sample data as real; tool-verify any factual/live claim; never guess.
- `get_record(full=True)` over the size cap now returns the pruned projection
  (every pivotable field) instead of a head/tail byte-chop fragment.
- `search_module_records` accepts the list-of-clauses `filters` shape and coerces
  it to a dict instead of hard-erroring.


## 0.4.91 (2026-07-20)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.90 (2026-07-20)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.89 (2026-07-20)

- **API-KEY auth for the on-box native-MCP adapter (preferred over bearer).**
  Corrects the `soc` 401 root cause: the MCP gateway replays the incoming
  `Authorization` header *verbatim* onto downstream `/api/3` calls, and CS-HMAC
  is signed for a single URI (`/mcp/soc/`) so the replay fails signature
  validation → 401. A FortiSOAR **API key** (`Authorization: API-KEY <key>`) is
  not URI-bound and carries the api-key user's identity, so it survives the
  replay -- no password, no token minting, no TTL. New optional `soar_api_key`
  config field; takes priority over `soar_username`/`soar_password`. (The prior
  0.4.87 "HMAC carries no forwardable identity" framing was imprecise -- the real
  issue is HMAC's URI-binding vs. the gateway's verbatim replay.)

## 0.4.88 (2026-07-20)

- **`list_agent_tools` op -- runtime introspection of the agentic tool surface.**
  A read-only operation that returns the exact tool slice a real turn advertises,
  per intent (`triage`/`build`) and optional mounted-module Key Store persona,
  with each tool's approval `tier`, `confirm_mode` and provenance (`safe` /
  `mcp:<server>` / `widget`). Unlike the static `SAFE_TOOLS` registry it reflects
  box truth: it reuses `_tools_for_persona` + `_resolve_profile` (so it cannot
  drift from the turn's own slice) and materializes native-MCP-gateway tools from
  the connector's `mcp_allowlist` for full fidelity. Params:
  `{intent?, module?, include_mcp?, verbose?}`.

## 0.4.87 (2026-07-20)

- **Bearer-auth the on-box native-MCP adapter for `api_call` servers.** Fixes the
  `soc` 401: record-fetch / playbook-trigger tools need a forwardable downstream
  identity, which HMAC alone doesn't carry.

## 0.4.86 (2026-07-20)

- **§3A materializer robustness.** Dynamic-tool-surface materializer hardening on
  `fsr-playbooks==0.4.35`.


## 0.4.85 (2026-07-19)

- **§2.4 build-completion salvage.** A free-form NEW-playbook build turn that
  narrated its finished YAML instead of calling `emit_playbook_offer` used to
  dead-end with no Deploy button. `_salvage_build_offer` now synthesizes the same
  `playbook_offer` card deterministically (from `last_assistant_yaml`, via the
  framework's `_offer_from_yaml`) so every build turn lands on a Deploy affordance.
  Editing an OPEN playbook stays exempt (raw fence + Save updates in place).


## 0.4.84 (2026-07-19)

- **Framework bump to `fsr-playbooks==0.4.34`.** Pulls in §2.3 **tool-output
  budgeting**: `shrink_history` now caps oversized tool_result bodies (e.g.
  `verify_playbook` ~47KB, duplicate enrichment ~40KB) to a head+tail preview,
  keeping the freshest result full -- so long tool chains stop blowing the context
  window. Deterministic (a clipped body stays byte-stable turn over turn).
- Also carried in 0.4.34: **manual_input approval gates** -- authoring
  `is_approval: true` now re-points the step to the distinct `ApprovalManualInput`
  step type instead of shipping a plain ManualInput.


## 0.4.83 (2026-07-19)

- **patch_proposal apply-patch accept path (feature).** Completes the
  `emit_patch_proposal` feature: the propose half was already wired (0.4.79) but
  its framework tool only landed in fsr-playbooks 0.4.32 (shipped 0.4.80), so the
  emitter is live on-box now. This adds the **apply** half -- on accept of a
  patch_proposal chat card the connector applies the fix to the OPEN playbook via
  `update_playbook` (snapshot-first, fail-closed PUT -- the same Save path the
  designer's own ```yaml fence uses).
  - `chat_resume` routes `reply_tool=apply_patch` to `_resume_apply_patch` before
    the action_card path; splices a deterministic `apply_patch`
    tool_use/tool_result + analyst summary (outage-survivable). Honest failure
    reporting: compile is fail-closed; an update failure surfaces the code and
    leaves the playbook unchanged.
  - **Snippet-aware apply (box-proven).** A patch_proposal's `after_yaml` is a
    minimal snippet (the changed lines, for a readable diff), NOT a whole
    playbook -- so it can't be compiled on its own. `_apply_patch_after_yaml` now
    uses a whole-doc `after_yaml` directly when it compiles, else reads the live
    open playbook (`decompile_playbook`) and splices `before_yaml` → `after_yaml`
    into it (exact substring, then a whitespace-tolerant line match), failing
    honestly (never a silent wrong edit) when the snippet is ambiguous/unfound.
  - patch_proposal cards are now persisted (`_persist_session_state._GATE_TYPES`)
    and `storage.save_card` keys them by `proposal_id`, matching the id the
    widget resumes with.
  - `ChatResumeParams` gains typed `reply_tool`/`workflow_iri`/`workflow_uuid`
    (chat_resume carries no entity, so the widget passes the open playbook's IRI).

## 0.4.80 (2026-07-19)

- **Connector-output-path lever (S3 build persona), via fsr-playbooks 0.4.32.**
  Three framework changes so a connector-action playbook's op output reliably
  flows onward into a downstream step:
  - **Compile-time auto-correct**: `vars.steps.<connstep>.<x>` is rewritten to
    `.data.<field>` when unambiguous (a real `.data` subkey, or `.result` /
    `.outputs` on a single-output op). A connector result is an envelope
    (`{data, status, message, operation}`); the model frequently drops the
    `.data`, which renders empty. Warn-and-fix on the Deploy compile path.
  - **Safe live-probe grounding**: the build verify path now defaults
    `live_probe` on, grounding a connector op's real output shape by executing
    it -- gated to `op_safety=='safe'` steps only, with `run_op` independently
    refusing non-safe categories without confirm, so no mutating op runs.
  - **op_safety**: pure-compute transform verbs (convert/parse/format/…) now
    classify safe.
  Live on 206: S3 build-persona eval 0/3 → 2/3 (the output-path failure mode is
  resolved; the residual is a distinct connector-availability misread).


## 0.4.79 (2026-07-18)

- **patch_proposal card wired through (feature).** The framework's new
  `emit_patch_proposal` emitter is now mapped to the widget's `patch_proposal`
  event: the returned card is spliced into the wire transcript and the turn
  halts as `awaiting_patch_proposal` so the build agent can offer a before→after
  value-level fix to one step/field of the open playbook, which the analyst
  accepts/rejects inline. Accept-side apply (`reply_tool=apply_patch` resume
  dispatch) is a follow-up increment.


## 0.4.78 (2026-07-18)

- **Deterministic diagnose-before-fix lever (fix).** The lever added in 0.4.77
  silently no-op'd: it gated on `why_did_playbook_fail`'s `ok` field, but a
  successful diagnosis of a FAILED run returns `ok=False` ("the run isn't
  healthy") -- so every real diagnosis was discarded. Now it gates on a resolution
  `code` + actual diagnostic content. With the fix live, the box's model reliably
  adds the missing key and the failed playbook runs (S6 3/3 on 206).


## 0.4.77 (2026-07-18)

- **Deterministic, transparent diagnose-before-fix for the build persona.** When
  the analyst reports a runtime failure on the OPEN playbook (e.g. "the last run
  failed, fix it"), the connector runs `why_did_playbook_fail` itself and folds
  the failing step + cause into the turn -- so even a weaker model that won't pick
  the diagnosis tool fixes the real defect instead of guessing, and we don't
  spend context making the model reach the tool. It streams a visible `activity`
  frame ("Checking why the last run failed…" → the failing step) so the analyst
  sees the connector consult the failed run -- the grounding is transparent, never
  hidden. Fires only on a build turn, on an open playbook, when the message reads
  as a failure report and a failed run is actually found.


## 0.4.76 (2026-07-18)

- **Build persona diagnoses a failed run before editing.** Pins
  `fsr-playbooks==0.4.30`: on any free-text "the last run failed, fix it" turn the
  build persona now calls `why_did_playbook_fail` first and fixes exactly the
  diagnosed defect, instead of guessing a cosmetic edit. The diagnose-first rule
  was previously trapped in the quick-action-chip section (ignored on normal
  turns). Surfaced by the S6 apply-the-fix eval scenario.


## 0.4.75 (2026-07-18)

- **Build persona can troubleshoot a failed run.** Pins `fsr-playbooks==0.4.29`,
  which exposes `why_did_playbook_fail` to the build tool slice and fixes the
  runtime-troubleshooting path (it was dead in this connector: it imported a
  `tools_triage` module that doesn't exist here, and `get_run_env` broke on
  pyfsr's typed `RunSummary`). `register_triage_tools()` now supplies the
  framework with the connector-owned `list_recent_failed_runs` via
  `set_failed_run_provider`. Live-verified on 8.0: `why_did_playbook_fail`
  returns `failing_step` + the runtime error (e.g. `insert_data() takes at
  least 2 positional arguments`) for a broken `create_record` playbook.


## 0.4.74 (2026-07-17)

- **Catch step-level keys the parser silently drops.** Pins `fsr-playbooks==0.4.28`,
  which flags any unrecognized top-level step key (e.g. a `create_record` authored
  with `module:`/`resource:`/`data:` as siblings of `type:` instead of under
  `arguments:`) as an `unknown_param` warning through `validate_yaml`/`verify_playbook`.
  Previously such a step compiled clean and crashed only at runtime
  (`insert_data() takes at least 2 positional arguments`). Live-verified on the box.


## 0.4.73 (2026-07-17)

- **manual-input SUBMIT (`respond_manual_input`) live-clean.** The card's submit
  leg forwards the analyst's values to the paused workflow run via pyfsr's typed
  `ManualInputAPI` -- a **no-owners `retrieve` → `resume`** rather than the one-call
  `answer()`. Through the on-box crudhub transport, the owner-scoped `answer()`
  retrieves `response_mapping.options` EMPTY and raises "exposes no response
  options" even though the run is resumable; the no-owners retrieve (the same path
  the pause-side card already uses) returns the options, so we mirror it: pick the
  primary option's `step_iri` and POST `mi.resume`. Live-verified on 8.0 inside the
  input-validity window: fresh trigger → pause → submit → the run advances past the
  gate to `finished`.
- Also carries the build-persona read/write fixes shipped in this cut: the
  `decompile_playbook` read op (F1) and the graft-by-name `update_playbook` write
  (F3) -- a one-field edit now PUTs in place (`method=put`, `changed=1 added=0
  removed=0`) and the edited playbook runs. Grounded S2 eval: 3/3 on 8.0.

## 0.4.72 (2026-07-16)

- Widget-facing **manual-input card rendering**. A `run_playbook`/`resume_playbook`
  result that PAUSED on a fillable form is rendered as a gating `manual_input` card
  that truncates the turn (`stop_reason: awaiting_manual_input`), with a generic
  two-level `dynamic_list` group→item picker. EMIT side live-verified on 8.0 (real
  `awaiting_manual_input` card with the actual group + device items).

## 0.4.71 (2026-07-16)

- Manual-input card scaffolding: `_manual_input_card_from_awaiting` +
  `_manual_field_to_widget` map a paused step's `awaiting` block (fields +
  response options) into the widget card contract, spliced into the wire
  transcript.


## 0.4.70 (2026-07-16)

- `resume_playbook` now shows + fills the paused step's FORM. `run_playbook`'s
  `awaiting_input` result carries an `awaiting` block with the manual-input's
  `fields` (from `input.schema.inputVariables` -- name/label/type) and response
  `options` (Ok/Cancel …), so the agent/analyst can see exactly what the run is
  waiting on. `resume_playbook` gains an `inputs` param: when supplied, the field
  values are submitted via pyfsr's `ManualInputAPI.answer` (fills fields + picks
  the option + resumes in one call); a button-only choice still goes through
  `approval`. Live-verified on 8.0: run → `awaiting_input` surfaces the
  "Choose a Manager and a Device" fields + Ok/Cancel options.


## 0.4.69 (2026-07-16)

- New persona-gated **`resume_playbook`** tool: continue a run `run_playbook`
  started that PAUSED on a manual-input / approval step (e.g. the validation
  playbook pausing to have a device selected). Thin wrapper over pyfsr's typed
  `PlaybooksAPI.approval(run_pk, decision)` -- picks a response option
  (`approve`=primary, `reject`=first non-primary, or an exact label) and resumes
  via the canonical `wfinput_resume` route. Agent-only + persona-gated (surfaces
  via a persona's `tools.allow`; excluded from `call_mcp_tool`).
- `run_playbook` follow fix: a record-action run's `/action/` task_id does NOT
  correlate via `log_list`, so following by task_id always timed out. Follow via
  pyfsr `wait_for_run` (by playbook) instead; when the run pauses it surfaces a
  new `awaiting_input` result carrying `run_pk` (read via `execution_history`) so
  the agent can `resume_playbook` it.
- `_live_crudhub._attach_pyfsr_apis` also attaches pyfsr's `ManualInputAPI` as
  `client.manual_input` (needed by `approval()`), so resume works on-box over the
  crudhub loopback. Live-verified on 8.0: run → `awaiting_input` → resume(reject)
  → `finished`.


## 0.4.68 (2026-07-16)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.67 (2026-07-16)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.66 (2026-07-16)

- `run_playbook`: fire record-context playbooks through the correct trigger route.
  A playbook whose trigger step is a record-action (`cybersponse.action`, the
  "Execute" list on a selected record) is now dispatched via
  `trigger_action(route, module, record_uuid)` when a `record` IRI is supplied,
  so the selected record actually populates `vars.input.records[0]`. Previously
  every run used the manual `notrigger` route, which left `input.records` empty
  and failed such playbooks with `list object has no element 0`. Manual
  (non-record-action) playbooks and record-less calls are unchanged. Also handles
  the `/action/` route's plural `task_ids` response when following the run.


## 0.4.65 (2026-07-16)

- Interim build of the `run_playbook` record-action trigger fix (superseded by
  0.4.66, which adds the `task_ids` follow handling).


## 0.4.64 (2026-07-16)

- Pin `fsr-playbooks==0.4.27` to activate persona-gated **tier-2 auto-run** for
  `run_playbook`: a playbook in the active persona's `run_playbook.auto` subset
  now auto-runs (no approval card) via the framework's
  `set_run_playbook_auto_resolver` hook. Playbooks in `allow` (not auto), or
  unlisted, still surface a tier-3 approval card.


## 0.4.63 (2026-07-16)

- Lookup-tool hardening: `_live_crudhub` recovers the real HTTP status (404 →
  clean `not_found`) and strips the loopback host; `search_module_records` gains
  a `field:value` equality filter (arg + parsed from `q`) alongside `$search`;
  genuine 404s enrich with nearest-module suggestions. New persona-gated
  `run_playbook` (advisory allowlist, human-approval gate), agent-only.


## 0.4.62 (2026-07-16)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.61 (2026-07-16)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.57 (2026-07-16)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.56 (2026-07-16)

- `health_check` now reports `p4_tool_output_contract` -- probes that the
  installed `fsr_playbooks.llm.tool_result` module (the P4 tool-output envelope
  contract, added in fsr_playbooks 0.4.24) is importable in the worker, so a box
  running a pre-0.4.24 wheel is visible up front instead of failing lazily
  inside `dispatch` on every tool call. Mirrors `c5_build_scoping.symbol_present`.


## 0.4.55 (2026-07-16)

- Persona spine unification: grounding/preflight/CaseState is now a
  persona-declared capability (`ResolvedPersona.capabilities.grounding`) instead
  of a hardcoded `triage` branch -- a Key Store persona opts in via
  `jSONValue.capabilities.grounding` and grounds on its own prompt (P3).
- Persona authoring ergonomics: `profiles.validate_persona_record()` lints a
  Key Store persona record (unknown tool/capability/field → actionable finding)
  and `docs/PERSONA_AUTHORING.md` documents the schema (P5).
- Pinned `fsr-playbooks==0.4.24` (the framework release carrying the P4
  tool-output envelope contract).


## 0.4.54 (2026-07-16)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.53 (2026-07-16)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.52 (2026-07-15)

- Build-turn tool slice: `build_playbook_from_trace` is dropped from the build
  slice until the session has a real recorded trace (`_session_has_trace`), so a
  from-scratch "create a playbook…" turn no longer reaches for it first and
  dead-ends on `empty_trace`. The triage→build handoff, which carries a
  populated trace, still offers it. `find_containment_actions` /
  `find_enrichment_actions` explicitly stay in the build slice (discovery).
- Pin `fsr-playbooks==0.4.23`: the hunt-floor guard now exempts
  `find_containment_actions` discovery in authoring turns (so a build agent can
  learn which op blocks an IP without the triage investigation gate; triage
  unchanged), and the build system prompt teaches native `create_record` /
  `update_record` steps vs connector operations (no `create_alert` connector op).


## 0.4.51 (2026-07-15)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.50 (2026-07-14)

- `resolve_persona` operation: resolves a module's Key Store persona UI
  framing ({found, label, ui}) for the widget, reusing the same
  `_resolve_profile` path the turn uses. Fail-open to {found: false}. The
  widget calls it as a fallback when its direct in-browser `/api/3/keys`
  read is 403-gated, and for `bind_modules` personas. `Profile` gains
  optional `label`/`ui` (parsed from `jSONValue`). Live-verified on 8.0.


## 0.4.49 (2026-07-14)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.48 (2026-07-13)

Redeploy -- no connector runtime change vs 0.4.47 (C5 stays live, `update_playbook`
via `PUT …?$relationships=true` intact). Carries the new `make bump-framework`
pin-automation tooling (build/dev only; `scripts/bump_framework.sh` +
`preflight_framework.py --version`). Rebuilt with the verified 0.4.22 wheel bundled.


## 0.4.47 (2026-07-13)

**C5 fully live + release-process guardrails.**

- **fsr-playbooks pin 0.4.19 → 0.4.22** (framework released to PyPI), so C5
  build-slice tool scoping (`TRIAGE_ONLY_TOOLS`) is now ACTIVE on-box, not just
  present. Live-verified: `health_check.c5_build_scoping = {symbol_present: true,
  triage_only_count: 19}` (the connector's hunt tools extended the set), and
  `update_playbook` via platform execute still edits in place (`method="put"`).
- **Build preflight guard** (`scripts/preflight_framework.py`, run by `build.sh`):
  statically verifies the pinned fsr_playbooks wheel defines every symbol the
  connector imports from it. A producer/consumer skew now fails the BUILD, not the
  box -- this is the guard for the 0.4.43 outage (registry.py imported
  `TRIAGE_ONLY_TOOLS` absent from the pinned 0.4.19 wheel).
- **build.sh hardening:** a wheel-download failure is now a hard error (opt out
  with `ALLOW_OFFLINE_BUILD=1`), and wheel materialization uses a pip-capable
  interpreter (the `make ship` PATH puts the pip-less uv venv first, which had
  silently skipped bundling the wheel).
- **health_check** reports `c5_build_scoping` for on-box visibility of a framework
  version skew.


## 0.4.46 (2026-07-13)

**Fix: a stale framework wheel no longer takes down the whole live surface.**
`register_triage_tools` hard-imported `TRIAGE_ONLY_TOOLS` (added in fsr_playbooks
0.4.20 for C5). Against the pinned **0.4.19** wheel bundled on-box the import
raised, breaking the ENTIRE `fsr_soc_triage` package import -- which cascaded into
the crudhub bridge (`_ensure_probes_bridge`'s `from fsr_soc_triage import
_live_crudhub` inherited the ImportError and silently returned unbound), so every
live op (`push_playbook`, `update_playbook`, and the triage tool surface) returned
`crudhub_unavailable` when invoked via `/api/integration/execute`. Now imported
defensively: when the symbol is absent, C5 build-slice scoping simply doesn't
extend and the live surface stays up. **Full C5 scoping still needs the framework
pin bumped to 0.4.20** (follow-up). Live-verified on 8.0: `update_playbook` via
platform execute now edits in place (`method="put"`), `live_crudhub_available: true`.

## 0.4.44 / 0.4.45 (2026-07-13)

Diagnostic builds: `health_check` now reports a `crudhub_transport` block (which
loopback imports resolve in the current worker + the full `fsr_soc_triage`
import-chain error) -- this is what pinpointed the 0.4.46 root cause. Read-only.


## 0.4.43 (2026-07-13)

Three-pillar "demoable" push (Tracks C1/C2/C5) -- first shipped build carrying
the C-work (0.4.42 was bumped for the materializer follow-up but deployed before
these landed):

- **C2 -- `update_playbook` modify-in-place (live-verified on 8.0).** The op now
  performs a real in-place playbook edit via the designer's own Save call:
  `PUT /api/3/workflows/<uuid>?$relationships=true&$versions=true` with a
  `preparePlaybookForSave`-shaped body (`_prepare_workflow_for_save`: strip
  `versions`, flatten `stepType`→IRI, stamp `lastModifyDate`). The
  `$relationships=true` query is load-bearing -- it cascades the PUT into the
  nested step/route/group records; without it the platform 409s treating the
  body's steps as create attempts. `import_jobs merge_replace` kept as a
  fallback. Returns `method: "put" | "import_jobs_merge_replace"`.
- **C1 -- per-quick-action build-prompt tailoring.** `ChatTurnParams.quick_action`
  (typed) threads the triage/build chip key into `_resolve_system_prompt`, which
  appends a BUILD-ONLY `# Active quick-action` directive routing each chip to its
  tools.
- **C5 -- build-persona tool scoping.** `tools_for_intent("build")` returns a
  scoped non-empty tool list (was an empty provider self-fill that leaked triage
  tools).


## 0.4.42 (2026-07-13)

Version bump ahead of the three-pillar push (materializer follow-up line); the
C1/C2/C5 work is described under 0.4.43, the first build that ships it.


## 0.4.41 (2026-07-06)

Connector-side workaround for a materializer activation bug: with the
``mcp_allowlist`` config field (0.4.40) declared, the materializer still stayed
dormant on-box -- framework <=0.4.20's ``configure()`` clobbers
``_client_factory`` with None when it's omitted, so the per-turn
``_apply_mcp_allowlist(config) -> configure(mcp_allowlist=al)`` call wiped the
on-box CS-HMAC adapter (``fsr_soc_triage._live_mcp.build_client``) that
``register_mcp_materializer()`` had set at import time. ``ensure_initialized``
then built the client from env creds -- which the on-box worker lacks (it rides
the crudhub loopback) -- so 0 ``mcp_soc__`` tool calls landed and the agent fell
back to the curated ``fmg_*``/``faz_*`` wrappers (``unknown_connector`` errors).

Fix here: ``_apply_mcp_allowlist`` re-passes ``client_factory=build_client``
alongside ``mcp_allowlist`` on every turn (redundant but harmless once the
framework's merge fix lands and is pinned). No framework bump needed.

## 0.4.40 (2026-07-06)

Declare `mcp_allowlist` in the connector config schema (`info.json`), so the
platform persists the field on the config record. The reading code
(`operations._apply_mcp_allowlist`) shipped in 0.4.39 but was inert without the
schema declaration -- writes to an undeclared field are stripped, so the
materializer stayed dormant in the live matrix even with the allowlist set.
With the field declared, the dynamic tool surface is activatable per-config
(no `FSRPB_MCP_ALLOWLIST` env var needed). Dormant by default (empty value).

## 0.4.39 (2026-07-06)

On-box MCP materializer client (`fsr_soc_triage/_live_mcp.py`): rides the
crudhub CS HMAC auth (`settings.APPLIANCE_PUBLIC_KEY`/`PRIVATE_KEY` via
`integrations.crudhub.HmacAuth`) against `localhost:8010`, so the dynamic-
tool-surface materializer (framework 0.4.19) works on-box without env creds
(the worker uses the crudhub loopback -- no `FSR_BASE_URL`/creds in its env).
Builds the streamable-HTTP transport via the `mcp` SDK directly (on-box pyfsr
is 0.7.9 -- no `pyfsr.api.native_mcp`). `register_mcp_materializer()` wires
`materializer.configure(client_factory=_live_mcp.build_client)` at import.
+ `make mcp-bridge-check` read-only probe. Dormant by default (empty allowlist)
→ no behavior change on upgrade. Live-verified on 8.0: 9 `mcp_soc__*` tools
materialize with full JSON schemas via the CS-HMAC adapter.

## 0.4.38 (2026-07-06)

Approval grant-mode (allow-once / always-allow) threaded into `run_op`
approval dispatch, with `session_id` carried through. `get_record` now retries
once on a stale session (401/403), and the remaining 401-on-stale-session
sites are covered. + 2 stale test fixes vs the P4 case-state phase tracking.


## 0.4.37 (2026-07-04)

Triage prompt -- sharp gate against source-specific wrappers when their
connector isn't configured. Adds a hard rule forbidding `fmg_*` / `faz_*` /
`siem_*` calls unless `list_configured_connectors` (called that turn) lists the
underlying connector (`fortinet-fortimanager-json-rpc` /
`fortinet-fortianalyzer` / `fortinet-fortisiem`); and clarifies that one
`unknown_connector` from a source-specific connector means every sibling wrapper
against it will also fail (gap-card once, stop). Live-verified on 8.0:
matrix T3 + T7 (FortiGate alert, FMG/FAZ unconfigured) went 3/3 unknown_connector
errors → 0, with an `emit_capability_gap_card` and a verdict.

## 0.4.36 (2026-07-04)

Triage prompt -- intelligent firewall/NOC investigation. Adds a "Firewall /
network-device alerts" subsection (identify the device from the record, check
configured capabilities first, pivot to FortiManager config/HA/policy-package +
FortiAnalyzer logs when configured, check ITSM for change windows, correlate
sibling detections + peer-IP threat intel), a device-alert capability-check
note in "What you do", and extends the `connector_not_configured` hard rule to
also cover `unknown_connector`. (0.4.37 sharpens the gate this introduced.)


## 0.4.35 (2026-07-04)

Bump `fsr-playbooks` pin 0.4.17 → 0.4.18 (framework `list_configured_connectors`
fix): an installed-but-inactive connector is no longer advertised as
`Available` -- `list_configured_connectors` now filters through the same
active-config source `run_op`'s preflight uses. Previously a connector with a
config record but no active config (e.g. whois-rdap) was listed `Available`
while `run_op` rejected it as `connector_not_configured`, so the agent called a
connector guaranteed to fail and wasted a turn. Also cleans
`find_enrichment_actions` / `find_containment_actions`, which call
`list_configured_connectors` internally.


## 0.4.34 (2026-07-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.33 (2026-07-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.32 (2026-07-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.31 (2026-07-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.30 (2026-07-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.29 (2026-07-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.28 (2026-07-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.27 (2026-07-02)

Triage prompt fix: investigate first, summarize once at the end. The agent
was opening triage turns with a full structured "Triage Summary" (indicator/
confidence/action table, MITRE, prioritized next-actions) built from the raw
record BEFORE running any lookup -- presenting a plan as a conclusion and
forcing a second summary once the real findings landed. A new "Order of
operations" section in `system_prompt_triage.md` requires lookups before the
verdict (a one-line plan orientation is allowed; a structured verdict is not),
and the Quick-action intents are marked opt-in (explicit request only) so
their record-only formats don't bleed into the default opening. Matrix T1 now
opens with a plan, runs 10 tools, then surfaces the verdict -- no premature
summary. (The 1 minor error is the pre-existing whois-rdap-not-configured env
gap, handled gracefully.)


## 0.4.26 (2026-07-02)

fsr-playbooks pin 0.4.16 → 0.4.17, pulling in:
- emit_action_card / emit_choice_card dispatch gate fixed to mirror the real
  registered signatures (was requiring `title`, a field neither tool accepts →
  every staged card bounced "title: Field required", 2 tool errors in matrix
  run 7 T1). A signature-sync guard now keeps the gate models aligned with
  the registered functions.
- (framework) conditional_refetch wired into the warming probes (G3).


## 0.4.25 (2026-07-02)

- `_warmup_needed` re-syncs the op catalog when `op_safety` is stale
  (empty/absent with operations present) -- version-skew staleness left a
  half-warmed catalog missing connectors configured after the last warm
  (e.g. fortinet-fortiguard-ioc), so the tier gate treated them as unknown
  and staged spurious approvals for read-only ops.
- `make ship` now force-warms after `verify_workers`, so a deploy guarantees
  a re-synced catalog.


## 0.4.24 (2026-07-02)

fsr-playbooks pin 0.4.15 → 0.4.16, pulling in:
- get_record dispatch gate fixed to mirror the real signature (iri /
  module+uuid / module+record_id, integer ids coerced) -- was bouncing
  every legitimate form on live triage turns.
- validate_yaml returns `corrected_yaml` + `auto_fixes` for auto-fixable
  foot-guns; parameters shape errors carry usage examples.
- verify_playbook output slimmed (lean typed_walk counts, deduped
  warnings, no duplicated compile evidence).

Connector: envelope `tags.case_state` summary (phase, record_key,
scenario, searched/enriched counts, unavailable connectors) on
chat_turn + chat_resume for the widget status strip.


## 0.4.23 (2026-07-02)

Case-state spine P3+P4 (fsr-playbooks pin 0.4.14 → 0.4.15).

- **Capability facts (§E).** The session now learns which connectors are
  unavailable (`connector_not_configured` / `connector_unhealthy`) from real
  `run_op` outcomes; a repeat attempt against a known-dead connector
  short-circuits with an informational redirect instead of re-probing.
- **Capability-gap "Re-check & continue" is now deterministic.** Clicking
  resume clears the learned unavailable facts, busts the framework's
  health/configured caches (so the re-probe is live, not a stale TTL verdict),
  and instructs the agent to retry the blocked operation without restarting
  the hunt. The "Skip" alternative leaves the learned facts in place.
- **Phase transitions (P4).** `CaseState.phase` advances at the persist
  chokepoint (pushed > contained > offered > awaiting_approval; first turn
  → building/investigating by intent), making build/triage completion
  auditable. Two evals added: a successful build session must end
  offered/pushed, and a transcript scan flags re-triage-after-resume.
- Test-suite hygiene: fixed 4 stale dispatch stubs (`_internal=True` kwarg);
  connector suite is fully green (188 passed).


## 0.4.22 (2026-07-02)

Hardening §G/§A + case-state spine P1+P2 live-verified on FortiSOAR 8.0.
Bump the pinned `fsr-playbooks` wheel 0.4.10 → 0.4.14 to pick up case-state
resume grounding, playbook-offer terminal emit, and Jinja filter catalog
fixes. No breaking changes; backward-compatible.


## 0.4.21 (2026-07-02)

Hotfix for 0.4.20: the case-state wiring imported `fsr_playbooks.agent.case_state`
unconditionally, which broke every chat turn when the installed wheel predated
the module. Case-state integration is now fail-open -- a missing/older wheel
degrades to the stateless per-turn behavior instead of erroring. Regression-tested.


## 0.4.20 (2026-07-02)

Case-state spine P1+P2 (first cut): `session_case_state` storage, grounding
cache in the system-prompt resolver, resume parity, and the persist chokepoint.
**Known-broken release** -- a hard `case_state` import against the older pinned
wheel failed all chat turns; fixed in 0.4.21.


## 0.4.19 (2026-07-02)

Contract parity (Stage 1-2) + ship infrastructure. Pydantic input/output
validation on `chat_turn`, `chat_poll`, `chat_resume`, `chat_history` op
boundaries ensures type safety and catches schema drift early. Platform ops
(`push_playbook`, `render_jinja`, `dry_run_playbook`) now fail-safe off-platform
instead of hanging. New `Makefile` (`make ship`, `make verify`, `make bridge-check`)
encodes the connector-versioned worker-recycle gotcha so deployments don't leave
stragglers on old bytecode. Requires pyfsr ≥ 0.7.9 for typed resource APIs.


## 0.4.18 (2026-07-02)

Live-debug checkpoint (intermediate version; no user-facing changes from 0.4.17).


## 0.4.17 (2026-07-02)

Live-debug checkpoint (intermediate version; no user-facing changes from 0.4.16).


## 0.4.16 (2026-07-02)

Live-debug checkpoint (intermediate version; no user-facing changes from 0.4.15).


## 0.4.15 (2026-07-02)

Live-debug checkpoint (intermediate version; no user-facing changes from 0.4.14).


## 0.4.14 (2026-07-02)

Live-debug checkpoint (intermediate version; no user-facing changes from 0.4.13).


## 0.4.12 (2026-06-30)

Bump the pinned `fsr-playbooks` wheel 0.4.8 -> 0.4.10. No connector-code
changes; this picks up the framework's self-teaching diagnostics, reference
playbook lint, and manual_input hoist.


## 0.4.11 (2026-06-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.10 (2026-06-14)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.9 (2026-06-14)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.8 (2026-06-14)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.4.7 (2026-06-10)

NOC FortiManager + FortiAnalyzer tooling -- first live bring-up on a real FMG
(192.0.2.205), plus the fixes that bring-up surfaced. This is the shipped
build of the 0.4.0-0.4.7 series (intermediate bumps were iterative live-fix
deploys for the same feature).

- **NOC device tools live.** `fmg_get_device_list/_status/_ha_status/
  _policy_package_status` (FMG `json_rpc_get`) + FAZ device-event hunt now work
  against a real FortiManager. Device-down triage + fleet posture sweep verified
  end-to-end.
- **FMG response shape.** Parse rows from the json-rpc connector's `get_response`
  envelope (not the named-op `result[].data` shape); sim fixtures now mirror it.
- **No more dropped fields / truncated lists.** NOC reads bypass `run_op`'s
  generic summarization (`summarize=False`) and project `data.fields` at the FMG
  query, so device `name`/`sn`/`ip` survive and the full device list returns.
- **Local connectors use direct execute.** `run_op` no longer routes a
  master-local config through the force-fail-playbook agent-wrap (which read a
  truncated persisted step result) just because a remote tenant agent echoes the
  same shared config.
- **Model devices ≠ outages.** `unknown` conn_status is kept distinct from
  `down`; model devices (`flags: is_model`) are tagged and not flagged as down.


## 0.3.134 (2026-06-09)

- **Fix: live chat polling never surfaced messages after a usage-less turn.**
  `turn_idx` was derived from `chat_turns`, whose rows are only written on a
  `UsageEvent`. A turn that failed before usage (e.g. the LLM gateway down)
  left no row, so the next turn reused the same `turn_idx` -- which the widget's
  monotonic `since_turn` poll fence then excluded forever (`chat_poll` returned
  `turn:null`, empty frames, no live messages). `turn_idx` now comes from
  `Storage.next_turn_idx()`, taking `MAX(turn)` across both `chat_turns` and
  `turn_progress` (the latter gets a `turn_start` row for every turn), so the
  counter increments unconditionally and stays aligned with the widget fence.


## 0.3.133 (2026-06-08)

- **Rename to `connector-fsr-soc-assistant` deployed.** First live rollout of
  the renamed identity (label "FSR SOC Assistant") to FortiCloud SOAR -- all
  10 workers verified on 0.3.133. Carries the 0.3.132 onchange provider
  config + base_url + OpenAI triage fix.


## 0.3.132 (2026-06-08)

- **Reliable playbook-offer on smaller models.** Tightened the triage system
  prompt so any request that mentions a playbook, automating the work, making
  it re-runnable, or "produce/write/generate the YAML" maps unambiguously to a
  `emit_playbook_offer` tool call -- never a hand-written ```yaml block. Larger
  models already inferred this; smaller models (e.g. gpt-4o-mini) would
  otherwise answer "produce the YAML" with inline prose YAML and skip the
  offer/accept/push path. No behavior change for models that already complied.

## 0.3.131 (2026-06-08)

- **OpenAI triage fix.** Triage turns 400'd on the OpenAI backend
  ("Missing required parameter: tools[0].type") because the connector
  advertises its intent tool-slice in the Anthropic shape
  (`{name, description, input_schema}`) regardless of provider. The OpenAI
  provider now normalizes any incoming tool shape into the OpenAI function
  envelope (`{type:"function", function:{…}}`), so triage works on OpenAI
  the same as build (which already expanded to openai_tools()). Anthropic
  unaffected.

## 0.3.130 (2026-06-08)

- **Provider-aware config UI (onchange).** The `LLM Provider` selector now
  drives FortiSOAR's `onchange` mechanism: choosing **anthropic** reveals
  Anthropic API Key / Model / Anthropic Base URL; choosing **openai** reveals
  OpenAI API Key / OpenAI Model / OpenAI Base URL. Provider-specific keys are
  required only under their branch, so a config saves cleanly for either
  backend without unrelated required-field errors.
- **Anthropic Base URL override.** New `anthropic_base_url` config field
  (defaults to the public API) lets the Anthropic backend target an
  Anthropic-compatible proxy/gateway, mirroring the existing
  `openai_base_url`. Wired through `AnthropicProvider(base_url=…)`.
- **Config-creation tooling** (`install_to_fsr.py`) is now provider-aware:
  `--provider anthropic|openai` + `--base-url`, sets `llm_provider`
  explicitly, and submits only the selected provider's keys.

## 0.3.129 (2026-06-08)

- **Live chat streaming fix.** Text now reaches the widget's `chat_poll`
  feed token-by-token AS THE MODEL GENERATES IT, instead of the whole
  answer appearing only on turn completion. Root cause: the Anthropic
  provider buffered every text delta and yielded them after the round-trip
  finished (a deliberate pre-live-streaming simplification). The OpenAI
  provider had the same bug. Both now stream live via one shared helper,
  `drain_with_idle_timeout` (`fsr_playbooks.llm._loop_helpers`), which surfaces
  each delta as produced while keeping the `STREAM_TIMEOUT_SECS` bound as a
  per-delta inactivity timeout that cancels a stalled upstream. LM Studio
  already streamed live (unchanged).

## 0.3.128 (2026-06-08)

- **OpenAI provider support.** The agent loop can now run on any OpenAI
  Chat Completions endpoint in addition to Anthropic. New `llm_provider`
  config selector (`anthropic` | `openai`); when set to `openai`, the
  connector uses `openai_api_key` + `openai_model` (default `gpt-4o`) +
  optional `openai_base_url` (defaults to the public OpenAI API, override
  for vLLM/Together/Groq/etc.). `anthropic_api_key` is no longer
  hard-required so OpenAI-only deployments save cleanly. The OpenAI path
  carries the SAME human-in-the-loop approval gating as Anthropic --
  tier-3+ tools (block IP, push playbook, …) suspend on a
  `pending_approval` envelope, persist through the sqlite-backed gateway,
  and resume on approve/deny. `list_models` and `health_check` are now
  provider-aware. Requires the bundled `openai` dependency (added to
  `requirements.txt`). Implemented in `fsr_playbooks.llm.openai_provider`.

## 0.3.127 (2026-06-07)

- **warmup now fills record-grounding tables.** `warmup` previously
  repopulated only connectors/operations/operation_params/op_safety, leaving
  `modules` / `module_fields` / `picklists` empty on a slim install -- so
  record-create/update steps had no grounding against the operator's own
  modules. It now fetches those from the target instance
  (`/api/3/staging_model_metadatas` + `/api/3/picklist_names`, both
  `$relationships=true`), best-effort. `_warmup_needed` also fires when
  `modules` is empty, so upgrades auto-fill. Counts surfaced in the envelope.

## 0.3.126 (2026-06-06)

- **build grounding 1.0 (live-proven).** Muted the internal
  `execute_api_request` fan-out that `siem_events_for_incident` /
  `_siem_pubv2_query` emit (`skill_trace.mute_recording()`), so the
  recorded skill trace no longer carries un-grounded internal calls.
  `build_fidelity` now passes with grounding 1.0 and action_coverage 1.0.


## 0.3.125 (2026-06-06)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.124 (2026-06-06)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.123 (2026-06-06)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.122 (2026-06-06)

- Vendors fsr_playbooks with STATIC_TYPE_FLOW phases 3-5: typed playbook parameters
  (`parameters: {name: type}`), source→target `type_mismatch` checking on
  connector params, and per-branch type-trace export from `verify_playbook`.
- Adds `echo_param_types` -- a `visible:false` diagnostic op that returns the
  runtime python type of each received param. Used by the Phase 4b ingestion-
  coercion probe; safe/read-only, kept for re-probing on FSR upgrades.


## 0.3.121 (2026-06-06)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.120 (2026-06-06)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.119 (2026-06-06)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.118 (2026-06-06)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.117 (2026-06-06)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.116 (2026-06-05)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.115 (2026-06-05)

- Re-vendor fsr_playbooks: fix a latent NameError in the agent-routed `run_op` wrap --
  `_run_op_via_agent_playbook` referenced `step_name` in its trace recorder but
  never accepted it (masked by a bare `except`, so the skill-call was silently
  dropped on every agent-wrapped op). `step_name` is now threaded through.


## 0.3.114 (2026-06-05)

- Fix agent-routed `run_op` for connectors installed BOTH locally and on an
  agent (e.g. `code-snippet`). `_configured_rows` deduped by name and dropped the
  agent row, so its agent-only configs were invisible to `_resolve_config_id` --
  the config name never resolved to its UUID, the agent force-fail wrap was
  skipped, and the op returned the empty fire-and-forget `in-progress` stub
  instead of its real result. Now the agent row's `configuration` entries +
  agent id are merged into the local row, so agent configs stay resolvable.
  Proven live: code-snippet wraps 4/4 (was 0/8). fsr_playbooks re-vendored.

## 0.3.111-0.3.113 (2026-06-05)

- Throwaway `debug_agent_ids` op used to diagnose the 0.3.114 fix; removed in
  0.3.114. No functional change.


## 0.3.110 (2026-06-05)

- Re-vendor fsr_playbooks with self-healing `siem_events_for_incident`: always supplies
  an incident-anchored ≤24h window in FortiSIEM's required microsecond-Z format,
  coerces a stale/seeded incident id to a live one, and falls back to a
  progressively-widening pub/v2 IP event search (no window cap) when the incident
  can't be queried directly -- surfacing accurate hints instead of the connector's
  misleading "Invalid Incident Id".


## 0.3.109 (2026-06-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.108 (2026-06-03)

- Saved playbooks are now linkable: on `playbook_offer` accept the reply carries
  a clickable deep link into the designer (`/playbooks/<workflow_uuid>`, falling
  back to `/playbooks/collections/<collection_uuid>`) as both a markdown link in
  the text and a `link` block on the `playbook_pushed` card.
- AI-named steps: `run_op` accepts an optional `step_name`; the agent supplies a
  short descriptive label for generic passthrough ops so a session-compiled
  playbook reads well instead of "Execute Api Request 2". Names are sanitized to
  the step-name charset and de-duplicated automatically.
- Playbooks compiled from a triage session import **active** (isActive=true)
  rather than as a disabled draft.


## 0.3.107 (2026-06-03)

Direct-containment chat: the triage agent can now be asked directly to block an
IP or isolate an EDR host, discovering the action via `find_containment_actions`
and staging it through `emit_action_card` (never `run_op`). Fixes a tier-ordering
safety bug where a probe-flagged `unsafe` op carrying the catch-all
`investigation` category (e.g. FortiEDR `isolate_collector`) auto-allowed at
tier 2 with no approval card; it now correctly escalates to tier 4 and surfaces
in containment discovery for host/endpoint targets.


## 0.3.106 (2026-06-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.105 (2026-06-03)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.104 (2026-06-03)

Fix FortiGuard IOC enrichment card: `ioc_search` returns threatinfo/risk_score/
recordTags at the top level of the record, not nested under `values`. The
extractor now falls back to the record itself (backward-compatible with the
nested shape) and uses `recordTags`/`ioc_tags`/`wf_cate` for tags, so ip /
domain / hash indicators render a proper ioc_enrichment card instead of being
silently dropped.


## 0.3.103 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.102 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.101 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.100 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.99 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.98 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.97 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.96 (2026-06-02)

- **FortiSIEM event queries fixed on connector 6.0.0.** The legacy XML
  `search_events` path 400s ("Content is not allowed in prolog") on this build,
  so `siem_search_ip` / `siem_search_host` / `siem_search_user` now route through
  the working pub/v2 JSON query API (submit → progress → results), shared with
  `siem_raw_query`.
- Fixed the `_SIEM_SELECT` attribute typo `rcvdBytes64` → `recvBytes64` -- a single
  unknown attribute made the pub/v2 engine 400 the entire query, which is why
  `siem_raw_query` failed live.
- `siem_raw_query` `where` now also accepts a `{field: value}` dict (friendly
  names like `sourceIPv4`/`destPort` aliased to backend attributes), not only a
  raw clause string.
- **Triage pre-flight resolves the native FortiSIEM incident id** from a case's
  member alert `sourcedata` (record id ≠ SIEM `incidentId`), so
  `siem_events_for_incident` gets the id the engine expects instead of the
  FortiSOAR record id.

## 0.3.95 (2026-06-02)

- First-class **FortiAnalyzer** triage tools, mirroring the FortiSIEM set:
  `faz_get_alerts` (recent FAZ event alerts for an ADOM/window),
  `faz_search_ip` (device-log pivot on an IP via
  `start_and_fetch_bulk_device_logs`, with src/dst/any filtering), and
  `faz_raw_query` (native JSON-RPC escape hatch). All read-only, returning the
  same compact event digest as the `siem_*` helpers.
- **Source-aware prompt curation.** The triage curator now matches tools to the
  data: it maps an alert's originating connector (FortiSIEM ↔ FortiAnalyzer) to
  the correct first-class toolset and pre-fills the right pivot. For a *case*
  spanning multiple sources, the normalizer harvests member-alert sources +
  indicators and the prompt recommends pivoting each indicator in EVERY source
  the case spans (e.g. the same external IP as both `siem_search_ip` and
  `faz_search_ip`), cross-checked against configured connectors.


## 0.3.94 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.93 (2026-06-02)

- Deploy reliability: `health_check` now reports per-worker identity --
  `connector_version` (read from the info.json shipped beside the running
  module, i.e. the version THIS worker actually loaded), the version-stamped
  `package` name, `pid`, `host`, and `contract_version`. New
  `scripts/verify_workers.py` probes health_check until the worker set settles
  and FAILS if any of the ~10 load-balanced integration workers is still on a
  stale version. `deploy.sh` runs it as a post-warmup gate (skip with
  `--no-verify`), so a green deploy now guarantees every worker recycled onto
  the new code -- closing the partial-publish hole where a flaky `'config_id'`
  publish left stragglers on old bytecode.


## 0.3.92 (2026-06-02)

- Refresh durability: `chat_history` now returns a render-fidelity transcript
  instead of per-turn metadata. New append-only `session_transcript` store
  records each visible exchange in chronological order -- a `{user}` entry for
  the analyst's text and a `{transcript: [...]}` entry holding the FULL
  wire-event array the widget rendered live (text + tool calls + cards +
  activity). On a page refresh the widget replays these through its render
  pipeline, so the conversation log comes back intact. Previously
  `chat_history` returned only model/token/stop_reason rows (no `user`/
  `transcript`), so the widget's rehydrate restored nothing and a refresh
  dropped the visible log.

## 0.3.91 (2026-06-02)

- Fix stuck chat on tool-level approval suspensions: `_envelope_stop_reason`
  now maps a loop `pending_approval` result straight to `approval_required`,
  even when a trailing `usage` frame follows the `approval_request` wire event.
  Previously the envelope stayed `pending_approval` (which the widget doesn't
  handle), leaving push_playbook and other non-run_op tier-gated approvals
  hung forever.
- Vendor refreshed `fsr_playbooks`: triage prompt now tells the agent to use the
  SIEM's own `incidentId` (from `sourcedata.incident_data.incidentId`) for SIEM
  incident ops rather than the FortiSOAR record id, and surfaces that id in the
  "What we already know" block. `run_op` error classification distinguishes
  4xx (op_request_rejected) / 5xx (upstream_error) / no-response
  (transport_failed). Build-prompt skeleton, conditional-required validator,
  and warm-health probe-cache fixes also included.

## 0.3.90 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.89 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.88 (2026-06-02)

B3 fix, part 3 (the real one): record run_op's resolved config id onto the
trace and emit it as the connector step's `arguments.config`. The 0.3.87
approach (resolve at compile via `connector_configs.resolve_config_id`) never
ran on-platform -- `connector_configs` isn't shipped, so the import raised and
silently fell back to `""`, and an agent-bound connector has no
`""`-resolvable default anyway. run_op already resolves the right config
(incl. the per-agent config id) via `_resolve_config_id(_configured_rows(...))`
at execution time; we now thread that `exec_config`/`config_id` into
`skill_trace.record_run_op`, store it on the SkillCall, and stamp it on the
compiled step (`_compile_run_connector_action`). Hand-authored steps still
default to the `""` sentinel.

## 0.3.87 (2026-06-02)

B3 fix, part 2: resolve the real default config UUID for connector steps
instead of the `""` sentinel. `""` ("use default config") works for plainly
configured connectors but NOT agent-bound ones (e.g. fortigate-firewall),
which have no non-agent default -- so `""` still failed with INTEGRATION-12.
`_resolve_connector_args` now calls `connector_configs.resolve_config_id(
connector, None)` to stamp the connector's default config UUID, falling back
to `""` offline (no live env). Lets a trace-built containment playbook on an
agent-bound connector run without a manual config fix.

## 0.3.86 (2026-06-02)

B3 fix: trace-compiled connector steps now carry a `config` key. The resolver
stamped `version`/`operationTitle`/`pickFromTenant` onto connector steps but
never `config`, so a trace-built playbook ran with config=None and FSR failed
the step with `INTEGRATION-12: Could not find a connector configuration
matching the given configuration id or name None`. `_resolve_connector_args`
now defaults `config` to `""` (FSR's "use the connector's default config"
sentinel, matching every hand-authored example), so trace-built playbooks run
without a manual config fix.

## 0.3.85 (2026-06-02)

Track B deploy of the skill-based playbook offer (contract 2.6.0 fields, on
2.7.0 transport). Carries the full triage→offer arc verified offline: a
triage turn emits an `emit_playbook_offer` card built from the recorded
`SkillTrace`, with per-step `skill_id`/`step_type`/`wiring_label`/`verified`
badges + a `draft_steps` tree; on accept the connector compiles the trace via
`build_playbook_from_trace` and pushes it (`_resume_playbook_offer_accept`).
The card always carries `has_mutating_action`, plus an `advisory` note when the
trace is confidently all-`safe` (analyst decides -- never refused). Adds the A1
connector-flow test proving the trace recorded under one turn's
`_session_trace_scope` persists and is reloaded on the next turn, so an offer
is reachable after a real approval.

## 0.3.84 (2026-06-02)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.83 (2026-06-01)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.82 (2026-06-01)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.81 (2026-06-01)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.80 (2026-06-01)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.79 (2026-06-01)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.78 (2026-06-01)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.77 (2026-06-01)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.76 (2026-06-01)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.75 (2026-06-01)

Fix `get_session_trace` (0.3.74): `default_storage` must be imported locally
(`from .storage import default_storage`) inside the handler, matching the
other storage-backed ops -- it is not a module global. The op now returns the
trace + compiled playbook instead of raising NameError.


## 0.3.74 (2026-06-01)

Adds `get_session_trace` (debug/utilities op): returns a session's recorded
skill trace (connector/operation/ref_prefix per action) plus the
deterministically-compiled playbook built from it. Read-only. Lets the
off-platform contract harness prove the full triage→record→compile→push→run
pipeline end to end.


## 0.3.73 (2026-06-01)

Playbook-offer is now analyst-decides: a triage that ran only read-only
lookups still gets a "Save as Playbook" offer (never refused), but the card
carries `has_mutating_action: false` + an `advisory` note so the analyst
chooses. A triage with a containment action offers as before. Additive,
backward-compatible card fields -- pre-2.6.x widgets ignore them.


## 0.3.72 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.71 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.70 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.69 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.68 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.67 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.66 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.65 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.64 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.63 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.62 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.61 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.60 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.59 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.58 (2026-05-31)

Contract 2.5.1 -- post-approval continuation. Approving an `action_card` no
longer dead-ends the turn: after executing the op (still splicing the real
`run_op` tool_use/tool_result, contract T6) the connector re-enters the agent
loop once so the model confirms the outcome and recommends next steps -- and may
propose the next containment step as a fresh `action_card`. An approve response
is now an ordinary turn envelope whose `stop_reason` may be `end_turn` or
another `awaiting_*`. Outage-survivable: falls back to the deterministic
executed-result summary + `end_turn` when no provider is reachable.


## 0.3.57 (2026-05-31)

Contract 2.5.0 -- action-based streaming. `chat_turn` now also writes a
per-event progress feed to SQLite as it streams, and a new read-only
`chat_poll(session_id, cursor)` op returns frames by cursor (with `done` +
`stop_reason` on a terminal frame). The widget fires `chat_turn` and polls
`chat_poll` in parallel to render incremental updates, then swaps to the
authoritative transcript on `done`. Fully additive and backward-compatible.


## 0.3.56 (2026-05-31)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.55 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.54 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.53 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.52 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.51 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.50 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.49 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.48 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.47 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.46 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.45 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.44 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.43 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.42 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.41 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.40 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.39 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.38 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.36 (2026-05-30)

The agent only sees, offers and runs connector configurations that the analyst's
teams can see. Record-write cards print labels instead of module keys, ids and
IRIs, and a cancelled card no longer leaks its id into the reply. Pins
fsr-playbooks 0.10.2 (offer steps in flow order, config-name check on the offer
and edit paths).


## 0.3.35 (2026-05-30)

**Agent-proxied connector detection (proper).** `_configured_rows` and
`list_configured_connectors` now correctly discover connectors configured via
FortiSOAR agent proxy (e.g. FortiGate). These show `config_count=0` in the
standard `configured=true` listing and were previously reported as "not
configured". Fix: `GET /api/3/agents` to list active agents, then
`POST connector_details?agent={id}&active=true` per agent -- this endpoint
returns real `config_count` and `configuration` arrays. Only merges rows with
`config_count > 0` and `status=Completed` so uninstalled/broken agent connectors
are excluded.


## 0.3.34 (2026-05-30)

**Agent-proxied connector detection.** `_configured_rows` and
`list_configured_connectors` now include connectors configured via a FortiSOAR
agent proxy (e.g. FortiGate, Active Directory, Exchange, Splunk). These show
`config_count=0` in the standard `connector_details?configured=true` listing
but have `agent_id` set in the full installed listing -- the fix merges them in.
Previously the agent reported these as "not configured" even when the agent proxy
was active and healthy.


## 0.3.33 (2026-05-30)

**Param-level live grounding + live op-definition warming.** Two related
reliability/perf fixes so the agent stops guessing op parameters live and works
off real instance-accurate op signatures at cache speed.

- **Param grounding (the deferred half of the sess-uq31go5p fix).** On an
  un-synced connector the offline param check no-ops, so the agent used to
  discover real param names by trial and error (`ip`→`ip_address`→`indicator`).
  `validate_op_grounded` now validates argument names against the **live**
  connector definition too (unknown-param typo detector + missing-required);
  `run_op` and `emit_action_card` both call it, so a card with a guessed param
  can't reach the analyst. Fail-open on any live hiccup.
- **Live op-definition cache + warmup.** That live fallback re-POSTed the full
  connector detail every pivot. It's now backed by a sqlite op-def cache
  (version-keyed, survives restarts), and post-install warmup pre-fetches live
  op definitions (operations + their parameters in one detail POST) for the
  box's configured connectors -- un-synced first -- so the first grounding call
  in a hunt is a cache hit instead of a multi-second round-trip.

## 0.3.32 (2026-05-30)

**Never dead-end the analyst -- `capability_gap` card (contract 2.3.0).** When
the instance can't do what an investigation needs (e.g. no containment
connector configured, or the one the agent reached for isn't configured /
healthy), the agent now surfaces a gating `capability_gap` card instead of a
prose dead end: what's missing, why, concrete fix steps (which connector to
configure), automation tips, manual fallbacks, and a "Re-check & continue"
resume button so the analyst fixes the gap and the agent resumes the blocked
step.

- New `emit_capability_gap_card` tool (vendored from `fsr_playbooks`); wired into the
  transcript as a `capability_gap` event that resumes via the existing
  `awaiting_choice` path (no new resume branch).
- `find_containment_actions` and `run_op`'s connector preflight
  (`connector_not_configured` / `connector_unhealthy`) now return a ready-to-
  forward `suggested_card`.
- `CONTRACT_VERSION` → `2.3.0`; persisted + summarized like other cards.

## 0.3.31 (2026-05-30)

**Warm the op catalogue at install/config-time, not on first chat.** A
`$replace` install re-ships the slim DB with the WARM tables truncated, so the
op catalogue is empty until warmed -- the exact `store_ops_count == 0` window
where `emit_action_card` op-grounding has to fall back to the live connector
definition (slower, and the gap that bit `sess-uq31go5p`).

- **Lifecycle-hook warmup** -- the connector now implements `on_app_start`,
  `on_add_config`, `on_update_config`, and `on_activate`; each kicks a gated,
  backgrounded `warmup` (`_warmup_needed` no-ops once populated, daemon thread
  so the catalog walk never blocks app-load / config-save, never raises). So
  saving/activating the connector config warms it.
- **Post-install warmup in `deploy.sh`** -- belt-and-suspenders for code-only
  `$replace` redeploys (which may not re-fire the hooks): an explicit
  `execute("warmup")` after install via `fsr_live.LiveFSR`. Idempotent;
  `--no-warmup` skips it. Non-fatal on failure (first chat backfills).


## 0.3.30 (2026-05-30)

**Live-triage grounding + self-heal hardening (Phase 1.6/1.7/1.8).** Closes the
`sess-uq31go5p` triage failure where a phantom `virustotal.lookup_hash` reached
the analyst as an approval card and dead-ended on execute.

- **Emit-time op grounding (1.6)** -- `emit_action_card` now runs the same
  two-layer grounding as `run_op` (shared `validate_op_grounded`: offline
  reference store + live connector-definition fallback when the store is
  un-synced). A hallucinated/typo'd op can no longer slip through to a card
  just because the connector's ops aren't catalogued. Fails open on any live
  lookup hiccup so a transient network error never blocks a real op.
- **Self-healing resume (1.7)** -- when a human-approved execute fails with an
  agent-fixable code (`unknown_operation` / `bad_params` / `missing_required` /
  `bad_select_value`), the resume re-enters the provider loop so the agent
  self-corrects and emits a fresh, re-gated card -- instead of the templated
  "contain manually" dead-end. Bounded to 2 passes/session; genuine infra
  failures (and any provider outage mid-heal) keep the deterministic,
  outage-survivable path.
- **`get_record(full=True)` cap (1.8)** -- `full=True` no longer dumps the raw
  ~100KB hydrated body into context: it returns a cleaned (null/empty +
  SLA/boilerplate stripped), hard 8KB-capped body flagged `coerced_full`. Keeps
  multi-turn triage under the per-minute token budget.


## 0.3.29 (2026-05-30)

**HITL approval hardening + parallel hunt dispatch.** Agent-loop hardening
(Phases 2.8 / 3.1 / 3.2) plus the contract bump that surfaces it:

- **Tamper-evident approvals (3.1)** -- each suspended tier-3+ action is bound
  to its `(approval_id, tool, args_hash, created_at)` under a server HMAC
  secret at suspend time and re-verified on resume (`hmac.compare_digest`). A
  store-tampered or stale session fails closed: the action is **not** executed
  and the turn ends with the new `approval_unverified` stop_reason.
- **Durable suspended sessions (3.2)** -- paused approvals are backed by a
  sqlite gateway so they survive a worker restart (web backend); a stable HMAC
  key is pinned so restored sessions still verify.
- **Parallel read-only dispatch (2.8)** -- independent read-only (tier ≤ 2)
  tool calls in a turn fan out concurrently (cap 8), collapsing live-hunt
  fan-out latency from sum → max; tier-3+ still suspends one at a time.
- **Contract 2.2.0** -- adds the `approval_unverified` stop_reason; the widget
  clears the pending approval card and prompts a re-issue. Backward-compatible
  (pre-2.2.0 widgets fall through to their generic `error` handling).


## 0.3.28 (2026-05-30)

**Hunt token reduction + one-call containment discovery.** Four changes that
keep multi-turn triage hunts cheap and stop the agent thrashing on
containment:

- **`get_record` projection** -- a hydrated FSR record (~100KB with
  `$relationships=true`) is pruned to a ~5KB triage projection (96% smaller):
  null/empty fields dropped, picklist objects collapsed to their values,
  hydrated reference lists (MITRE, related records) thinned to a capped
  `{iri,label}` index. Every indicator scalar
  (sourceIp/destinationIp/host/user/hashes) is preserved. `full=true` returns
  the raw body (rare debugging only).
- **FortiSIEM/FortiAnalyzer event-list digest** -- `run_op` results that are a
  long list of uniform rows (≥8) collapse to an aggregate digest (count, time
  window, top-N facets for src/dst IP, user, host, action, event type, port +
  a few raw-blob-stripped sample rows) instead of dumping raw events (~40KB →
  ~1.8KB). Small/structured results pass through untouched.
- **`get_record` colon-IRI** -- now accepts the `module:uuid` shorthand the
  triage prompt uses (e.g. `alerts:54f2…`) in `iri`; previously 404'd and
  wasted a pivot.
- **`find_containment_actions(target_type, probe)`** -- new triage tool that
  returns the response actions actually configured + healthy on this instance,
  filtered by indicator type and gated on approval tier (≥3), with the
  connector/op/required-params ready for `emit_action_card`. One call instead
  of hunting connector-by-connector. When nothing can contain the target, it
  steers the agent to surface the capability gap via `emit_choice_card`
  (Acknowledge / Escalate / Create-ticket) rather than dead-ending.


## 0.3.27 (2026-05-30)

**Demo polish -- hunts now finish staging their action card.** Two changes
so a live triage hunt doesn't exhaust its tool budget before staging
containment: (1) the per-turn tool-round ceiling `MAX_TOOL_TURNS` is raised
12 → 16 (and §2.8 parallel dispatch already collapses each independent
fan-out into a single round, so the effective headroom is larger still); and
(2) the triage prompt now treats the staged `emit_action_card` as the
deliverable -- once scope is clear and containment is warranted it stages the
card *before* kicking off another round of optional enrichment, and stages a
still-unstaged warranted card immediately rather than letting extra TI
lookups crowd it out.

## 0.3.26 (2026-05-30)

**Parallel read-only tool dispatch (hardening plan 2.8).** A live hunt's
wall-clock was dominated by *sequential* tool round-trips -- the agent loop
awaited each `run_op`/`get_record` (all slow upstream SIEM/TI I/O) before
the next, even when the calls were mutually independent (the initial
record/indicator gather, per-connector enrichment). The Anthropic provider
now fans out the read-only (tier ≤ 2) calls in a turn concurrently via
`asyncio.gather` + `asyncio.to_thread(dispatch, …)`, capped at
`MAX_PARALLEL_TOOLS=8`, collapsing fan-out latency from *sum → max*.
`tool_result` blocks stay in `tool_use` order (Anthropic requires the
pairing). Any tier-3+ call is the approval boundary: read-only calls before
it resolve in parallel, then it routes through the existing
suspend/`pending_approval` path unchanged. Re-vendors the latest `fsr_playbooks`
(intent resolution `intents.py`, `search_module_records` registration,
triage-prompt parallel-lookup guidance).

## 0.3.25 (2026-05-30)

**New read-only triage tool: `get_record`.** Closes the prompt↔tool gap
(hardening plan 2.1) where the triage prompt told the agent to pull an
alert/event/asset row by `iri`/`module`/`uuid` -- for attack-timeline and
blast-radius work -- but no fetch tool existed, forcing blind `run_op`
calls. Adds `get_record(iri | module+uuid, relationships=True)` wrapping
`GET /api/3/<module>/<uuid>?$relationships=true` (tier-1, read-only) so
related entities (correlated alerts, linked assets, indicators) come back
hydrated inline. Also re-vendors the Phase 1 hardening already landed in
`fsr_playbooks` (op-argument validation `_validate_op_params`; tool results
flagged `is_error`).

## 0.3.15 (2026-05-29)

**Fix: chat_resume param schema rejected card-based resumes (HTTP 400).**
`info.json` declared `approval_id` as the only resume id and marked it
**required**, and omitted `session_id`/`card_id`/`choice_id`/`args`/
`value` entirely -- so the platform's operation-param validator 400'd
(`Parameters Approval ID … not provided`) before the handler ran on
every action_card/choice resume the widget makes. This was the real
cause of the "chat_resume returns stop=None" symptom. Schema now: every
resume id is optional, `session_id` is the only required field besides
`decision`, and `decision` includes `choice`.

## 0.3.14 (2026-05-29)

**Auto-card mutating run_op -- the agent no longer has to choose
emit_action_card.** A `run_op` on a mutating/containment op (tier 3+)
is suspended by the existing approval gate; the connector now renders
that suspension as an `action_card` automatically (id = approval_id,
args = the run_op params, all fields editable) and truncates it to be
the turn's last event (`stop=awaiting_action_card`). The triage prompt
adherence problem (agent sometimes calling run_op instead of
emit_action_card) is now handled in code rather than begged of the LLM.
Both card surfaces -- explicit `emit_action_card` and auto-carded
`run_op` -- resume through the same deterministic execute path (T6) and
the orphaned SuspendedSession is cleaned up on resume.

Testing pins **Haiku** (`claude-haiku-4-5-20251001`) as the default
config model.

## 0.3.13 (2026-05-29)

**Fix: action_card approve→execute loop now closes (contract T6).**
On `chat_resume(card_id, decision=approve)` the connector now executes
the approved operation **directly** (`run_op` with `confirm=True` +
`_approved=True`) and splices the real `tool_result` into the
transcript, then closes at `end_turn`. Previously the approve path
handed an "execute it now" instruction back to the model, but the
triage prompt forbids the agent from running mutating ops -- so the
turn dead-ended (no `tool_result`; `stop` never closed). Execution is
deterministic (no second Anthropic round-trip) so the demo survives an
LLM outage; a failed op degrades to an analyst-facing warning instead
of dead-ending the chat. Reject/choice/generic resumes are unchanged.

## 0.1.0 (2026-05-28)

All 10 declared operations are now functional end-to-end.

**New: agent loop.**
- `chat_turn` -- runs one LLM-driven authoring turn via
  `fsr_playbooks.llm.run_agent_turn`; sqlite-backed history.
- `chat_resume` -- resumes a turn paused at an HITL approval gate.
- `chat_history` -- returns prior turns for a session.
- `PersistedApprovalGateway` persists paused turns so an FSR worker
  restart doesn't drop them.

**New: platform-side ops via `integrations.crudhub`.**
- `push_playbook` -- `POST /api/3/workflows/`.
- `render_jinja` -- `POST /api/wf/api/jinja-editor/` with optional
  context + `from_pb_execution` lookup.
- `dry_run_playbook` -- push + trigger + poll until terminal + optional
  hard-purge. Single-call E2E smoke check.

**Packaging.**
- `scripts/build.sh` rsyncs `fsr_playbooks/` + `store/fsr_reference.db`
  from fsr-playbook-framework into the connector tree, then tars (~14M).
  FortiSOAR's installer handles `requirements.txt` at install time
  (no need to vendor `anthropic` / `httpx`).
- `operations._import_fsr_playbooks` falls back to the sibling vendored
  layout for on-platform installs where the connector dir isn't on
  `sys.path`.

## 0.0.1 (2026-05-28)

Initial scaffold. Pure-compute ops only: `health_check`,
`compile_yaml`, `validate_yaml`, `resolve_yaml`.
