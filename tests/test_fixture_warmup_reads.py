"""The three reads the connector warmup makes in an offline turn are served by
the fixture box, and the warmup never writes a reference DB:

- `/api/3/staging_model_metadatas`, read-only from the reference data;
- `connector_details?configured=true`, from the bundle's declared connectors;
- the connector healthcheck, from the same declared list (not a live check).
"""
import fnmatch
import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from scripts import fixture_box as FB  # noqa: E402
from scripts.fixture_box import FixtureBox  # noqa: E402

U = "https://box.example.com"


def _world(*names, **extra):
    return FixtureBox({"modules": {}, "configured_connectors": [
        {"name": n, "version": "1.0.0", "label": n, **extra} for n in names]})


# -- staging_model_metadatas --------------------------------------------------
def test_staging_model_metadatas_is_served_with_modules_and_fields():
    box = FixtureBox({"modules": {}})
    s, body = box.get(
        f"{U}/api/3/staging_model_metadatas?$limit=2147483647&$orderby=type"
        "&$relationships=true")
    assert s == 200 and body["hydra:totalItems"] > 20
    by_type = {m["type"]: m for m in body["hydra:member"]}
    assert "alerts" in by_type
    assert any(a["name"] == "severity" for a in by_type["alerts"]["attributes"])
    assert not box.misses


def test_staging_model_metadatas_honours_the_type_in_filter():
    box = FixtureBox({"modules": {}})
    s, body = box.get(
        f"{U}/api/3/staging_model_metadatas?type$in=alerts|incidents&$limit=500")
    assert s == 200
    assert {m["type"] for m in body["hydra:member"]} == {"alerts", "incidents"}


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_warmup_write_back_leaves_modules_and_picklists_as_they_were(tmp_path):
    """The warmup rewrites the modules, fields and picklists it reads. Served
    from the reference data, that rewrite must keep every module and every
    picklist item, and must touch only the copy it is given."""
    from fsr_playbooks._db import default_db_path
    import ops_warmup
    ref = default_db_path()
    before_sha = _sha(ref)
    copy = tmp_path / "ref.db"
    shutil.copyfile(ref, copy)

    def counts(db):
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            return (c.execute("SELECT COUNT(*) FROM modules").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM picklists").fetchone()[0],
                    sorted(r[0] for r in c.execute("SELECT name FROM modules")))

    want = counts(copy)
    box = FixtureBox({"modules": {}})
    out = ops_warmup._warmup_modules_picklists(
        lambda url, method="GET", body=None: box.get(url)[1], copy)
    assert "error" not in out, out
    got = counts(copy)
    assert got[0] == want[0] and got[2] == want[2]
    assert got[1] == want[1]
    assert out["module_fields"] > 0
    assert _sha(ref) == before_sha, "the warmup wrote the reference DB"


# -- connector_details?configured=true ---------------------------------------
def test_configured_read_lists_the_declared_connectors():
    box = _world("virustotal", "servicenow")
    got = box._declared_inventory(
        "POST", "/api/integration/connector_details", {"configured": "true"})
    assert [c["name"] for c in got["data"]] == ["virustotal", "servicenow"]


def test_an_explicit_empty_declaration_is_served_and_an_absent_one_is_not():
    """`configured_connectors: []` is a scenario (nothing configured) and is
    answered; no declaration is no answer (a visible miss), never an implicit
    empty list."""
    q = {"configured": "true"}
    path = "/api/integration/connector_details"
    empty = FixtureBox({"modules": {}, "configured_connectors": []})
    assert empty._declared_inventory("POST", path, q) == {"data": []}
    assert FixtureBox({"modules": {}})._declared_inventory("POST", path, q) is None


def test_every_bundle_declares_its_configured_connectors():
    """No implicit inventory: a bundle states what is configured. An explicit
    empty list is allowed (the scenario is an unconfigured box); a bundle with
    no declaration is not."""
    root = _REPO / "scripts" / "eval_fixtures" / "ztpf"
    for p in sorted(root.glob("*.json")):
        b = FB.load_bundle(p.stem)
        assert isinstance(b.get("configured_connectors"), list), (
            f"{p.stem} declares no configured_connectors (use [] if the "
            "scenario is an unconfigured box)")


def test_every_bundle_with_ops_declares_the_connectors_its_ops_call():
    """configured_connectors is inferred from the ops a bundle records: every
    connector an op names (a glob such as `fortinet-fortisiem*` included) is
    declared. A bundle with no ops declares none."""
    root = _REPO / "scripts" / "eval_fixtures" / "ztpf"
    checked = 0
    for p in sorted(root.glob("*.json")):
        b = FB.load_bundle(p.stem)
        ops = {o["connector"] for o in b.get("ops") or []}
        declared = [c["name"] for c in b.get("configured_connectors") or []]
        for pat in ops:
            assert any(fnmatch.fnmatch(d, pat) for d in declared), (
                f"{p.stem}: ops call {pat!r} but it declares {declared}")
            checked += 1
    assert checked > 10


# -- healthcheck ---------------------------------------------------------------
def test_healthcheck_is_answered_from_the_declared_connectors():
    box = _world("virustotal")
    path = "/api/integration/connectors/healthcheck/virustotal/1.0.0/"
    got = box._declared_inventory("GET", path, {})
    assert got["status"] == "Available"


def test_healthcheck_reports_a_declared_health_and_misses_an_undeclared_connector():
    box = _world("virustotal", health="Disconnected")
    path = "/api/integration/connectors/healthcheck/virustotal/1.0.0/"
    assert box._declared_inventory("GET", path, {})["status"] == "Disconnected"
    other = "/api/integration/connectors/healthcheck/slack/1.0.0/"
    assert box._declared_inventory("GET", other, {}) is None


# -- offline turns never write a reference DB ----------------------------------
def test_an_explicit_reference_db_is_copied_not_written_by_the_offline_catalog(
        tmp_path, monkeypatch):
    """The offline turn's warmup writes the catalog it runs on. Even when the
    caller points FSRPB_DB at a catalog of their own, the turn runs on a copy."""
    from fsr_playbooks._db import default_db_path
    from scripts import local_turn as LT
    mine = tmp_path / "mine.db"
    shutil.copyfile(default_db_path(), mine)
    monkeypatch.setenv("FSRPB_DB", str(mine))
    monkeypatch.delenv("FSR_REFERENCE_DB", raising=False)
    monkeypatch.setattr(LT, "_SELF_CATALOG", None)
    monkeypatch.setattr(LT, "_SELF_CATALOG_TRIED", None)
    path = LT._self_catalog_path()
    assert path and Path(path).resolve() != mine.resolve()
