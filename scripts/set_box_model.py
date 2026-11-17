#!/usr/bin/env python3
"""Change the LLM model on an EXISTING Agentic-Assistant config, in place.

Reads the connector's current configuration off the box and PUTs it back with
only the model field changed -- so the encrypted API key, base URL, MCP tier and
every other field survive untouched (and never pass through a shell argv or a
transcript). Rolling back is the same command with the old model id.

Why a script and not a curl: rebuilding the config by hand means retyping the
secret. Read-modify-write is the only way to change one field safely.

Usage (source the target box's env first):

    set -a; . ../../fsr-playbook-framework/.env.ztpf-8.0; set +a
    .venv/bin/python scripts/set_box_model.py --model gpt-5-mini
    .venv/bin/python scripts/set_box_model.py --model gpt-5-mini --rename fsrpb-5mini
    .venv/bin/python scripts/set_box_model.py --show

`--config NAME` targets a non-default config; without it the connector's
default config is used (the one the widget/UI chat actually runs on).
"""
from __future__ import annotations

import argparse
import sys

import urllib3
from pyfsr.config import EnvConfig

urllib3.disable_warnings()

CONNECTOR = "connector-fsr-agentic-assistant"
# Which config field carries the model id, per provider. The info.json schema
# gates provider fields behind llm_provider's onchange map, so the field name
# differs by provider -- anthropic uses a bare `model`, openai `openai_model`.
MODEL_FIELD = {"openai": "openai_model", "anthropic": "model"}


def _configs(client) -> list[dict]:
    """Every configuration for this connector, across ALL pages.

    `?limit=200` is silently IGNORED -- the endpoint's page-size parameter is
    `page_size`, and unrecognized filters are dropped rather than rejected. So
    `?limit=200` yields the default page (`itemsPerPage` 30 on a stock box)
    over an unordered QuerySet. On a box with more configurations than that,
    this connector's config falls off page 1 -- `--show` omits it and
    `--model` reports "no config matching <default>", which reads exactly like
    the configuration having been deleted. It has not been.

    Paging is used here rather than `page_size` alone so the size of the box
    stops mattering. `client.connectors.list_configurations(connector=...)`
    is the typed equivalent, if this ever needs more than raw dicts.
    """
    rows: list[dict] = []
    page = 1
    while True:
        resp = client.get(f"/api/integration/configuration/?page={page}")
        rows += resp.get("data") or []
        if not resp.get("nextPage"):
            break
        page += 1
    conn_ids = {c.get("id") for c in
                (client.get(f"/api/integration/connectors/?name={CONNECTOR}").get("data") or [])}
    return [r for r in rows if r.get("connector") in conn_ids]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="model id to set, e.g. gpt-5-mini")
    ap.add_argument("--config", help="config name to change (default: the default config)")
    ap.add_argument("--rename", help="also rename the config (names often encode the model)")
    ap.add_argument("--show", action="store_true", help="print current configs and exit")
    args = ap.parse_args(argv)

    client = EnvConfig.from_env().client(timeout=60)
    rows = _configs(client)
    if not rows:
        print(f"no {CONNECTOR} configurations found on this box", file=sys.stderr)
        return 2

    if args.show or not args.model:
        for r in rows:
            cfg = r.get("config") or {}
            prov = cfg.get("llm_provider", "?")
            model = cfg.get(MODEL_FIELD.get(prov, "openai_model")) or cfg.get("model")
            print(f"  {'*' if r.get('default') else ' '} {r.get('name')}  "
                  f"provider={prov} model={model} config_id={r.get('config_id')}")
        print("\n  (* = default: the config the UI chat runs on)")
        return 0 if args.show else 2

    if args.config:
        target = next((r for r in rows if r.get("name") == args.config), None)
    else:
        target = next((r for r in rows if r.get("default")), None)
    if target is None:
        print(f"no config matching {args.config or '<default>'}", file=sys.stderr)
        return 2

    cfg = dict(target.get("config") or {})
    provider = cfg.get("llm_provider", "openai")
    field = MODEL_FIELD.get(provider, "openai_model")
    was = cfg.get(field)
    if was == args.model:
        print(f"{target['name']}: already {field}={args.model}, nothing to do")
        return 0
    cfg[field] = args.model

    # Upsert by NAME via pyfsr, not a hand-rolled PUT to the configuration id:
    # that PUT 400s (the endpoint wants the platform's own envelope), and the
    # connector's post-save warmup hook can 500 on a write that actually landed.
    # upsert_configuration handles both. validate=False because the config
    # schema gates provider fields behind llm_provider's onchange map.
    name = args.rename or target.get("name")
    res = client.connectors.upsert_configuration(
        CONNECTOR, cfg, name=name, default=bool(target.get("default")),
        validate=False)
    cid = res.get("config_id") or res.get("id") if hasattr(res, "get") else None
    print(f"{name}: {field} {was} -> {args.model}  (config_id={cid})"
          + (f"  [new config; {target['name']} left intact for rollback]"
             if args.rename else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
