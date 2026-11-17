#!/usr/bin/env bash
# Which LLM config a push makes the connector DEFAULT.
#
# Every push ensures the default config 'fsrpb-live' is Anthropic + Haiku,
# refreshed in place by name (install_to_fsr upserts, so no duplicates).
#   SOC_DEFAULT_MODEL=<id>   override the model (default below)
#   KEEP_DEFAULT_CONFIG=1    leave the box's default config alone
#
# claude-haiku-5-5 is the current Haiku. It is not in the framework catalog yet,
# so the connector's health check treats it as "unknown" (a warning, allowed).
# The older claude-haiku-4-5-20251001 is catalogued 'discouraged' and the health
# check REFUSES it, so it must not be the default.
DEFAULT_CONFIG_NAME="fsrpb-live"
DEFAULT_CONFIG_PROVIDER="anthropic"
DEFAULT_CONFIG_MODEL_FALLBACK="claude-haiku-5-5"

# Prints the install_to_fsr.py arguments, one per line; returns 1 (prints
# nothing) when the opt-out is set. --config-only: the tarball was already
# installed once by the fallback step; a second $replace install bumps the live
# version past the built one and fails worker verification. The config name goes via
# FSR_BUILDER_CONFIG_NAME=$DEFAULT_CONFIG_NAME.
default_config_args() {
    [[ "${KEEP_DEFAULT_CONFIG:-}" == "1" ]] && return 1
    printf '%s\n' --config-only --with-config --provider "$DEFAULT_CONFIG_PROVIDER" \
        --model "${SOC_DEFAULT_MODEL:-$DEFAULT_CONFIG_MODEL_FALLBACK}"
}
