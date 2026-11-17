#!/usr/bin/env python
"""Export the connector's tool-registry manifest for the framework's census.

The framework registers most tools at import (``fsr_playbooks.llm.tools.SAFE_TOOLS``),
including the record reads and writes, the hunt tools and the playbook runs.
This connector's ``register_*_tools`` functions append the rest (ZTPF's
template test) at runtime. Without this manifest, a census built from the
framework repo alone is blind to those (tracker #67).

This script calls the four ``register_*_tools`` functions, captures the
late-registered tool names and their tiers, and writes them to the framework's
``data/connector_tool_registry.json``. The framework's ``tool_census.py`` reads
that manifest by default, so the census no longer needs a live turn to learn
the truth.

Usage (from the connector repo root, with the framework venv active)::

    python scripts/export_registry_manifest.py [--out PATH]

    # default: writes to <framework_repo>/data/connector_tool_registry.json
    # --out: write to a custom path instead
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _find_framework_data() -> Path | None:
    """Locate the framework's ``data/`` dir via the installed package."""
    try:
        import fsr_playbooks
        pkg_root = Path(fsr_playbooks.__file__).resolve().parent.parent
        data = pkg_root / "data"
        if data.is_dir():
            return data
    except ImportError:
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    default_out = _find_framework_data()
    ap.add_argument("--out", type=Path, default=None,
                    help="output path (default: <framework>/data/connector_tool_registry.json)")
    args = ap.parse_args(argv)

    out: Path = args.out or (default_out / "connector_tool_registry.json" if default_out
                             else Path("connector_tool_registry.json"))

    # The connector package lives one directory up from this script.
    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

    # 1. Capture the BASE SAFE_TOOLS (framework's static list, before the
    #    connector's register functions append to it).
    from fsr_playbooks.llm.tools import SAFE_TOOLS, TOOL_TIERS
    base = list(SAFE_TOOLS)
    base_set = set(base)

    # 2. Call the connector's register functions.
    from fsr_soc_triage.registry import (
        register_triage_tools,
        register_record_tools,
        register_ztpf_tools,
        register_playbook_tools,
    )
    register_triage_tools()
    register_record_tools()
    register_ztpf_tools()
    register_playbook_tools()

    # 3. The late-registered tools.
    late = sorted(set(SAFE_TOOLS) - base_set)
    late_tiers = {n: TOOL_TIERS.get(n, 0) for n in late}

    # 4. Which register function added each tool (for traceability).
    #    Re-derive from the source: each register function appends a known set.
    #    (register_triage_tools only wires hooks; the hunt tools are the
    #    framework's own.)
    record_names = [n for n in ("create_record", "update_record") if n in late]
    ztpf_names = [n for n in ("test_template",) if n in late]
    playbook_names = [n for n in ("resume_playbook", "list_module_playbooks") if n in late]

    manifest = {
        "source": "fsr_soc_triage.registry -- register_triage_tools + register_record_tools + register_ztpf_tools + register_playbook_tools",
        "note": ("The tools the connector registers into the framework's "
                 "SAFE_TOOLS/TOOL_TIERS at runtime. The framework advertises "
                 f"{len(base)} at import (fsr_playbooks.llm.tools.SAFE_TOOLS); "
                 f"a deployed agent sees {len(SAFE_TOOLS)}. This manifest lets "
                 "the census and lint know the truth without a live turn "
                 "(tracker #67). Regenerate with scripts/export_registry_manifest.py "
                 "in the connector repo."),
        "framework_safe_tools_count": len(base),
        "connector_registered_count": len(late),
        "runtime_total": len(SAFE_TOOLS),
        "tools": late,
        "tiers": late_tiers,
        "registered_by": {
            "register_record_tools": record_names,
            "register_ztpf_tools": ztpf_names,
            "register_playbook_tools": playbook_names,
        },
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out}")
    print(f"  framework base: {len(base)} tools")
    print(f"  connector-registered: {len(late)} tools")
    print(f"  runtime total: {len(SAFE_TOOLS)} tools")
    unattributed = sorted(set(late) - set(record_names) - set(ztpf_names)
                          - set(playbook_names))
    if unattributed:
        print(f"  WARNING: registered by no known register_*_tools: {unattributed}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
