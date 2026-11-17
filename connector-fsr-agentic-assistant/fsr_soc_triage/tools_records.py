"""The Agentic assistant's record-write policy: who may write to which module.

The writes themselves (``create_record`` / ``update_record``: field checks,
picklist resolution, transport, the pre-card validator) are the framework's
(``fsr_playbooks.mcp_server.tools_records``). They ask the host's write gate
before anything is carded or sent; ``registry.register_record_tools`` registers
``_write_gate`` as that gate. The answer comes from the turn's active persona (a
Key Store ``fsr_assistant_profile:<module>`` record): no persona, no writes.
"""
from __future__ import annotations

from typing import Optional

from .profiles import get_active_profile

# Tool error envelopes are BUILT through their model (PLAN.md Phase 5), the same
# argument as `operations._frame`: a hand-written literal can silently drop
# `code` or `message`, and the agent then has nothing stable to retry against.
try:  # package context (runtime)
    from ..pydantic_models import tool_err
except ImportError:  # standalone (unit test with the connector root on sys.path)
    from pydantic_models import tool_err


def _write_gate(module: str) -> Optional[dict]:
    """Return a refusal dict when the active persona may not write to ``module``;
    ``None`` when the write is permitted. No active persona ⇒ refused."""
    profile = get_active_profile()
    if profile is None:
        return tool_err(
            "write_not_permitted",
            "record writes require an assistant persona with a "
            "may_write scope; no persona is active for this "
            "session (mount the assistant on a module with a "
            "fsr_assistant_profile:<module> Key Store record)")
    if not profile.may_write_module(module):
        # Name the route that IS open. A read-only persona is usually read-only
        # *by design* -- ZTPF devices mutate through approval-gated playbooks,
        # not raw record writes -- so "you may not write here" is a true
        # statement of policy and a dead end for the analyst, who asked for a
        # thing the system can in fact do. Live: "add an action to this
        # device" refused here, and the turn ended having done nothing, even
        # though `Add Step from Action to Device` was one discovery call away.
        return tool_err(
            "write_not_permitted",
            f"the active persona does not permit direct writes to "
            f"{module!r}; its may_write scope is "
            f"{profile.may_write or '[] (read-only)'}. This is a deliberate "
            f"policy, not a missing permission -- do NOT retry the write or "
            f"ask the analyst to grant access. If a playbook can make this "
            f"change, that is the supported route: call "
            f"`list_module_playbooks` and run the one that fits (it will ask "
            f"the analyst to approve). Only if nothing fits, say what you "
            f"could not do and why.")
    return None
