#!/usr/bin/env python3
"""Draft a module-scoped assistant persona for a task, review it, optionally save it.

This is the P1 CLI over ``fsr_soc_triage.persona_author`` (see
docs/PERSONA_BUILDER_PLAN.md): the assistant authors a good, knowledge-rich
persona instead of you hand-writing Key Store JSON.

  author_persona.py --box 92 --module ztpf_templates \
      --goal "help author and test ZTPF device config templates"

It drafts, prints the persona and what it would grant, and lints it. With
``--save`` it writes the Key Store record (refused if the lint has any error).
Saving here is a dev convenience; the runtime offer-card path (P2) puts the same
save behind the tier-3 approval card so a human confirms the tools and write
scope before it lands.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONN = HERE.parent
sys.path.insert(0, str(CONN))


def _box_env_file(alias: str) -> str:
    out = subprocess.run(["bash", str(CONN / "scripts" / "box.sh"), "--need", "env", "--print"],
                         env={**os.environ, "BOX": alias}, capture_output=True, text=True)
    env = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
    envf = env.get("BOX_ENV", "")
    if not envf or envf == "-":
        sys.exit(f"box {alias}: no pyfsr env file (run `make doctor BOX={alias}` first)")
    return envf


def _build_client(alias: str):
    from pyfsr import FortiSOAR
    return FortiSOAR.from_env_file(_box_env_file(alias))


def _build_provider(llm: str):
    """Build the drafting provider through the shared harness resolver, so the CLI
    picks the same model/key every other harness does. Frank (free) by default."""
    from fsr_playbooks.harness.llm import LLMConfigError, resolve_llm
    from fsr_playbooks.llm.factory import _REGISTRY
    try:
        r = resolve_llm(llm, None, None, env=dict(os.environ))
    except LLMConfigError as exc:
        sys.exit(f"could not resolve LLM {llm!r}: {exc}\n"
                 f"For --llm frank set FRANK_BASE_URL/FRANK_API_KEY (VPN on).")
    # Build the provider straight from the registry factory (no box ConfigProvider
    # locally); frank/openai are the OpenAI-compatible provider.
    import inspect
    fac = _REGISTRY[r.provider]
    kwargs = {"base_url": r.base_url, "api_key": r.api_key, "model": r.model}
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    sig = inspect.signature(fac)
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fac(**kwargs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--box", required=True, help="experimental box alias (scripts/box.sh --list)")
    ap.add_argument("--module", required=True, help="the module this persona mounts on")
    ap.add_argument("--goal", required=True, help="the task the assistant should be expert at")
    ap.add_argument("--llm", default="frank", help="frank|openai|anthropic (default: frank, free)")
    ap.add_argument("--samples", type=int, default=3, help="records to read for the data model")
    ap.add_argument("--save", action="store_true", help="write the Key Store record (refused on lint error)")
    args = ap.parse_args(argv)

    from fsr_soc_triage import persona_author as pa

    client = _build_client(args.box)
    knowledge = pa.gather_module_knowledge(client, args.module, samples=args.samples)
    print(f"Module {args.module}: {len(knowledge['fields'])} fields, "
          f"{len(knowledge['relations'])} relation(s) read.")

    provider = _build_provider(args.llm)
    rec, issues = pa.draft_persona(provider, args.module, args.goal, knowledge=knowledge)
    if rec is None:
        sys.exit("the model did not return a usable persona; try rephrasing --goal.")

    print("\n" + "=" * 60 + "\nDRAFTED PERSONA\n" + "=" * 60)
    print(pa.summarize(rec))
    print("\n--- system prompt ---\n" + rec["value"])
    print("\n--- jSONValue ---\n" + json.dumps(rec.get("jSONValue", {}), indent=2))

    errors = [i for i in issues if i.level == "error"]
    if issues:
        print("\n--- lint ---")
        for i in issues:
            print(f"  [{i.level}] {i.field}: {i.message}")

    if not args.save:
        print("\n(not saved; re-run with --save to write the Key Store record)")
        return 0
    if errors:
        sys.exit("\nrefusing to save: fix the lint errors above first.")

    key = pa.key_for(args.module)
    existing = client.get("/api/3/keys", params={"key": key, "$limit": 1})
    members = existing.get("hydra:member", []) if isinstance(existing, dict) else existing
    payload = {"value": rec["value"], "jSONValue": rec.get("jSONValue", {})}
    if members:
        client.put(members[0]["@id"], data=payload)
        print(f"\nupdated persona {key}")
    else:
        client.post("/api/3/keys", data={"name": key, "key": key, **payload})
        print(f"\ncreated persona {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
