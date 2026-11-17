#!/usr/bin/env python3
"""A durable local copy of every chat session a box holds.

Boxes keep few sessions (the on-box db is keyed by the connector's name, so a
rename orphans it, and nothing else keeps history). Without history there is
nothing to audit. This keeps them: it pulls sessions through the connector's own
`list_sessions` + `chat_history` ops (the same ones session_analyze.py reads) into a
store OUTSIDE any git repo, and the analysis tools read that store back.

    session_store.py export  --env <box .env> --box <alias>   # incremental
    session_store.py import  --db <sessions.db> --box <alias> # an orphaned db file
    session_store.py summary [--box <alias>] [--days 30]      # per box per day
    session_analyze.py --store <alias>                        # analyze exported
    session_health.py  --store <alias>

Store layout (override the root with FSR_AGENTIC_SESSIONS):

    ~/.fsr-agentic/sessions/<alias>/<session id>.json   {session_id, box,
                                                         fetched_at, summary, history}
    ~/.fsr-agentic/sessions/<alias>/index.json          {sessions: {id: summary+fetched_at}}

`summary` is the `list_sessions` row, `history` the `chat_history` response.
Export is incremental and idempotent: a session is re-fetched only when its row
changed (entries, last activity, rating), and a local copy is NEVER deleted when
the box no longer lists it.

Synthetic vs real: widget chat sessions are `sess-<random>-<n>`; every harness
that drives the connector (live sanity, sweeps, evals, analyst-sim, probes) names
its sessions with its own prefix. A session is `real` iff its id starts with
`sess-`, else `synthetic`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

REAL_PREFIX = "sess-"
# Transcript event fields searched by `list_sessions(query=...)`; the connector's
# own search reads the same keys (storage.Storage._payload_plain_text).
_TEXT_KEYS = ("text", "display", "question", "title", "summary", "name",
              "missing", "error")


def store_root() -> Path:
    return Path(os.environ.get("FSR_AGENTIC_SESSIONS")
                or Path.home() / ".fsr-agentic" / "sessions").expanduser()


def is_synthetic(session_id: str) -> bool:
    return not str(session_id).startswith(REAL_PREFIX)


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str, sort_keys=True))
    os.replace(tmp, path)


def _safe(session_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session_id)


def read_index(root: Path, alias: str) -> dict:
    p = root / alias / "index.json"
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d.get("sessions"), dict) else {"sessions": {}}
    except (OSError, ValueError):
        return {"sessions": {}}


def read_session(root: Path, alias: str, session_id: str) -> Optional[dict]:
    try:
        return json.loads((root / alias / f"{_safe(session_id)}.json").read_text())
    except (OSError, ValueError):
        return None


def aliases(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "index.json").exists())


# ── export ──────────────────────────────────────────────────────────────────

_ROW_KEYS = ("entries", "last_activity", "rating", "title")


def _changed(old: Optional[dict], row: dict) -> bool:
    return old is None or any(old.get(k) != row.get(k) for k in _ROW_KEYS)


def export_sessions(op: Callable[[str, dict], dict], alias: str, *,
                    root: Optional[Path] = None, limit: int = 100000,
                    now: Optional[float] = None) -> dict:
    """Pull every session `op` lists into the store. Returns counts:
    {listed, new, updated, unchanged, errors, kept_only_local}."""
    root = root or store_root()
    now = time.time() if now is None else now
    rows = [r for r in (op("list_sessions", {"limit": limit}).get("sessions") or [])
            if isinstance(r, dict) and r.get("session_id")]
    index = read_index(root, alias)
    known = index["sessions"]
    out = dict(listed=len(rows), new=0, updated=0, unchanged=0, errors=0)
    for row in rows:
        sid = row["session_id"]
        old = known.get(sid)
        if not _changed(old, row):
            out["unchanged"] += 1
            continue
        try:
            hist = op("chat_history", {"session_id": sid, "limit": 500})
        except Exception:  # noqa: BLE001 -- one bad session must not stop the rest
            out["errors"] += 1
            continue
        _atomic_write(root / alias / f"{_safe(sid)}.json", {
            "session_id": sid, "box": alias, "fetched_at": now,
            "summary": row, "history": hist})
        known[sid] = {**row, "fetched_at": now}
        out["new" if old is None else "updated"] += 1
        # Persist the index as we go: an interrupted export keeps what it fetched.
        _atomic_write(root / alias / "index.json", index)
    listed = {r["session_id"] for r in rows}
    out["kept_only_local"] = sum(1 for sid in known if sid not in listed)
    index["last_export"] = {"at": now, **out}
    _atomic_write(root / alias / "index.json", index)
    return out


# ── import a sessions db file (a box's old, orphaned db) ────────────────────

def _open_connector_storage(db_path: Path):
    """The connector's own Storage over a PRIVATE COPY of `db_path` (opening it
    migrates the schema, which must never touch the file we were given). Returns
    (storage, tmpdir); the caller removes tmpdir."""
    import importlib
    import shutil
    import tempfile
    import types
    pkg_dir = Path(__file__).resolve().parent.parent / "connector-fsr-agentic-assistant"
    if str(pkg_dir) not in sys.path:
        sys.path.insert(0, str(pkg_dir))   # the vendored fsr_playbooks, as in tests/conftest
    if "_session_store_conn" not in sys.modules:
        pkg = types.ModuleType("_session_store_conn")
        pkg.__path__ = [str(pkg_dir)]
        sys.modules["_session_store_conn"] = pkg
    tmp = Path(tempfile.mkdtemp(prefix="session-import-"))
    shutil.copy2(db_path, tmp / "copy.db")
    return importlib.import_module("_session_store_conn.storage").Storage(tmp / "copy.db"), tmp


def import_db(db_path: Path, alias: str, *, root: Optional[Path] = None,
              now: Optional[float] = None) -> dict:
    """Write every session of a connector sessions db into the store, in the same
    per-session format as `export_sessions`, tagged `source: "legacy_db"`. Rows
    are converted by the connector's own Storage (list_sessions / list_transcript
    / card decisions), so there is no second transcript format. A session id
    already in the store (from a live export or an earlier import) is NEVER
    overwritten. Returns {found, imported, skipped_existing, errors}."""
    import shutil
    root = root or store_root()
    now = time.time() if now is None else now
    if not Path(db_path).is_file():
        raise SystemExit(f"no sessions db at {db_path}")
    storage, tmp = _open_connector_storage(Path(db_path))
    try:
        rows = storage.list_sessions(limit=10**7)
        index = read_index(root, alias)
        known = index["sessions"]
        out = dict(found=len(rows), imported=0, skipped_existing=0, errors=0)
        for row in rows:
            sid = row["session_id"]
            if sid in known or (root / alias / f"{_safe(sid)}.json").exists():
                out["skipped_existing"] += 1
                continue
            try:
                hist = {"turns": storage.list_transcript(sid, limit=500),
                        "initiator": storage.get_session_initiator(sid),
                        "card_decisions": storage.list_card_decisions(sid)}
            except Exception:  # noqa: BLE001 -- one unreadable session must not stop the rest
                out["errors"] += 1
                continue
            _atomic_write(root / alias / f"{_safe(sid)}.json", {
                "session_id": sid, "box": alias, "fetched_at": now,
                "source": "legacy_db", "summary": row, "history": hist})
            known[sid] = {**row, "fetched_at": now, "source": "legacy_db"}
            out["imported"] += 1
        _atomic_write(root / alias / "index.json", index)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── the store as a box: StoreOp answers list_sessions / chat_history ────────

class StoreOp:
    """`op(name, params)` over the exported store, same shapes as the box ops, so
    session_analyze's modes (calls, defect sweep, replay, diagnose) and
    session_health run on exported sessions unchanged. alias 'all' = every box."""

    def __init__(self, alias: str, root: Optional[Path] = None):
        self.root = root or store_root()
        self.aliases = aliases(self.root) if alias in ("", "all") else [alias]
        if alias not in ("", "all") and not (self.root / alias / "index.json").exists():
            raise SystemExit(f"no exported sessions for box '{alias}' under "
                             f"{self.root} -- run: make session-export BOX={alias}")

    def _rows(self) -> list[dict]:
        rows = [r for a in self.aliases for r in read_index(self.root, a)["sessions"].values()]
        return sorted(rows, key=lambda r: r.get("last_activity") or 0, reverse=True)

    def _find(self, sid: str) -> Optional[dict]:
        for a in self.aliases:
            d = read_session(self.root, a, sid)
            if d:
                return d
        return None

    @staticmethod
    def _plain(history: dict) -> str:
        parts: list[str] = []
        for t in history.get("turns") or []:
            if isinstance(t, str):
                parts.append(t)
            elif isinstance(t, dict):
                if t.get("user") is not None:
                    parts.append(str(t["user"]))
                for ev in t.get("transcript") or []:
                    if isinstance(ev, dict):
                        parts += [v for k in _TEXT_KEYS
                                  if isinstance((v := ev.get(k)), str) and v]
        return " ".join(parts)

    def __call__(self, name: str, params: dict) -> dict:
        limit = int(params.get("limit") or 50)
        if name == "list_sessions":
            query = (params.get("query") or "").strip().lower()
            if not query:
                return {"sessions": self._rows()[:limit]}
            hits = []
            for row in self._rows():
                d = self._find(row["session_id"])
                text = self._plain((d or {}).get("history") or {})
                i = text.lower().find(query)
                if i >= 0:
                    hits.append({**row, "snippet": text[max(0, i - 60): i + 60 + len(query)]})
                if len(hits) >= limit:
                    break
            return {"sessions": hits, "query": params.get("query")}
        if name == "chat_history":
            d = self._find(params.get("session_id") or "")
            return (d or {}).get("history") or {"turns": []}
        raise SystemExit(f"the session store answers list_sessions and chat_history, not {name}")


# ── summary ─────────────────────────────────────────────────────────────────

def summarize(root: Optional[Path] = None, *, box: Optional[str] = None,
              days: Optional[int] = None, now: Optional[float] = None) -> list[dict]:
    """Rows {box, day (UTC, of the session's start), total, synthetic, real}."""
    root = root or store_root()
    now = time.time() if now is None else now
    cutoff = now - days * 86400 if days else 0
    acc: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for alias in ([box] if box else aliases(root)):
        for sid, row in read_index(root, alias)["sessions"].items():
            ts = row.get("started_at") or row.get("last_activity") or 0
            if ts < cutoff:
                continue
            day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            acc[(alias, day)][0 if is_synthetic(sid) else 1] += 1
    return [dict(box=b, day=d, total=s + r, synthetic=s, real=r)
            for (b, d), (s, r) in sorted(acc.items())]


def render_summary(rows: list[dict]) -> str:
    if not rows:
        return "no exported sessions (run: make session-export BOX=<alias>)"
    L = [f"{'box':<8} {'day (UTC)':<11} {'total':>5} {'synthetic':>9} {'real':>5}"]
    for r in rows:
        L.append(f"{r['box']:<8} {r['day']:<11} {r['total']:>5} {r['synthetic']:>9} {r['real']:>5}")
    for b in sorted({r["box"] for r in rows}):
        mine = [r for r in rows if r["box"] == b]
        L.append(f"{b:<8} {'TOTAL':<11} {sum(r['total'] for r in mine):>5} "
                 f"{sum(r['synthetic'] for r in mine):>9} {sum(r['real'] for r in mine):>5}")
    return "\n".join(L)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="pull a box's sessions into the local store")
    e.add_argument("--env", required=True, help="box .env")
    e.add_argument("--box", required=True, help="the box alias (the store folder)")
    i = sub.add_parser("import", help="import sessions from a connector sessions db file")
    i.add_argument("--db", required=True, help="path to the sqlite file")
    i.add_argument("--box", required=True, help="the box alias (the store folder)")
    s = sub.add_parser("summary", help="sessions per box per day, synthetic vs real")
    s.add_argument("--box")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "export":
        from session_analyze import box_op
        t0 = time.time()
        try:
            res = export_sessions(box_op(a.env), a.box)
        except SystemExit:
            raise
        except Exception as ex:  # noqa: BLE001 -- unreachable box: say so, do not trace
            print(f"session-export {a.box}: box unreachable ({type(ex).__name__})",
                  file=sys.stderr)
            return 1
        print(f"session-export {a.box}: {res['listed']} on box, +{res['new']} new, "
              f"{res['updated']} updated, {res['unchanged']} unchanged, "
              f"{res['errors']} errors, {res['kept_only_local']} kept only locally "
              f"({time.time() - t0:.1f}s) -> {store_root() / a.box}")
        return 0
    if a.cmd == "import":
        res = import_db(Path(a.db), a.box)
        print(f"session-import {a.box}: {res['found']} in db, +{res['imported']} imported, "
              f"{res['skipped_existing']} already in the store, {res['errors']} errors "
              f"-> {store_root() / a.box}")
        return 0
    rows = summarize(box=a.box, days=a.days)
    print(json.dumps(rows, indent=1) if a.json else render_summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
