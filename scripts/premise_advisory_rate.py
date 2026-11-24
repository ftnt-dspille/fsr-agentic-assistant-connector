#!/usr/bin/env python3
"""How often the verdict card shows the records advisory (Phase 2 step 3a).

    make premise-advisory-rate BOX=<alias> [LIMIT=60]
    make premise-advisory-rate STORE=1 BOX=<alias>     # the exported local store

The advisory is the card line "a change record for this host exists that this
investigation did not read" (or "the asset record conflicts with this verdict").
The framework writes what the card shows as `premise.advisory` on the stored
verdict card, so the live rate is read straight from the stored turns: of the
turns that ended in a verdict card, how many carried an advisory, which kind
and why (unread or conflict). Nothing here changes a decision; it is a count.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from fsr_playbooks.harness.frames import frames_of

sys.path.insert(0, str(Path(__file__).resolve().parent))


def advisory_of(card: dict) -> list[dict]:
    """The advisory entries the card showed (kind + state), nothing else."""
    adv = (card.get("premise") or {}).get("advisory")
    return [a for a in adv if isinstance(a, dict) and a.get("kind") and a.get("state")] \
        if isinstance(adv, list) else []


def collect(histories: Iterable[tuple[str, dict]]) -> dict:
    """Per turn that ended in a verdict card: was an advisory shown. The last
    card of a turn is the one the analyst is left with."""
    turns = []
    for sid, hist in histories:
        for n, entry in enumerate((hist or {}).get("turns") or []):
            cards = frames_of(entry or {}, "verdict_card")
            if not cards:
                continue
            card = cards[-1]
            turns.append({"session": sid, "turn": n,
                          "disposition": card.get("disposition"),
                          "status": (card.get("premise") or {}).get("status"),
                          "advisory": advisory_of(card)})
    shown = [t for t in turns if t["advisory"]]
    by = Counter(f'{a["kind"]}/{a["state"]}' for t in shown for a in t["advisory"])
    return {"verdict_turns": len(turns), "shown": len(shown),
            "rate": round(len(shown) / len(turns), 3) if turns else None,
            "by_kind_state": dict(sorted(by.items())),
            "by_disposition": dict(sorted(Counter(
                str(t["disposition"]) for t in shown).items())),
            "turns": turns}


def render(res: dict) -> str:
    out = [f'verdict turns: {res["verdict_turns"]}, advisory shown: {res["shown"]}'
           + (f' ({res["rate"] * 100:.1f}% per verdict turn)' if res["rate"] is not None else "")]
    for k, v in res["by_kind_state"].items():
        out.append(f"  {k}: {v}")
    for k, v in res["by_disposition"].items():
        out.append(f"  on a {k} verdict: {v}")
    for t in res["turns"]:
        if t["advisory"]:
            out.append("  session {session} turn {turn}: {adv}".format(
                adv=", ".join(f'{a["kind"]}/{a["state"]}' for a in t["advisory"]), **t))
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    from session_analyze import STORE_PREFIX, box_op
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", help="path to a box .env")
    ap.add_argument("--store", metavar="ALIAS", help="the exported local store instead of a box")
    ap.add_argument("--limit", type=int, default=60, help="sessions to read (newest first)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    op = box_op((STORE_PREFIX + a.store) if a.store else a.env)
    ids = [s.get("session_id") for s in (op("list_sessions", {"limit": a.limit})
                                         .get("sessions") or [])]
    hists = [(sid, op("chat_history", {"session_id": sid, "limit": 500}))
             for sid in ids if sid]
    res = collect(hists)
    print(json.dumps(res, indent=1) if a.json else render(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
