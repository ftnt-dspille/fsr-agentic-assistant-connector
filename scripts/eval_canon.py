"""One union -> constituent canonicalizer for every eval script.

The model calls the consolidated tools -- `emit_card(card_type=...)`,
`find(kind=...)` -- while graders ask for what each call DID:
`emit_playbook_offer`, `find_containment_actions`. A grader that reads raw
names is blind to the call it grades. Live: the S1/S3 `_offer_yaml` looked for
a tool call named `emit_playbook_offer`, never found one, and graded the last
```yaml fence in prose instead of the offered bytes; t1_scenarios' "resolved
via find_containment_actions" could not see `find(kind="action")`.

The map itself lives once, in the framework's `tooling/evals/scoring.py`
(`canonicalize_trace`). `tooling/` ships in the framework repo, not the wheel,
so it is found next to the editable `fsr_playbooks` (FSRPB_DEV); a second copy
here would be a parallel name list -- the bug class this module exists for.
"""
from __future__ import annotations

import importlib.util
from typing import Any, Callable, Optional

_CACHE: dict[str, Any] = {}
_WARNED = False


def canonicalizer() -> Optional[Callable]:
    """The framework scorer's `canonicalize_trace`, or None on a pinned wheel."""
    if "fn" in _CACHE:
        return _CACHE["fn"]
    fn = None
    try:

        # The one resolver: against a pinned wheel fsr_playbooks sits in
        # site-packages with no tooling/ beside it, so deriving the root from its
        # __file__ would never find scoring.py even when a checkout is available.
        try:
            from framework_tooling import framework_root
        except ImportError:
            from scripts.framework_tooling import framework_root
        src = framework_root()
        path = src / "tooling" / "evals" / "scoring.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location("_fsr_eval_scoring", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "canonicalize_trace", None)
    except Exception:  # noqa: BLE001 -- a grader must still run without it
        fn = None
    _CACHE["fn"] = fn
    return fn


def canonical_tool_uses(transcript: list | None) -> list[dict]:
    """The transcript's tool calls, each renamed to the constituent it ran.

    A union call keeps its real name in `via`; for `emit_card` the card's
    `payload` becomes the call's `args`. Read arguments with `call_args`.
    Without the framework's `tooling/` (CI on the pinned wheel) the raw calls
    come back, with one warning -- a silently literal-only grader is the
    false-FAIL this exists to prevent.
    """
    global _WARNED
    calls = [e for e in transcript or []
             if isinstance(e, dict) and e.get("type") in ("tool_call", "tool_use")]
    canon = canonicalizer()
    if canon is None:
        if not _WARNED:
            _WARNED = True
            print("[eval_canon] WARNING: framework tooling/evals/scoring.py not "
                  "found; graders see raw union names (emit_card, find)")
        return calls
    return canon(calls) or []


def call_args(call: dict) -> dict:
    """A (possibly canonicalized) call's arguments."""
    a = call.get("args")
    if not isinstance(a, dict):
        a = call.get("input")
    return a if isinstance(a, dict) else {}


def tool_names(transcript: list | None) -> list[str]:
    return [c.get("name") or "?" for c in canonical_tool_uses(transcript)]


def offered_yaml(transcript: list | None) -> str:
    """The YAML of the LAST playbook offer the turn made, or ""."""
    for c in reversed(canonical_tool_uses(transcript)):
        if c.get("name") == "emit_playbook_offer":
            y = call_args(c).get("yaml")
            if isinstance(y, str) and y.strip():
                return y.strip()
    return ""
