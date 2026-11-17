"""Conversation history replayed to the model as native tool turns.

Earlier turns reach the model the way the provider APIs define them: an
assistant message carrying `tool_use` blocks, answered by a user message
carrying `tool_result` blocks with the matching ids. The framework's
OpenAI-compatible providers translate that block form into `tool_calls` /
`role: tool` messages (fsr_playbooks/llm/replay.py).

Why not text: history used to collapse every call to `[called name(args)]`
and every result to `[tool result: ...]`. A model reading those learns that
writing the string is how a tool gets called, and on a three-turn refine it
did exactly that in 3 of 4 runs -- a turn that announced an edit and never
made it.

Two inputs share one converter:
  * the widget's `messages[]` (assistant content is a block list in which
    calls and results sit together, ids optional), and
  * the stored canonical conversation a `chat_resume` reloads, whose
    assistant turns `transcript_blocks` now writes in the same block form.
A string `content` (any older stored session) passes through untouched.

Only the most recent `NATIVE_TURNS` assistant turns replay their calls; an
older turn keeps its prose plus one sentence naming the tools it used. That
bounds the context without ever writing a call-shaped string.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Assistant turns, counted from the newest, whose tool calls replay natively.
NATIVE_TURNS = 4
#: Same cap the widget applies before it re-sends a prior result.
RESULT_CAP = 2048
_NO_RESULT = "No result was recorded for this call."
_ID_OK = re.compile(r"[^A-Za-z0-9_-]")

# Cards the analyst was shown that no model call produced on its own line in
# history. Said in a sentence, never in call syntax.
_CARD_PROSE = {
    "action_card": "I proposed an action for the analyst to approve: {op}.",
    "choice_card": "I asked the analyst to choose between: {opts}.",
    "manual_input": "I asked the analyst for input.",
    "capability_gap": "I told the analyst a capability is missing: {missing}.",
}


_PARKED = ("Not run. Staged for the analyst to approve as \"{op}\"; nothing "
           "changes unless they approve it.")


def _awaits_result(blocks: list[dict], tool_use_id: Any) -> bool:
    """Is there a call with this id in `blocks` that has no result yet?"""
    called = any(b.get("type") == "tool_use" and b.get("id") == tool_use_id for b in blocks)
    answered = any(b.get("type") == "tool_result" and b.get("tool_use_id") == tool_use_id
                   for b in blocks)
    return called and not answered


def cap_result(text: Any) -> str:
    """A prior result as a string, head+tail kept past `RESULT_CAP`."""
    s = text if isinstance(text, str) else json.dumps(text, default=str)
    if len(s) <= RESULT_CAP:
        return s
    head, tail = s[:RESULT_CAP - 200], s[-120:]
    return (f"{head}\n... ({len(s) - len(head) - len(tail)} chars truncated)"
            f" ...\n{tail}")


def transcript_blocks(wire_transcript: list[dict]) -> list[dict]:
    """One turn's wire transcript as an assistant block list, for the stored
    conversation. Streamed text tokens are coalesced (the transcript carries
    them one event per token); calls and results keep their ids. Never empty
    -- Anthropic rejects an empty assistant turn."""
    blocks: list[dict] = []
    text: list[str] = []

    def flush() -> None:
        joined = "".join(text).strip()
        text.clear()
        if joined:
            blocks.append({"type": "text", "text": joined})

    for ev in wire_transcript or []:
        t = ev.get("type") if isinstance(ev, dict) else None
        if t == "text":
            text.append(str(ev.get("text") or ""))
            continue
        if t == "tool_use":
            flush()
            blocks.append({"type": "tool_use", "id": ev.get("id"),
                           "name": ev.get("name"), "input": ev.get("input") or {}})
        elif t == "tool_result":
            flush()
            blocks.append({"type": "tool_result",
                           "tool_use_id": ev.get("tool_use_id"),
                           "content": cap_result(ev.get("content"))})
        elif t in _CARD_PROSE:
            flush()
            opts = ", ".join(str(o.get("value")) for o in ev.get("options") or []
                             if isinstance(o, dict))
            op = (str(ev.get("summary") or "").strip()
                  or ".".join(x for x in (ev.get("connector"), ev.get("operation")) if x))
            # The call the card parks never gets a result of its own (an
            # approved write runs as a new call). Left open, replay answered it
            # "No result was recorded", and after a cancel the model read that
            # as something having gone wrong: "I also used an alert ID I hadn't
            # confirmed". Answer it with what actually happened.
            parked = ev.get("preceding_tool_use_id")
            if t == "action_card" and parked and _awaits_result(blocks, parked):
                blocks.append({"type": "tool_result", "tool_use_id": parked,
                               "content": _PARKED.format(op=op or "the action")})
                continue
            blocks.append({"type": "text", "text": _CARD_PROSE[t].format(
                op=op or "an operation", opts=opts or "options",
                missing=ev.get("missing") or "unknown")})
    flush()
    return blocks or [{"type": "text", "text": "(no visible output)"}]


def _safe_id(raw: Any, fallback: str, used: set[str]) -> str:
    cid = _ID_OK.sub("_", str(raw))[:64] if raw else ""
    cid = cid or fallback
    base, n = cid, 1
    while cid in used:
        n += 1
        cid = f"{base[:60]}_{n}"
    used.add(cid)
    return cid


def _tool_names(blocks: list) -> list[str]:
    names: list[str] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            n = str(b.get("name") or "")
            if n and n not in names:
                names.append(n)
    return names


def _text_blocks(blocks: list) -> list[dict]:
    out = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            t = t if isinstance(t, str) else ("" if t is None else str(t))
            if t.strip():
                out.append({"type": "text", "text": t})
        elif isinstance(b, str) and b.strip():
            out.append({"type": "text", "text": b})
    return out


def text_of(blocks: list) -> str:
    """The words in a block list (text blocks only), joined by newlines."""
    return "\n".join(b["text"] for b in _text_blocks(blocks))


def _prose_turn(blocks: list) -> str:
    """An older assistant turn: its words, plus the tools it used named in a
    sentence. Not call-shaped by design."""
    parts = [b["text"] for b in _text_blocks(blocks)]
    names = _tool_names(blocks)
    if names:
        parts.append("(Tools used in this reply: " + ", ".join(names) + ".)")
    return "\n".join(parts) or "(no visible output)"


def _native_turn(blocks: list, turn: int, used: set[str]) -> list[tuple[str, Any]]:
    """One assistant block list as properly paired (role, content) turns:
    assistant[text, tool_use...] -> user[tool_result...] -> assistant[...]."""
    out: list[tuple[str, Any]] = []
    cur: list[dict] = []          # assistant blocks of the open segment
    calls: list[dict] = []        # tool_use blocks of the open segment
    results: dict[str, dict] = {}  # id -> tool_result block
    raw_ids: dict[str, str] = {}  # sanitized raw id -> assigned id
    n = 0

    def close() -> None:
        if not cur:
            return
        out.append(("assistant", list(cur)))
        if calls:
            out.append(("user", [results.get(c["id"]) or {
                "type": "tool_result", "tool_use_id": c["id"],
                "content": _NO_RESULT} for c in calls]))
        cur.clear()
        calls.clear()
        results.clear()
        raw_ids.clear()

    for b in blocks:
        if isinstance(b, str):
            b = {"type": "text", "text": b}
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            tb = _text_blocks([b])
            if not tb:
                continue
            if calls:  # text after calls belongs to the next round
                close()
            cur.extend(tb)
        elif t == "tool_use":
            if results:
                close()
            n += 1
            cid = _safe_id(b.get("id"), f"hist{turn}_{n}", used)
            if b.get("id"):
                raw_ids[_ID_OK.sub("_", str(b.get("id")))[:64]] = cid
            inp = b.get("input")
            if not isinstance(inp, dict):
                inp = {} if inp in (None, "") else {"value": inp}
            block = {"type": "tool_use", "id": cid,
                     "name": str(b.get("name") or "tool"), "input": inp}
            cur.append(block)
            calls.append(block)
        elif t == "tool_result":
            open_calls = [c for c in calls if c["id"] not in results]
            if not open_calls:
                continue  # a result with no call to answer: nothing to pair
            want = b.get("tool_use_id")
            want = raw_ids.get(_ID_OK.sub("_", str(want))[:64]) if want else None
            match = (next((c for c in open_calls if c["id"] == want), None)
                     or next((c for c in open_calls
                              if b.get("tool") and c["name"] == b.get("tool")), None)
                     or open_calls[0])
            results[match["id"]] = {"type": "tool_result",
                                    "tool_use_id": match["id"],
                                    "content": cap_result(b.get("content"))}
    close()
    return out


def replay_turns(prior: list | None) -> list[tuple[str, Any]]:
    """Wire/stored messages -> (role, content) turns ready to become
    `Message`s. See the module docstring for the shape and the window."""
    entries = [m for m in prior or [] if isinstance(m, dict)
               and m.get("role") in ("user", "assistant")
               and m.get("content") is not None]
    asst_idx = [i for i, m in enumerate(entries) if m["role"] == "assistant"
                and isinstance(m["content"], list)]
    native = set(asst_idx[-NATIVE_TURNS:]) if NATIVE_TURNS > 0 else set()
    used: set[str] = set()
    out: list[tuple[str, Any]] = []
    for i, m in enumerate(entries):
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append((role, content))
        elif role == "assistant":
            out.extend(_native_turn(content, i, used) if i in native
                       else [("assistant", _prose_turn(content))])
        else:
            # A user turn never carries calls of its own; keep its words.
            out.append((role, text_of(content)))
    return out


def _content_text(content: Any) -> str:
    """The words of one stored or wire message content, string or block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return text_of(content)
    return ""


def with_resume_outcomes(prior: list | None, stored: list | None) -> list:
    """Put back the outcomes of resume turns the widget's history lacks.

    A playbook save and an approved action run happen on a chat_resume, which
    the connector answers without a model turn. The connector writes what
    happened into the stored conversation as entries marked ``outcome``. The
    widget's `messages[]` does not carry them, so a later chat_turn replayed a
    conversation in which the save and the run never happened: live, the
    analyst's next turn was answered "the save was not confirmed".

    Each marked assistant entry whose words the widget's history does not
    already contain is inserted, with the request it answers, just before the
    latest user message (the turn now being answered). Outcomes are facts:
    they go in as history, never as a directive."""
    prior = list(prior or [])
    if not prior or not isinstance(prior[-1], dict) or prior[-1].get("role") != "user":
        return prior
    have = "\n".join(_content_text(m.get("content")) for m in prior
                     if isinstance(m, dict) and m.get("role") == "assistant")
    picked: list[dict] = []
    seen: set[str] = set()
    for i, m in enumerate(stored or []):
        if not (isinstance(m, dict) and m.get("outcome") and m.get("role") == "assistant"):
            continue
        said = _content_text(m.get("content")).strip()
        if not said or said in have or said in seen:
            continue
        seen.add(said)
        req = stored[i - 1] if i else None
        if isinstance(req, dict) and req.get("outcome") and req.get("role") == "user":
            picked.append(dict(req))
        picked.append(dict(m))
    if not picked:
        return prior
    return prior[:-1] + picked + prior[-1:]


def is_analyst_turn(message: Any) -> bool:
    """A user message that holds the analyst's words -- not a replayed
    tool_result turn. Code that folds context into "the first/last user
    message" must look only at these."""
    return (getattr(message, "role", None) == "user"
            and isinstance(getattr(message, "content", None), str))
