"""Auto-rewarm on instance mismatch.

`_warmup_needed` fires on a warm-derivable table that is *empty* (or on the
module-fields marker being absent). A catalog warmed from a DIFFERENT SOAR than
the configured target ($FSR_BASE_URL) is *full* but wrong -- its picklist IRIs /
connector configs mis-resolve -- yet the emptiness gate never notices, so the
stale-instance catalog used to sit there forever while the compiler only warned. `_warmup_instance_mismatch` is the
identity sibling that drives a re-warm so a wrong-instance catalog self-heals.

These tests cover the pure gating helper against a stamped `_catalog_meta`
table; the live warm path (crudhub) is exercised elsewhere.
"""
import importlib
import sqlite3
import sys
import types
from pathlib import Path


# operations.py uses relative imports (`from .pydantic_models import ...`), so
# it must load inside a package. The connector dir name has a dash (illegal as
# a module name) and ships no __init__.py -- mirror the sidecar/box loader by
# registering a synthetic `fsrpb_connector` package anchored at the dir.
CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)
if CONNECTOR_DIR not in sys.path:
    sys.path.insert(0, CONNECTOR_DIR)
if "fsrpb_connector" not in sys.modules:
    _pkg = types.ModuleType("fsrpb_connector")
    _pkg.__path__ = [CONNECTOR_DIR]
    sys.modules["fsrpb_connector"] = _pkg

ops = importlib.import_module("fsrpb_connector.operations")  # noqa: E402
from fsr_playbooks import _catalog_meta  # noqa: E402


def _stamped_db(tmp_path, base_url: str) -> Path:
    """A minimal reference DB carrying only the instance stamp."""
    db = tmp_path / "ref.db"
    with sqlite3.connect(db) as con:
        _catalog_meta.stamp_instance(con, instance_label=None, base_url=base_url)
        con.commit()
    return db


class TestInstanceMismatchGate:
    def test_mismatch_true_when_target_differs(self, tmp_path, monkeypatch):
        db = _stamped_db(tmp_path, "https://10.0.0.205")
        monkeypatch.setenv("FSR_BASE_URL", "https://10.0.0.159")
        assert ops._warmup_instance_mismatch(db) is True

    def test_mismatch_false_when_target_matches(self, tmp_path, monkeypatch):
        # normalize_base_url ignores scheme + trailing slash → still a match.
        db = _stamped_db(tmp_path, "https://10.0.0.159")
        monkeypatch.setenv("FSR_BASE_URL", "http://10.0.0.159/")
        assert ops._warmup_instance_mismatch(db) is False

    def test_no_op_when_target_unset(self, tmp_path, monkeypatch):
        # On-platform: FSR_BASE_URL unset → no explicit target → never re-warm.
        db = _stamped_db(tmp_path, "https://10.0.0.205")
        monkeypatch.delenv("FSR_BASE_URL", raising=False)
        assert ops._warmup_instance_mismatch(db) is False

    def test_no_op_when_catalog_unstamped(self, tmp_path, monkeypatch):
        db = tmp_path / "ref.db"
        with sqlite3.connect(db) as con:  # table exists, no stamp
            _catalog_meta.ensure_table(con)
            con.commit()
        monkeypatch.setenv("FSR_BASE_URL", "https://10.0.0.159")
        assert ops._warmup_instance_mismatch(db) is False

    def test_no_op_when_db_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FSR_BASE_URL", "https://10.0.0.159")
        assert ops._warmup_instance_mismatch(tmp_path / "missing.db") is False

    def test_restamp_clears_mismatch(self, tmp_path, monkeypatch):
        # The re-stamp the warmup op performs after a successful warm must flip
        # the gate from mismatch→ok, so the auto-rewarm doesn't loop.
        db = _stamped_db(tmp_path, "https://10.0.0.205")
        monkeypatch.setenv("FSR_BASE_URL", "https://10.0.0.159")
        assert ops._warmup_instance_mismatch(db) is True
        with sqlite3.connect(db) as con:
            _catalog_meta.stamp_instance(con, instance_label=None,
                                         base_url=ops._warmup_target_base_url())
            con.commit()
        assert ops._warmup_instance_mismatch(db) is False


class TestTargetBaseUrl:
    def test_reads_fsr_base_url(self, monkeypatch):
        monkeypatch.setenv("FSR_BASE_URL", "  https://box/  ")
        assert ops._warmup_target_base_url() == "https://box/"

    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("FSR_BASE_URL", raising=False)
        assert ops._warmup_target_base_url() == ""
