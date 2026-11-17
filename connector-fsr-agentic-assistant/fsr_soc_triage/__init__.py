"""SOC triage / NOC investigation cluster (connector-owned).

Carved out of the `fsr_playbooks` package namespace (RECONCILIATION_PLAN Phase 1,
D4=(a)). Depends on the published `fsr_playbooks` for the compiler + authoring +
run/exec surface (`fsr_playbooks.execution_api`); this package adds the triage,
NOC, and live-investigation tools the SOC connector layers on top.

Importing this package registers the triage/NOC tools into the library's tool
REGISTRY (D5 = Option A), so `fsr_playbooks.llm.tools.anthropic_tools()` advertises
the triage slice, and seeds the connector's triage system prompt into the
library's intent-prompt cache so `load_intent_prompt("triage")` returns it.
Both registrations are idempotent.
"""
from .registry import (
    register_playbook_tools,
    register_prior_case_tools,
    register_record_tools,
    register_triage_prompt,
    register_triage_tools,
    register_ztpf_tools,
)
from .catalog_probe import register_catalog_probe
from .enrichment_view import install_enrichment_view
from . import tools_agent  # registers triage_build_turn/resume + session primitives via @mcp.tool()

register_triage_tools()
# Threat-intel run_op results are shaped to fit the model's context budget, so
# the verdict fields survive the framework's clip of older results.
install_enrichment_view()
# Persona-gated record writes (create_record/update_record) — tier-3, carded,
# surfaced only via a persona allowlist. Idempotent + independent of triage.
register_record_tools()
# ZTPF authoring: test_template (render + lint a template's Jinja) — tier-1,
# read-only, persona-allowlist-only. Idempotent.
register_ztpf_tools()
# Cross-case memory: prior_cases (earlier alerts/incidents sharing an indicator)
# - tier-1, read-only, in the default triage slice. Idempotent.
register_prior_case_tools()
# Persona-gated playbook execution (run_playbook) — dynamic tier (auto ⇒ 2,
# else 3-carded), persona-allowlist-only. Overrides the library dev-path impl.
register_playbook_tools()
register_triage_prompt()
# Let the framework verify a connector against the LIVE box before it reports
# "not found in store". The reference catalog is a warmup snapshot, so a
# connector installed since the last warm is otherwise declared nonexistent —
# the FortiAnalyzer failure on 159. Consulted only on the miss path.
register_catalog_probe()


def register_mcp_materializer() -> None:
    """Wire the dynamic tool-surface materializer to build its on-box client
    via the crudhub CS HMAC adapter (``_live_mcp.build_client``).

    Idempotent + safe off-box: ``configure`` only stores the factory + an empty
    allowlist; the live client is built lazily inside ``ensure_initialized``
    (called from ``anthropic_tools`` on the first turn), so Django settings are
    up by then. ``build_client`` returns None off-box / when the ``mcp`` SDK or
    appliance keys are absent - the materializer then stays dormant (curated
    SAFE_TOOLS keep working). The allowlist itself is read from
    ``FSRPB_MCP_ALLOWLIST`` (JSON env) at init time; default empty = nothing
    materialized.
    """
    try:
        from fsr_playbooks.mcp_server import materializer
        from . import _live_mcp
        materializer.configure(client_factory=_live_mcp.build_client)
    except Exception:  # noqa: BLE001 - never break import over the materializer
        # (framework pre-0.4.19 / import path issue / etc.) - silent degrade
        pass


register_mcp_materializer()

__all__ = ["register_triage_tools", "register_record_tools",
           "register_ztpf_tools", "register_playbook_tools",
           "register_triage_prompt", "register_catalog_probe", "tools_agent"]
