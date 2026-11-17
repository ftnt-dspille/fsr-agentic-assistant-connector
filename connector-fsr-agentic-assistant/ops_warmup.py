"""warmup domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from .pydantic_models import (
    WarmupParams,
)

from .ops_common import _check_params, _err, _log, _reference_db_path, _require_transport
from .ops_scheduling import _CATEGORY_TO_SAFETY



_WARMUP_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS warmup_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,          -- UTC ISO8601 of run completion
    forced          INTEGER NOT NULL DEFAULT 0,
    total_s         REAL,
    list_fetch_s    REAL,
    detail_loop_s   REAL,
    detail_calls    INTEGER,
    db_write_s      REAL,
    connectors      INTEGER,
    operations      INTEGER,
    operation_params INTEGER,
    op_safety       INTEGER,
    slowest_details TEXT,                   -- JSON: [{connector, s}, ...]
    timings_json    TEXT                    -- full timings dict, JSON
)
"""


def _record_warmup_run(db_path, timings: dict, counts: dict, forced: bool) -> None:
    """Append one telemetry row per warmup so slow runs can be analyzed later
    via `query_store` (e.g. `SELECT ts, total_s, detail_loop_s, slowest_details
    FROM warmup_runs ORDER BY id DESC LIMIT 20`). Best-effort and self-healing:
    creates the table if an older reference DB predates it, and never raises --
    telemetry must not fail the warmup it measures."""
    import sqlite3
    import time  # operations.py has no module-level `import time`; warmup
                 # imports it function-locally, so this helper must too.
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with sqlite3.connect(db_path) as con:
            con.execute(_WARMUP_RUNS_DDL)
            con.execute(
                "INSERT INTO warmup_runs (ts, forced, total_s, list_fetch_s, "
                "detail_loop_s, detail_calls, db_write_s, connectors, operations, "
                "operation_params, op_safety, slowest_details, timings_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, 1 if forced else 0,
                 timings.get("total_s"), timings.get("list_fetch_s"),
                 # detail_loop_s column now holds the bulk op-fetch time;
                 # detail_calls holds the count of per-connector FALLBACK POSTs.
                 timings.get("bulk_ops_fetch_s"),
                 timings.get("fallback_detail_calls"),
                 timings.get("db_write_s"),
                 counts.get("connectors"), counts.get("operations"),
                 counts.get("operation_params"), counts.get("op_safety"),
                 json.dumps(timings.get("slowest_details") or []),
                 json.dumps(timings)))
            con.commit()
    except Exception as e:  # noqa: BLE001 -- telemetry is best-effort
        _log(f"warmup: failed to record telemetry: {e!r}", "warning")


def _version_key(version) -> tuple:
    """Numeric sort key for a connector version ("6.10.0" > "6.9.2"); a string
    compare got that backwards. Non-numeric parts sort as 0."""
    import re as _re
    return tuple(int(x) for x in _re.findall(r"\d+", str(version or ""))) or (0,)


def _bulk_operations_map(mr, schemas_out: "dict | None" = None) -> "tuple[dict, list]":
    """Return ({connector_name: operations[]}, debug_calls) for ALL connectors
    in as few round-trips as possible.

    Replaces warmup's old per-connector detail loop (one POST per connector --
    ~4s each, ~175s total for 42 connectors). `POST /api/integration/
    connector_details/?format=json` with an empty body returns EVERY connector
    record WITH its operations[].parameters[] inline in a SINGLE call (~1s, 542
    ops verified live). Because we send NO `configured=true` filter, agent-bound
    connectors are included too -- that filter (not the agent binding itself) is
    what hides them from connector listings.

    Belt-and-braces for cross-instance safety: we ALSO call connector_details
    once per active agent (`&agent=<id>`) and merge any connector the global
    call missed, so an agent-proxied connector's ops are never dropped even on
    a build where the unfiltered call omits them. A few extra ~2s calls, not 42.
    Best-effort: any failure just yields a smaller map; warmup's per-connector
    fallback then fills the gaps."""
    calls: list = []  # (label, seconds)
    import time as _t
    best: dict = {}  # name -> (version_key, ops)

    def _ingest(rows):
        # FortiSOAR keeps versions side by side after an upgrade (both answer
        # here). Keep the HIGHEST version's operations -- the same record the
        # connectors row keeps -- or the catalog pairs the new version with
        # the old version's op list and rejects the new ops as unknown.
        for d in (rows or []):
            nm = d.get("name")
            ops = d.get("operations")
            if not (nm and ops):
                continue
            vk = _version_key(d.get("version"))
            if nm not in best or vk > best[nm][0]:
                best[nm] = (vk, ops)
                # The detail records carry the config SCHEMA (the listing
                # only has saved configs); hand it to the caller when asked.
                if schemas_out is not None:
                    cs = _config_schema_of(d)
                    if cs is not None:
                        schemas_out[nm] = cs
                    else:
                        schemas_out.pop(nm, None)

    # 1) Global, unfiltered -- one call returns all connectors + ops.
    try:
        t0 = _t.time()
        page = mr("/api/integration/connector_details/?format=json", "POST", body={}) or {}
        _ingest(page.get("data"))
        calls.append(("global", round(_t.time() - t0, 2)))
    except Exception:  # noqa: BLE001 -- fall through to per-agent + per-connector
        calls.append(("global", -1.0))

    # 2) Per active agent -- merge any agent-only connectors the global missed.
    try:
        agents = (mr("/api/3/agents", "GET") or {}).get("hydra:member") or []
        for a in agents:
            aid = a.get("agentId")
            if not (a.get("active") and aid):
                continue
            try:
                t0 = _t.time()
                page = mr(f"/api/integration/connector_details/?format=json&agent={aid}",
                          "POST", body={}) or {}
                _ingest(page.get("data"))
                calls.append((f"agent:{aid[:8]}", round(_t.time() - t0, 2)))
            except Exception:  # noqa: BLE001
                calls.append((f"agent:{aid[:8]}", -1.0))
    except Exception:  # noqa: BLE001
        pass
    return {nm: ops for nm, (_vk, ops) in best.items()}, calls


def _catalog_meta_module():
    """``fsr_playbooks._catalog_meta`` when the pinned framework has the
    modules-warmed marker, else None. getattr-style so an older wheel reports
    "no marker API" instead of forcing a warm on every turn."""
    try:
        from fsr_playbooks import _catalog_meta
    except Exception:  # noqa: BLE001 -- an old or broken wheel must not break the gate
        return None
    return _catalog_meta if hasattr(_catalog_meta, "record_modules_warmed") else None


def _modules_warmed_at(db_path) -> "str | None":
    """The ``modules_warmed_at`` stamp the framework writes beside module_fields,
    or None (absent, or the framework predates the marker). Logging only."""
    import sqlite3
    meta = _catalog_meta_module()
    if meta is None or not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            return meta.get(con, "modules_warmed_at")
    except sqlite3.Error:
        return None


def _warmup_gate_reason(db_path) -> "str | None":
    """Why the catalog needs a warm, or None when it is warm. Cheap: read-only
    COUNTs and one meta lookup, no live call. Checked in this order:

    1. connectors / modules empty -- a fresh slim DB never warmed (also catches
       an upgrade that warmed connectors/ops but not the record-grounding tables).
    2. op_safety empty while operations > 0 -- the DB was warmed by an older
       connector that predated the op_safety classifier, so the tier gate has no
       safety rows and escalates reads it should auto-allow (tier 3 instead of 2).
    3. module_fields empty -- the shipped slim DB ships ``modules`` populated and
       ``module_fields`` EMPTY by design (the framework build keeps the field
       lists off the wheel; they are this box's schema). ``modules`` non-empty
       here, so the miss is only the fields: ``find(kind='field')`` answers
       ``module_fields_unwarmed`` until a warm fills them.
    4. the ``modules_warmed_at`` marker is missing -- the field rows were not
       written by a box warm (only when the framework has the marker API).

    A connector configured on the box AFTER the last warm isn't detected here --
    run `make warmup` (force) after adding connectors, or re-ship (force-warms).

    Emptiness and markers only. The IDENTITY sibling -- catalog warmed from a
    *different* SOAR than the configured target -- is `_warmup_instance_mismatch`;
    the trigger sites gate on either."""
    import sqlite3
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            for tbl in ("connectors", "modules"):
                if con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] == 0:
                    return f"{tbl} table empty"
            if con.execute("SELECT COUNT(*) FROM operations").fetchone()[0] > 0:
                try:
                    saf = con.execute(
                        "SELECT COUNT(*) FROM op_safety"
                    ).fetchone()[0]
                except sqlite3.OperationalError:
                    saf = 0  # table absent on a pre-op_safety DB -> stale
                if saf == 0:
                    return "op_safety empty"
            try:
                fields = con.execute("SELECT COUNT(*) FROM module_fields").fetchone()[0]
            except sqlite3.OperationalError:
                fields = 0  # table absent on an old DB -> not warmed
            if fields == 0:
                return "module fields empty"
            meta = _catalog_meta_module()
            if meta is not None and meta.get(con, "modules_warmed_at") is None:
                return "modules_warmed_at marker missing"
            return None
    except sqlite3.Error:
        return None


def _warmup_needed(db_path) -> bool:
    """True when the catalog needs a warm. See `_warmup_gate_reason` for the
    signals (emptiness, stale op_safety, unwarmed module fields, missing marker).

    Emptiness and markers only. The IDENTITY sibling -- catalog warmed from a
    *different* SOAR than the configured target -- is `_warmup_instance_mismatch`;
    the trigger sites gate on either."""
    return _warmup_gate_reason(db_path) is not None


# One gate-driven warm at a time, and a backoff after each attempt. The gate is
# re-read on every lifecycle hook and chat turn, so a warm that cannot clear it
# (a box with no fields for a module, crudhub down) would otherwise re-run on
# every turn. Process-local on purpose: a restart is a fresh attempt.
_GATED_WARM_BACKOFF_S = 300.0
_gated_warm_lock = threading.Lock()
_gated_warm_busy = False
_gated_warm_started_at: "float | None" = None


def _claim_gated_warm() -> "str | None":
    """Take the single gate-driven warm slot. Returns None when claimed -- the
    caller MUST call `_release_gated_warm` when its warm ends -- otherwise the
    reason it must not warm now (already running, or inside the backoff)."""
    global _gated_warm_busy, _gated_warm_started_at
    with _gated_warm_lock:
        if _gated_warm_busy:
            return "a catalog warm is already running"
        now = time.monotonic()
        if (_gated_warm_started_at is not None
                and now - _gated_warm_started_at < _GATED_WARM_BACKOFF_S):
            return (f"the last catalog warm started {int(now - _gated_warm_started_at)}s "
                    f"ago; backing off {int(_GATED_WARM_BACKOFF_S)}s")
        _gated_warm_busy = True
        _gated_warm_started_at = now
        return None


def _release_gated_warm() -> None:
    global _gated_warm_busy
    with _gated_warm_lock:
        _gated_warm_busy = False


def _warmup_target_base_url() -> str:
    """The SOAR the catalog *should* reflect: ``$FSR_BASE_URL`` -- the same var
    the compiler's ``instance_guard`` reads. Empty on-platform, where the
    appliance is reached via crudhub and there is no explicit target. So both
    the mismatch-driven re-warm and the re-stamp below are dev/CLI-only and
    leave on-platform warmup behavior unchanged."""
    import os
    return (os.environ.get("FSR_BASE_URL") or "").strip()


def _warmup_instance_mismatch(db_path) -> bool:
    """True when the catalog was warmed from a DIFFERENT SOAR than the
    configured target (``$FSR_BASE_URL``).

    This is the identity sibling of `_warmup_needed`'s emptiness check: a
    catalog warmed from instance A is *full* but wrong for target B, so its
    picklist IRIs / connector configs mis-resolve and the compiler's
    ``instance_guard`` warns on every compile. `_warmup_needed` never notices
    (tables aren't empty), so the stale-instance catalog used to sit there
    forever. Gating a re-warm on this makes a wrong-instance catalog self-heal
    instead of only warning. No-op when FSR_BASE_URL is unset (on-platform) or
    the catalog carries no instance stamp."""
    target = _warmup_target_base_url()
    if not target or not db_path.exists():
        return False
    try:
        import sqlite3
        from fsr_playbooks import _catalog_meta
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            status, _label, _hash = _catalog_meta.check_instance(con, target)
        return status == "mismatch"
    except Exception:  # noqa: BLE001 -- a freshness probe must never break warmup gating
        return False


def _trigger_background_warmup(config: dict | None) -> bool:
    """Kick a one-shot warmup in a background thread when the catalog needs one.

    Called from the connector lifecycle hooks (`on_app_start` / `on_add_config`
    / `on_update_config` / `on_activate`) so a fresh `$replace` install --
    which ships the record-grounding and FortiSOAR-derived tables thin (see
    `_warmup_gate_reason`) -- is warmed at install / config-save time, instead
    of deferring the cost (and the un-synced-catalogue window where
    `emit_action_card` op-grounding has to fall back to the live connector
    definition) to the analyst's first chat.

    Every call logs ONE decision line at info: "warmup skipped: catalog warm
    (modules_warmed_at=...)", "warmup started: <reason>", or "warmup skipped:
    <why it must wait>". The same line is what an operator greps to see why a
    lookup still answers module_fields_unwarmed.

    Idempotent and single-flight: the gate is checked first, then the warm slot
    is claimed (`_claim_gated_warm`), so a warm already running, or one that
    started within the backoff window, is not repeated. Backgrounded so the slow
    catalog walk never blocks app-load / config-save in the platform UI. NEVER
    raises -- a hook that throws can break connector lifecycle handling on the
    platform; warmup's own errors (crudhub-absent on a dev box, etc.) are
    swallowed and the next trigger (hook, chat turn or an unwarmed-field lookup)
    retries once the backoff has passed."""
    claimed = False
    try:
        _db = _reference_db_path()
        needed = _warmup_needed(_db)
        mismatch = _warmup_instance_mismatch(_db)
        if not needed and not mismatch:
            _log("warmup skipped: catalog warm "
                 f"(modules_warmed_at={_modules_warmed_at(_db) or 'absent'})")
            return False
        why = []
        if needed:
            why.append(_warmup_gate_reason(_db) or "catalog gate")
        if mismatch:
            why.append("catalog warmed from a different FortiSOAR instance")
        why_s = "; ".join(why)
        blocked = _claim_gated_warm()
        if blocked is not None:
            _log(f"warmup skipped: {blocked} (pending: {why_s})")
            return False
        claimed = True
        _log(f"warmup started: {why_s}")
        def _bg(_cfg=config or {}):
            try:
                warmup(_cfg, {})
            except Exception as e:  # noqa: BLE001 -- best-effort; the next trigger retries
                _log(f"warmup failed: {type(e).__name__}: {e}", "warning")
            finally:
                _release_gated_warm()

        # NOT a daemon thread. FortiSOAR calls on_app_start in a multiprocessing
        # child that exits as soon as the hooks return; a daemon thread died
        # with it a few seconds in, silently, so the install never got its
        # module fields. Process exit joins a non-daemon thread, so the warm
        # finishes first. In a long-lived worker it only delays a reload.
        threading.Thread(target=_bg, name="fsrpb-warmup-hook",
                         daemon=False).start()
        return True
    except Exception:  # noqa: BLE001 -- hooks must never raise
        if claimed:
            _release_gated_warm()
        return False


def _warmup_param_rows(connector: str, op_name: str,
                       params: list[dict]) -> list[tuple]:
    """Flatten connector operation params, including nested onchange branches.

    FortiSOAR operation definitions model conditional fields as:
    `param.onchange[option_value] -> [child params]`. The reference DB stores
    one row per visible field plus `parent_param_name`/`condition_value`, so
    get_op_schema can reconstruct branch-specific argument sets.
    """
    rows: list[tuple] = []

    def _default(value):
        return json.dumps(value) if value is not None else ""

    def _options(value):
        return json.dumps(value) if isinstance(value, list) else ""

    def _walk(p: dict, parent: str, condition: str, ord_idx: int) -> None:
        name = p.get("name") or ""
        if not name:
            return
        rows.append((
            connector, op_name,
            parent,
            condition,
            name,
            p.get("title") or name,
            p.get("type") or "",
            1 if p.get("required") else 0,
            _default(p.get("value")),
            _options(p.get("options")),
            p.get("tooltip") or "",
            p.get("placeholder") or "",
            p.get("description") or "",
            1 if p.get("visible", True) else 0,
            1 if p.get("editable", True) else 0,
            ord_idx,
            "",  # observed_type
            "",  # coerces_from
        ))
        onchange = p.get("onchange")
        if not isinstance(onchange, dict):
            return
        for cond_value, subparams in onchange.items():
            if not isinstance(subparams, list):
                continue
            for sub_ord, sub in enumerate(subparams):
                if isinstance(sub, dict):
                    _walk(sub, name, str(cond_value), sub_ord)

    for ord_idx, p in enumerate(params or []):
        if isinstance(p, dict):
            _walk(p, "", "", ord_idx)
    return rows


# Endpoints mirror python/probes/probe_modules.py (the offline miner) so the
# operator's own SOAR fills modules/module_fields/picklists exactly as the
# bundled DB was built. `$relationships=true` is the magic param: without it
# `attributes` (module fields) and `picklists` (items) come back null.
_MODULES_URL = ("/api/3/staging_model_metadatas"
                "?$limit=2147483647&$orderby=type&$relationships=true")
_PICKLIST_NAMES_URL = ("/api/3/picklist_names"
                       "?$export=false&$limit=2147483647&$orderby=name"
                       "&$relationships=true")


def _pl_list_name(attr: dict):
    """The picklist listName an attribute binds to (e.g. 'AlertStatus'), or
    None. Mirrors probe_modules._picklist_list_name."""
    ds = attr.get("dataSource") or {}
    if not isinstance(ds, dict):
        return None
    for f in (ds.get("query") or {}).get("filters", []) or []:
        if isinstance(f, dict) and f.get("field") == "listName__name":
            v = f.get("value")
            if isinstance(v, str):
                return v
    return None


def _warmup_modules_picklists(mr, db_path) -> dict:
    """Fill modules / module_fields / picklists from the operator's OWN SOAR.

    The shipped reference DB keeps `modules` as a name baseline and leaves
    `module_fields` EMPTY (the framework build's choice, see
    `_warmup_gate_reason`); without this warm a fresh install can't ground
    record-create/update steps or `find(kind='field')` against the operator's
    modules. Best-effort and self-contained: any
    failure leaves the prior (empty) tables and returns zero counts rather than
    aborting the core connector/op warm. Returns {modules, module_fields,
    picklists} counts (or an `error` key)."""
    import sqlite3

    def _cell(v):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (str, int, float)) or v is None:
            return v
        return json.dumps(v, default=str)

    try:
        # 1) Picklists: build list_name -> [options] and the (name,value,iri) rows.
        pl = mr(_PICKLIST_NAMES_URL, "GET") or {}
        list_options: dict = {}
        pl_rows: list = []
        for pl_name in pl.get("hydra:member") or []:
            if not isinstance(pl_name, dict):
                continue
            nm = pl_name.get("name")
            opts = []
            for it in pl_name.get("picklists") or []:
                iv = it.get("itemValue")
                iid = it.get("@id")
                if iv is not None:
                    opts.append(iv)
                if nm and isinstance(iv, str) and isinstance(iid, str):
                    pl_rows.append((nm, iv, iid))
            if nm:
                list_options[nm] = opts

        # 2) Modules + their inline attributes (fields).
        md = mr(_MODULES_URL, "GET") or {}
        mod_rows: list = []
        field_rows: list = []
        for m in md.get("hydra:member") or []:
            if not isinstance(m, dict) or not m.get("type"):
                continue
            name = m.get("type")
            mod_rows.append((
                name,
                m.get("displayName") or m.get("module") or name,
                m.get("module"),
                m.get("descriptions"),
            ))
            for attr in m.get("attributes") or []:
                if not isinstance(attr, dict) or not attr.get("name"):
                    continue
                ln = _pl_list_name(attr)
                options = list_options.get(ln) if ln else None
                validation = attr.get("validation")
                required = isinstance(validation, dict) and validation.get("required") is True
                field_rows.append((
                    name,
                    attr.get("name"),
                    attr.get("title") or attr.get("displayName") or attr.get("name"),
                    attr.get("type") or attr.get("formType"),
                    1 if required else 0,
                    json.dumps(options) if options else None,
                    attr.get("tooltip"),
                    ln,
                ))

        mod_rows = [tuple(_cell(v) for v in r) for r in mod_rows]
        field_rows = [tuple(_cell(v) for v in r) for r in field_rows]

        with sqlite3.connect(db_path) as con:
            con.execute("BEGIN")
            try:
                con.execute("DELETE FROM picklists")
                con.execute("DELETE FROM module_fields")
                con.execute("DELETE FROM modules")
                if pl_rows:
                    con.executemany(
                        "INSERT OR REPLACE INTO picklists "
                        "(list_name, item_value, item_iri) VALUES (?,?,?)",
                        pl_rows)
                if mod_rows:
                    con.executemany(
                        "INSERT OR REPLACE INTO modules "
                        "(name, label, plural, description) VALUES (?,?,?,?)",
                        mod_rows)
                if field_rows:
                    con.executemany(
                        "INSERT OR REPLACE INTO module_fields "
                        "(module_name, field_name, title, type, required, "
                        "picklist_options, tooltip, picklist_name) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        field_rows)
                    # These rows ARE this box's schema, so the verifier may
                    # treat an unknown field as an error rather than a guess.
                    # On-platform there is no base URL to stamp, which is why
                    # this marker exists. getattr: older pinned wheels lack it.
                    try:
                        from fsr_playbooks import _catalog_meta
                        _mark = getattr(_catalog_meta, "record_modules_warmed", None)
                        if _mark is not None:
                            _mark(con)
                    except ImportError:
                        pass
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return {"modules": len(mod_rows),
                "module_fields": len(field_rows),
                "picklists": len(pl_rows)}
    except Exception as e:  # noqa: BLE001 -- best-effort; core warm already done
        _log(f"warmup: modules/picklists warm failed: {type(e).__name__}: {e}",
             "warning")
        return {"modules": 0, "module_fields": 0, "picklists": 0,
                "error": f"{type(e).__name__}: {e}"}


def _warmup_clobber_refusal(db_path) -> "dict | None":
    """Refuse a warmup that would clobber the framework's full dev reference
    cache. Returns an error envelope to refuse, or None to allow.

    In a source checkout `_reference_db_path()` resolves to
    `<repo>/data/fsr_reference.db` -- the ~700-connector corpus the compiler and
    the framework's tooling gate depend on. A warmup syncs a *box's* handful of
    installed connectors, so writing it there silently guts that corpus (this
    has bitten the local dev loop). On a real appliance / wheel install the
    path is the packaged slim DB, so this never trips there.

    Escape hatches: point the write elsewhere (`FSR_REFERENCE_DB` or `FSRPB_DB`
    → a scratch copy), or set `FSRPB_ALLOW_DEV_DB_CLOBBER=1` to rebuild the dev
    cache in place on purpose."""
    import os
    try:
        from fsr_playbooks._db import REPO_PROBED_DB
        is_dev_cache = db_path.resolve() == REPO_PROBED_DB.resolve()
    except Exception:  # noqa: BLE001 -- if we can't tell, don't block warmup
        return None
    if not is_dev_cache or os.environ.get("FSRPB_ALLOW_DEV_DB_CLOBBER") == "1":
        return None
    return _err(
        "warmup_clobber_guard",
        f"refusing to warmup-write the dev reference cache ({db_path}): a box "
        "sync would overwrite the full connector corpus the compiler and "
        "tooling gate depend on. Set FSR_REFERENCE_DB or FSRPB_DB to a scratch "
        "copy first, or FSRPB_ALLOW_DEV_DB_CLOBBER=1 to rebuild it deliberately.",
        phase="dev-only")


def _schedule_health_pass(mr) -> bool:
    """Start the connector-health + op-definition pass in the background.

    NOT a daemon thread: when warmup runs from on_app_start, FortiSOAR's hook
    process exits as soon as the hooks return, and a daemon thread dies with it
    mid-pass, silently. Both passes are time-budgeted and every probe carries
    its own timeout, so the process waits a bounded few minutes at most.
    """
    try:
        import threading
        from fsr_soc_triage._live_crudhub import CrudhubLiveClient
        from fsr_playbooks.mcp_server.tools_execution import (
            populate_connector_health, populate_op_definitions)
    except Exception as e:  # noqa: BLE001 -- best-effort; preflight backfills
        _log(f"connector health pass unavailable: {type(e).__name__}: {e}", "warning")
        return False

    def _bg_health(_mr=mr):
        try:
            client = CrudhubLiveClient(_mr)
            populate_connector_health(client, time_budget_s=90.0)
            populate_op_definitions(client, time_budget_s=60.0)
        except Exception as e:  # noqa: BLE001 -- best-effort; preflight/grounding backfill
            _log(f"connector health pass failed: {type(e).__name__}: {e}", "warning")

    try:
        threading.Thread(target=_bg_health, name="fsrpb-health-warm",
                         daemon=False).start()
    except Exception as e:  # noqa: BLE001
        _log(f"connector health pass not started: {type(e).__name__}: {e}", "warning")
        return False
    return True


def _config_schema_of(member: dict) -> "dict | None":
    """The connector's CONFIG SCHEMA ({fields: [...]}) from a listing record,
    or None when the record does not carry it.

    /api/integration/connectors/ puts the SAVED CONFIGURATIONS (a bare list of
    config records) under `configuration`, not the schema; only the older
    /api/3 shape ({fields: []}) and a detail record's `config_schema` hold the
    schema. A list is never a schema: storing it made every warmed connector
    read as config-free downstream. None leaves the column empty so readers
    fall back to the box's detail endpoint."""
    cs = member.get("config_schema")
    if isinstance(cs, dict):
        return cs
    cfg = member.get("configuration")
    if isinstance(cfg, dict) and isinstance(cfg.get("fields"), list):
        return cfg
    return None


def warmup(config: dict, params: dict) -> dict:
    """Populate FortiSOAR-derivable reference tables from the live
    `/api/3/connectors/` API. Idempotent; pass `force=True` to repopulate.

    Returns counts so the caller can verify the warm path filled
    something -- empty counts mean the API call succeeded but FortiSOAR
    returned no connectors (a real condition worth surfacing)."""
    _check_params(WarmupParams, params)
    import sqlite3
    import time

    force = bool((params or {}).get("force"))
    db_path = _reference_db_path()
    if not db_path.exists():
        return _err("reference_db_missing",
                    f"reference DB not found at {db_path}; rebuild the connector "
                    "(scripts/build.sh) and reinstall it.")

    _refusal = _warmup_clobber_refusal(db_path)
    if _refusal is not None:
        return _refusal

    if not force and not _warmup_needed(db_path) and not _warmup_instance_mismatch(db_path):
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            counts = {
                t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("connectors", "operations", "operation_params",
                          "op_safety")
            }
        return {"ok": True, "skipped": "already_warm", "counts": counts}

    mr, err = _require_transport(
        "warmup requires the FortiSOAR runtime; "
        "integrations.crudhub.make_request is not importable.")
    if err:
        return err

    # Phase timing -- surfaced in the return envelope AND logged, so a slow
    # warmup (the cold-box / busy-loopback case) can be analyzed from the API
    # response alone, without appliance log access.
    t0 = time.time()
    timings: dict[str, Any] = {}
    try:
        # The connector catalog lives at /api/integration/connectors/ (NOT
        # /api/3/connectors/, which 404s). Its envelope is a CUSTOM shape --
        # {status, totalItems, data[], itemsPerPage, nextPage} -- not Hydra.
        # `nextPage` is an INTEGER 1-based page number (or null), not a URL.
        # (Endpoint live-verified; see fortisoar-api-docs.)
        members: list[dict] = []
        page_no: int | None = 1
        _t_list = time.time()
        while page_no:
            page = mr(f"/api/integration/connectors/"
                      f"?page_size=200&page={page_no}", "GET") or {}
            members.extend(page.get("data") or [])
            page_no = page.get("nextPage")
        timings["list_fetch_s"] = round(time.time() - _t_list, 2)
        _log(f"warmup: connector list fetched ({len(members)} records) in "
             f"{timings['list_fetch_s']}s")
    except Exception as e:  # noqa: BLE001
        return _err("connectors_fetch_failed", f"{type(e).__name__}: {e}",
                    phase="connectors")

    # Dedupe by connector name. The live catalog can return the same name
    # more than once -- e.g. multiple records left by repeated $replace
    # upgrades, or a connector present both system-wide and as a dev copy.
    # `connectors.name` is UNIQUE, so a raw insert of dupes would 500 the
    # whole warm. Keep the highest-version record per name (falls back to
    # last-seen when versions are equal/unsortable).
    by_name: dict[str, dict] = {}
    for m in members:
        nm = m.get("name")
        if not nm:
            continue
        prev = by_name.get(nm)
        if prev is None or _version_key(m.get("version")) >= _version_key(prev.get("version")):
            by_name[nm] = m
    members = list(by_name.values())

    # Single transaction; replace any prior partial warm.
    conn_rows: list[tuple] = []
    op_rows: list[tuple] = []
    param_rows: list[tuple] = []
    safety_rows: list[tuple] = []

    # Bulk-fetch every connector's operations in ~1-3 calls instead of one
    # POST per connector (the old ~175s detail loop). The per-connector POST
    # remains below as a FALLBACK for any connector the bulk map missed.
    _t_bulk = time.time()
    schemas_by_name: dict = {}
    ops_by_name, _bulk_calls = _bulk_operations_map(mr, schemas_by_name)
    timings["bulk_ops_fetch_s"] = round(time.time() - _t_bulk, 2)
    timings["bulk_ops_calls"] = _bulk_calls
    timings["bulk_ops_connectors"] = len(ops_by_name)
    _log(f"warmup: bulk ops map = {len(ops_by_name)} connectors in "
         f"{timings['bulk_ops_fetch_s']}s via {_bulk_calls}")

    # Track any per-connector FALLBACK POSTs (should be rare now) so a
    # regression back to the slow path is visible in the telemetry.
    _detail_times: list[tuple] = []  # (connector_name, seconds)
    _t_detail = time.time()

    for m in members:
        name = m.get("name")
        if not name:
            continue
        # /api/integration/connectors/ returns `category` as a LIST and
        # `configuration` as a bare list of fields (older /api/3 shape used
        # a scalar category + {fields:[]}). Normalize both.
        cat = m.get("category")
        if isinstance(cat, list):
            cat = (cat[0] if cat else "")
        cat = (cat or "").lower()
        cfg_schema = _config_schema_of(m)
        if cfg_schema is None:
            cfg_schema = schemas_by_name.get(name)
        conn_rows.append((
            name,
            m.get("version") or "",
            m.get("label") or m.get("title") or name,
            cat,
            m.get("description") or "",
            m.get("publisher") or "",
            m.get("contributor") or "",
            1 if m.get("active") else 0,
            1 if m.get("system") else 0,
            1 if m.get("cs_approved") else 0,
            1 if m.get("cs_compatible") else 0,
            1 if m.get("ingestion_supported") else 0,
            json.dumps(m.get("tags") or []),
            json.dumps(cfg_schema) if cfg_schema is not None else "",
            "fortisoar_api",
            "",  # source_path
            json.dumps(m),  # info_json -- full record for downstream tools
            "",  # source_code
            "",  # rpm_fingerprint
        ))
        # The list endpoint omits operations. The ONLY way to enumerate
        # them is a POST to the per-connector detail endpoint, which
        # returns the full record incl. operations[].parameters[].
        # (Verified.) Best-effort: a detail failure leaves this
        # connector op-less rather than aborting the whole warmup.
        # Prefer ops inlined on the list record, then the bulk map, and only
        # fall back to a per-connector POST if both miss (rare).
        ops = m.get("operations") or ops_by_name.get(name)
        if not ops and m.get("id") is not None:
            _t_one = time.time()
            try:
                detail = mr(f"/api/integration/connectors/{m['id']}/",
                            "POST", body={}) or {}
                ops = detail.get("operations") or []
            except Exception as e:  # noqa: BLE001
                ops = []
                _log(f"warmup: detail fetch failed for {name}: "
                     f"{type(e).__name__}: {e}", "warning")
            _detail_times.append((name, round(time.time() - _t_one, 2)))
        for op in (ops or []):
            op_name = op.get("operation") or op.get("name")
            if not op_name:
                continue
            category = (op.get("category") or "").lower()
            op_rows.append((
                name, op_name,
                op.get("title") or op_name,
                op.get("annotation") or "",
                category,
                op.get("description") or "",
                1 if op.get("visible", True) else 0,
                1 if op.get("enabled", True) else 0,
                json.dumps(op.get("output_schema") or {}),
                json.dumps(op.get("conditional_output_schema") or {}),
                "",  # output_schema_observed -- only filled by probes
            ))
            safety = _CATEGORY_TO_SAFETY.get(category)
            if safety:
                safety_rows.append((
                    name, op_name, safety,
                    f"category={category}",
                    "warmup_classifier",
                    1,  # classifier_version
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ))
            param_rows.extend(
                _warmup_param_rows(name, op_name, op.get("parameters") or []))

    # Coerce every bound cell to a sqlite-storable scalar. Live connector
    # JSON is irregular -- a field we expect to be a string (e.g. a param
    # `type` or `tooltip`) is occasionally a list/dict, which sqlite can't
    # bind ("unsupported type"). Bools → int; lists/dicts → JSON text;
    # str/int/float/None pass through.
    def _cell(v):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (str, int, float)) or v is None:
            return v
        return json.dumps(v, default=str)

    def _clean(rows):
        return [tuple(_cell(v) for v in r) for r in rows]

    # Loop timing + count of per-connector FALLBACK POSTs (0 on the happy path
    # now that the bulk map covers everything). A non-zero count + slow loop
    # signals the bulk path regressed and we're back to per-connector fetches.
    timings["row_build_s"] = round(time.time() - _t_detail, 2)
    timings["fallback_detail_calls"] = len(_detail_times)
    if _detail_times:
        slow = sorted(_detail_times, key=lambda x: x[1], reverse=True)[:5]
        timings["slowest_details"] = [{"connector": n, "s": s} for n, s in slow]
        _log(f"warmup: {len(_detail_times)} FALLBACK detail POSTs took "
             f"{timings['row_build_s']}s; slowest="
             f"{', '.join(f'{n}={s}s' for n, s in slow)}")

    conn_rows = _clean(conn_rows)
    op_rows = _clean(op_rows)
    param_rows = _clean(param_rows)
    safety_rows = _clean(safety_rows)

    _t_db = time.time()
    with sqlite3.connect(db_path) as con:
        con.execute("BEGIN")
        try:
            con.execute("DELETE FROM connectors")
            con.execute("DELETE FROM operations")
            con.execute("DELETE FROM operation_params")
            con.execute("DELETE FROM op_safety")
            if conn_rows:
                con.executemany(
                    "INSERT OR REPLACE INTO connectors VALUES (" + ",".join(["?"] * 19) + ")",
                    conn_rows)
            if op_rows:
                con.executemany(
                    "INSERT OR REPLACE INTO operations VALUES (" + ",".join(["?"] * 11) + ")",
                    op_rows)
            if param_rows:
                con.executemany(
                    "INSERT OR REPLACE INTO operation_params VALUES (" + ",".join(["?"] * 18) + ")",
                    param_rows)
            if safety_rows:
                con.executemany(
                    "INSERT OR REPLACE INTO op_safety VALUES (" + ",".join(["?"] * 7) + ")",
                    safety_rows)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    timings["db_write_s"] = round(time.time() - _t_db, 2)

    # Record-grounding tables (modules / module_fields / picklists). `modules`
    # ships as a baseline and `module_fields` ships empty; both are warmed from
    # the operator's own SOAR so record-create/update steps ground against THEIR
    # modules. Best-effort: a failure here never fails the core connector/op
    # warm above.
    _t_mp = time.time()
    mp_counts = _warmup_modules_picklists(mr, db_path)
    timings["modules_picklists_s"] = round(time.time() - _t_mp, 2)
    _log(f"warmup: record-grounding tables -- "
         f"modules={mp_counts.get('modules')} "
         f"fields={mp_counts.get('module_fields')} "
         f"picklists={mp_counts.get('picklists')} "
         f"in {timings['modules_picklists_s']}s")

    # Discover which connectors are actually configured + healthy and cache a
    # per-config healthcheck. `run_op`'s preflight reads this cache (4h TTL for
    # healthy / 5min for unhealthy) so the agent fails fast with an actionable
    # "configure/fix this connector" error instead of firing ops blind.
    #
    # Healthchecks hit upstream vendor services and can be slow (~45 connectors
    # on a busy box), so the pass runs in a background thread and warmup returns
    # immediately. Anything the thread hasn't reached yet is filled lazily by
    # `run_op`'s preflight on first use -- so this is never a blocker on warmup
    # or the demo path.
    # The same background pass also pre-warms the live op-definition cache for
    # UN-SYNCED connectors (configured on the box but absent from the bundled
    # catalog), so the un-synced grounding path in `validate_op_grounded` --
    # op-existence AND param-name validation -- is a cache hit on first use
    # instead of a multi-second live detail fetch repeated every pivot.
    health_scheduled = _schedule_health_pass(mr)

    timings["total_s"] = round(time.time() - t0, 2)
    _log(f"warmup done in {timings['total_s']}s -- "
         f"list={timings.get('list_fetch_s')}s "
         f"bulk_ops={timings.get('bulk_ops_fetch_s')}s "
         f"fallback_posts={timings.get('fallback_detail_calls')} "
         f"db={timings.get('db_write_s')}s -- "
         f"{len(conn_rows)} connectors, {len(op_rows)} ops")

    counts = {
        "connectors": len(conn_rows),
        "operations": len(op_rows),
        "operation_params": len(param_rows),
        "op_safety": len(safety_rows),
        "modules": mp_counts.get("modules", 0),
        "module_fields": mp_counts.get("module_fields", 0),
        "picklists": mp_counts.get("picklists", 0),
    }
    # Re-stamp the catalog to the instance we just warmed from, so the
    # compiler's instance_guard stops flagging a mismatch and the
    # mismatch-driven auto-rewarm gate (`_warmup_instance_mismatch`) doesn't
    # re-fire on the next turn (which would loop). Dev/CLI-only: on-platform
    # FSR_BASE_URL is unset (crudhub reaches the local appliance), so we skip
    # stamping there and the install-time build stamp stands unchanged.
    _target = _warmup_target_base_url()
    if _target:
        try:
            from fsr_playbooks import _catalog_meta
            with sqlite3.connect(db_path) as con:
                _catalog_meta.stamp_instance(
                    con, instance_label=None, base_url=_target)
                con.commit()
        except Exception:  # noqa: BLE001 -- stamping must never fail the warm
            pass

    # Persist this run's telemetry so a slow warmup can be analyzed later via
    # `query_store` (SELECT ... FROM warmup_runs ORDER BY ts DESC). Created
    # defensively so it works on any already-installed reference DB without a
    # rebuild. Best-effort: telemetry must never fail the warmup itself.
    _record_warmup_run(db_path, timings, counts, force)

    return {
        "ok": True,
        "ms": int((time.time() - t0) * 1000),
        "timings": timings,
        "counts": counts,
        "connector_health": "scheduled_background" if health_scheduled else "unavailable",
    }


def _register_unwarmed_field_kick() -> None:
    """Hand the triage registry the warm it needs when `find(kind='field')`
    answers module_fields_unwarmed (fsr_soc_triage/registry.py wraps `find`).
    The kick is `_trigger_background_warmup`: non-blocking and single-flight, so
    the lookup that missed returns at once and the next one succeeds. Without the
    triage package the lookup keeps its plain answer."""
    try:
        from fsr_soc_triage.registry import set_unwarmed_field_kick
    except Exception:  # noqa: BLE001 -- no triage package: plain answer, no kick
        return
    set_unwarmed_field_kick(lambda: _trigger_background_warmup({}))


_register_unwarmed_field_kick()
