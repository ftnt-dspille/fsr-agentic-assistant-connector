"""The analyst's name for each tool call, sent on the tool_use frame.

The widget used to keep its own phrase table, a hand copy of the connector's
tool names that drifted: live, a new tool showed in the activity list as its
raw name ("Edr host"), and 8 of the 24 tools advertised to triage had no
phrase. The connector registers the tools, so it names them; the widget only
adds detail it can read from the arguments (which record, which query).
`tests/test_tool_labels.py` fails when a registered tool has no label here.

Labels are past tense, in the analyst's words: no tool names, ids or flags.
"""
from __future__ import annotations

TOOL_LABELS: dict[str, str] = {
    # Records
    "get_record": "Read the record",
    "search_module_records": "Searched records",
    "prior_cases": "Checked earlier cases for the indicator",
    "create_record": "Created a record",
    "update_record": "Updated the record",
    "picklist": "Looked up picklist values",
    "list_picklists": "Listed picklists",
    "picklist_for_field": "Looked up a field's picklist",
    "resolve_picklist_value": "Looked up a picklist value",
    # Agent workflows (framework; offered once the host opts in)
    "debug_run": "Worked out why a playbook run failed",
    "verify_effect": "Checked the change on the record",
    "orient": "Looked at what this system has",
    "provision_module": "Set up a module",
    "repair_playbook": "Prepared a fix for the playbook",
    "investigate_alert": "Gathered the alert's evidence",
    "build_playbook": "Checked the playbook draft",
    # Hunting and enrichment
    "siem_search": "Searched FortiSIEM",
    "siem_search_ip": "Searched FortiSIEM for an IP",
    "siem_search_host": "Searched FortiSIEM for a host",
    "siem_search_user": "Searched FortiSIEM for a user",
    "siem_events_for_incident": "Read the FortiSIEM incident's events",
    "siem_raw_query": "Searched FortiSIEM",
    "faz_search": "Searched FortiAnalyzer",
    "faz_search_ip": "Searched FortiAnalyzer for an IP",
    "faz_search_device_events": "Searched FortiAnalyzer device logs",
    "faz_search_by_serial": "Searched FortiAnalyzer by serial number",
    "faz_get_alerts": "Read FortiAnalyzer alerts",
    "faz_event_summary": "Summarized FortiAnalyzer events",
    "faz_raw_query": "Searched FortiAnalyzer",
    "fmg_device": "Checked the device in FortiManager",
    "fmg_get_device_list": "Listed FortiManager devices",
    "fmg_get_device_status": "Checked the device in FortiManager",
    "fmg_get_ha_status": "Checked the device's HA status",
    "fmg_get_policy_package_status": "Checked the device's policy package",
    "edr_host": "Checked the host in FortiEDR",

    # Connectors and actions
    "run_op": "Ran a connector action",
    "find": "Looked up what is available",
    "find_connector": "Looked up connectors",
    "find_operation": "Looked up connector actions",
    "find_containment_actions": "Looked up containment actions",
    "find_enrichment_actions": "Looked up enrichment actions",
    "find_record_actions": "Looked up record actions",
    "get_op_schema": "Read an action's inputs",
    "list_configured_connectors": "Listed configured connectors",
    "connector_health": "Checked connector health",
    "healthcheck_connector": "Checked connector health",
    "get_run_env": "Read the run environment",
    "propose_http_fallback": "Proposed a direct API call",
    # Playbooks
    "run_playbook": "Ran a playbook",
    "resume_playbook": "Resumed the playbook",
    "list_module_playbooks": "Listed playbooks",
    "list_playbook_runs": "Listed playbook runs",
    "why_did_playbook_fail": "Diagnosed a playbook run",
    "search_playbooks": "Searched playbooks",
    "analyze_playbook": "Read the playbook",
    "edit_playbook": "Edited the playbook",
    "build_playbook_from_trace": "Drafted a playbook from this investigation",
    "compose_playbook": "Built the playbook from a plan",
    "plan_playbook": "Planned the build",
    "validate_yaml": "Validated the playbook",
    "compile_yaml": "Compiled the playbook",
    "verify_playbook": "Checked the playbook",
    "verify_enhancement": "Checked the edit",
    "push_playbook": "Saved the playbook",
    "dry_run_playbook": "Dry-ran the playbook",
    "step_through_playbook": "Stepped through the playbook",
    "diagnose_yaml_against_pb_execution": "Compared the playbook with its run",
    "get_step_type": "Looked up a playbook step",
    "find_step_examples": "Looked up step examples",
    "find_jinja_filter": "Looked up a template filter",
    "find_jinja_pattern": "Looked up a template pattern",
    "get_filter_examples": "Looked up filter examples",
    "test_template": "Tested the template",
    # Cards (the card itself is what the analyst sees)
    "emit_card": "Prepared a card",
    "emit_action_card": "Prepared an action for approval",
    "emit_choice_card": "Asked a question",
    "emit_capability_gap_card": "Flagged a missing capability",
    "emit_decision_step": "Prepared a decision",
    "emit_enhancement_offer": "Offered an edit",
    "emit_manual_input": "Prepared a form",
    "emit_playbook_offer": "Offered a playbook",
}


def tool_label(name: object) -> str | None:
    """The analyst's name for tool `name`, or None when it has none (a native
    MCP tool, which the widget names from its server and tool)."""
    return TOOL_LABELS.get(name) if isinstance(name, str) else None
