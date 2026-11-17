"""Keep an enrichment verdict inside the model's context budget.

The model reads a `run_op` result through the framework's budgeter. The newest
result is kept whole up to a cap; an older one is clipped to a head and a tail
(`fsr_playbooks.llm._loop_helpers.shrink_history`), so anything in the middle is
gone. A VirusTotal IP report with `summarize: false` is ~26 KB, and its verdict
(`last_analysis_stats`) sits after the per-engine map and the whois/rdap blocks.
Live, the analyst got an IOC card reading 0/92 while the model answered "the
malicious and harmless vote counts were cut off in the tool output".

This module shapes such a result, for the known threat-intel connectors only,
before the framework sees it. Two rules keep the card safe:

* Verdict keys are moved to the front of each object, so the framework's
  head-clip keeps them. Moving a key changes no value.
* Only a field that carries nothing the card builders read may be dropped, and
  the dropped names are listed under `omitted_for_size`. A field the card reads,
  at any depth, is never dropped, so the card built from the shaped result is
  the card built from the raw one.

The budget sits below the framework's clip threshold. A result that fits comes
back as it was. Other connectors and failed results are never reshaped.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import json
from typing import Any

from wire_enrichment import _SCORE_KEYS, _TAG_KEYS, _VERDICT_KEYS

#: Serialized size a shaped result must fit. Under the framework's
#: `_RESULT_CAP_CHARS` (8000), so `shrink_history` never clips a shaped result.
ENRICHMENT_BUDGET = 6000

_ENRICHER_HINTS = ("virustotal", "alienvault", "otx", "abuseipdb", "shodan",
                   "fortiguard")

# Payload keys the IOC card builders read (wire_enrichment), beyond the verdict,
# score and tag tuples imported below. Read at any depth, so never dropped.
_CARD_EXTRA_KEYS = frozenset({
    "ioc_tags", "ioc_cate", "spam_cates", "av_cate", "wf_cate", "recordTags",
    "confidence", "related", "malware_families", "countryCode", "isTor",
    "totalReports", "abuseConfidenceScore", "hostnames", "org", "organization",
    "ports", "country", "country_name", "asn", "as_owner", "as_info", "geoip",
    "threatinfo", "riskinfo", "risk_score", "pulse_info", "validation", "alienvault",
    "false_positive", "assessment", "source", "name", "message", "count",
    "values", "general", "attributes", "last_analysis_stats", "malicious",
    "reputation", "type", "id",
})
# Request parameters the builders read (`params.get("ip")`) are not payload
# fields and are not listed here.
_CARD_READ_KEYS = (frozenset(_VERDICT_KEYS) | frozenset(_TAG_KEYS)
                   | frozenset(_SCORE_KEYS) | _CARD_EXTRA_KEYS)

# Envelope keys the framework and the card read. Never dropped by key.
_PROTECTED = frozenset({"ok", "code", "message", "status", "error", "connector",
                        "op", "data", "result", "records", "output_shape",
                        "output_top_keys"})

def is_enricher(connector: str) -> bool:
    """A threat-intel connector whose result the card builder reads."""
    name = (connector or "").lower()
    return any(h in name for h in _ENRICHER_HINTS)


def _size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return 0


def _carries_card_key(value: Any) -> bool:
    """True when any object at any depth under `value` has a key the card reads."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if any(k in _CARD_READ_KEYS for k in node):
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _verdict_first(node: Any) -> None:
    """In place: each object lists its verdict-bearing keys before the rest.
    Reordering changes no value, only what the framework's head-clip keeps."""
    if isinstance(node, dict):
        keys = list(node)
        ordered = [k for k in keys if k in _CARD_READ_KEYS or k in _PROTECTED]
        ordered += [k for k in keys if k not in ordered]
        items = [(k, node.pop(k)) for k in ordered]
        node.update(items)
        for value in node.values():
            _verdict_first(value)
    elif isinstance(node, list):
        for item in node:
            _verdict_first(item)


def _largest_droppable(root: Any) -> tuple[dict, str] | None:
    """The (parent, key) of the largest non-verdict value under `root`."""
    best: tuple[int, dict, str] | None = None
    stack = [root]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if (key not in _PROTECTED and key not in _CARD_READ_KEYS
                    and value not in (None, "", [], {})
                    and not _carries_card_key(value)):
                size = _size(value)
                if best is None or size > best[0]:
                    best = (size, node, key)
            if isinstance(value, dict):
                stack.append(value)
    return None if best is None else (best[1], best[2])


def shape_enrichment_result(result: Any, connector: str) -> Any:
    """`result` shaped to fit ENRICHMENT_BUDGET, or unchanged when it fits, is
    not a successful threat-intel result, or the connector is not one."""
    if not is_enricher(connector) or not isinstance(result, dict) or not result.get("ok"):
        return result
    if _size(result) <= ENRICHMENT_BUDGET:
        return result
    out = copy.deepcopy(result)
    _verdict_first(out)
    dropped: list[str] = []
    while _size(out) > ENRICHMENT_BUDGET:
        hit = _largest_droppable(out)
        if hit is None:
            break
        parent, key = hit
        dropped.append(key)
        del parent[key]
    if dropped:
        out["omitted_for_size"] = sorted(set(dropped))
    return out


def wrap_run_op(fn):
    """`fn` (the framework's run_op) with its enrichment results shaped. Keeps
    the signature, so argument binding and the tool schema are unchanged."""
    if getattr(fn, "_shapes_enrichment", False):
        return fn

    @functools.wraps(fn)
    def run_op_shaped(*args, **kwargs):
        result = fn(*args, **kwargs)
        connector = kwargs.get("connector", args[0] if args else "")
        return shape_enrichment_result(result, str(connector or ""))

    run_op_shaped._shapes_enrichment = True
    return run_op_shaped


def install_enrichment_view() -> bool:
    """Wrap the library's registered run_op once. True when it was wrapped now."""
    from fsr_playbooks.llm.tools import REGISTRY
    spec = REGISTRY.get("run_op")
    if spec is None or getattr(spec.fn, "_shapes_enrichment", False):
        return False
    REGISTRY["run_op"] = dataclasses.replace(spec, fn=wrap_run_op(spec.fn))
    return True
