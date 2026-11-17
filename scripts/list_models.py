#!/usr/bin/env python3
"""List the models the Frank gateway's virtual key actually permits.

Worth a target of its own because a STALE model id does not fail like a config
error -- it 403s with "Provider 'X' is not allowed for this virtual key", which
reads like a broken harness rather than a wrong id, and (when only some turns
carry it) like a model that fell apart after run 1. Checking the live list is
seconds; re-deriving that trap from symptoms has cost hours.

Credentials are read from the environment / the framework repo's .env -- never
passed on a command line, where they would land in shell history and logs.

Usage:  make models
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The framework repo's .env is the canonical home for FRANK_* (gitignored).
# FSR_PLAYBOOK_FRAMEWORK points at that checkout (the same variable pyfsr reads);
# the default is the sibling checkout under ~/PycharmProjects.
_FRAMEWORK_ENV = (
    Path(os.environ.get("FSR_PLAYBOOK_FRAMEWORK")
         or Path.home() / "PycharmProjects" / "fsr-playbook-framework")
    / ".env"
)


def _env(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    for candidate in (Path(".env"), _FRAMEWORK_ENV):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            m = re.match(rf'\s*{name}\s*=\s*"?([^"\n#]+)"?', line)
            if m:
                return m.group(1).strip()
    return None


def main() -> int:
    base = _env("FRANK_BASE_URL") or _env("OPENAI_ENDPOINT")
    key = _env("FRANK_API_KEY") or _env("OPENAI_API_KEY")
    if not base or not key:
        print("need FRANK_BASE_URL + FRANK_API_KEY (env or framework .env)",
              file=sys.stderr)
        return 2

    req = urllib.request.Request(
        base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"gateway returned {e.code}: {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"could not reach {base}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    ids = sorted(m["id"] for m in data.get("data", []) if m.get("id"))
    configured = _env("FRANK_MODEL")
    print(f"gateway: {base}")
    print(f"permitted models ({len(ids)}):")
    for i in ids:
        mark = "  ← FRANK_MODEL" if i == configured else ""
        print(f"  · {i}{mark}")
    if configured and configured not in ids:
        print(f"\n⚠ FRANK_MODEL={configured!r} is NOT in the permitted list -- "
              f"turns using it will 403 (and read as a broken harness).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
