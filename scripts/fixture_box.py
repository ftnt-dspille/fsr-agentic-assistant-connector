"""A record-table cassette backend -- a fake FortiSOAR that actually queries.

WHY THIS EXISTS
---------------
The URL-substring cassette in `local_turn.py` answers one canned body per URL
fragment. That is enough for a persona lookup and a single mounted record, and
it is NOT enough for any question about RELATED records, because every such
question is a filter:

    "steps on this device"          -> ztpfDevices.uuid == <uuid>
    "steps with no run group"       -> ztpfRunGroups IS NULL
    "the latest run group"          -> sort by createDate, take one

A substring rule cannot tell those apart -- it returns the same rows for all
three. A fixture that answers "pending steps" with a completed step does not
merely fail to catch a defect, it MANUFACTURES one: the model reads the rows,
sees they contradict the question, and either flails or narrates something
false. That failure would be graded against the model.

So this serves a real record table instead: the same rows the box has, with the
filter/sort/limit semantics the connector's query path actually uses. `filters`
that mean "no run group" return exactly the two ungrouped steps, and nothing
else.

WHAT IT IS NOT
--------------
Not FortiSOAR. It implements the read surface the assistant reaches through --
`GET /api/3/<module>`, `GET /api/3/<module>/<uuid>`, `POST /api/query/<module>`
-- and refuses everything else LOUDLY (`599`), because the whole value here is
that an unanswered read is visible rather than silently empty. Writes are not
served at all: a write must stop at the approval gate, and a fixture box that
accepted one would hide exactly that.

The exception is a bundle that declares `writable: true`. A BUILD ("set this
profile up like our other ones") is N approved writes, and a box that refuses
the first one ends the arc at card one -- nothing past it is ever exercised.
Writable, an approved `create_record` / `update_record` applies to the table
in memory and is logged in `writes`, so the next read sees it and a grader can
judge the resulting WORLD rather than the cards. The approval gate still sits
in front: the cassette only reaches `write()` from a tool body, which a tier-3
call only enters after the analyst approves.
"""
from __future__ import annotations

import copy
import json
import re
import uuid as _uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, unquote_plus

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class FixtureBoxError(RuntimeError):
    """The bundle is malformed -- a build-time error, never a turn outcome."""


def load_bundle(name_or_path: str, *, root: Optional[Path] = None,
                _depth: int = 0) -> dict:
    """Load a fixture bundle by bare name (`ztpf_device_surface`) or path."""
    root = root or (Path(__file__).parent / "eval_fixtures" / "ztpf")
    p = Path(name_or_path)
    if not p.exists():
        p = root / (name_or_path if name_or_path.endswith(".json")
                    else f"{name_or_path}.json")
    if not p.exists():
        raise FixtureBoxError(f"no fixture bundle {name_or_path!r} (looked at {p})")
    bundle = json.loads(p.read_text())

    # `extends` lets a variant re-present the SAME captured box under a
    # different persona substrate, or with house rules layered on, without a
    # second copy of the capture. A copy would drift, and a drifted fixture
    # reads as a model result.
    parent = bundle.pop("extends", None)
    if parent:
        if _depth > 4:
            raise FixtureBoxError(f"{p}: `extends` chain is too deep / cyclic")
        base = load_bundle(parent, root=root, _depth=_depth + 1)
        merged = dict(base)
        merged.update({k: v for k, v in bundle.items() if k != "modules"})
        mods = dict(base.get("modules") or {})
        mods.update(bundle.get("modules") or {})
        merged["modules"] = mods
        bundle = merged

    if not isinstance(bundle.get("modules"), dict):
        raise FixtureBoxError(f"{p}: bundle has no `modules` map")

    # A module row written as `{"$file": "alert_c2_exfil"}` is loaded from the
    # standalone triage fixture instead of being pasted in. The alert/incident
    # captures are already mounted BY FILE as the drawer record (`record:` in a
    # scenario spec); copying them into a bundle so the TABLE can answer too
    # would leave the same record living in two places -- and "mounting a copy
    # is how a fixture starts lying about what the box holds" is the exact
    # hazard the mount path warns about. One file, both roles.
    for mod, rows in bundle["modules"].items():
        if not isinstance(rows, list):
            continue
        bundle["modules"][mod] = [
            _load_record_file(r["$file"], p)
            if isinstance(r, dict) and set(r) == {"$file"} else r
            for r in rows
        ]
    _reanchor(bundle)
    return bundle


def _epoch_of(value: Any) -> float:
    """Epoch seconds of a FortiSIEM ISO-8601 'Z' timestamp (0.0 when unreadable)."""
    import datetime as _dt
    try:
        d = _dt.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return 0.0
    return d.replace(tzinfo=_dt.timezone.utc).timestamp()


def _reanchor(bundle: dict) -> None:
    """Move a bundle's recorded times so its first alert is `alert_age_hours` old
    at load time. `time_anchor` is set on the bundle; every alert createDate and
    every event phRecvTime moves by the same delta, so the events keep their
    offsets from the alert. A row that says "26h-old record" stays 26h old on any
    day it runs."""
    import datetime as _dt
    anchor = bundle.get("time_anchor")
    if not isinstance(anchor, dict):
        return
    alerts = [r for r in (bundle.get("modules") or {}).get("alerts") or []
              if isinstance(r, dict) and r.get("createDate")]
    if not alerts:
        return
    target = _dt.datetime.now(_dt.timezone.utc).timestamp() - float(
        anchor["alert_age_hours"]) * 3600
    alert_orig = float(alerts[0]["createDate"])
    delta = target - alert_orig
    for r in alerts:
        r["createDate"] = float(r["createDate"]) + delta
    events = [r for entry in bundle.get("ops") or []
              for r in ((entry.get("response") or {}).get("data") or [])
              if isinstance(r, dict) and r.get("phRecvTime")]
    ev_delta = delta
    if anchor.get("events_end_at_alert") and events:
        # The recorded beacon ran some hours before the alert fired. For a row
        # whose point is the alert's own window, end the beacon at the alert.
        last = max(_epoch_of(r["phRecvTime"]) for r in events)
        ev_delta = (alert_orig + delta) - last
    for r in events:
        t = _dt.datetime.fromtimestamp(_epoch_of(r["phRecvTime"]) + ev_delta,
                                       _dt.timezone.utc)
        r["phRecvTime"] = t.strftime("%Y-%m-%dT%H:%M:%SZ")


_TRIAGE_FIXTURES = (Path(__file__).resolve().parent.parent
                    / "connector-fsr-agentic-assistant" / "fsr_soc_triage"
                    / "tests" / "fixtures" / "triage")


def _load_record_file(name: str, bundle_path: Path) -> dict:
    """Resolve a `{"$file": ...}` row: bundle-relative first, then triage."""
    for cand in (bundle_path.parent / name, bundle_path.parent / f"{name}.json",
                 _TRIAGE_FIXTURES / name, _TRIAGE_FIXTURES / f"{name}.json"):
        if cand.exists():
            rec = json.loads(cand.read_text())
            if not isinstance(rec, dict):
                raise FixtureBoxError(f"{cand}: expected one record object")
            return rec
    raise FixtureBoxError(
        f"{bundle_path}: no record file {name!r} (looked beside the bundle "
        f"and in {_TRIAGE_FIXTURES})")


# --------------------------------------------------------------------------
# Field access + condition evaluation
# --------------------------------------------------------------------------
def _get_path(rec: Any, field: str) -> Any:
    """`ztpfDevices.uuid` -> rec["ztpfDevices"]["uuid"], `None` if any hop is
    missing. Picklists are followed the way the query API does: a bare
    `status` compared against a string matches its `itemValue`."""
    cur = rec
    for part in field.split("."):
        if isinstance(cur, list):
            # A to-many hop: collect, so `ztpfDevices.uuid` works whether the
            # relationship came back as one object or a list of them.
            cur = [(_get_path(i, part) if isinstance(i, (dict, list)) else None)
                   for i in cur]
            cur = [v for v in cur if v is not None]
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _public(rec: Any) -> Any:
    """A row as the box would serve it: without the bundle's own annotations.

    `_staged` (and any `_`-prefixed key) is a note for whoever reads the
    bundle. Served, the model read it as a record field and told the analyst
    "the profile record is still `_staged`" -- a fixture artifact in the reply,
    which no box would ever produce.
    """
    if isinstance(rec, dict):
        return {k: v for k, v in rec.items() if not str(k).startswith("_")}
    return rec


def _is_empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _scalarize(v: Any) -> Any:
    """Compare against the value a human (and the query API) means: a picklist
    by its display value, a relationship by its iri."""
    if isinstance(v, dict):
        for k in ("itemValue", "@id", "uuid", "name"):
            if v.get(k) is not None:
                return v[k]
    return v


def _match(rec: dict, cond: dict) -> bool:
    field = cond.get("field") or cond.get("key") or ""
    op = (cond.get("operator") or cond.get("op") or "eq").lower()
    want = cond.get("value")
    got = _get_path(rec, field)

    if op == "isnull":
        return _is_empty(got) is bool(want)
    if _is_empty(got) and op in ("eq", "in", "contains"):
        # An absent field never equals a wanted value. Explicit, because the
        # `None == None` case would otherwise make "no run group" match a
        # filter for a SPECIFIC run group.
        return False

    cand = [_scalarize(g) for g in (got if isinstance(got, list) else [got])]
    if op in ("eq", "="):
        return any(str(cv) == str(want) for cv in cand)
    if op in ("neq", "ne", "!="):
        return all(str(cv) != str(want) for cv in cand)
    if op == "in":
        wants = {str(w) for w in (want if isinstance(want, list) else [want])}
        return any(str(cv) in wants for cv in cand)
    if op in ("contains", "like"):
        return any(str(want).lower() in str(cv).lower() for cv in cand)
    if op in ("gt", "gte", "lt", "lte"):
        try:
            fns = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
                   "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
            return any(fns[op](float(cv), float(want)) for cv in cand)
        except (TypeError, ValueError):
            return False
    # An operator we do not implement must not silently pass everything --
    # that would be the substring cassette's failure mode wearing a filter.
    raise FixtureBoxError(f"fixture box does not implement operator {op!r}")


def _match_body(rec: dict, body: dict) -> bool:
    conds = body.get("filters") or []
    if not conds:
        return True
    logic = (body.get("logic") or "AND").upper()
    results = []
    for c in conds:
        if isinstance(c, dict) and ("filters" in c or "logic" in c):
            results.append(_match_body(rec, c))
        elif isinstance(c, dict):
            results.append(_match(rec, c))
    return all(results) if logic == "AND" else any(results)


def _qs_conditions(query: str) -> list[dict]:
    """The field filters carried on a GET query string, as `_match` conditions.

    crudhub takes plain `?<field>=<value>` equality filters on a collection read,
    and the persona resolver's direct lookup is exactly that
    (`?scopeModule=<module>&$limit=1`). A box that ignored them would hand back
    whichever row sorts first -- the same wrong-persona failure the `keys`
    handler already guards against, one URL shape over.

    `$`-prefixed params are crudhub controls (`$limit`, `$search`,
    `$relationships`), not fields, and are handled by the caller.
    """
    conds: list[dict] = []
    for part in query.split("&"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        # Form-encoded, as `urlencode` writes it: a space arrives as `+` and
        # `$limit` as `%24limit`. Decoding after the `$` check turned
        # `%24limit=5` into a field filter no record satisfies, and plain
        # `unquote` left `+` literal -- either way a name lookup matched nothing.
        field = unquote_plus(k)
        if not field or field.startswith("$"):
            continue
        if "[" in field:
            # A structured param (`sort[0][field]`, `filters[0][operator]`) is
            # not an equality filter. Treating it as one would match nothing and
            # return a confidently empty page -- the silent-wrong-answer failure
            # this box exists to make impossible.
            raise FixtureBoxError(
                f"fixture box does not implement query param {field!r}")
        value = unquote_plus(v)
        if field.endswith("$like"):
            # crudhub's substring filter: `name$like=%abc%`, case-insensitive.
            conds.append({"field": field[:-len("$like")], "operator": "like",
                          "value": value.strip("%")})
            continue
        conds.append({"field": field, "operator": "eq", "value": value})
    return conds


def _search_text(rec: dict, q: str) -> bool:
    return q.lower() in json.dumps(rec, default=str).lower()


def _sort(rows: list[dict], sort: Any) -> list[dict]:
    if not sort:
        return rows
    specs = sort if isinstance(sort, list) else [sort]
    for spec in reversed(specs):
        if isinstance(spec, dict):
            field = spec.get("field")
            desc = str(spec.get("direction", "ASC")).upper() == "DESC"
        else:
            field, desc = str(spec), False
            if field.startswith("-"):
                field, desc = field[1:], True
        if not field:
            continue

        def key(r, _f=field):
            v = _scalarize(_get_path(r, _f))
            return (v is None, str(v) if not isinstance(v, (int, float)) else v)
        try:
            rows = sorted(rows, key=key, reverse=desc)
        except TypeError:
            rows = sorted(rows, key=lambda r, _f=field: str(
                _scalarize(_get_path(r, _f))), reverse=desc)
    return rows


# --------------------------------------------------------------------------
# The persona substrate + learned house rules
# --------------------------------------------------------------------------
PERSONA_MODULE = "assistant_personas"
SKILL_MODULE = "assistant_skills"

# Picklists are platform content every box has, not scenario data. A bundle
# that carries none is answered from the reference DB the `picklist` tool
# already reads first -- the same catalog, so the live fallback it takes for a
# name the DB lacks gets a real box's answer (200, no items) instead of an
# unserved 404 (N5: `build_from_alert_context` asked for one).
_PICKLIST_MODULES = ("picklists", "picklist_names")


def _reference_picklists(module: str, query: str) -> Optional[dict]:
    """`/api/3/picklists?listName.name=X` / `/api/3/picklist_names` from the
    reference DB, in the API's shape. None when the DB can't be read -- then
    the read stays an honest miss."""
    try:
        import sqlite3
        from fsr_playbooks._db import default_db_path
        con = sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT list_name, item_value, item_iri FROM picklists").fetchall()
        con.close()
    except Exception:  # noqa: BLE001 -- no catalog: leave it a visible miss
        return None
    if module == "picklist_names":
        names = sorted({r[0] for r in rows})
        members = [{"name": n} for n in names]
    else:
        want = None
        for part in query.split("&"):
            k, _, v = part.partition("=")
            if unquote_plus(k) == "listName.name":
                want = unquote_plus(v)
        import uuid as _uuid
        members = []
        for ln, val, iri in rows:
            if want is not None and ln != want:
                continue
            # The shipped baseline blanks item IRIs (they are per install); a
            # box always has one, so stand in a stable one for the shape.
            if not iri:
                iri = "/api/3/picklists/" + str(
                    _uuid.uuid5(_uuid.NAMESPACE_URL, f"picklist/{ln}/{val}"))
            members.append({"@id": iri, "uuid": iri.rsplit("/", 1)[-1],
                            "itemValue": val, "listName": {"name": ln}})
    return {"hydra:member": members, "hydra:totalItems": len(members)}


def _personas_as_module_rows(personas: dict[str, dict]) -> list[dict]:
    """The bundle's captured Key Store personas, re-expressed as
    `assistant_personas` module records.

    Uses the connector's OWN serializer (`profile_to_module_record`) rather than
    re-deriving the column names here: the round trip
    `parse_persona_module_record(profile_to_module_record(p)) == p` is a property
    the connector's tests already hold, so a persona served through the module
    path resolves to the same `Profile` the Key Store path resolves to. Anything
    less and a module-path failure could be the fixture's spelling rather than
    the code's behavior -- and a second hand-copied column list is exactly the
    parallel-name drift that bites during a rename.

    Requires the connector package importable (the harness bootstrap does that);
    a failure here is a build-time error, never a turn outcome.
    """
    try:
        from fsr_soc_triage.profiles import (  # noqa: PLC0415
            parse_profile_record, profile_to_module_record)
    except Exception as exc:  # noqa: BLE001
        raise FixtureBoxError(
            "persona_source=module needs the connector package importable "
            f"(fsr_soc_triage.profiles): {exc}") from exc

    rows: list[dict] = []
    for key, rec in (personas or {}).items():
        module = str(rec.get("key") or key).split(":", 1)[-1]
        prof = parse_profile_record(rec, module)
        if prof is None:
            raise FixtureBoxError(
                f"bundle persona {key!r} does not parse as a Profile; a fixture "
                "that silently drops a persona serves the wrong assistant")
        row = profile_to_module_record(prof)
        row.setdefault("uuid", f"persona-{module}")
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# The box
# --------------------------------------------------------------------------
class _PlaybooksAPI:
    """The slice of pyfsr's typed `client.playbooks` that discovery uses.

    Without it, `list_module_playbooks` returns `no_playbooks_api` and the
    whole "what can I run here / run it" arc is unreachable offline. That is
    not a hypothetical: the first offline drive of "run the pending steps"
    ended with the agent unable to discover a playbook name and (correctly)
    refusing to invent one -- a harness gap wearing the costume of a model
    result.

    `find` honors `active` and `trigger_type` because discovery depends on
    both: a deactivated playbook is absent from the real Execute menu, and
    offering one is an offer that cannot be honored.
    """

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def find(self, *, trigger_type: Optional[str] = None,
             relationships: bool = False, limit: int = 100,
             active: Optional[bool] = None, **_kw: Any) -> list[dict]:
        rows = self._rows
        if active is not None:
            rows = [r for r in rows if bool(r.get("isActive")) is bool(active)]
        needle = (_kw.get("name_contains") or "").lower()
        if needle:
            # pyfsr: `name$like %x%`, case-insensitive. Ignored before, so a
            # name search returned every playbook in the bundle.
            rows = [r for r in rows if needle in str(r.get("name") or "").lower()]
        if trigger_type and trigger_type != "manual":
            # Only the manual/record-action surface is captured. Say so rather
            # than returning the manual rows under another trigger's name.
            return []
        return [dict(r) for r in rows[:max(1, int(limit))]]

    def list(self, *, name: Optional[str] = None, collection: Optional[str] = None,
             limit: int = 50, **_kw: Any) -> list[dict]:
        """pyfsr's `playbooks.list`: `name` is an exact match, as on the box.
        Without it `list_playbook_runs` died "'_PlaybooksAPI' object has no
        attribute 'list'" -- reported as a tool bug, a gap in this shim."""
        rows = self._rows
        if name is not None:
            rows = [r for r in rows if r.get("name") == name]
        if collection is not None:
            want = collection.rstrip("/").rsplit("/", 1)[-1]
            rows = [r for r in rows
                    if str(r.get("collection") or "").rstrip("/").rsplit("/", 1)[-1] == want]
        return [dict(r) for r in rows[:max(1, int(limit))]]


def _op_matches(entry: dict, connector: str, op: str, params: dict) -> bool:
    """Connector names match as globs (`fortinet-fortisiem*` covers v1 and v2,
    which the SOC helpers pick between at runtime); `match` values compare as
    case-insensitive strings, so "80" and 80 are the same recorded call."""
    from fnmatch import fnmatch
    if not fnmatch(connector or "", entry["connector"]) or entry["op"] != op:
        return False
    for k, want in (entry.get("match") or {}).items():
        if str(params.get(k, "")).strip().lower() != str(want).strip().lower():
            return False
    return True


class FixtureBox:
    """Answers the read surface from an in-memory record table.

    `reads` records every request; `misses` records the ones this box could not
    answer -- same contract as the substring cassette, so the sweep's unserved
    work list covers both backends.
    """

    def __init__(self, bundle: dict):
        self.modules: dict[str, list[dict]] = {
            m: list(rows) for m, rows in (bundle.get("modules") or {}).items()}
        self.personas: dict[str, dict] = bundle.get("personas") or {}
        # A time-aware world serves FortiSIEM pub/v2 event results only for the
        # timeRange the query was submitted with, the way the real engine does.
        # Off by default: the older rows carry fixed timestamps and never
        # depended on the window.
        self.time_aware = bool(bundle.get("time_aware"))
        self._siem_range: Optional[tuple[float, float]] = None
        # Key Store records that are not personas (e.g. `fsr_autonomy_policy`).
        # Served by `keys_body` whatever the persona substrate, so a world can
        # fix the admin's policy the same way it fixes the evidence.
        self.extra_keys: list[dict] = [dict(k) for k in bundle.get("keys") or []]
        self.playbooks = _PlaybooksAPI(bundle.get("playbooks") or [])
        # The same playbooks as the `workflows` module, because `run_playbook`'s
        # pre-card check reads `/api/3/workflows?name=` for identity + isActive.
        # Unserved, that check failed open, so offline rows never exercised the
        # refusal of an invented or deactivated playbook (N5).
        if bundle.get("playbooks") and "workflows" not in self.modules:
            self.modules["workflows"] = [
                {"@id": f"/api/3/workflows/{r.get('uuid')}", **r}
                for r in bundle["playbooks"] if isinstance(r, dict)]
        self.reads: list[str] = []
        self.misses: list[str] = []
        self.writable: bool = bool(bundle.get("writable"))
        self.writes: list[dict] = []

        # The CONNECTOR world: recorded responses for other connectors' ops,
        # which the agent reaches through `run_op` (enrichment, SIEM, EDR). The
        # record table cannot answer those, and the default offline answer is
        # a canned "malicious, confidence 85" for EVERY indicator -- so no
        # offline row could ever ask "is this benign?" and get a true answer.
        # A bundle that declares `ops` supplies the evidence instead, which is
        # what lets a scenario carry a KNOWN correct verdict.
        #   ops: [{"connector": "virustotal" | "fortinet-fortisiem*",
        #          "op": "query_ip", "match": {"ip": "203.0.113.9"},
        #          "response": {...}}]           first match wins
        #   strict_ops: an unrecorded op is a loud miss, not the canned answer
        #   enrichment_actions / configured_connectors: override the canned
        #   discovery answers so the world is internally consistent
        self.ops: list[dict] = [o for o in (bundle.get("ops") or [])
                                if isinstance(o, dict)]
        for o in self.ops:
            if not o.get("connector") or not o.get("op") or "response" not in o:
                raise FixtureBoxError(
                    f"ops entry needs connector, op and response: {o!r}")
        self.strict_ops: bool = bool(bundle.get("strict_ops"))
        self.enrichment_actions: Optional[dict] = bundle.get("enrichment_actions")
        self.configured_connectors: Optional[list] = bundle.get("configured_connectors")
        self.op_calls: list[str] = []

        # The MCP world: native-MCP servers and their tools, with recorded
        # answers. Served to the connector as the platform's MCP Configurations
        # plus a gateway client, so a turn lists and calls `mcp_<server>__<tool>`
        # exactly as on a box. Without it no offline row could reach an MCP
        # tool, and no eval covered an investigation made through one.
        #   mcp_servers: {"fortisiem": {"tools": [
        #       {"name": "get_reputation_by_entity", "description": "...",
        #        "input_schema": {...},
        #        "responses": [{"mentions": "198.51.100.77",   # anywhere in the args
        #                       "match": {"flat_key": "v"},      # optional, exact
        #                       "response": {...}}]}]}}   first match wins
        # A call with no matching response is an honest miss, never a guess.
        self.mcp_servers: dict[str, list[dict]] = {}
        for server, spec in (bundle.get("mcp_servers") or {}).items():
            tools = (spec or {}).get("tools")
            if not isinstance(tools, list) or not all(
                    isinstance(t, dict) and t.get("name") for t in tools):
                raise FixtureBoxError(
                    f"mcp_servers[{server!r}].tools must be a list of named tools")
            self.mcp_servers[str(server)] = tools
        self.mcp_calls: list[str] = []

        # Recorded raw HTTP for the surfaces no record table can answer --
        # playbook RUN history and run envs (`/api/wf/api/...`), which the
        # failure-diagnosis tools read through pyfsr's own client. Captured
        # from a real box by `scripts/record_runs.py`, replayed through a
        # transport adapter so pyfsr's code runs unchanged.
        #   http: [{"method": "GET", "path": "/api/wf/api/workflows/",
        #           "query": {"template_iri": "..."}, "status": 200,
        #           "body": {...}}]          first match wins
        self.http: list[dict] = [h for h in (bundle.get("http") or [])
                                 if isinstance(h, dict)]
        self.http_misses: list[str] = []

        # Which persona substrate this box presents. The 8.0 native path reads
        # the `assistant_personas` MODULE first and only then falls back to the
        # Key Store; a box that serves `keys` alone leaves the native path
        # unexercised -- which is exactly how it shipped, unexercised by 100% of
        # offline rows. `both` is the real 8.0 box (module wins); `module`
        # proves the native path resolves on its own, with no fallback to hide
        # behind. `keys` (the default) keeps every existing row on the legacy
        # path it was written against.
        self.persona_source: str = str(
            bundle.get("persona_source") or "keys").lower()
        if self.persona_source not in ("keys", "module", "both"):
            raise FixtureBoxError(
                "persona_source must be keys|module|both, not "
                f"{self.persona_source!r}")
        if self.persona_source in ("module", "both"):
            self.modules.setdefault(
                PERSONA_MODULE, _personas_as_module_rows(self.personas))

        # Learned house rules. `load_skills` reads `/api/3/assistant_skills`
        # unfiltered and fails open on anything but a 200, so an unserved module
        # and a feature that did nothing are indistinguishable from the turn's
        # side. Only an opted-in bundle serves it, so an existing row's behavior
        # is unchanged.
        skills = bundle.get("skills")
        if skills is not None:
            if not isinstance(skills, list):
                raise FixtureBoxError("bundle `skills` must be a list of records")
            self.modules.setdefault(SKILL_MODULE, [dict(s) for s in skills])

    # -- helpers ---------------------------------------------------------
    def keys_body(self, key: Optional[str] = None) -> dict:
        """`/api/3/keys` as the persona resolver expects it.

        The `key=` filter is HONORED, and that is not a detail. The resolver's
        direct lookup is `?key=fsr_assistant_profile:<module>&$limit=1`; a
        fixture that ignores the filter and returns the whole key list hands
        back whichever persona happens to sort first, and the turn then runs
        as the wrong assistant with the wrong tool slice. Observed exactly
        that: a `ztpf_devices` turn came back as the ZTPF Template Author.

        With no filter the full list is served, which is what the
        `bind_modules` scan fallback reads.
        """
        if self.persona_source == "module":
            # The native path is the ONLY substrate here. An empty persona key
            # list is the honest answer for a box with no legacy keys, and it is
            # what makes a module-path failure show up as a failure instead of
            # being papered over by the Key Store fallback.
            members = []
        else:
            members = [dict(p) for p in self.personas.values()]
        members += [dict(k) for k in self.extra_keys]
        if key:
            members = [m for m in members if m.get("key") == key]
        return {"hydra:member": members, "hydra:totalItems": len(members)}

    def record(self, module: str, uuid: str) -> Optional[dict]:
        for r in self.modules.get(module, []):
            if r.get("uuid") == uuid or str(r.get("id")) == str(uuid):
                return r
        return None

    # -- sandbox writes (`writable: true` bundles only) ----------------------
    _IRI_RE = re.compile(r"^/api/3/([a-zA-Z0-9_]+)/([0-9a-fA-F-]{36})$")

    def _link(self, v: Any) -> Any:
        """A relation IRI as a captured row carries it: a stub with the
        target's @id/uuid and label, so `ztpfAutomationProfiles.uuid` filters
        match a written row exactly as they match a captured one. An IRI that
        names nothing in the box stays a bare stub -- `dangling` in
        `world_links` -- rather than being quietly accepted."""
        if isinstance(v, list):
            return [self._link(x) for x in v]
        if not isinstance(v, str):
            return v
        m = self._IRI_RE.match(v.strip())
        if not m:
            return v
        target = self.record(m.group(1), m.group(2))
        stub: dict[str, Any] = {"@id": v.strip(), "uuid": m.group(2)}
        if target is None and m.group(1) == "picklists":
            return stub
        for k in ("name", "itemValue", "stepSequence"):
            if target is not None and k in target:
                stub[k] = target[k]
        return stub

    def write(self, method: str, url: str,
              body: Optional[dict]) -> tuple[int, dict]:
        """Apply an approved POST (create) / PUT (update) to the table."""
        entry = {"method": method.upper(), "url": url, "body": body}
        path = url.split("?", 1)[0]
        m = re.search(r"/api/3/([a-zA-Z0-9_]+)(?:/([^/]+))?/?$", path)
        if not self.writable or not m or not isinstance(body, dict):
            self.misses.append(f"{method.upper()} {url}")
            return 599, {"message": "fixture box does not serve this write"}
        module, ident = m.group(1), m.group(2)
        rows = self.modules.get(module)
        if rows is None:
            self.misses.append(f"{method.upper()} {url}")
            return 404, {"message": f"no module {module}"}
        fields = {k: self._link(v) for k, v in body.items()}
        if method.upper() == "POST" and not ident:
            uid = str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                                  f"fixture-write:{module}:{len(self.writes)}"))
            nid = 1 + max([int(r.get("id") or 0) for r in rows] or [0])
            rec = {"@id": f"/api/3/{module}/{uid}", "uuid": uid, "id": nid,
                   **fields}
            rows.append(rec)
        elif method.upper() == "PUT" and ident:
            rec = self.record(module, ident)
            if rec is None:
                return 404, {"message": f"no {module} record {ident}"}
            rec.update(fields)
        else:
            self.misses.append(f"{method.upper()} {url}")
            return 405, {"message": f"{method} {url} is not a record write"}
        entry["iri"] = rec["@id"]
        self.writes.append(entry)
        return 200, _public(rec)

    def _collection(self, module: str, *, body: Optional[dict] = None,
                    q: str = "", limit: int = 30, page: int = 1,
                    sort: Any = None) -> Optional[dict]:
        rows = self.modules.get(module)
        if rows is None:
            return None
        out = [r for r in rows if (not body or _match_body(r, body))
               and (not q or _search_text(r, q))]
        out = _sort(out, sort)
        total = len(out)
        # `$page` is 1-based, as on the box. Ignoring it served page 1 for
        # every page, and the model reported the box's paging as broken.
        limit = max(1, int(limit))
        start = (max(1, int(page)) - 1) * limit
        return {"hydra:member": [_public(r) for r in out[start:start + limit]],
                "hydra:totalItems": total}

    # -- the HTTP-ish surface -------------------------------------------
    @property
    def has_world(self) -> bool:
        return bool(self.ops or self.strict_ops or self.enrichment_actions is not None
                    or self.configured_connectors is not None or self.mcp_servers)

    def mcp_list(self, server: str) -> list[dict]:
        """The tool list `server` advertises (name, description, input_schema)."""
        return [{"name": t["name"],
                 "description": t.get("description") or "",
                 "input_schema": t.get("input_schema")
                 or {"type": "object", "properties": {}}}
                for t in self.mcp_servers.get(server, [])]

    def mcp_call(self, server: str, tool: str, args: Optional[dict]) -> Any:
        """The recorded answer for one MCP tool call, in the gateway's
        `{status, result, error}` envelope; an unrecorded call says so."""
        args = args or {}
        self.mcp_calls.append(f"{server}.{tool}")
        flat = json.dumps(args, sort_keys=True, default=str).lower()
        for t in self.mcp_servers.get(server, []):
            if t["name"] != tool:
                continue
            for r in t.get("responses") or []:
                want = r.get("match") or {}
                if not all(str(args.get(k, "")).strip().lower() == str(v).strip().lower()
                           for k, v in want.items()):
                    continue
                # `mentions`: the call names this value anywhere in its args.
                # Real MCP tools nest their input (`{"params": {"ip": [...]}}`),
                # which a flat key match cannot express.
                m = r.get("mentions")
                if m and str(m).lower() not in flat:
                    continue
                return r.get("response")
        self.misses.append(f"MCP {server}.{tool} {json.dumps(args, sort_keys=True)}")
        return {"status": "failed", "result": None,
                "error": f"{tool}: no data for these arguments"}

    def op(self, connector: str, op: str, params: Optional[dict]) -> Optional[Any]:
        """The recorded response for this run_op call, or None.

        In a time-aware world the SIEM event query remembers the timeRange it was
        submitted with, and its results keep only the recorded events inside that
        range. A lookback that stops short of the events comes back empty, as it
        does on a box."""
        params = params or {}
        self.op_calls.append(f"{connector}.{op}")
        endpoint = str(params.get("endpoint") or "")
        if self.time_aware and op == "execute_api_request":
            if endpoint.endswith("/eventQuery"):
                tr = (params.get("payload") or {}).get("timeRange") or {}
                self._siem_range = (float(tr.get("from") or 0), float(tr.get("to") or 0))
        for entry in self.ops:
            if _op_matches(entry, connector, op, params):
                resp = entry["response"]
                if (self.time_aware and self._siem_range is not None
                        and endpoint.endswith("/events/results")
                        and isinstance(resp, dict) and isinstance(resp.get("data"), list)):
                    lo, hi = self._siem_range
                    resp = {**resp, "data": [r for r in resp["data"]
                                             if lo <= _epoch_of(r.get("phRecvTime")) <= hi]}
                return resp
        return None

    def records_path(self, url: str) -> bool:
        """Whether the recorded HTTP covers this URL's path at all -- so a
        caller can route to `replay` without logging a miss for every
        ordinary record read."""
        from urllib.parse import urlparse
        path = urlparse(url).path
        return any(h.get("path") == path for h in self.http)

    def replay(self, method: str, url: str) -> Optional[tuple[int, Any]]:
        """The recorded response for a raw request, or None (and a logged
        miss). Matches method + path exactly and every RECORDED query param;
        params the recording did not carry (cache-busters, ordering added by
        a newer client) do not prevent a match."""
        from urllib.parse import parse_qsl, urlparse
        u = urlparse(url)
        got = dict(parse_qsl(u.query, keep_blank_values=True))
        for h in self.http:
            if (h.get("method") or "GET").upper() != method.upper():
                continue
            if h.get("path") != u.path:
                continue
            want = h.get("query") or {}
            if all(str(got.get(k)) == str(v) for k, v in want.items()):
                return int(h.get("status") or 200), copy.deepcopy(h.get("body"))
        declared = self._declared_inventory(method, u.path, got)
        if declared is not None:
            return 200, declared
        self.http_misses.append(f"{method.upper()} {u.path}?{u.query}")
        return None

    def _declared_inventory(self, method: str, path: str,
                            query: dict) -> Optional[dict]:
        """A world that declares `configured_connectors` answers the raw reads
        that ask what is configured, from that list. Since the replay client made
        a world's client look live, the action-card preflight reads
        `connector_details?configured=true` through it; unanswered, every block
        card was refused as "not configured" on a world that configures the
        FortiGate (the policy rows went from 2/2 to 0/2)."""
        if self.configured_connectors is None:
            return None
        if (method.upper() == "POST" and path == "/api/integration/connector_details/"
                and str(query.get("configured")).lower() == "true"):
            return {"data": [
                {"name": c["name"], "version": c.get("version"),
                 "label": c.get("label"), "status": c.get("status", "Completed"),
                 "active": True,
                 "configuration": [{"config_id": f"cfg-{c['name']}",
                                    "name": "default", "default": True}]}
                for c in self.configured_connectors
                if isinstance(c, dict) and c.get("name")]}
        if method.upper() == "GET" and path == "/api/3/agents":
            return {"hydra:member": [], "hydra:totalItems": 0}
        return None

    def get(self, url: str) -> tuple[int, dict]:
        self.reads.append(url)
        path = url.split("?", 1)[0]
        query = url.split("?", 1)[1] if "?" in url else ""

        if "/api/3/keys" in path:
            m = re.search(r"(?:^|[?&])key=([^&]+)", query)
            return 200, self.keys_body(unquote(m.group(1)) if m else None)

        m = re.search(r"/api/3/([a-zA-Z0-9_]+)(?:/([^/]+))?/?$", path)
        if not m:
            self.misses.append(url)
            return 404, {"hydra:member": [], "hydra:totalItems": 0}
        module, ident = m.group(1), m.group(2)

        if ident:
            rec = self.record(module, ident)
            if rec is None:
                self.misses.append(url)
                return 404, {"message": f"no {module} record {ident}"}
            return 200, _public(rec)

        if module in _PICKLIST_MODULES and module not in self.modules:
            served = _reference_picklists(module, query)
            if served is not None:
                return 200, served
        if module not in self.modules:
            self.misses.append(url)
            return 404, {"hydra:member": [], "hydra:totalItems": 0}
        qs = {unquote_plus(k): v for k, v in
              (p.split("=", 1) for p in query.split("&") if "=" in p)}
        conds = _qs_conditions(query)
        body = self._collection(module, q=unquote_plus(qs.get("$search", "")),
                                limit=int(qs.get("$limit") or 30),
                                page=int(qs.get("$page") or 1),
                                body={"filters": conds} if conds else None)
        return 200, (body or {"hydra:member": [], "hydra:totalItems": 0})

    def post(self, url: str, json_body: Optional[dict] = None) -> tuple[int, dict]:
        self.reads.append(f"POST {url}")
        path = url.split("?", 1)[0]
        qs = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&")
                  if "=" in p) if "?" in url else {}
        m = re.search(r"/api/query/([a-zA-Z0-9_]+)", path)
        if not m:
            # A POST that is not a query is a WRITE. Refusing it loudly is the
            # point: a write has to stop at the approval gate, and a box that
            # accepted one would hide the gate not firing.
            self.misses.append(f"POST {url}")
            return 599, {"message": "fixture box serves reads only; this POST "
                                    "looks like a write and was not served"}
        module = m.group(1)
        if module not in self.modules:
            self.misses.append(f"POST {url}")
            return 404, {"hydra:member": [], "hydra:totalItems": 0}
        body = json_body or {}
        return 200, (self._collection(
            module, body=body, limit=int(qs.get("$limit") or 30),
            page=int(qs.get("$page") or 1), sort=body.get("sort")) or {"hydra:member": [], "hydra:totalItems": 0})
