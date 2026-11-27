"""scripts/fetch_pinned_wheel.sh: a rebuild of the same exact pin copies the cached wheel instead of
downloading it again, a different pin downloads, and a failed download caches nothing.

The index is replaced by a fake `pip` (PIP=...) that records each download and writes a wheel with
the name pip would use, so no network is touched.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parent.parent / "scripts" / "fetch_pinned_wheel.sh"

# Fake `python -m pip download --no-deps --only-binary=:all: -d <dir> <pin>`.
_FAKE_PIP = r"""#!/bin/sh
[ "$1" = "-m" ] && [ "$2" = "pip" ] || exit 9
[ -n "$FAKE_PIP_FAIL" ] && exit 1
dir=""; pin=""
while [ $# -gt 0 ]; do
    case "$1" in -d) dir="$2"; shift ;; esac
    pin="$1"; shift
done
echo "$pin" >> "$FAKE_PIP_LOG"
name=$(echo "$pin" | sed 's/==.*//; s/-/_/g')
ver=$(echo "$pin" | sed 's/.*==//')
mkdir -p "$dir"
echo "wheel for $pin" > "$dir/${name}-${ver}-py3-none-any.whl"
"""


@pytest.fixture
def env(tmp_path):
    pip = tmp_path / "fakepip"
    pip.write_text(_FAKE_PIP)
    pip.chmod(0o755)
    log = tmp_path / "pip.log"
    return {"PIP": str(pip), "FAKE_PIP_LOG": str(log)}, log


def _fetch(env, pin, dest, cache, extra=None):
    e = {k: v for k, v in os.environ.items() if k not in ("FAKE_PIP_FAIL",)}
    e.update(env, **(extra or {}))
    return subprocess.run(["bash", str(HELPER), pin, str(dest), str(cache)],
                          capture_output=True, text=True, env=e, timeout=120)


def _downloads(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_a_cold_pin_downloads_and_is_cached(tmp_path, env):
    e, log = env
    cache = tmp_path / "cache"
    r = _fetch(e, "fsr-playbooks==0.4.50", tmp_path / "d1", cache)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _downloads(log) == ["fsr-playbooks==0.4.50"]
    assert (tmp_path / "d1" / "fsr_playbooks-0.4.50-py3-none-any.whl").is_file()
    assert (cache / "fsr-playbooks-0.4.50" / "fsr_playbooks-0.4.50-py3-none-any.whl").is_file()


def test_the_same_pin_again_copies_from_the_cache_without_a_download(tmp_path, env):
    e, log = env
    cache = tmp_path / "cache"
    assert _fetch(e, "fsr-playbooks==0.4.50", tmp_path / "d1", cache).returncode == 0
    r = _fetch(e, "fsr-playbooks==0.4.50", tmp_path / "d2", cache)     # a fresh wheels/ dir
    assert r.returncode == 0, r.stdout + r.stderr
    assert _downloads(log) == ["fsr-playbooks==0.4.50"], "the cached pin must not be downloaded again"
    assert "cached:" in r.stdout
    assert (tmp_path / "d2" / "fsr_playbooks-0.4.50-py3-none-any.whl").read_text() == \
        (tmp_path / "d1" / "fsr_playbooks-0.4.50-py3-none-any.whl").read_text()


def test_a_different_pin_is_never_served_from_the_cache(tmp_path, env):
    e, log = env
    cache = tmp_path / "cache"
    assert _fetch(e, "fsr-playbooks==0.4.50", tmp_path / "d1", cache).returncode == 0
    r = _fetch(e, "fsr-playbooks==0.4.51", tmp_path / "d2", cache)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _downloads(log) == ["fsr-playbooks==0.4.50", "fsr-playbooks==0.4.51"]
    assert (tmp_path / "d2" / "fsr_playbooks-0.4.51-py3-none-any.whl").is_file()
    assert "0.4.51" in (tmp_path / "d2" / "fsr_playbooks-0.4.51-py3-none-any.whl").name


def test_a_failed_download_fails_and_caches_nothing(tmp_path, env):
    e, _ = env
    cache = tmp_path / "cache"
    r = _fetch(e, "fsr-playbooks==0.4.52", tmp_path / "d1", cache, extra={"FAKE_PIP_FAIL": "1"})
    assert r.returncode != 0
    assert not (cache / "fsr-playbooks-0.4.52").exists()


def test_a_non_exact_pin_is_refused(tmp_path, env):
    e, log = env
    r = _fetch(e, "fsr-playbooks>=0.4", tmp_path / "d1", tmp_path / "cache")
    assert r.returncode == 2
    assert _downloads(log) == []
