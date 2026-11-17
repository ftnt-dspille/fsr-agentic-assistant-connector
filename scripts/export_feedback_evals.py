#!/usr/bin/env python3
"""X5: turn analysts' right/wrong ratings on auto-triage decisions into
known-answer eval rows.

The Monitor (M3) lets an analyst rate each auto-triage decision "right" or
"wrong" (connector op `monitor_feedback`, table `decision_feedback`). Each
labelled decision is a real case with a known answer, so it becomes one
`known_answer` row (the shape of `ka_prior_case_false_positive`):

  * a sweep scenario   <out>/sweep/ka_fb_<right|wrong>_<id>.json
  * a world fixture    <out>/fixtures/ka_fb_<right|wrong>_<id>.json
    replaying what the session read (the record, every run_op result), with
    `strict_ops` so an unrecorded lookup is an honest miss.

GRADER SHAPE (uses the existing `verdict_is`; no grader was added)
  rated right  -> {"verdict_is": {"any_of": ["<verdict the AI gave>"]}}
  rated wrong  -> {"verdict_is": {"any_of": [every OTHER disposition]}}
                  A "wrong" rating says the AI's verdict was not the answer; it
                  does not say what the answer was. So the row grades "the
                  verdict must NOT be X" (any other disposition passes), never
                  a guessed correct verdict. The row's `why` says so.

USAGE
  # offline: a connector sqlite store (the box db, or an exported copy)
  python scripts/export_feedback_evals.py --from-store path/to/store.db
  # live read of the connector on a box (read-only: monitor_cases + chat_history)
  envrun <box env file> -- python scripts/export_feedback_evals.py --from-box 92
  # review <out>/sweep and <out>/fixtures, then copy the rows you accept into
  # the tracked set (scripts/conversations/sweep + scripts/eval_fixtures/ztpf)
  python scripts/export_feedback_evals.py --promote [--only name,name]

  --out DIR        default scratch/feedback_evals (gitignored). Rows are NEVER
                   written into tracked fixtures except by --promote.
  --hours N        --from-box look-back window (default 2160 = 90 days)

SCRUBBING (every row, before anything is written)
  IPv4 addresses -> 192.0.2.x (private) / 198.51.100.x / 203.0.113.x (public);
  hostnames (host-ish fields, and their short form) -> host-N[.example.com];
  usernames (user-ish fields, `DOMAIN\\user`) and emails -> user-N /
  user-N@example.com; record uuids -> fixed fake uuids; epoch and ISO
  timestamps shift together to a fixed anchor. One mapping per row, so the same
  value is the same placeholder everywhere. After scrubbing the row is
  re-scanned: a learned original still present, or any IPv4 outside the
  documentation ranges, aborts the export. Not covered: IPv6, and a name that
  appears only in free text (an analyst note) and in no host/user field. Read
  the rows before --promote.

WHAT IS REPLAYED
  run_op -> an `ops` entry (connector, op, scalar params as the match, the
  result without the tool layer's output_shape keys). edr_host -> the FortiEDR
  `get_collector_list` / `get_event_list_extended` results it wrapped.
  find(kind=action) -> `enrichment_actions`. get_record / search_module_records
  / prior_cases -> records in the module tables. NOT replayed, and not a skip:
  update_record / create_record (the session's own writes, not inputs to the
  verdict; the world is not writable, so it refuses them, and no grader
  requires them), picklist / get_op_schema / find of fields (schema lookups the
  framework answers itself), emit_* (output).

THE RECORD. A session does not store the record it triaged (auto_triage mounts
it as the chat entity), and a typical session never re-reads it. `--from-box`
therefore reads the record now and removes the fields the session itself wrote
(a close), putting status back to Open. `--from-store` has no box to read, so a
session whose transcript never read the record is skipped (no_record_snapshot).

A row is skipped (counted, with the reason) when: unrated or conflicting
ratings, no verdict card, the record could not be obtained, or the session used
a tool this exporter cannot replay (named exactly, e.g.
`unreplayable_tool:siem_search`; with --allow-partial the row is written and its
`why` lists the gap).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

SWEEP_DIR = _HERE / "conversations" / "sweep"
FIXTURE_DIR = _HERE / "eval_fixtures" / "ztpf"
DEFAULT_OUT = _ROOT / "scratch" / "feedback_evals"

ALLOWED = ("true_positive", "false_positive", "benign", "suspicious",
           "needs_more_info")
USER_PROMPT = "Triage this alert. Check the evidence and give me your verdict."

# Tools whose result we can put back in the world.
_REPLAY = {"run_op", "edr_host", "get_record", "search_module_records", "prior_cases",
           "find"}
# Writes the session made. They are outputs of the decision, not inputs to the
# verdict: never replayed, never required. The world refuses a write (it is not
# `writable`), the way every existing known-answer row does.
_WRITES = {"update_record", "create_record"}
# Schema and capability lookups (a picklist value, a field list, an op's
# parameters). The framework answers them from its own reference data, not from
# the box, so they carry no case evidence and there is nothing to replay.
_SCHEMA = {"picklist", "get_op_schema", "list_configured_connectors", "get_step_type"}
# Keys the run_op tool layer adds to a connector result; not part of the world.
_TOOL_NOISE = ("output_shape", "output_top_keys", "output_is_list", "schema_cached")
# Tools that only emit output to the analyst: nothing to replay.
_OUTPUT_PREFIXES = ("emit_",)

_HOST_KEYS = {"computername", "hostname", "host", "devicename", "srchostname",
              "desthostname", "sourcehost", "destinationhost", "fqdn",
              "dnsname", "machinename", "assetname"}
_USER_KEYS = {"user", "username", "sourceuser", "destinationuser", "accountname",
              "srcuser", "destuser", "loginname", "samaccountname"}
_TIME_KEYS = re.compile(r"(date|time|_at$|^at$|timestamp)", re.I)

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
_IRI = re.compile(r"^/api/3/([A-Za-z0-9_]+)/([0-9a-fA-F-]{36})$")
_DOC_NETS = [ipaddress.ip_network(n) for n in
             ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
_ANCHOR = 1790000000.0


# --------------------------------------------------------------------------
# Scrubbing
# --------------------------------------------------------------------------
def _is_doc_ip(s: str) -> bool:
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    return any(ip in n for n in _DOC_NETS)


class Scrubber:
    """One consistent original -> placeholder mapping for one row."""

    def __init__(self) -> None:
        self.ips: dict[str, str] = {}
        self.hosts: dict[str, str] = {}      # lower-case original -> placeholder
        self.users: dict[str, str] = {}
        self.emails: dict[str, str] = {}
        self.uuids: dict[str, str] = {}
        self._epochs: list[float] = []
        self._used_ips: set[str] = set()
        self.delta: float = 0.0

    # ---- learning ----
    def learn(self, obj: Any, key: str = "") -> None:
        if isinstance(obj, dict):
            m = _IRI.match(str(obj.get("@id") or ""))
            if m and m.group(1) != "picklists":
                self._learn_uuid(m.group(2))
                if obj.get("uuid"):
                    self._learn_uuid(str(obj["uuid"]))
            for k, v in obj.items():
                self.learn(k if isinstance(k, str) else "", "")
                self.learn(v, str(k))
        elif isinstance(obj, list):
            for v in obj:
                self.learn(v, key)
        elif isinstance(obj, str):
            self._learn_str(obj, key)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            if _TIME_KEYS.search(key) and 1.0e9 <= float(obj) <= 2.5e9:
                self._epochs.append(float(obj))

    def _learn_uuid(self, u: str) -> None:
        u = u.lower()
        if u not in self.uuids:
            self.uuids[u] = "00000000-0000-4000-8000-%012d" % (len(self.uuids) + 1)

    def _learn_str(self, s: str, key: str) -> None:
        for em in _EMAIL.findall(s):
            self._learn_email(em)
        for ip in _IPV4.findall(s):
            self._learn_ip(ip)
        for t in _ISO.findall(s):
            ts = _iso_to_epoch(t)
            if ts:
                self._epochs.append(ts)
        norm = key.replace("_", "").lower()
        v = s.strip()
        if not v or _IPV4.fullmatch(v):
            return
        if norm in _HOST_KEYS and len(v) >= 3:
            self._learn_host(v)
        elif norm in _USER_KEYS and len(v) >= 2 and "@" not in v:
            self._learn_user(v)

    def _learn_ip(self, ip: str) -> None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return
        if ip in self.ips:
            return
        if _is_doc_ip(ip):
            self.ips[ip] = ip
            self._used_ips.add(ip)
            return
        nets = ([_DOC_NETS[0], _DOC_NETS[1], _DOC_NETS[2]] if addr.is_private
                else [_DOC_NETS[1], _DOC_NETS[2], _DOC_NETS[0]])
        for net in nets:
            for host in net.hosts():
                cand = str(host)
                if cand not in self._used_ips:
                    self._used_ips.add(cand)
                    self.ips[ip] = cand
                    return
        raise RuntimeError("out of documentation addresses for one row")

    def _learn_host(self, v: str) -> None:
        low = v.lower()
        if low in self.hosts:
            return
        n = len({p for p in self.hosts.values() if "." not in p}) + 1
        label = f"host-{n}"
        if "." in low:
            self.hosts[low] = f"{label}.example.com"
            short = low.split(".", 1)[0]
            if len(short) >= 4 and short not in self.hosts:
                self.hosts[short] = label
        else:
            self.hosts[low] = label

    def _learn_user(self, v: str) -> None:
        for part in {v, v.split("\\")[-1]}:
            low = part.lower()
            if low and low not in self.users:
                self.users[low] = f"user-{len(self.users) + 1}"
        if "\\" in v:      # DOMAIN\name -> user-N, domain left to its own pass
            self.users[v.lower()] = self.users[v.split("\\")[-1].lower()]

    def _learn_email(self, em: str) -> None:
        low = em.lower()
        if low in self.emails:
            return
        n = len(self.emails) + 1
        self.emails[low] = f"user-e{n}@example.com"
        self._learn_user(em.split("@", 1)[0])

    def finish(self) -> None:
        """After learning: fix the time shift."""
        if self._epochs:
            self.delta = _ANCHOR - min(self._epochs)

    # ---- applying ----
    def text(self, s: str) -> str:
        for em in sorted(self.emails, key=len, reverse=True):
            s = re.sub(re.escape(em), self.emails[em], s, flags=re.I)
        s = _IPV4.sub(lambda m: self.ips.get(m.group(0), m.group(0)), s)
        for u in sorted(self.uuids, key=len, reverse=True):
            s = re.sub(re.escape(u), self.uuids[u], s, flags=re.I)
        for table in (self.hosts, self.users):
            for orig in sorted(table, key=len, reverse=True):
                s = re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(orig)
                           + r"(?![A-Za-z0-9_-])", table[orig], s, flags=re.I)
        if self.delta:
            s = _ISO.sub(self._shift_iso, s)
        return s

    def _shift_iso(self, m: "re.Match[str]") -> str:
        ts = _iso_to_epoch(m.group(0))
        if not ts:
            return m.group(0)
        out = datetime.fromtimestamp(ts + self.delta, tz=timezone.utc)
        return out.strftime("%Y-%m-%dT%H:%M:%S") + ("Z" if m.group(0).endswith("Z") else "")

    def apply(self, obj: Any, key: str = "") -> Any:
        if isinstance(obj, dict):
            return {self.text(k) if isinstance(k, str) else k: self.apply(v, str(k))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.apply(v, key) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, (int, float)) and not isinstance(obj, bool) \
                and _TIME_KEYS.search(key) and 1.0e9 <= float(obj) <= 2.5e9:
            return float(obj) + self.delta
        return obj

    # ---- self-check ----
    def assert_clean(self, serialized: str, *, where: str) -> None:
        low = serialized.lower()
        leaks = []
        for table in (self.ips, self.hosts, self.users, self.emails, self.uuids):
            for orig, repl in table.items():
                if orig.lower() == repl.lower():
                    continue
                if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(orig.lower())
                             + r"(?![A-Za-z0-9_-])", low):
                    leaks.append(orig)
        stray = [ip for ip in _IPV4.findall(serialized)
                 if not _is_doc_ip(ip)]
        if leaks or stray:
            # Report the COUNT only: the values are the sensitive part.
            raise SystemExit(f"{where}: scrub left {len(leaks)} learned value(s) and "
                             f"{len(stray)} non-documentation IPv4 in the output; "
                             f"row not written")


def _iso_to_epoch(t: str) -> Optional[float]:
    try:
        base = t.rstrip("Z").split(".")[0]
        return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Sources -> decisions
# --------------------------------------------------------------------------
# A decision is {session_id, record_iri, feedback: [{rating, note}], frames}.
def decisions_from_store(db_path: str) -> list[dict]:
    p = Path(db_path)
    if not p.is_file():
        raise SystemExit(f"--from-store: no such file {db_path!r}")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        by_sid: dict[str, dict] = {}
        for sid, iri, rating, note in con.execute(
                "SELECT session_id, record_iri, rating, note FROM decision_feedback "
                "ORDER BY updated_at"):
            d = by_sid.setdefault(sid, {"session_id": sid, "record_iri": iri,
                                        "feedback": [], "frames": []})
            d["feedback"].append({"rating": rating, "note": note})
        for sid, d in by_sid.items():
            for (pj,) in con.execute(
                    "SELECT payload_json FROM session_transcript WHERE session_id=? "
                    "AND role='assistant' ORDER BY seq", (sid,)):
                try:
                    d["frames"] += json.loads(pj).get("transcript") or []
                except (TypeError, ValueError, AttributeError):
                    continue
    finally:
        con.close()
    return list(by_sid.values())


def decisions_from_box(alias: str, hours: float, *, client: Any = None) -> list[dict]:
    """Read-only: `monitor_cases` view=list rows that carry feedback, then the
    session transcript of each through `chat_history`. `mine` (the caller's own
    rating and note) wins; failing that the right/wrong counts decide."""
    if client is None:
        from fsr_live import LiveFSR
        try:
            client = LiveFSR.from_env()
        except Exception as e:  # noqa: BLE001
            raise SystemExit(
                f"--from-box {alias}: no box environment ({e}). Run under the box's "
                f"env: envrun $(scripts/box.sh --need env --print | sed -n 's/^BOX_ENV=//p') -- "
                f"python scripts/export_feedback_evals.py --from-box {alias}") from e
    res = client.execute("monitor_cases", {"view": "list", "bucket": "all",
                                           "hours": hours, "limit": 200,
                                           "check_reopened": False})
    if res.ok is False or not isinstance(res.data, dict):
        raise SystemExit(f"monitor_cases failed: {res.error}")
    out = []
    for row in res.data.get("rows") or []:
        fb = row.get("feedback") or {}
        right, wrong = int(fb.get("right") or 0), int(fb.get("wrong") or 0)
        if not (right or wrong):
            continue
        entries: list[dict] = []
        if fb.get("mine") in ("right", "wrong"):
            entries = [{"rating": fb["mine"], "note": fb.get("note")}]
        else:
            entries = ([{"rating": "right", "note": None}] * right
                       + [{"rating": "wrong", "note": None}] * wrong)
        h = client.execute("chat_history", {"session_id": row["session_id"], "limit": 200})
        frames: list[dict] = []
        for turn in (h.data or {}).get("turns") or []:
            frames += turn.get("transcript") or []
        record = None
        try:    # the record as it is now; build_row undoes the session's own writes
            got = client.client.get(row["record_iri"])
            record = got if isinstance(got, dict) else None
        except Exception:  # noqa: BLE001
            record = None
        out.append({"session_id": row["session_id"], "record_iri": row.get("record_iri"),
                    "feedback": entries, "frames": frames, "record": record})
    return out


# --------------------------------------------------------------------------
# Decision -> row
# --------------------------------------------------------------------------
def _calls(frames: list[dict]) -> list[dict]:
    """Every tool call with its result, through the framework's frame reader.

    A live session streams each `tool_use` twice under one id: first with an
    empty input, then with the full one. The framework's `tool_calls` keeps the
    first, so a call arrives with no arguments. Merge by id here and keep the
    last non-empty input."""
    from fsr_playbooks.harness.frames import frames_of, tool_results
    results = {r.tool_use_id: r.body for r in tool_results(frames)}
    order: list[str] = []
    calls: dict[str, dict] = {}
    for f in frames_of(frames, "tool_use"):
        cid = f.get("id") or f"anon{len(order)}"
        if cid not in calls:
            order.append(cid)
            calls[cid] = {"name": f.get("name"), "input": {}, "result": None,
                          "has_result": cid in results}
        if f.get("input"):
            calls[cid]["input"] = f["input"]
    for cid in order:
        calls[cid]["result"] = results.get(cid)
    return [calls[c] for c in order]


def _op_entry(conn: str, op: str, params: dict, response: Any) -> dict:
    match = {k: v for k, v in params.items()
             if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    return {"connector": conn, "op": op, "match": match,
            "response": response if isinstance(response, dict)
            else {"ok": True, "data": response}}


def _edr_entries(args: dict, body: Any) -> Optional[list[dict]]:
    """`edr_host` is a wrapper: it runs the FortiEDR connector's
    `get_collector_list` (by IP, or the whole roster when only a name is given)
    and then `get_event_list_extended` for the device it found. Its result is
    what those two ops returned, so replay them. None when the result is an
    error (nothing readable to replay)."""
    if not isinstance(body, dict) or body.get("ok") is not True:
        return None
    ip = str(args.get("ip") or "").strip()
    conn = "fortinet-fortiedr"
    collectors = body.get("collectors") or []
    first = (_op_entry(conn, "get_collector_list", {"ips": ip}, {"ok": True, "data": collectors})
             if ip else
             _op_entry(conn, "get_collector_list", {"itemsPerPage": 1000},
                       {"ok": True, "data": collectors}))
    out = [first]
    ev = body.get("events")
    if isinstance(ev, dict) and ev.get("ok") is True:
        device = (collectors[0].get("name") if collectors else None) or args.get("host")
        if device:
            out.append(_op_entry(conn, "get_event_list_extended", {"device": device},
                                 {"ok": True, "data": ev.get("samples") or []}))
    elif isinstance(ev, dict):
        return None          # events errored: a partial read, not replayable
    return out


def _enrichment_from_find(body: Any) -> dict[str, list[dict]]:
    """`find(kind=action)` results back into the world's `enrichment_actions`."""
    if not isinstance(body, dict) or not isinstance(body.get("actions"), list):
        return {}
    target = str(body.get("target_type") or "").strip()
    if not target:
        return {}
    out = []
    for a in body["actions"]:
        if not isinstance(a, dict) or not a.get("connector") or not a.get("op"):
            continue
        names = [n for n in (a.get("required") or []) if isinstance(n, str)]
        ind = a.get("indicator_param")
        if ind and ind not in names:
            names.append(ind)
        out.append({"connector": a["connector"], "op": a["op"],
                    "title": a.get("title") or a["op"], "category": "threat-intel",
                    "tier": a.get("tier", 1), "requires_approval": False,
                    "status": "Available", "deprecated": False,
                    "required_params": [{"name": n, "type": "string"} for n in names],
                    "params": [{"name": n, "type": "string", "required": True}
                               for n in names],
                    "runs_on_agent": False})
    return {target: out} if out else {}


def _undo_session_writes(record: dict, writes: list[dict]) -> dict:
    """The record as read AFTER the session may have written to it (a live read
    sees the close). Drop the fields the session wrote; a written status goes
    back to Open. Without this the closed status and reason in the world would
    hand the agent the answer."""
    rec = copy.deepcopy(record)
    for w in writes:
        for k in (w.get("fields") or {}):
            rec.pop(k, None)
            if k == "status":
                rec["status"] = {"@type": "Picklist", "itemValue": "Open"}
    return rec


def _records_in(body: Any) -> list[dict]:
    """Module records in a tool result: the body itself, or items of a list
    one or two levels down (hydra:member, records, cases, ...)."""
    found: list[dict] = []

    def rec(x: Any) -> bool:
        m = _IRI.match(str(x.get("@id") or "")) if isinstance(x, dict) else None
        return bool(m and m.group(1) != "picklists")

    if rec(body):
        return [body]
    if isinstance(body, dict):
        for v in body.values():
            if rec(v):
                found.append(v)
            elif isinstance(v, list):
                found += [i for i in v if rec(i)]
            elif isinstance(v, dict):
                found += [i for x in v.values() if isinstance(x, list)
                          for i in x if rec(i)]
    elif isinstance(body, list):
        found += [i for i in body if rec(i)]
    return found


def _rating_of(feedback: list[dict]) -> tuple[Optional[str], str]:
    r = Counter(e.get("rating") for e in feedback)
    right, wrong = r.get("right", 0), r.get("wrong", 0)
    if right and not wrong:
        rating = "right"
    elif wrong and not right:
        rating = "wrong"
    else:
        return None, "unrated" if not (right or wrong) else "conflicting_ratings"
    notes = [str(e["note"]).strip() for e in feedback
             if e.get("rating") == rating and e.get("note")]
    return rating, " | ".join(dict.fromkeys(notes))


def expected_dispositions(verdict: str, rating: str) -> list[str]:
    """right -> the AI's verdict; wrong -> anything but it."""
    if rating == "right":
        return [verdict]
    return [d for d in ALLOWED if d != verdict]


def build_row(decision: dict, *, allow_partial: bool = False) -> tuple[Optional[dict], str]:
    """(row, "") or (None, skip reason). row = {name, spec, bundle, meta}."""
    rating, note = _rating_of(decision.get("feedback") or [])
    if rating is None:
        return None, note
    frames = [f for f in decision.get("frames") or [] if isinstance(f, dict)]
    from fsr_playbooks.harness.frames import frames_of
    cards = frames_of(frames, "verdict_card")
    verdict = str((cards[-1] if cards else {}).get("disposition") or "").strip().lower()
    if verdict not in ALLOWED:
        return None, "no_verdict"

    iri = decision.get("record_iri") or ""
    m = _IRI.match(iri)
    if not m:
        return None, "bad_record_iri"
    module, uuid = m.group(1), m.group(2).lower()

    calls = _calls(frames)
    modules: dict[str, list[dict]] = {}
    ops: list[dict] = []
    seen_ops: set[str] = set()
    gaps: list[str] = []
    connectors: list[str] = []
    enrichment: dict[str, list[dict]] = {}
    writes: list[dict] = []

    def add_op(entry: dict) -> None:
        key = json.dumps([entry["connector"], entry["op"], entry["match"]],
                         sort_keys=True, default=str)
        if key in seen_ops:
            return
        seen_ops.add(key)
        ops.append(entry)
        if entry["connector"] not in connectors:
            connectors.append(entry["connector"])

    def gap(label: str) -> None:
        if label not in gaps:
            gaps.append(label)

    for c in calls:
        name = str(c["name"] or "")
        if name.startswith(_OUTPUT_PREFIXES) or name in _SCHEMA:
            continue
        if name in _WRITES:
            writes.append(c["input"])
            continue
        if name not in _REPLAY:
            gap(name)
            continue
        if not c["has_result"]:
            gap(f"{name}(no result)")
            continue
        body, args = c["result"], c["input"]
        if name == "run_op":
            conn, op = args.get("connector"), args.get("op") or args.get("operation")
            if not conn or not op:
                gap("run_op(no arguments)")
                continue
            if isinstance(body, dict):
                body = {k: v for k, v in body.items() if k not in _TOOL_NOISE}
            params = args.get("params") if isinstance(args.get("params"), dict) else {}
            add_op(_op_entry(conn, op, params, body))
        elif name == "edr_host":
            entries = _edr_entries(args, body)
            if entries is None:
                gap("edr_host(error result)")
            for e in entries or []:
                add_op(e)
        elif name == "find":
            if args.get("kind") == "action":
                for tgt, acts in _enrichment_from_find(body).items():
                    enrichment.setdefault(tgt, [])
                    enrichment[tgt] += [a for a in acts if not any(
                        x["connector"] == a["connector"] and x["op"] == a["op"]
                        for x in enrichment[tgt])]
            # Other `find` kinds are schema discovery (fields, playbooks).
        else:
            found = _records_in(body)
            for r in found:
                mod = _IRI.match(r["@id"]).group(1)
                rows = modules.setdefault(mod, [])
                if not any(x.get("uuid") == r.get("uuid") for x in rows):
                    rows.append(copy.deepcopy(r))
            if name == "prior_cases" and isinstance(body, dict) \
                    and (body.get("count") or 0) > len(found):
                gap("prior_cases(earlier cases not readable)")
    if gaps and not allow_partial:
        return None, "unreplayable_tool:" + ",".join(sorted(set(gaps)))

    # The record the session triaged. A transcript rarely holds it (it is the
    # mounted entity, not a tool read), so a source may attach a live read.
    snap = decision.get("record")
    if isinstance(snap, dict) and snap.get("uuid"):
        sm = _IRI.match(str(snap.get("@id") or iri))
        rows = modules.setdefault(sm.group(1) if sm else module, [])
        if not any(str(x.get("uuid", "")).lower() == uuid for x in rows):
            rows.insert(0, _undo_session_writes(snap, writes))
    mounted = next((r for r in modules.get(module, [])
                    if str(r.get("uuid", "")).lower() == uuid), None)
    if mounted is None:
        return None, "no_record_snapshot"

    tag = hashlib.sha1(decision["session_id"].encode()).hexdigest()[:8]
    name = f"ka_fb_{rating}_{tag}"
    expect = expected_dispositions(verdict, rating)

    bundle: dict[str, Any] = {
        "_README": [
            "KNOWN-ANSWER world exported from an analyst-rated auto-triage decision.",
            "Replays what the session read: the record and each recorded lookup.",
            "strict_ops: an op not recorded here is an honest miss, never the canned default.",
            "Values were scrubbed to documentation addresses and placeholder names.",
        ],
        "strict_ops": True,
        "configured_connectors": [
            {"name": c, "status": "Completed", "version": "1.0.0", "label": c,
             "config_count": 1} for c in connectors],
        "personas": {},
        "skills": [],
        "modules": modules,
        "ops": ops,
    }
    if enrichment:
        bundle["enrichment_actions"] = enrichment
        for acts in enrichment.values():
            for a in acts:
                if not any(c["name"] == a["connector"]
                           for c in bundle["configured_connectors"]):
                    bundle["configured_connectors"].append(
                        {"name": a["connector"], "status": "Completed",
                         "version": "1.0.0", "label": a["connector"], "config_count": 1})
    if gaps:
        bundle["_README"].append("PARTIAL world: not replayed: " + ", ".join(sorted(set(gaps))))

    if rating == "right":
        verdict_line = (f"An analyst rated the AI's verdict {verdict!r} RIGHT, so the "
                        f"answer is {verdict!r}.")
    else:
        verdict_line = (f"An analyst rated the AI's verdict {verdict!r} WRONG. A wrong "
                        f"rating does not say what the right answer was, so this row "
                        f"grades only that the verdict must NOT be {verdict!r}; any "
                        f"other disposition passes.")
    why = ["KNOWN-ANSWER row: exported from analyst feedback on a real auto-triage "
           "decision (X5). Its records bundle fixes the evidence (strict_ops).", "",
           verdict_line]
    if note:
        why += ["", f"Analyst note: {note}"]
    if gaps:
        why += ["", "PARTIAL world, not replayed: " + ", ".join(sorted(set(gaps)))]

    spec = {
        "name": name, "category": "known_answer", "intent": "triage", "module": module,
        "records": name, "mount": {"module": module, "uuid": uuid},
        "why": why,
        "steps": [{"user": USER_PROMPT}],
        "expect": {"tools_are_real": True, "verdict_is": {"any_of": expect}},
    }

    # One mapping across the spec and the bundle, so a value is the same
    # placeholder in both.
    sc = Scrubber()
    sc._learn_uuid(uuid)
    sc.learn(bundle)
    sc.learn(spec["why"])
    sc.finish()
    spec = sc.apply(spec)
    bundle = sc.apply(bundle)
    sc.assert_clean(json.dumps(spec) + json.dumps(bundle), where=name)
    return {"name": name, "spec": spec, "bundle": bundle,
            "meta": {"rating": rating, "verdict": verdict, "expect": expect}}, ""


# --------------------------------------------------------------------------
# Output, validation, promotion
# --------------------------------------------------------------------------
def write_rows(rows: list[dict], out: Path) -> None:
    (out / "sweep").mkdir(parents=True, exist_ok=True)
    (out / "fixtures").mkdir(parents=True, exist_ok=True)
    for r in rows:
        (out / "sweep" / f"{r['name']}.json").write_text(
            json.dumps(r["spec"], indent=1) + "\n")
        (out / "fixtures" / f"{r['name']}.json").write_text(
            json.dumps(r["bundle"], indent=1) + "\n")


def validate_out(out: Path, names: Optional[list[str]] = None) -> list[str]:
    """Load every row in `out` the way chat_sweep does (build_graders, the
    known-answer guard, the fixture world) and return the names that loaded.
    Raises SystemExit on any row the sweep would refuse."""
    from scripts import chat_sweep, fixture_box
    from scripts.fixture_box import FixtureBox
    names = names or sorted(p.stem for p in (out / "sweep").glob("*.json"))
    if not names:
        return []
    real_load = fixture_box.load_bundle
    old_corpus = chat_sweep.CORPUS
    fixture_box.load_bundle = lambda n, **kw: real_load(n, root=out / "fixtures", **kw)
    chat_sweep.CORPUS = out / "sweep"
    try:
        loaded = chat_sweep.load_corpus(only=",".join(names))
        for sc in loaded:
            spec = json.loads((out / "sweep" / f"{sc.name}.json").read_text())
            box = FixtureBox(fixture_box.load_bundle(spec["records"]))
            if box.record(spec["mount"]["module"], spec["mount"]["uuid"]) is None:
                raise SystemExit(f"{sc.name}: mounted record is not in the world")
        return [s.name for s in loaded]
    finally:
        chat_sweep.CORPUS = old_corpus
        fixture_box.load_bundle = real_load


def promote(out: Path, only: Optional[list[str]], *, force: bool = False,
            sweep_dir: Path = SWEEP_DIR, fixture_dir: Path = FIXTURE_DIR) -> list[str]:
    names = only or sorted(p.stem for p in (out / "sweep").glob("*.json"))
    if not names:
        raise SystemExit(f"nothing to promote in {out}")
    validate_out(out, names)
    moved = []
    for n in names:
        s, f = out / "sweep" / f"{n}.json", out / "fixtures" / f"{n}.json"
        if not (s.is_file() and f.is_file()):
            raise SystemExit(f"{n}: not in {out}")
        for src, dst in ((s, sweep_dir / s.name), (f, fixture_dir / f.name)):
            if dst.exists() and not force:
                raise SystemExit(f"{dst} exists (pass --force to replace)")
            shutil.copyfile(src, dst)
        moved.append(n)
    return moved


def export(decisions: list[dict], out: Path, *, allow_partial: bool = False) -> dict:
    rows, skipped = [], Counter()
    for d in decisions:
        row, why = build_row(d, allow_partial=allow_partial)
        if row is None:
            skipped[why] += 1
            continue
        rows.append(row)
    write_rows(rows, out)
    if rows:
        validate_out(out, [r["name"] for r in rows])
    return {"decisions": len(decisions), "rows": [r["name"] for r in rows],
            "skipped": dict(skipped), "out": str(out)}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--from-store", metavar="SQLITE", help="a connector sqlite store")
    src.add_argument("--from-box", metavar="ALIAS",
                     help="a box alias (BOX=<alias> accepted); run under envrun")
    src.add_argument("--promote", action="store_true",
                     help="copy reviewed rows from --out into the tracked set")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--hours", type=float, default=2160.0)
    ap.add_argument("--only", default="", help="--promote: comma-separated row names")
    ap.add_argument("--force", action="store_true", help="--promote: replace existing rows")
    ap.add_argument("--allow-partial", action="store_true",
                    help="export rows whose session used tools that cannot be replayed")
    args = ap.parse_args(argv)
    out = Path(args.out).expanduser()

    if args.promote:
        names = promote(out, [n.strip() for n in args.only.split(",") if n.strip()] or None,
                        force=args.force)
        print(f"promoted {len(names)} row(s): {', '.join(names)}")
        return 0
    if args.from_store:
        decisions = decisions_from_store(args.from_store)
    elif args.from_box:
        decisions = decisions_from_box(args.from_box.removeprefix("BOX="), args.hours)
    else:
        ap.error("one of --from-store, --from-box, --promote is required")
    summary = export(decisions, out, allow_partial=args.allow_partial)
    print(f"{len(summary['rows'])} row(s) from {summary['decisions']} rated decision(s) "
          f"-> {summary['out']}")
    for n in summary["rows"]:
        print(f"  {n}")
    for why, n in sorted(summary["skipped"].items()):
        if n:
            print(f"  skipped {n}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
