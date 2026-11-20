"""The box lease (scripts/box.sh): one agent's live checks cannot run against
another agent's deploy. A push or stamp takes the lease; another holder is
refused, naming who holds it; the same holder renews; a lapsed lease is free."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOX_SH = ROOT / "scripts" / "box.sh"


def _lease(tmp, holder, fn="bx_lease_take", box="92", ttl="1200", extra=""):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FSR_LEASE", "AGENT_TREES"))}
    env.update(FSR_LEASE_DIR=str(tmp), FSR_LEASE_HOLDER=holder, FSR_LEASE_TTL=ttl)
    return subprocess.run(["bash", "-c", f'source "{BOX_SH}"; {fn} {box} {extra}; rc=$?; echo "LEASE=$BOX_LEASE"; exit $rc'],
                          capture_output=True, text=True, env=env)


def test_a_second_holder_is_refused_and_told_who_holds_it(tmp_path):
    a = _lease(tmp_path, "tree:a")
    assert a.returncode == 0 and "LEASE=tree:a#1" in a.stdout
    b = _lease(tmp_path, "tree:b")
    assert b.returncode == 2
    assert "held by tree:a" in b.stderr and "make box-release BOX=92 FORCE=1" in b.stderr


def test_the_holder_renews_without_a_new_generation(tmp_path):
    _lease(tmp_path, "tree:a")
    again = _lease(tmp_path, "tree:a")
    assert again.returncode == 0 and "LEASE=tree:a#1" in again.stdout


def test_a_lapsed_lease_is_free_and_starts_a_new_generation(tmp_path):
    _lease(tmp_path, "tree:a", ttl="-1")
    b = _lease(tmp_path, "tree:b")
    assert b.returncode == 0 and "LEASE=tree:b#2" in b.stdout


def test_release_is_the_holders_unless_forced(tmp_path):
    _lease(tmp_path, "tree:a")
    assert _lease(tmp_path, "tree:b", fn="bx_lease_release").returncode == 2
    assert _lease(tmp_path, "tree:b", fn="bx_lease_release", extra="force").returncode == 0
    assert _lease(tmp_path, "tree:b").returncode == 0


def test_boxes_are_leased_independently(tmp_path):
    assert _lease(tmp_path, "tree:a", box="92").returncode == 0
    assert _lease(tmp_path, "tree:b", box="81").returncode == 0


def test_the_holder_defaults_to_the_agent_tree_from_the_cwd(tmp_path):
    trees = tmp_path / "trees"; (trees / "smoke" / "connector").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FSR_LEASE",))}
    env.update(AGENT_TREES=str(trees))
    r = subprocess.run(["bash", "-c", f'source "{BOX_SH}"; bx_lease_holder'], cwd=trees / "smoke" / "connector",
                       capture_output=True, text=True, env=env)
    assert r.stdout.strip() == "tree:smoke"
