#!/usr/bin/env python3
"""Plant, find and clean up "needles" in an external source the agent should find.

A needle is a record carrying a per-run random token. The live grader
(`live_rows`, expectation `planted_token`) then asks whether that token reached
the agent's answer THROUGH A TOOL RESULT, which is the only way a model can know
it. This is the first planter: ServiceNow, through the box's own connector
config (`client.connectors.execute`, the route the agent's `run_op` takes).
It writes only to the lab ServiceNow instance, never to FortiSOAR.

    make plant BOX=<alias> VERB=health
    make plant BOX=<alias> VERB=plant   SCENARIO=snow_incident_needle CONFIG=<config name>
    make plant BOX=<alias> VERB=find    TOKEN=<token>      (omit TOKEN: every planted token)
    make plant BOX=<alias> VERB=cleanup

Verbs:
  health   read-only: every ServiceNow config and its health.
  plant    create each item of the scenario file, record ids in the state file.
  find     search the source for the token(s) through the connector (open
           records only: `active=true`).
  cleanup  remove exactly what the state file lists, then confirm find is empty.

Cleanup: the connector exposes a delete only for the security-incident table
(`delete_SIR`, hard-wired to it). Any other table is CLOSED instead
(`update_servicenow_incident`), after which the active-only find no longer sees
it.

State file (default scratch/planted_state.json, gitignored): one entry per
planted item, so cleanup is exact and the live grader can read the tokens.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

SCENARIO_DIR = _HERE / "planted"
DEFAULT_STATE = _REPO / "scratch" / "planted_state.json"
CONNECTOR = "servicenow"
SIR_TABLE = "sn_si_incident"
CLOSED_STATE = "7"
TEST_LABEL = "NEEDLE-TEST"
CLOSE_CODES = ("Resolved by caller", "Solved (Permanently)", "Solved (Work Around)")


class PlanterError(RuntimeError):
    pass


# ---------------------------------------------------------------- scenario ---
def load_scenario(name_or_path: str) -> dict:
    p = Path(name_or_path)
    if not p.exists():
        p = SCENARIO_DIR / f"{name_or_path}.json"
    if not p.exists():
        raise PlanterError(f"no scenario {name_or_path!r} (looked in {SCENARIO_DIR})")
    spec = json.loads(p.read_text())
    items = spec.get("items") or []
    if not items or not any(i.get("role") == "needle" for i in items):
        raise PlanterError(f"{p.name}: needs items, at least one with role 'needle'")
    labels = [i.get("label") for i in items]
    if len(set(labels)) != len(labels) or not all(labels):
        raise PlanterError(f"{p.name}: item labels must be unique and non-empty")
    for i in items:
        if TEST_LABEL not in str(i.get("short_description", "")):
            raise PlanterError(f"{p.name}: item {i['label']!r} must carry {TEST_LABEL} "
                               f"in short_description (a planted record is clearly a test)")
        if "{token}" not in str(i.get("short_description", "")):
            raise PlanterError(f"{p.name}: item {i['label']!r} has no {{token}} to find it by")
    return spec


def new_token() -> str:
    return "NDL-" + secrets.token_hex(6)


# ------------------------------------------------------------------- state ---
def load_state(path: Path | str = DEFAULT_STATE) -> dict:
    p = Path(path)
    if not p.exists():
        return {"items": []}
    return json.loads(p.read_text())


def save_state(state: dict, path: Path | str = DEFAULT_STATE) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1))


def tokens_for(state: dict, scenario: str) -> dict[str, str]:
    """{label: token} of the newest uncleaned plant of `scenario`, for the live
    grader. Empty when nothing is planted."""
    out: dict[str, str] = {}
    for it in state.get("items", []):
        if it.get("scenario") == scenario and not it.get("cleaned"):
            out[it["label"]] = it["token"]
    return out


# --------------------------------------------------------------- connector ---
def _run(client: Any, op: str, params: dict, config: str) -> Any:
    res = client.connectors.execute(CONNECTOR, op, config=config, params=params)
    if not getattr(res, "ok", False):
        raise PlanterError(f"{op} failed: {str(getattr(res, 'message', '') or res)[:300]}")
    return res.data


def sys_ids_in(data: Any) -> list[str]:
    """Every `sys_id` in a connector result, in order, de-duplicated. The
    connector wraps the ServiceNow body differently per operation, so walk it."""
    found: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            v = o.get("sys_id")
            if isinstance(v, str) and v and v not in found:
                found.append(v)
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(data)
    return found


def _records_with(data: Any, token: str) -> list[str]:
    """sys_ids of the records in `data` whose own fields mention `token`."""
    out: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            sid = o.get("sys_id")
            if isinstance(sid, str) and sid and token in json.dumps(o, default=str) \
                    and sid not in out:
                out.append(sid)
            for x in o.values():
                if isinstance(x, (dict, list)):
                    walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(data)
    return out


def health(client: Any) -> list[dict]:
    """Read-only: [{config, default, status, message}] for every ServiceNow config."""
    out = []
    for cf in client.connectors.configurations(CONNECTOR):
        try:
            h = client.connectors.healthcheck(CONNECTOR, config=cf.config_id)
            status, msg = h.status, str(h.message or "")
        except Exception as e:  # noqa: BLE001 -- a failed check is a finding
            status, msg = "error", f"{type(e).__name__}: {e}"
        out.append({"config": cf.name, "default": bool(cf.default),
                    "status": status, "message": msg[:200]})
    return out


def plant(client: Any, scenario: dict, config: str, state: dict) -> list[dict]:
    """Create every item; record each in `state` right after it exists so a
    failure halfway still leaves cleanup an exact list."""
    table = scenario.get("table", "incident")
    made = []
    for item in scenario["items"]:
        token = new_token()
        value = {k: str(v).replace("{token}", token) for k, v in item.items()
                 if k in ("short_description", "description", "category", "urgency",
                          "impact", "severity")}
        data = _run(client, "create_new_record", {"table_name": table, "value": value}, config)
        ids = sys_ids_in(data)
        if not ids:
            raise PlanterError(f"create_new_record for {item['label']!r} returned no sys_id; "
                               f"cannot track it for cleanup")
        entry = {"scenario": scenario["name"], "label": item["label"], "role": item["role"],
                 "token": token, "table": table, "sys_id": ids[0], "config": config,
                 "cleaned": False}
        state["items"].append(entry)
        made.append(entry)
        save_state_hook(state)
    return made


# Overridden by the CLI so every plant is persisted as it happens; a no-op in tests.
def save_state_hook(state: dict) -> None:  # noqa: D401
    path = state.get("_path")
    if path:
        save_state({k: v for k, v in state.items() if k != "_path"}, path)


def find(client: Any, token: str, config: str, table: str = "incident",
         sys_id: str | None = None) -> list[str]:
    """sys_ids of OPEN records whose own fields mention `token`.

    Live-verified on connector 3.6.x: `advance_search` honours exactly one plain
    `field=value` condition. Anything else (`LIKE`, `^`, STARTSWITH) is silently
    dropped and the call returns the table's default first page, so a LIKE query
    cannot find a needle. When the planter knows the record's `sys_id` it asks
    for that (`sys_id=<id>`); without one it falls back to the LIKE query, whose
    unrelated rows the token filter discards (so it can miss, never lie)."""
    query = f"sys_id={sys_id}" if sys_id else f"active=true^short_descriptionLIKE{token}"
    data = _run(client, "advance_search",
                {"table_name": table, "adv_query": query, "sysparm_limit": 20}, config)
    return [i for i in _records_with(data, token) if _is_open(data, i)]


def _is_open(data: Any, sys_id: str) -> bool:
    """False only when the record in `data` says it is inactive (the connector
    returns closed records for a `sys_id=` query)."""
    def walk(o: Any) -> bool | None:
        if isinstance(o, dict):
            if o.get("sys_id") == sys_id and "active" in o:
                return str(o["active"]).lower() != "false"
            for x in o.values():
                if isinstance(x, (dict, list)):
                    r = walk(x)
                    if r is not None:
                        return r
        elif isinstance(o, list):
            for x in o:
                r = walk(x)
                if r is not None:
                    return r
        return None
    return walk(data) is not False


def _close(client: Any, it: dict) -> None:
    """Close an incident. The resolution code is a choice list that differs per
    instance (a wrong value fails the data policy as a missing Resolution code),
    so try the known ASCII values in turn."""
    err = None
    for code in CLOSE_CODES:
        try:
            _run(client, "update_servicenow_incident",
                 {"sys_id": it["sys_id"], "state": CLOSED_STATE,
                  "work_notes": f"{TEST_LABEL}: planted test record closed by cleanup",
                  "other_fields": {"close_code": code,
                                   "close_notes": f"{TEST_LABEL} cleanup"}}, it["config"])
            return
        except PlanterError as e:
            err = e
            if "esolution code" not in str(e) and "close_code" not in str(e):
                raise
    raise err  # type: ignore[misc]


def cleanup(client: Any, state: dict) -> list[dict]:
    """Remove exactly the uncleaned items in `state`; mark each cleaned only
    after the source stops returning it."""
    done = []
    for it in state.get("items", []):
        if it.get("cleaned"):
            continue
        if it["table"] == SIR_TABLE:
            _run(client, "delete_SIR", {"sir_sys_id": it["sys_id"], "table_name": it["table"]},
                 it["config"])
        else:
            _close(client, it)
        if find(client, it["token"], it["config"], it["table"], it["sys_id"]):
            raise PlanterError(f"{it['label']}: still found after cleanup; left in the state file")
        it["cleaned"] = True
        done.append(it)
        save_state_hook(state)
    return done


# --------------------------------------------------------------------- CLI ---
def _client():
    # No conftest import: it scrubs FSR_* from the environment.
    import urllib3
    from pyfsr.config import EnvConfig
    urllib3.disable_warnings()
    return EnvConfig.from_env().client(timeout=120)


def main(argv: list[str] | None = None, client: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verb", choices=["health", "plant", "find", "cleanup"])
    ap.add_argument("--scenario", default="snow_incident_needle")
    ap.add_argument("--config", help="ServiceNow config name (required for plant; "
                                     "find/cleanup default to the state file's)")
    ap.add_argument("--token")
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    a = ap.parse_args(argv)
    client = client or _client()

    if a.verb == "health":
        for r in health(client):
            print(f"{r['config']:<24} default={r['default']!s:<5} {r['status']:<14} {r['message']}")
        return 0

    state = load_state(a.state)
    state["_path"] = a.state
    if a.verb == "plant":
        if not a.config:
            raise SystemExit("plant needs --config <ServiceNow config name> (check `health` first)")
        if tokens_for(state, a.scenario):
            raise SystemExit("this scenario is already planted; cleanup first")
        for e in plant(client, load_scenario(a.scenario), a.config, state):
            print(f"planted {e['label']:<22} token={e['token']} id=...{e['sys_id'][-4:]}")
        return 0
    if a.verb == "find":
        known = {i["token"]: i for i in state["items"]}
        targets = ([(a.token, a.config or known.get(a.token, {}).get("config", ""),
                     known.get(a.token, {}).get("table", "incident"),
                     known.get(a.token, {}).get("sys_id"))] if a.token else
                   [(i["token"], i["config"], i["table"], i["sys_id"]) for i in state["items"]
                    if not i.get("cleaned")])
        if not targets:
            print("nothing planted")
            return 0
        total = 0
        for tok, cfg, table, sid in targets:
            ids = find(client, tok, cfg or a.config, table, sid)
            total += len(ids)
            print(f"{tok}: {[i[-4:] for i in ids]}")
        return 0 if total else 1
    done = cleanup(client, state)
    print(f"cleaned {len(done)} item(s); find returns nothing for each")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
