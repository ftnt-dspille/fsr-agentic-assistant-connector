"""warmup must never clobber the framework's full dev reference cache.

In a source checkout `_reference_db_path()` resolves to the ~700-connector dev
corpus the compiler and the framework's tooling gate depend on; a box sync of a
handful of installed connectors would silently gut it. On a real appliance the
path is the packaged slim DB, so the guard is inert there.
"""
from pathlib import Path

from connector_fsr_agentic_assistant import operations as ops
from fsr_playbooks._db import REPO_PROBED_DB


def test_refuses_dev_reference_cache():
    r = ops._warmup_clobber_refusal(REPO_PROBED_DB)
    assert isinstance(r, dict)
    assert r.get("error", {}).get("code") == "warmup_clobber_guard", r


def test_allows_scratch_target():
    assert ops._warmup_clobber_refusal(Path("/tmp/fsr_scratch_ref.db")) is None


def test_opt_in_env_allows_in_place_rebuild(monkeypatch):
    monkeypatch.setenv("FSRPB_ALLOW_DEV_DB_CLOBBER", "1")
    assert ops._warmup_clobber_refusal(REPO_PROBED_DB) is None
