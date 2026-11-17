#!/usr/bin/env python3
"""Live golden-utterance gate -- the on-box outcome layer of "tests like mine".

For each row in tests/golden_utterances.json it drives the REAL chat_turn against
a box (via local_turn.py --tools live, so the box's data and configured model),
simulating exactly how the widget sends it: the page-pinned `intent` + the
analyst's free-typed `message`. Then it asserts the OUTCOME the deterministic
routing tests cannot see:
  - resolved_intent (turn_inputs.intent)  -- the routing held live
  - card_in / card_not                    -- it authored (playbook_offer), did
                                             not dead-end on a verdict_card
  - no_unknown_connector                  -- no run_op spray at unconfigured connectors
  - language                              -- analyst-facing text did not drift

This is the gate that would have caught box 159 sess-dmrfucve. Run it before
`make stable`. It needs a box env (FSR_* creds) loaded via envrun -- the Makefile
target does that; do not shell-source the .env.

Exit 0 = every live expectation met; 1 = a failure; 2 = a drive/setup error.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
LOCAL_TURN = HERE / "local_turn.py"
DEFAULT_GOLDENS = (HERE.parent / "connector-fsr-agentic-assistant"
                   / "tests" / "golden_utterances.json")

# Unambiguous non-English markers -- the defect drifted a whole response to
# Spanish. Accented letters / inverted punctuation, plus a few function words
# that do not occur in English analyst text. Heuristic on purpose: it only has
# to catch a wholesale language switch, not score fluency.
_NON_EN = set("áéíóúñ¿¡")
_ES_WORDS = (" el ", " la ", " los ", " las ", " no se ", " está ", " conector ",
             " disponible ", " por lo ", " para ", " con ", " una ")


def _looks_english(text: str) -> bool:
    t = (text or "").lower()
    if not t.strip():
        return True  # no prose to judge (card-only answer) -- not a drift
    if any(ch in _NON_EN for ch in t):
        return False
    t = " " + t + " "  # pad so a boundary word matches at either end
    hits = sum(t.count(w) for w in _ES_WORDS)
    return hits < 2


def _drive(py: str, row: dict, llm: str, timeout: int) -> dict:
    # A build-from-spec turn can run 70+ tool calls; let a row raise its ceiling.
    timeout = int(row.get("timeout_s") or timeout)
    cmd = [py, str(LOCAL_TURN), "--json", "--tools", "live", "--llm", llm,
           "--intent", row.get("intent_pin") or "triage",
           "--message", row["message"]]
    if row.get("module"):
        cmd += ["--module", row["module"]]
    env = {"FSRPB_DEV": "1"}
    import os
    run_env = {**os.environ, **env}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       env=run_env)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"local_turn failed (rc={p.returncode}): "
                           f"{(p.stderr or p.stdout)[-400:]}")
    return json.loads(p.stdout)


def _observe(result: dict) -> dict:
    tr = result.get("transcript", [])
    resolved = None
    cards = []
    unknown = 0
    texts = []
    for e in tr:
        if not isinstance(e, dict):
            continue
        t = e.get("type", "")
        if t == "turn_inputs":
            resolved = e.get("intent")
        elif t == "text":
            texts.append(e.get("text", ""))
        if t.endswith("_card") or t.endswith("_offer"):
            cards.append(t)
        c = e.get("content")
        if isinstance(c, dict) and c.get("code") == "unknown_connector":
            unknown += 1
    return {"resolved_intent": resolved, "cards": cards,
            "unknown_connector": unknown, "text": "\n".join(texts),
            "tools_called": result.get("tools_called", [])}


def _check(row: dict, obs: dict) -> list[str]:
    exp = row.get("expect", {})
    fails = []
    if "resolved_intent" in exp and obs["resolved_intent"] != exp["resolved_intent"]:
        fails.append(f"resolved_intent {obs['resolved_intent']!r} != "
                     f"{exp['resolved_intent']!r}")
    for want in exp.get("card_in", []):
        if want not in obs["cards"]:
            fails.append(f"missing card {want!r} (saw {obs['cards'] or 'none'})")
    for forbid in exp.get("card_not", []):
        if forbid in obs["cards"]:
            fails.append(f"forbidden card {forbid!r} was emitted")
    if exp.get("no_unknown_connector") and obs["unknown_connector"]:
        fails.append(f"{obs['unknown_connector']} run_op(s) hit an unconfigured "
                     f"connector (unknown_connector)")
    if exp.get("language") == "en" and not _looks_english(obs["text"]):
        fails.append("analyst-facing text is not English (language drift)")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goldens", default=str(DEFAULT_GOLDENS))
    ap.add_argument("--python", default=sys.executable,
                    help="python that can import the connector (dev_env)")
    ap.add_argument("--llm", default="frank")
    ap.add_argument("--only", default=None, help="run one golden by name")
    ap.add_argument("--timeout", type=int, default=480)
    args = ap.parse_args(argv)

    rows = [r for r in json.loads(pathlib.Path(args.goldens).read_text())
            if r.get("expect")]
    if args.only:
        rows = [r for r in rows if r["name"] == args.only]
    if not rows:
        print("no goldens to run", file=sys.stderr)
        return 2

    any_fail = False
    for row in rows:
        name = row["name"]
        try:
            obs = _observe(_drive(args.python, row, args.llm, args.timeout))
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {name}: {e}")
            any_fail = True
            continue
        fails = _check(row, obs)
        if fails:
            any_fail = True
            print(f"FAIL {name}: resolved={obs['resolved_intent']} "
                  f"cards={obs['cards'] or 'none'} unknown={obs['unknown_connector']}")
            for f in fails:
                print(f"     - {f}")
        else:
            print(f"PASS {name}: resolved={obs['resolved_intent']} "
                  f"cards={obs['cards'] or 'none'}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
