"""Install (with $replace) the built connector tarball onto a live FortiSOAR,
recycle the integrations workers, and optionally create/refresh a configuration.

Thin wrapper over the **pyfsr** SDK — the install, worker-recycle, and config
upsert are all pyfsr ConnectorsAPI calls now (no hand-built requests):

    client.connectors.install_from_file(tgz, replace=True, wait=True)
    client.connectors.republish(NAME)                       # recycle workers
    client.connectors.upsert_configuration(NAME, cfg, name=..., default=...)

Repeatable: $replace upgrades in place; upsert updates the named config rather
than duplicating it (and tolerates the connector's post-save warmup-hook 500).

Auth + target come from the env via pyfsr's EnvConfig (FSR_BASE_URL, FSR_API_KEY
or FSR_USERNAME/FSR_PASSWORD, FSR_VERIFY_SSL, FSR_PORT). Run:

    set -a && . /path/to/.env && set +a
    python scripts/install_to_fsr.py                 # install/replace only
    python scripts/install_to_fsr.py --with-config    # + create/refresh config
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import urllib3

# Same rule as scripts/via_make_guard.sh: under Claude Code this runs only from
# a make target (GNU make exports MAKELEVEL to every recipe).
if os.environ.get("CLAUDECODE") and not os.environ.get("MAKELEVEL") and os.environ.get("VIA_MAKE") != "1":
    sys.exit("refused: install_to_fsr.py is run through make (make deploy BOX=<box>), not directly.")

from pyfsr.config import EnvConfig
from pyfsr.exceptions import FortiSOARException

# SOAR boxes use self-signed certs, so every pyfsr call to them (verify_ssl
# off) prints an InsecureRequestWarning. The publish flow makes several, which
# buries the real progress lines -- silence just that one warning.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = Path(__file__).resolve().parent.parent
NAME = "connector-fsr-agentic-assistant"
CONFIG_NAME = os.environ.get("FSR_BUILDER_CONFIG_NAME", "fsrpb-live")

# Generous client timeout: entering the Connector Studio dev workspace (the
# worker-recycle in republish) clones the whole installed tree — slow for a
# connector that ships a wheel + reference DB — and the tgz upload is large too.
_CLIENT_TIMEOUT = int(os.environ.get("FSR_BUILDER_TIMEOUT", "600"))


def _version() -> str:
    return json.loads((ROOT / NAME / "info.json").read_text())["version"]


def _best_model(provider: str) -> str:
    """The best-evidenced model for `provider`, read from the catalog.

    These defaults used to be literals -- `gpt-4.1-mini` and
    `claude-haiku-4-5-20251001` -- and both were later MEASURED failing and
    demoted to `discouraged`. The installer went on creating configs pinned to
    them, so a fresh install produced a configuration the connector's own
    health check refuses: the assistant dead on arrival, for a reason that
    looks nothing like a model choice. It shipped that way on fsr159.

    Reading the catalog instead means a demotion moves the default with it.
    `allowed_models` is ordered best-evidence-first and never returns a
    discouraged id.
    """
    from fsr_playbooks.llm.model_catalog import allowed_models

    got = allowed_models(provider)
    if not got:
        raise SystemExit(
            f"no non-discouraged model in the catalog for {provider!r} -- "
            f"pass --model explicitly")
    return got[0]


def _config_values(args) -> tuple[dict, str] | None:
    """Build the provider-aware config field map + a human description, or None
    (with a printed reason) when the provider's API key isn't set.

    The info.json schema gates the provider-specific fields behind
    ``llm_provider``'s onchange map, so we set ``llm_provider`` explicitly and
    submit only that provider's keys.
    """
    if args.provider == "openai":
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            print("[3] --with-config --provider openai given but OPENAI_API_KEY not set; skipping")
            return None
        model = (args.model or os.environ.get("FSR_BUILDER_MODEL")
                 or _best_model("openai"))
        base_url = args.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        cfg = {"llm_provider": "openai", "openai_api_key": key,
               "openai_model": model, "openai_base_url": base_url}
        return cfg, f"provider=openai model={model} base_url={base_url}"
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("[3] --with-config given but ANTHROPIC_API_KEY not set; skipping")
        return None
    model = (args.model or os.environ.get("FSR_BUILDER_MODEL")
             or _best_model("anthropic"))
    cfg = {"llm_provider": "anthropic", "anthropic_api_key": key, "model": model}
    base_url = args.base_url or os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        cfg["anthropic_base_url"] = base_url
    desc = f"provider=anthropic model={model}" + (f" base_url={base_url}" if base_url else "")
    return cfg, desc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tarball", help="path to .tgz (default: dist/<name>-<ver>.tgz)")
    ap.add_argument("--with-config", action="store_true",
                    help="create/refresh a config with the selected provider's key")
    ap.add_argument("--provider", choices=("anthropic", "openai"),
                    default=os.environ.get("FSR_LLM_PROVIDER", "anthropic"),
                    help="which LLM backend the config selects (default: anthropic). "
                         "anthropic uses ANTHROPIC_API_KEY (+ --model, ANTHROPIC_BASE_URL); "
                         "openai uses OPENAI_API_KEY (+ --model, OPENAI_BASE_URL).")
    ap.add_argument("--base-url", default=None,
                    help="override the provider base URL (else ANTHROPIC_BASE_URL / "
                         "OPENAI_BASE_URL env, else the connector default)")
    ap.add_argument("--no-default", action="store_true",
                    help="create/refresh the config WITHOUT marking it the connector default")
    ap.add_argument("--no-publish", action="store_true",
                    help="skip the post-install worker-recycle (leaves uwsgi workers "
                         "on possibly-stale cached code)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--config-only", action="store_true",
                    help="skip the install + worker recycle (a second $replace install "
                         "bumps the box's live version past the built one); only "
                         "create/refresh the config against the version already live")
    args = ap.parse_args(argv)

    version = _version()
    tgz = Path(args.tarball) if args.tarball else ROOT / "dist" / f"{NAME}-{version}.tgz"
    if not tgz.is_file() and not args.config_only:
        sys.exit(f"tarball not found: {tgz} (run scripts/build.sh)")

    try:
        client = EnvConfig.from_env().client(timeout=_CLIENT_TIMEOUT)
    except ValueError as e:
        sys.exit(str(e))
    conns = client.connectors

    if args.config_only:
        conns.clear_cache()
        version = conns.resolve_version(NAME)
        print(f"[1] --config-only: no install; configuring live {NAME} {version}")
    else:
        print(f"[1] install {NAME} {version} ({tgz.name}, {tgz.stat().st_size // 1024} KB) "
              f"with $replace → {client.base_url}")
        _t = time.time()
        try:
            conns.install_from_file(str(tgz), replace=True, wait=True, timeout=_CLIENT_TIMEOUT)
        except FortiSOARException as e:
            print(f"   FAILED after {time.time() - _t:.0f}s: {e.status_code} {e.message}")
            return 1
        conns.clear_cache()
        live_ver = conns.resolve_version(NAME)
        print(f"   installed: live version={live_ver} ({time.time() - _t:.0f}s)")
        if live_ver != version:
            print(f"   WARN: live version {live_ver} != built {version}")

        if not args.no_publish:
            print("[2] republish to recycle integrations workers (clear ghost bytecode)")
            try:
                res = conns.republish(NAME)
                print(f"   publish OK (dev_id={res.get('dev_id')}) — workers recycling (~5s)")
            except Exception as e:  # noqa: BLE001 — install already succeeded; recycle is best-effort
                print(f"   WARN: worker-recycle failed ({type(e).__name__}: {e}); "
                      "workers may serve stale code until the next restart")

    if args.with_config:
        built = _config_values(args)
        if built is None:
            return 1 if args.config_only else 0
        cfg_values, desc = built
        print(f"[3] create/refresh config {CONFIG_NAME!r} ({desc})")
        try:
            # validate=False: the connector's config schema gates fields behind an
            # onchange map and we submit exactly one provider's keys; let the
            # platform be the validator (matches the prior raw-requests behavior).
            cfg = conns.upsert_configuration(
                NAME, cfg_values, name=CONFIG_NAME, version=version,
                default=not args.no_default, validate=False)
        except FortiSOARException as e:
            print(f"   config FAILED: {e.status_code} {e.message}")
            return 1
        cid = cfg.get("config_id") or cfg.get("id") if hasattr(cfg, "get") else getattr(cfg, "config_id", None)
        print(f"   config_id={cid} {desc}")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
