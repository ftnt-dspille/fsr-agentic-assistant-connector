"""A same-version re-ship that the platform re-mints is verified under the live number.

Live: `make ship BUMP=none` installed 0.6.20 onto a box already on 0.6.20; the
box stored it as 0.6.21, every worker ran the shipped package (build id
matched), and the worker check for 0.6.20 failed a ship that had landed.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "scripts" / "deploy.sh").read_text()


def _sed_expr() -> str:
    m = re.search(r"LIVE_VER=\"\$\(sed -n '([^']+)'", SRC)
    assert m, "deploy must read the live version from the install output"
    return m.group(1)


def test_the_live_version_is_read_from_the_install_line():
    out = subprocess.run(["sed", "-n", _sed_expr()], input=(
        "[1] install x 0.6.20 (x.tgz, 3587 KB) with $replace\n"
        "   installed: live version=0.6.21 (17s)\n"
        "   WARN: live version 0.6.21 != built 0.6.20\n"), capture_output=True, text=True).stdout
    assert out.strip() == "0.6.21"


def test_verify_warmup_and_smoke_use_the_live_number():
    after = SRC[SRC.index('LIVE_VER="$(sed'):]
    assert 'VERIFY_VER="$LIVE_VER"' in after
    for line in ('--version "$VERIFY_VER" --tarball "$TGZ"',
                 'card_smoke.py" --version "$VERIFY_VER"',
                 'FSR_BUILDER_VERSION="$VERIFY_VER" "$PY" - "$ROOT"'):
        assert line in after, line
    assert '--version "$NEW" --tarball' not in after
