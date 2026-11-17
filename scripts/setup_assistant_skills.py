#!/usr/bin/env python
"""Idempotently stand up the Assistant Skills feature on a FortiSOAR box, end to
end, using pyfsr's TYPED admin/record APIs (never a hand-rolled POST -- that was
the shape drift that bit the first attempt):

  1. create the `assistant_skills` custom module (+ publish) with the exact
     camelCase field API keys the connector read-path expects
     (`scopeModule`/`trigger`/`instruction`/`enabled`/`priority`/`source`);
  2. upsert the missing `ztpf_automation_actions` persona into the Key Store --
     an AUTHORING persona with NO build/containment tooling (that absence is
     what stopped the "Build a playbook" leak), plus the generic write tools so
     the agent can capture skills;
  3. grant skill-capture on the ztpf personas (add `assistant_skills` to
     `may_write` and `create_record`/`update_record` to `tools.allow`) -- a
     read-merge-write so existing persona content is preserved;
  4. seed the 8 FIXME-derived skill records (idempotent on scopeModule+trigger).

Everything is idempotent and commit-safe: NO box, creds, or IPs live here -- the
box comes from `EnvConfig.from_env()`. Point at a box by sourcing its env first:

    set -a; . ../../fsr_all_widgets/fortisoar-widget-harness/.env.159; set +a
    .venv/bin/python scripts/setup_assistant_skills.py --dry-run   # print, no write
    .venv/bin/python scripts/setup_assistant_skills.py             # do it

`--personas-from <bundle.json>` also upserts every persona in an eval bundle's
`personas` map into the Key Store (before the grants), so the box runs the same
personas the offline rows grade, e.g.
    --personas-from scripts/eval_fixtures/ztpf/ztpf_world_live.json

Step selection (default = all four): --no-module --no-persona --no-grants --no-skills.
Publishing the new module is appliance-wide; pass --no-publish to stage only.

The seed skills are ztpf-scoped (the FIXMEs came from live ztpf sessions). A
newly added skill needs a worker recycle to take effect (the profile cache is
process-life) -- a connector publish/release does that.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import urllib3

from pyfsr.config import EnvConfig
from pyfsr.pagination import extract_members

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# The connector package (for the persona (de)serializers -- single source of the
# `assistant_personas` field mapping, so this installer can't drift from the
# read-path in fsr_soc_triage/profiles.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "connector-fsr-agentic-assistant"))

# ── The skills module ────────────────────────────────────────────────────────
SKILL_MODULE = "assistant_skills"
# Field API keys MUST match fsr_soc_triage/skills.py (F_SCOPE/F_TRIGGER/...).
F_SCOPE, F_TRIGGER, F_INSTRUCTION = "scopeModule", "trigger", "instruction"
F_ENABLED, F_PRIORITY, F_SOURCE = "enabled", "priority", "source"

# ── The personas module (P0) ─────────────────────────────────────────────────
# Field API keys come from fsr_soc_triage/profiles.py (imported below) so the
# installer and the read-path share ONE definition.
try:
    from fsr_soc_triage import profiles as _profiles  # type: ignore
    PERSONA_MODULE = _profiles.PERSONA_MODULE
    PF_SCOPE = _profiles.PF_SCOPE
    PF_LABEL = _profiles.PF_LABEL
    PF_PROMPT = _profiles.PF_PROMPT
    PF_VERSION = _profiles.PF_VERSION
    PF_ENABLED = _profiles.PF_ENABLED
    PF_TOOLS_ALLOW = _profiles.PF_TOOLS_ALLOW
    PF_MAY_WRITE = _profiles.PF_MAY_WRITE
    PF_RP_ALLOW = _profiles.PF_RP_ALLOW
    PF_RP_AUTO = _profiles.PF_RP_AUTO
    PF_BIND = _profiles.PF_BIND
    PF_ADVANCED = _profiles.PF_ADVANCED
    # JSON-bearing Long Text columns that must be textarea, not varchar(255).
    PF_AREA_FIELDS = (PF_PROMPT, PF_TOOLS_ALLOW, PF_MAY_WRITE, PF_RP_ALLOW,
                      PF_RP_AUTO, PF_BIND, PF_ADVANCED)
except Exception as _e:  # noqa: BLE001 -- connector not importable ⇒ persona steps disabled.
    _profiles = None
    PERSONA_MODULE = "assistant_personas"

# ── The missing authoring persona ────────────────────────────────────────────
AA_MODULE = "ztpf_automation_actions"
AA_KEY = f"fsr_assistant_profile:{AA_MODULE}"

AA_PROMPT = (
    "You are the ZTPF Automation-Action Author. On this screen you AUTHOR and "
    "review `ztpf_automation_actions` records -- reusable templates keyed by "
    "Category + Type that call the right playbook for a class of device work. "
    "Ground every answer in the record on screen and its related data; read it "
    "with the tools before answering.\n\n"
    "YOU DO NOT BUILD PLAYBOOKS HERE, and you never touch alert/case triage or "
    "device-containment tooling. If asked to 'build a playbook', clarify that "
    "here you author the automation-ACTION record (the template) that references "
    "an existing playbook -- you do not compile or design playbooks.\n\n"
    "WHERE AN ACTION RAN -- an action's executions live in the "
    "`ztpf_device_automation_steps` module (real field names, live-verified on "
    "8.0): the action relationship is `ztpfAutomationActions`, the device is "
    "`ztpfDevices`, the run group is `ztpfRunGroups`; step order is `stepNumber`; "
    "results are `outputMarkdown` / `outputSourceData`. To find every device an "
    "action ran on, search that module for steps where "
    "`ztpfAutomationActions.uuid` = this action, then collect the DISTINCT "
    "`ztpfDevices` from those steps, e.g.\n"
    "  search_module_records(module=\"ztpf_device_automation_steps\",\n"
    "    filters={\"ztpfAutomationActions.uuid\": \"<this action's uuid>\"}).\n"
    "Show each step's `outputMarkdown` / `outputSourceData` for the results.\n\n"
    "When a search returns zero rows that is an authoritative answer -- report it "
    "plainly, do not retry the same query in other syntaxes."
)

AA_JSON_VALUE: dict[str, Any] = {
    "version": 1,
    "label": "ZTPF Automation-Action Author",
    # Read + author record tools only. NO build/verify/compile/emit, NO
    # containment, NO triage -- the deliberate absence that stops the build leak.
    "tools": {"allow": [
        "get_record", "search_module_records",
        "create_record", "update_record",
    ]},
    "may_write": [AA_MODULE, SKILL_MODULE],
    "bind_modules": [],
    "run_playbook": {"allow": [], "auto": []},
    "ui": {
        "label": "ZTPF Automation-Action Author",
        "greeting": "Automation action: {name}",
        "placeholder": "Ask about this automation action or author a new one…",
        "footer": None,
        "quickActions": [
            {"key": "where", "label": "Where has this run?",
             "sub": "Devices this action touched",
             "prompt": "Which devices has this automation action run on? Traverse "
                       "the device automation steps for this action and list the "
                       "distinct devices, with each step's result."},
            {"key": "author", "label": "Author a new action",
             "sub": "Template keyed by Category + Type",
             "prompt": "Help me author a new automation-action record: ask me for "
                       "the Category, Type, and the playbook it should call, then "
                       "create it."},
        ],
    },
}

AA_RECORD = {
    "key": AA_KEY, "value": AA_PROMPT, "jSONValue": AA_JSON_VALUE,
    "notes": "Module-scoped AUTHORING persona for ztpf_automation_actions -- no "
             "build/containment/triage tools (stops the 'build a playbook' leak); "
             "grounds the ztpfAutomationActions->steps->ztpfDevices traversal and "
             "lets the agent capture skills (may_write assistant_skills).",
}

# Personas that should be able to CAPTURE skills (grant step). Their keys.
GRANT_PERSONA_KEYS = [
    "fsr_assistant_profile:ztpf_devices",
    "fsr_assistant_profile:ztpf_metadata_sources",
    AA_KEY,  # the one we just upserted (self-consistent; no-op if already granted)
]
WRITE_TOOLS = ("create_record", "update_record")

# ── The FIXME-derived seed skills (from live ztpf sessions) ────────────────
# (scope_module, trigger, instruction, priority)
SEED_SKILLS: list[tuple[str, str, str, int]] = [
    ("ztpf_devices", "hide deprecated playbooks",
     "When listing playbooks, do not offer any whose name contains \"deprecated\".", 50),
    ("ztpf_devices", "run-group age",
     "When summarizing run groups, include how long ago each run group ran "
     "(relative time from its timestamp).", 40),
    ("ztpf_devices", "run-group and step ordering",
     "Sort run groups by createDate descending; within a run group, sort steps by "
     "`stepNumber` ascending (execution order).", 40),
    ("ztpf_devices", "pending steps semantics",
     "\"list pending device steps\" = steps on this device with no run group "
     "(`ztpfRunGroups` is empty). \"Run the pending steps\" = run the playbook "
     "\"Create Run Group and Execute Steps\" (never run the step records individually).", 60),
    ("ztpf_devices", "which run group is the device's last one",
     "A device's most recent run group is named in its `ztpfArtifacts` field: `ztfGroup` is the run gr"
     "oup's name and `ztfGroup_fqid` its IRI. For any question about the last or latest run group, start there"
     ': read that run group and search its steps with `ztpfRunGroups` = that IRI. Copy the IRI from the record exactly, never retype it.', 60),
    ("ztpf_devices", 'summarizing a device',
     'When you summarize a device or say what is going on with it, include the outcome of its la'
     'st run group (named in `ztpfArtifacts.ztfGroup`), and if that run group failed, which step'
     ' failed and why.', 55),
    ("ztpf_devices", 'why a step failed',
     "The reason a step failed is in that step's `outputStatusMessage`, with detail in `outputMa"
     'rkdown` and `outputSourceData`. Search results leave those fields out, so read the failed '
     'step with get_record (or pass them in `fields`) and quote the message.', 55),
    ("ztpf_devices", "'the devices in this module'",
     "\"the devices in this module\" means the `ztpf_devices` module -- read all of "
     "its records.", 30),
    ("ztpf_automation_actions", "devices an action ran on",
     "To find every device an action ran on, traverse in order: search "
     "`ztpf_device_automation_steps` where `ztpfAutomationActions.uuid` = this "
     "action, then collect the distinct `ztpfDevices` from those steps. Show each "
     "step's `outputMarkdown` / `outputSourceData` for results.", 50),
    ("ztpf_automation_actions", "author, do not build",
     "Here you AUTHOR automation-action records (templates keyed by Category + Type "
     "that call the right playbook). Do NOT offer to \"build a playbook\" or use "
     "containment/triage tooling.", 60),
    ("ztpf_metadata_sources", "create in this module",
     "Help create a record IN the current module; a static metadata source needs a "
     "`name` and the raw script/data body.", 40),
]


# ── helpers ──────────────────────────────────────────────────────────────────
def _find_key(client: Any, key: str) -> dict | None:
    page = client.get("/api/3/keys", params={"key": key, "$limit": 1})
    members = extract_members(page) if isinstance(page, dict) else None
    return members[0] if members else None


def _upsert_key(client: Any, record: dict, dry: bool) -> str:
    existing = _find_key(client, record["key"])
    if dry:
        return "UPDATE" if existing else "CREATE"
    keys = client.records("keys")
    if existing:
        ref = existing.get("@id") or existing.get("uuid") or existing.get("id")
        keys.update(ref, record, resolve_picklists=False, raw=True)
        return "UPDATED"
    keys.create(record, resolve_picklists=False, raw=True)
    return "CREATED"


# ── steps ────────────────────────────────────────────────────────────────────
def _ensure_instruction_is_long(admin: Any, publish: bool, dry: bool) -> None:
    """Self-heal a pre-existing module whose `instruction` was created as a
    single-line (varchar 255) field: widen it to a Long Text (textarea) column so
    the longer house-rules fit. Idempotent -- a no-op when already long."""
    field = admin.get_field(SKILL_MODULE, F_INSTRUCTION)
    form_type = (field or {}).get("formType") if field else None
    if form_type in ("textarea", "richtext", "html"):
        return
    print(f"    `{F_INSTRUCTION}` is '{form_type}' (varchar 255) -- widening to Long Text.")
    if dry:
        print("    [dry-run] would set_field_type(instruction -> textarea) + publish.")
        return
    # set_field_type edits STAGING; make sure staging exists (it mirrors published).
    if admin.get_staging(SKILL_MODULE) is None:
        admin.get_or_create_module(SKILL_MODULE, publish=False)
    admin.set_field_type(SKILL_MODULE, F_INSTRUCTION, db_type="string", form_type="textarea")
    if publish:
        admin.publish()
        print("    widened + published.")
    else:
        print("    widened in staging (NOT published; pass without --no-publish to apply).")


def step_module(client: Any, admin: Any, publish: bool, dry: bool) -> None:
    print(f"\n[1] module `{SKILL_MODULE}`")
    existing = admin.get_published(SKILL_MODULE) or admin.get_staging(SKILL_MODULE)
    if existing is not None:
        print(f"    already exists ({'published' if admin.is_published(SKILL_MODULE) else 'staging'}).")
        _ensure_instruction_is_long(admin, publish, dry)
        return
    fields = [
        admin.text_field(F_TRIGGER, label="Trigger", required=True, searchable=True),
        # Instruction is a Long Text (textarea) field: a single-line 'text' field
        # is a varchar(255) column and the longer house-rules overflow it.
        admin.text_field(F_INSTRUCTION, label="Instruction", required=True, area=True),
        admin.text_field(F_SCOPE, label="Scope Module", searchable=True,
                         tooltip="Module this rule applies to; blank = global."),
        admin.checkbox_field(F_ENABLED, label="Enabled", default_value=True),
        admin.integer_field(F_PRIORITY, label="Priority", default_value=0,
                            tooltip="Higher sorts earlier in the injected block."),
        admin.text_field(F_SOURCE, label="Source", searchable=True,
                         tooltip="analyst | agent (provenance)."),
    ]
    if dry:
        print(f"    [dry-run] would create + {'publish' if publish else 'stage'} with fields: "
              f"{[F_TRIGGER, F_INSTRUCTION, F_SCOPE, F_ENABLED, F_PRIORITY, F_SOURCE]}")
        return
    admin.create_module(
        SKILL_MODULE,
        label="Assistant Skill", plural="Assistant Skills",
        fields=fields,
        display_template="{{ trigger }}",
        default_sort=[{"field": "priority", "direction": "DESC"}],
        grant_to=["Full App Permissions"],
        add_to_nav=False,
    )
    print("    created in staging.")
    if publish:
        admin.publish()
        print("    published (module is live).")
    else:
        print("    NOT published (--no-publish); records can't be written until you publish.")


def step_persona(client: Any, dry: bool) -> None:
    print(f"\n[2] persona `{AA_KEY}`")
    action = _upsert_key(client, AA_RECORD, dry)
    print(f"    {action} (tools.allow={AA_JSON_VALUE['tools']['allow']}, "
          f"may_write={AA_JSON_VALUE['may_write']})")


def step_bundle_personas(client: Any, bundle: str, dry: bool) -> None:
    import json
    personas = (json.load(open(bundle)).get("personas") or {})
    print(f"\n[2b] {len(personas)} persona(s) from {os.path.basename(bundle)}")
    for name, rec in personas.items():
        if not rec.get("key", "").startswith("fsr_assistant_profile:"):
            print(f"    {name}: no fsr_assistant_profile key -- skipped.")
            continue
        record = {"key": rec["key"], "value": rec["value"],
                  "jSONValue": rec.get("jSONValue") or {}}
        print(f"    {rec['key']}: {_upsert_key(client, record, dry)}")


def step_grants(client: Any, dry: bool) -> None:
    print("\n[3] grant skill-capture on ztpf personas")
    for key in GRANT_PERSONA_KEYS:
        rec = _find_key(client, key)
        if rec is None:
            print(f"    {key}: MISSING -- skipped (create it first).")
            continue
        jv = dict(rec.get("jSONValue") or {})
        may = list(jv.get("may_write") or [])
        tools = dict(jv.get("tools") or {})
        allow = list(tools.get("allow") or [])
        changed = False
        if SKILL_MODULE not in may:
            may.append(SKILL_MODULE); changed = True
        for t in WRITE_TOOLS:
            if t not in allow:
                allow.append(t); changed = True
        if not changed:
            print(f"    {key}: already granted -- no change.")
            continue
        jv["may_write"] = may
        tools["allow"] = allow
        jv["tools"] = tools
        merged = {"key": key, "value": rec.get("value"), "jSONValue": jv,
                  "notes": rec.get("notes")}
        if dry:
            print(f"    {key}: would add may_write+={SKILL_MODULE}, tools.allow->{allow}")
            continue
        _upsert_key(client, merged, dry=False)
        print(f"    {key}: granted (may_write+={SKILL_MODULE}, tools.allow->{allow}).")


def step_skills(client: Any, dry: bool) -> None:
    print(f"\n[4] seed {len(SEED_SKILLS)} skills into `{SKILL_MODULE}`")
    if not dry and client.modules_admin.get_published(SKILL_MODULE) is None:
        print("    module is not published -- cannot write records. "
              "Run without --no-publish (step 1) first. Skipping.")
        return
    recs = client.records(SKILL_MODULE)
    for scope, trigger, instruction, priority in SEED_SKILLS:
        data = {F_SCOPE: scope, F_TRIGGER: trigger, F_INSTRUCTION: instruction,
                F_ENABLED: True, F_PRIORITY: priority, F_SOURCE: "analyst"}
        if dry:
            print(f"    [dry-run] get_or_create [{scope}] {trigger!r}")
            continue
        _rec, created = recs.get_or_create(
            data, key=[F_SCOPE, F_TRIGGER], raw=True, resolve_picklists=False)
        print(f"    [{scope}] {trigger!r}: {'CREATED' if created else 'exists'}")


def step_persona_module(client: Any, admin: Any, publish: bool, dry: bool) -> None:
    """Create the `assistant_personas` module (P0) -- the grid-editable persona
    substrate. Mirrors step_module; JSON/prompt columns are Long Text (textarea)
    so they don't overflow varchar(255). Idempotent; self-heals area columns on a
    pre-existing module."""
    print(f"\n[5] module `{PERSONA_MODULE}`")
    if _profiles is None:
        print("    connector package not importable -- skipped (run from the "
              "connector checkout so fsr_soc_triage.profiles resolves).")
        return
    existing = admin.get_published(PERSONA_MODULE) or admin.get_staging(PERSONA_MODULE)
    if existing is not None:
        print(f"    already exists ({'published' if admin.is_published(PERSONA_MODULE) else 'staging'}).")
        _ensure_persona_area_fields(admin, publish, dry)
        return
    fields = [
        admin.text_field(PF_SCOPE, label="Scope Module", required=True, searchable=True,
                         tooltip="Module this persona serves (one per module)."),
        admin.text_field(PF_LABEL, label="Label", searchable=True),
        admin.text_field(PF_PROMPT, label="Prompt", required=True, area=True,
                         tooltip="The persona system prompt."),
        admin.integer_field(PF_VERSION, label="Version", default_value=1),
        admin.checkbox_field(PF_ENABLED, label="Enabled", default_value=True),
        admin.text_field(PF_TOOLS_ALLOW, label="Tools Allow (JSON)", area=True,
                         tooltip='JSON array, e.g. ["get_record","search_module_records"].'),
        admin.text_field(PF_MAY_WRITE, label="May Write (JSON)", area=True,
                         tooltip="JSON array of modules this persona may write."),
        admin.text_field(PF_RP_ALLOW, label="Run Playbook Allow (JSON)", area=True),
        admin.text_field(PF_RP_AUTO, label="Run Playbook Auto (JSON)", area=True),
        admin.text_field(PF_BIND, label="Bind Modules (JSON)", area=True,
                         tooltip="JSON array of extra modules this persona also serves."),
        admin.text_field(PF_ADVANCED, label="Advanced (JSON)", area=True,
                         tooltip='JSON object: {"mcp":…,"capabilities":…,"ui":…}.'),
    ]
    if dry:
        print(f"    [dry-run] would create + {'publish' if publish else 'stage'} with fields: "
              f"{[PF_SCOPE, PF_LABEL, PF_PROMPT, PF_VERSION, PF_ENABLED, PF_TOOLS_ALLOW, PF_MAY_WRITE, PF_RP_ALLOW, PF_RP_AUTO, PF_BIND, PF_ADVANCED]}")
        return
    admin.create_module(
        PERSONA_MODULE,
        label="Assistant Persona", plural="Assistant Personas",
        fields=fields,
        display_template="{{ label }}",
        default_sort=[{"field": PF_SCOPE, "direction": "ASC"}],
        grant_to=["Full App Permissions"],
        add_to_nav=False,
    )
    print("    created in staging.")
    if publish:
        admin.publish()
        print("    published (module is live).")
    else:
        print("    NOT published (--no-publish); records can't be written until you publish.")


def _ensure_persona_area_fields(admin: Any, publish: bool, dry: bool) -> None:
    """Widen any persona JSON/prompt column that a pre-existing module created as
    a single-line varchar(255) to Long Text (textarea). Idempotent no-op when all
    are already area columns."""
    to_fix = []
    for f in PF_AREA_FIELDS:
        field = admin.get_field(PERSONA_MODULE, f)
        if (field or {}).get("formType") not in ("textarea", "richtext", "html"):
            to_fix.append(f)
    if not to_fix:
        return
    print(f"    widening varchar columns to Long Text: {to_fix}")
    if dry:
        print("    [dry-run] would set_field_type(->textarea) + publish.")
        return
    if admin.get_staging(PERSONA_MODULE) is None:
        admin.get_or_create_module(PERSONA_MODULE, publish=False)
    for f in to_fix:
        admin.set_field_type(PERSONA_MODULE, f, db_type="string", form_type="textarea")
    if publish:
        admin.publish()
        print("    widened + published.")
    else:
        print("    widened in staging (NOT published).")


def step_migrate_personas(client: Any, dry: bool) -> None:
    """Copy every Key Store `fsr_assistant_profile:*` persona into the
    `assistant_personas` module (idempotent on scopeModule). Reuses the read-path
    (de)serializers so a migrated row round-trips to the same Profile."""
    print(f"\n[6] migrate Key Store personas → `{PERSONA_MODULE}`")
    if _profiles is None:
        print("    connector package not importable -- skipped.")
        return
    if not dry and client.modules_admin.get_published(PERSONA_MODULE) is None:
        print("    module not published -- run step 5 (without --no-publish) first. Skipping.")
        return
    page = client.get("/api/3/keys", params={"$limit": _profiles._SCAN_LIMIT})
    members = extract_members(page) if isinstance(page, dict) else []
    recs = client.records(PERSONA_MODULE)
    n = 0
    for rec in members:
        key = rec.get("key")
        if not isinstance(key, str) or not key.startswith(_profiles.KEY_PREFIX):
            continue
        module = key[len(_profiles.KEY_PREFIX):]
        profile = _profiles.parse_profile_record(rec, module)
        if profile is None:
            print(f"    {module}: unparseable persona -- skipped.")
            continue
        data = _profiles.profile_to_module_record(profile)
        n += 1
        if dry:
            print(f"    [dry-run] get_or_create persona [{module}]")
            continue
        _rec, created = recs.get_or_create(
            data, key=[PF_SCOPE], raw=True, resolve_picklists=False)
        print(f"    [{module}]: {'CREATED' if created else 'exists'}")
    if n == 0:
        print("    no Key Store personas found to migrate.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no writes")
    ap.add_argument("--no-module", action="store_true", help="skip step 1 (module)")
    ap.add_argument("--no-persona", action="store_true", help="skip step 2 (persona)")
    ap.add_argument("--personas-from", metavar="BUNDLE",
                    help="step 2b: upsert an eval bundle's personas into the Key Store")
    ap.add_argument("--no-grants", action="store_true", help="skip step 3 (grants)")
    ap.add_argument("--no-skills", action="store_true", help="skip step 4 (seed skills)")
    ap.add_argument("--no-persona-module", action="store_true",
                    help="skip step 5 (create the assistant_personas module)")
    ap.add_argument("--migrate-keystore-personas", action="store_true",
                    help="step 6: copy existing Key Store personas into the module")
    ap.add_argument("--no-publish", action="store_true",
                    help="stage the module but do not publish (appliance-wide)")
    args = ap.parse_args()

    try:
        client = EnvConfig.from_env().client(timeout=90)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not build client from env: {e}", file=sys.stderr)
        return 2
    print(f"box = {client.base_url}    {'(DRY-RUN)' if args.dry_run else ''}")

    if not args.no_module:
        step_module(client, client.modules_admin, publish=not args.no_publish, dry=args.dry_run)
    if not args.no_persona:
        step_persona(client, dry=args.dry_run)
    if args.personas_from:
        step_bundle_personas(client, args.personas_from, dry=args.dry_run)
    if not args.no_grants:
        step_grants(client, dry=args.dry_run)
    if not args.no_skills:
        step_skills(client, dry=args.dry_run)
    if not args.no_persona_module:
        step_persona_module(client, client.modules_admin, publish=not args.no_publish, dry=args.dry_run)
    if args.migrate_keystore_personas:
        step_migrate_personas(client, dry=args.dry_run)

    print("\nDone." + ("" if args.dry_run else
          " Recycle workers (connector publish/release) so new personas/skills load."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
