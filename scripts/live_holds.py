"""live_holds -- the fast measurement target for the Phase 2 shadow hold.

    make live-holds BOX=<alias> [N=8]

Seeds N alerts in parallel (C2 destinations and benign destinations, half of
them with a planted asset row for the subject), turns auto-triage on for the
test source only under a policy that can never act, waits for every turn
concurrently, then prints the shadow hold result for just those turns (the
`premise_advisory_rate` collect/render) plus the per-alert table and wall time.

It leaves out everything live_autonomy spends its time on that the hold does
not need: block, undo, kill switch, rate cap, the serial rows. Cleanup is the
same (`cleanup_seeded`) and always runs, bounded. Nothing is created in an
external system: only alerts and assets on the box, both deleted at the end.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from box_lock import hold_box  # noqa: E402
from fsr_live import LiveFSR  # noqa: E402
from fsr_playbooks.harness.frames import frames_of  # noqa: E402
from live_autonomy import (BENIGN_DST, SOURCE, TURN_TIMEOUT, Box, RunOverrun,  # noqa: E402
                           _await_turn, _policy, _seed, _synthetic_c2, arm_budget,
                           cleanup_seeded, severity_iris)
from premise_advisory_rate import collect, render  # noqa: E402

RUN_BUDGET = 600       # seconds for the whole run; past it the run stops and cleans up
CLEANUP_BUDGET = 120
SETTLE = 20            # seconds a stored turn may take to show its verdict card
MAX_WORKERS = 16

#: The cycle the N alerts are drawn from: (kind, planted asset row for the subject).
#: Two C2 and two benign destinations, each with and without an asset, so a run of
#: 4 or more has every combination the hold should and should not fire on.
MIX = (("c2", False), ("benign", False), ("c2", True), ("benign", True))


def plan_seeds(n: int) -> list[dict]:
    """N seed specs: {kind, asset, src, dst, label}. Every source is distinct
    (documentation range) so no two alerts group under one storm lead."""
    if n < 1:
        raise ValueError("N must be at least 1")
    kinds = [MIX[i % len(MIX)] for i in range(n)]
    c2 = iter(_synthetic_c2(sum(1 for k, _a in kinds if k == "c2")))
    out = []
    for i, (kind, asset) in enumerate(kinds):
        if kind == "c2":
            src, dst = next(c2)
            label = "C2 Tracker Malware IP"
        else:
            src, dst = f"198.51.100.{200 + i}", BENIGN_DST
            label = "newly observed destination"
        out.append({"kind": kind, "asset": asset, "src": src, "dst": dst, "label": label})
    return out


def no_act_policy() -> dict:
    """A policy that can never act: shadow mode, kill switch off, and the
    benign address protected. The hold is read from the verdict card, which
    the policy does not gate."""
    return {**_policy([BENIGN_DST], enabled=False), "mode": "shadow"}


def _watch(box, sid: str, timeout: float, settle: float, sleep, now) -> dict:
    """One alert: wait for its turn, then for a verdict card (bounded)."""
    turned = _await_turn(box, sid, timeout, sleep=sleep, now=now)
    hist: dict = {}
    if turned:
        end = now() + settle
        while True:
            hist = box.op("chat_history", {"session_id": sid, "limit": 500}, timeout=60)
            if any(frames_of(t or {}, "verdict_card") for t in hist.get("turns") or []) \
                    or now() >= end:
                break
            sleep(5)
    return {"session_id": sid, "turned": turned, "history": hist}


def wait_all(box, sids: list[str], *, timeout: float = TURN_TIMEOUT, settle: float = SETTLE,
             sleep=time.sleep, now=time.time) -> dict[str, dict]:
    """Wait for every session's turn at once. Returns {session_id: {session_id,
    turned, history}}, one entry per session whatever happened to the others."""
    if not sids:
        return {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(sids))) as pool:
        futs = {sid: pool.submit(_watch, box, sid, timeout, settle, sleep, now) for sid in sids}
        out = {}
        for sid, f in futs.items():
            try:
                out[sid] = f.result()
            except Exception as e:  # noqa: BLE001 -- one alert's read error is that alert's result
                out[sid] = {"session_id": sid, "turned": False, "history": {}, "error": str(e)}
        return out


def table(specs: list[dict], results: dict[str, dict], sids: dict[str, str]) -> str:
    """One line per seeded alert: what was planted, what the turn ended in."""
    rep = collect([(r["session_id"], r["history"]) for r in results.values()])
    by_sid = {t["session"]: t for t in rep["turns"]}
    lines = [f"{'kind':7} {'asset':5} {'turn':5} {'verdict':22} {'hold':5} why"]
    for spec in specs:
        sid = sids.get(spec["src"], "")
        r, t = results.get(sid, {}), by_sid.get(sids.get(spec["src"], ""))
        if t:
            pre = t["preread"]
            verdict = f"{t['disposition']} {t['confidence']}"
            hold = "HOLD" if pre.get("hold") else ("no" if pre else "-")
            why = ", ".join(pre.get("why") or [])
        else:
            verdict, hold, why = "(no verdict card)", "-", r.get("error", "")
        lines.append(f"{spec['kind']:7} {'yes' if spec['asset'] else 'no':5} "
                     f"{'yes' if r.get('turned') else 'NO':5} {verdict:22} {hold:5} {why}")
    return "\n".join(lines)


def run(box, n: int = 8, *, wait=wait_all) -> int:
    t0 = time.time()
    specs = plan_seeds(n)
    prior = box.policy_record()
    prior_value = json.loads(prior["value"]) if prior and prior.get("value") else None
    seeded: list[str] = []
    assets: list[str] = []
    sids: dict[str, str] = {}
    results: dict[str, dict] = {}
    print(f"live holds -> {box.base}  connector={box.fsr.version}  config={box.fsr.config}  n={n}")
    arm_budget(RUN_BUDGET, "live holds")
    try:
        iris = severity_iris(box)
        if "High" not in iris:
            raise SystemExit(f"could not find the High severity IRI (have {sorted(iris)})")
        box.set_policy(no_act_policy())
        trig = box.op("set_auto_triage", {"agent_config": box.fsr.config, "module": "alerts",
                                          "severities": ["High", "Critical"],
                                          "sources": [SOURCE], "enabled": True})
        if not trig.get("ok"):
            raise SystemExit(f"set_auto_triage failed: {trig}")

        def seed(spec: dict) -> tuple[dict, str, str | None]:
            asset = None
            if spec["asset"]:
                r = box.s.post(f"{box.base}/api/3/assets", verify=box.verify, timeout=60, json={
                    "name": f"holds-{spec['src']}", "hostname": f"holds-{spec['src'].replace('.', '-')}",
                    "ip": spec["src"],
                    "description": "Seeded asset for the shadow hold measurement."})
                r.raise_for_status()
                asset = r.json()["uuid"]
                assets.append(asset)
            u = _seed(box, iris, spec["src"], spec["dst"], label=spec["label"])
            seeded.append(u)
            return spec, u, asset

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(specs))) as pool:
            for spec, u, _a in pool.map(seed, specs):
                sids[spec["src"]] = f"auto:alerts:{u}"
        print(f"  seeded {len(seeded)} alert(s), {len(assets)} asset(s) in {round(time.time() - t0)}s;"
              " waiting for turns ...")
        results = wait(box, list(sids.values()))
    finally:
        signal.alarm(CLEANUP_BUDGET)
        print("\n  cleanup:")
        cleanup_seeded(box, seeded, prior_value, assets)
        signal.alarm(0)

    res = collect([(r["session_id"], r["history"]) for r in results.values()])
    print("\n" + table(specs, results, sids) + "\n")
    print(render(res))
    turned = sum(1 for r in results.values() if r["turned"])
    print(f"\nturns {turned}/{len(specs)}, wall time {round(time.time() - t0)}s")
    return 0 if turned == len(specs) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=8, help="alerts to seed (default 8)")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    hold_box()
    return run(Box(LiveFSR.from_env()), args.n)


if __name__ == "__main__":
    sys.exit(main())
