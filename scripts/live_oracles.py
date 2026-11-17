"""Expected outputs for `live_writes` rows, computed without the code under test.

A rendered template is only proven by comparing it with text worked out
independently: here, plain Python over device data collected by a separate
playbook run. Jinja is never used to build the expectation, so a template that
reads the wrong key, or a render path that feeds it an empty context, cannot
agree with it by accident.

A row names its oracle:  "oracle": {"name": "<function below>", ...args}.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable


def code_block_after(text: str, heading: str) -> str | None:
    i = text.find(heading)
    m = re.search(r"```[a-zA-Z]*\n(.*?)```", text[i:], re.S) if i >= 0 else None
    return m.group(1) if m else None


def lines(text: str | None) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def collect(arc, source: str) -> Any:
    """The metadata source's data on the arc's device, from its own playbook
    run in a separate conversation (the 'Data Collected' pause carries it)."""
    from scripts.live_writes import Arc
    rec = next(r for r in arc.fsr.client.get("/api/3/ztpf_metadata_sources",
                                             params={"$limit": 200})["hydra:member"]
               if r.get("name") == source)
    spec = {"name": f"collect {source}", "mount": {"module": "ztpf_metadata_sources", "uuid": rec["uuid"]},
            "steps": [{"user": "Validate this metadata source by running the "
                               "'Get Metadata Source Data on Device' playbook."}]}
    sub = Arc(arc.fsr, spec, arc.picked_device or arc.device, arc.log)
    sub.run()
    pause = next((p for p in sub.pauses if not p.get("fields")), None)
    if pause is None:
        raise RuntimeError(f"collecting {source!r}: no 'Data Collected' pause ({sub.problems})")
    raw = json.loads(code_block_after(pause["question"], "Metadata Results") or "null")
    return raw.get(rec["datakey"], raw) if isinstance(raw, dict) else raw


def physical_interface_descriptions(ints: list[dict], device: str) -> list[str]:
    """One `config system interface` block: every interface with an address,
    sorted by name, gets a description and its allowaccess."""
    out = ["config system interface"]
    for i in sorted(ints, key=lambda x: x["name"]):
        ip = i.get("ip") or []
        if not ip or ip[0] == "0.0.0.0":
            continue
        out += [f'edit "{i["name"]}"', f'set description "{i.get("alias") or i["name"]} on {device}"']
        aa = i.get("allowaccess")
        aa = " ".join(aa) if isinstance(aa, list) else (aa or "")
        if aa:
            out.append(f"set allowaccess {aa}")
        out.append("next")
    return out + ["end"]


ORACLES: dict[str, Callable[[list[dict], str], list[str]]] = {
    "physical_interface_descriptions": physical_interface_descriptions,
}


def check(spec: dict, arc) -> list[str]:
    """Compare the arc's 'Template Rendered' output with the oracle's text."""
    rendered = next((p for p in arc.pauses if not p.get("fields") and "Rendered" in (p.get("question") or "")), None)
    if rendered is None:
        return ["oracle: the template was never rendered on a device (no 'Template Rendered' pause)"]
    got = lines(code_block_after(rendered["question"], "Template Results"))
    device = arc.picked_device or arc.device
    want = ORACLES[spec["name"]](collect(arc, spec["source"]), device)
    if got == want:
        arc.log(f"   oracle: rendered output equals the expected text ({len(want)} lines)")
        return []
    import difflib
    diff = "\n".join(difflib.unified_diff(want, got, "expected", "rendered", lineterm=""))
    scripts = [arc.fsr.client.get(f"/api/3/{m}/{u}").get("script") or ""
               for m, u in arc.created if m == "ztpf_templates"]
    saved = "".join(f"\n--- the template the chat saved ---\n{sc}" for sc in scripts)
    return [f"oracle {spec['name']}: rendered output differs from the expected text\n{diff}{saved}"]
