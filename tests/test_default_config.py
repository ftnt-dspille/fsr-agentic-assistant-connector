"""Every push makes the default connector config Anthropic + Haiku.

Paired cases: the default, the model override, the opt-out, and that deploy.sh
really routes the default through it (name 'fsrpb-live', no --no-default).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SH = ROOT / "scripts" / "default_config.sh"


def _args(**env):
    e = {k: v for k, v in os.environ.items()
         if k not in ("KEEP_DEFAULT_CONFIG", "SOC_DEFAULT_MODEL")}
    e.update(env)
    r = subprocess.run(["bash", "-c", f"source {SH}; default_config_args"],
                       capture_output=True, text=True, env=e)
    return r.returncode, r.stdout.split()


def test_default_is_anthropic_haiku():
    rc, a = _args()
    assert rc == 0
    assert a == ["--config-only", "--with-config", "--provider", "anthropic",
                 "--model", "claude-haiku-5-5"]
    assert "--no-default" not in a


def test_model_is_overridable():
    rc, a = _args(SOC_DEFAULT_MODEL="claude-sonnet-5")
    assert a[a.index("--model") + 1] == "claude-sonnet-5"


def test_opt_out_leaves_default_alone():
    rc, a = _args(KEEP_DEFAULT_CONFIG="1")
    assert rc == 1 and a == []


def test_haiku_default_passes_the_connector_health_check():
    # the discouraged 4.5 id is refused by the health check; the default must not be it
    from fsr_playbooks.llm.model_catalog import check
    assert check("claude-haiku-5-5", "anthropic").ok


def test_deploy_installs_fsrpb_live_through_it():
    s = (ROOT / "scripts" / "deploy.sh").read_text()
    assert "default_config.sh" in s
    assert 'FSR_BUILDER_CONFIG_NAME="$DEFAULT_CONFIG_NAME"' in s
    assert "default_config_args" in s
    assert 'DEFAULT_CONFIG_NAME="fsrpb-live"' in SH.read_text()
