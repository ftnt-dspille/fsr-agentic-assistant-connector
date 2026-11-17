"""ZTPF authoring tool: ``test_template`` — render + lint a template's Jinja.

The high-value primitive for the `ztpf_templates` authoring persona (see
`docs/plans/module-scoped-assistant-personas.md` §7c). A `ztpf_templates` record
carries its own test fixture: the ``script`` field is the Jinja body and
``exampleJinjaVars`` is a JSON render context. So "test this template" needs no
external input — read the record, render ``script`` against ``exampleJinjaVars``,
and lint the Jinja.

This is READ-ONLY (tier 1, auto-confirm): it never writes. Writing an edited
template body back is `update_record` (tier 3, approval-carded). Like the
record-write tools it is excluded from both default slices and surfaces only
through a persona's ``tools.allow``.

LINT + RENDER (see §7c.3) — real ztpf templates use the `{% do %}` extension, the
`{% break/continue %}` loopcontrols, FSR/Ansible-only filters (regex_replace,
ipaddr, b64encode…), AND reference the grounding `record`, so a naive offline
render/lint false-flags valid production templates. Handling:
  - LINT syntax: an AUTHORITATIVE parse with the FSR extensions (`do`,
    `loopcontrols`) enabled — so a valid `{% do %}` template is NOT reported as a
    syntax error. Filter-name warnings reuse
    ``fsr_playbooks.compiler.jinja_checks.check_jinja`` (its catalog is the full
    Ansible+FSR superset), keeping only its ``unknown_jinja_filter`` findings.
  - RENDER: ON-BOX, prefer the LIVE FSR jinja-editor (`/api/wf/api/jinja-editor/`
    via the crudhub loopback — same endpoint the `render_jinja` op uses) so every
    runtime filter + `do` resolve against the real runtime. OFF-BOX (dev/tests),
    fall back to an offline StrictUndefined env (extensions enabled) that surfaces
    undefined-variable refs and carries the runtime filters real ztpf scripts use
    (regex_replace/search/findall, ipaddr); any other runtime-only filter is
    reported as an ``offline_gap`` (render on the box), NOT a false render_error.
All fsr_playbooks / crudhub imports are defensive so a missing symbol degrades
gracefully rather than breaking package import.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# jinja2 rejects an unknown filter/test at compile time with "No filter named 'x'."
# The OFFLINE renderer carries only jinja2 builtins, so an FSR/Ansible-only filter
# trips this on a perfectly valid template — an offline GAP, not a defect.
_OFFLINE_FILTER_GAP = re.compile(r"No (?:filter|test) named '([^']+)'")

# Extensions the FSR Jinja runtime enables — `{% do %}` and `{% break/continue %}`
# are common in real ztpf template bodies; enable them so the offline lint/render
# match the runtime instead of false-flagging them as syntax errors.
_JINJA_EXTENSIONS = ["jinja2.ext.do", "jinja2.ext.loopcontrols"]

from fsr_playbooks.mcp_server import _shared
from fsr_playbooks.mcp_server._shared import mcp

# Field names on a ztpf_templates record (live 8.0 schema).
_SCRIPT_FIELD = "script"
_VARS_FIELD = "exampleJinjaVars"


def _jinja_env(undefined: Any = None):
    """A jinja2 Environment with the FSR runtime extensions enabled (and an
    optional undefined policy). Used for the authoritative offline syntax parse
    and the offline fallback render."""
    import jinja2
    kwargs: dict[str, Any] = {"autoescape": False, "extensions": _JINJA_EXTENSIONS}
    if undefined is not None:
        kwargs["undefined"] = undefined
    return jinja2.Environment(**kwargs)


# Transparent-reauth session request — shared with tools_records via _common.
from ._common import live_request as _request  # noqa: E402


def _read_record(module: str, uuid: str) -> tuple[Optional[dict], Optional[dict]]:
    """Read one record, returning ``(record, error)`` with exactly one non-None.

    Prefers the crudhub loopback (``make_cyops_request``) so it works in EVERY
    in-worker context — the agent loop AND a direct ``call_mcp_tool`` / playbook
    invocation, where ``_shared._live_client()`` isn't wired. Falls back to the
    pyfsr ``_live_client`` for dev/off-box."""
    # 1. crudhub loopback (on-box, context-independent).
    try:
        from connectors.cyops_utilities.crudhub import make_cyops_request  # type: ignore
    except Exception:  # noqa: BLE001 — off-box / dev: fall through to _live_client
        make_cyops_request = None
    if make_cyops_request is not None:
        try:
            rec = make_cyops_request(f"/api/3/{module}/{uuid}", "GET")
        except Exception as exc:  # noqa: BLE001
            return None, {"ok": False, "code": "transport_error",
                          "message": f"GET /api/3/{module}/{uuid} failed: {exc}"}
        if isinstance(rec, dict):
            return rec, None
        return None, {"ok": False, "code": "read_failed",
                      "message": f"unexpected crudhub response for {module}/{uuid}"}
    # 2. pyfsr client fallback (dev / off-box).
    client = _shared._live_client()
    if client is None:
        return None, {"ok": False, "code": "no_fsr_configured",
                      "message": "no FSR instance is configured to read the record"}
    url = f"{client.base_url}/api/3/{module}/{uuid}"
    try:
        r = _request("get", client, url)
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "code": "transport_error",
                      "message": f"GET {url} failed: {exc}"}
    status = getattr(r, "status_code", None)
    if status != 200:
        return None, {"ok": False, "code": f"http_{status}",
                      "message": (getattr(r, "text", "") or f"HTTP {status}")[:500]}
    try:
        return r.json(), None
    except Exception:  # noqa: BLE001
        return {}, None


def _coerce_context(raw: Any) -> tuple[dict, Optional[str]]:
    """Coerce a render context to a dict. ``exampleJinjaVars`` round-trips as an
    object over crudhub but may arrive as a JSON string; accept both. Returns
    ``(context, warning)`` — warning is a human note when the value was unusable
    (rendered as an empty context rather than failing hard)."""
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}, f"{_VARS_FIELD} is not valid JSON; rendered with no vars"
        if isinstance(parsed, dict):
            return parsed, None
        return {}, f"{_VARS_FIELD} JSON is not an object; rendered with no vars"
    return {}, f"{_VARS_FIELD} is a {type(raw).__name__}, not an object; rendered with no vars"


def _syntax_finding(script: str) -> Optional[dict]:
    """Authoritative syntax check: parse with the FSR extensions enabled so a
    valid `{% do %}` / `{% break %}` template is not false-flagged. Returns a
    single ``jinja_syntax_error`` finding dict, or ``None`` when it parses."""
    import jinja2
    try:
        _jinja_env().parse(script)
        return None
    except jinja2.TemplateSyntaxError as e:  # noqa: PERF203
        line = getattr(e, "lineno", None)
        where = f" (line {line})" if line else ""
        return {"code": "jinja_syntax_error", "severity": "error",
                "path": _SCRIPT_FIELD,
                "message": f"template won't parse{where}: {e.message}"}
    except Exception as e:  # noqa: BLE001 — any parse failure is a syntax finding
        return {"code": "jinja_syntax_error", "severity": "error",
                "path": _SCRIPT_FIELD, "message": f"template won't parse: {e}"}


def _filter_warnings(script: str) -> list[dict]:
    """Unknown-filter warnings from the linter's catalog (full FSR + Ansible
    superset). Keeps ONLY ``unknown_jinja_filter`` findings — ``check_jinja``'s
    own env lacks our extensions, so its syntax findings are superseded by
    ``_syntax_finding``. Best-effort; [] if the linter is unavailable."""
    try:
        from fsr_playbooks.compiler.jinja_checks import check_jinja
        found = check_jinja(script, step_id="ztpf_template", path=_SCRIPT_FIELD)
    except Exception:  # noqa: BLE001
        return []
    return [f for f in found
            if isinstance(f, dict) and f.get("code") == "unknown_jinja_filter"]


def _lint(script: str) -> list[dict]:
    """Lint the template: an authoritative syntax finding (if any), else the
    unknown-filter warnings. A template that won't parse skips the filter walk."""
    syn = _syntax_finding(script)
    if syn is not None:
        return [syn]
    return _filter_warnings(script)


def _live_render(script: str, context: dict):
    """Render via FSR's own jinja-editor (the crudhub loopback — same endpoint the
    ``render_jinja`` op uses), so `do` + all runtime filters resolve. Returns:
      - ``None``            — no live path (off-box / crudhub not importable).
      - ``("ok", value)``   — rendered output from the real runtime.
      - ``("error", msg)``  — the editor endpoint rejected the template."""
    try:
        from connectors.cyops_utilities.crudhub import make_cyops_request  # type: ignore
    except Exception:  # noqa: BLE001 — off-box / dev: signal "no live path"
        return None
    try:
        resp = make_cyops_request("/api/wf/api/jinja-editor/?format=json", "POST",
                                  body={"template": script, "values": context})
    except Exception as e:  # noqa: BLE001
        return "error", f"live jinja-editor failed: {e}"
    # Normalize like operations.render_jinja does.
    if isinstance(resp, dict):
        for k in ("result", "output", "rendered", "value"):
            if k in resp:
                return "ok", resp[k]
    return "ok", resp


class _OfflineGap(Exception):
    """A runtime filter was called in a form the offline copy doesn't carry."""


def _regex_flags(ignorecase: bool, multiline: bool) -> int:
    return (re.I if ignorecase else 0) | (re.M if multiline else 0)


def _regex_replace(value: Any, pattern: str = "", replacement: str = "",
                   ignorecase: bool = False, multiline: bool = False,
                   count: int = 0) -> str:
    return re.sub(pattern, replacement, str(value), count=count,
                  flags=_regex_flags(ignorecase, multiline))


def _regex_search(value: Any, pattern: str, *groups: str,
                  ignorecase: bool = False, multiline: bool = False) -> Any:
    m = re.search(pattern, str(value), flags=_regex_flags(ignorecase, multiline))
    if m is None:
        return None
    if not groups:
        return m.group(0)
    out = []
    for g in groups:   # Ansible's '\\1' / '\\g<name>' group selectors
        sel = re.fullmatch(r"\\(?:(\d+)|g<(\w+)>)", g)
        if sel is None:
            raise _OfflineGap(f"regex_search group selector {g!r}")
        out.append(m.group(int(sel.group(1)) if sel.group(1) else sel.group(2)))
    return out


def _regex_findall(value: Any, pattern: str, multiline: bool = False,
                   ignorecase: bool = False) -> list:
    return re.findall(pattern, str(value), flags=_regex_flags(ignorecase, multiline))


def _ipaddr(value: Any, query: Any = "") -> Any:
    """The `ipaddr` queries real ztpf scripts use, with ansible.utils semantics:
    an integer picks the Nth address of the value's network (keeping the
    prefix), and the named queries read one part of it. Anything else is an
    offline gap, never a guess."""
    import ipaddress
    try:
        v = ipaddress.ip_interface(str(value).strip())
    except ValueError:
        return False
    net, size = v.network, v.network.num_addresses
    if query in ("", None):
        return value
    if isinstance(query, int) or (isinstance(query, str) and query.lstrip("-").isdigit()):
        n = int(query)
        try:
            return f"{net[n]}/{net.prefixlen}"
        except IndexError:
            return False
    on_network = v.ip == net.network_address and size > 1
    named = {
        "address": None if on_network else str(v.ip),
        "host": (str(v.ip) if size == 1 else
                 None if on_network else f"{v.ip}/{net.prefixlen}"),
        "network": str(net.network_address),
        "netmask": str(net.netmask),
        "prefix": net.prefixlen,
        "broadcast": str(net.broadcast_address),
        "cidr": str(net),
        "net": str(net) if on_network else False,
        "size": size,
        "public": value if not v.ip.is_private else False,
        "private": value if v.ip.is_private else False,
    }
    if query not in named:
        raise _OfflineGap(f"ipaddr({query!r})")
    out = named[query]
    return False if out is None else out


# Runtime-only filters the offline renderer carries, so a real template (and
# the agent's own drafts) render offline instead of stopping at "render on the
# box". These are the ones real ztpf scripts use; anything else stays a gap.
_OFFLINE_FILTERS = {"regex_replace": _regex_replace, "regex_search": _regex_search,
                    "regex_findall": _regex_findall, "ipaddr": _ipaddr}


def _offline_render(script: str, context: dict) -> tuple[Any, Optional[str], Optional[str]]:
    """Offline fallback render (StrictUndefined, extensions enabled). Returns
    ``(rendered, error, offline_gap)`` — a runtime-only filter it can't resolve is
    an ``offline_gap`` (valid template, render on box), an undefined variable or
    bad expression is a real ``error``."""
    import jinja2
    try:
        env = _jinja_env(jinja2.StrictUndefined)
        env.filters.update(_OFFLINE_FILTERS)
        tmpl = env.from_string(script)
        return tmpl.render(context), None, None
    except _OfflineGap as e:
        return None, None, (
            f"offline render skipped: the template uses {e}, a form the offline "
            f"renderer doesn't carry. Lint still ran; it renders on the box "
            f"(live jinja-editor).")
    except jinja2.TemplateAssertionError as e:  # unknown filter/test at compile
        m = _OFFLINE_FILTER_GAP.search(str(e))
        if m and _is_known_filter(m.group(1)):
            return None, None, (
                f"offline render skipped: the template uses {m.group(1)!r}, a "
                f"runtime-only filter/test the offline renderer doesn't carry. "
                f"Lint still ran; it renders on the box (live jinja-editor).")
        return None, str(e), None
    except jinja2.UndefinedError as e:
        return None, str(e), None
    except jinja2.TemplateSyntaxError as e:
        line = getattr(e, "lineno", None)
        return None, f"line {line}: {e.message}" if line else str(e), None
    except Exception as e:  # noqa: BLE001
        return None, str(e), None


def _is_known_filter(name: str) -> bool:
    """True if ``name`` is a filter/test the linter recognizes (jinja2 builtins ∪
    FSR widget palette ∪ Ansible superset). Used to tell a valid-but-offline-
    unrenderable filter from a genuinely unknown one.

    Asks ``check_jinja`` (the linter) rather than reaching for its private
    ``_KNOWN_*`` frozensets — those aren't in every pinned ``fsr_playbooks`` wheel,
    but ``check_jinja`` is: a known filter yields no ``unknown_jinja_filter``
    finding, an unknown one does. Fail-open to False."""
    try:
        from fsr_playbooks.compiler.jinja_checks import check_jinja
        findings = check_jinja("{{ _x | %s }}" % name, step_id="_probe", path="_")
        return not any(
            isinstance(f, dict) and f.get("code") == "unknown_jinja_filter"
            for f in findings)
    except Exception:  # noqa: BLE001
        return False


def _render(script: str, context: dict) -> tuple[Any, Optional[str], Optional[str], str]:
    """Render ``script`` against ``context``, preferring the live FSR jinja-editor
    on-box and falling back to the offline env off-box. Returns
    ``(rendered, error, offline_gap, render_via)`` where ``render_via`` is
    ``"live"`` or ``"offline"``."""
    live = _live_render(script, context)
    if live is not None:
        kind, val = live
        if kind == "ok":
            return val, None, None, "live"
        return None, str(val), None, "live"
    rendered, error, offline_gap = _offline_render(script, context)
    return rendered, error, offline_gap, "offline"


@mcp.tool()
def _read_path(path: str) -> Optional[Any]:
    """GET one API path (crudhub on-box, else the pyfsr client); None on failure."""
    try:
        from connectors.cyops_utilities.crudhub import make_cyops_request  # type: ignore
        return make_cyops_request(path, "GET")
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        return None
    client = _shared._live_client()
    if client is None:
        return None
    try:
        r = _request("get", client, f"{getattr(client, 'base_url', '') or ''}{path}")
        return r.json() if getattr(r, "status_code", None) == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _names_read(script: str, root: str) -> list[str]:
    """The attributes a template reads off ``root`` (``data.x`` / ``data['x']``),
    from its syntax tree; [] when it does not parse (the lint reports that)."""
    try:
        import jinja2.nodes as n
        tree = _jinja_env().parse(script)
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for node in tree.find_all((n.Getattr, n.Getitem)):
        if not (isinstance(node.node, n.Name) and node.node.name == root):
            continue
        key = node.attr if isinstance(node, n.Getattr) else (
            node.arg.value if isinstance(node.arg, n.Const) else None)
        if isinstance(key, str) and key not in out:
            out.append(key)
    return out


def _reads(script: str, root: str) -> bool:
    try:
        import jinja2.nodes as n
        return any(nm.name == root for nm in _jinja_env().parse(script).find_all(n.Name))
    except Exception:  # noqa: BLE001
        return False


def _source_example(datakey: str) -> Optional[dict]:
    """The metadata source that produces ``data.<datakey>``: its name, example
    output for that key, and example ``record``."""
    from urllib.parse import quote
    page = _read_path(f"/api/3/ztpf_metadata_sources?datakey={quote(datakey)}&$limit=1")
    rows = (page or {}).get("hydra:member") if isinstance(page, dict) else None
    src = rows[0] if rows else None
    if not isinstance(src, dict):
        return None
    out = src.get("exampleOutput")
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            out = None
    value = out.get(datakey, out) if isinstance(out, dict) else out
    jv = src.get("exampleJinjaVars")
    record = jv.get("record") if isinstance(jv, dict) else None
    return {"name": src.get("name"), "value": value, "record": record}


def _fill_from_sources(script: str, ctx: dict) -> tuple[dict, dict, list[dict]]:
    """Give the render the device data the template reads.

    Live, a template looping over ``data.dvmdb_ints_physical`` was "tested" with
    a context that had no such key: the loop ran zero times, the render came back
    ``config system interface / end`` with no findings, and the template was
    saved with a whitespace bug that glued every line together on the device.
    Each ``data.<key>`` the script reads and the context lacks is taken from the
    example output of the metadata source with that datakey (and ``record``
    from its example vars, when the script reads it). Whatever is still missing
    is a finding: that render did not exercise it.
    """
    data = dict(ctx.get("data") or {}) if isinstance(ctx.get("data"), dict) else {}
    filled: dict[str, str] = {}
    findings: list[dict] = []
    record = None
    for key in _names_read(script, "data"):
        if key in data:
            continue
        ex = _source_example(key)
        if ex and ex.get("value") is not None:
            data[key] = ex["value"]
            filled[f"data.{key}"] = f"example output of metadata source {ex['name']!r}"
            record = record or ex.get("record")
        else:
            findings.append({"severity": "warning", "code": "data_not_in_context",
                             "message": (f"data.{key} is not in the render context and no metadata "
                                         f"source with datakey {key!r} has example output, so this "
                                         "render ran without it: a loop over it rendered nothing. "
                                         "Pass it in `context` to test what the device will return.")})
    out = {**ctx, "data": data} if (data or "data" in ctx) else dict(ctx)
    if record and "record" not in out and _reads(script, "record"):
        out["record"] = record
        filled["record"] = "example record of the same metadata source"
    return out, filled, findings


# FortiOS CLI commands glued onto the previous text ("interfaceedit \"port1\"",
# "\"MGMT\"set allowaccess", "httpnext"): the template's `-%}` trims ate the
# newlines. Each command belongs on its own line.
_GLUED_CLI = re.compile(r'"(?:set|unset|edit|config|next|end)\b|[a-z]edit "|\Snext(?=\s|$)')


def _cli_findings(rendered: Any) -> list[dict]:
    if not isinstance(rendered, str):
        return []
    bad = [i + 1 for i, ln in enumerate(rendered.splitlines()) if _GLUED_CLI.search(ln)]
    if not bad:
        return []
    return [{"severity": "warning", "code": "cli_commands_run_together",
             "message": (f"rendered line(s) {bad[:5]} run CLI commands together on one line; "
                         "a `-%}` or `{%-` whitespace trim removed the newline between them. "
                         "Each command must be on its own line.")}]


# A field holds Jinja when it has a tag or an expression in it.
_JINJA_MARK = re.compile(r"\{\{|\{%")
# Rendered output handed back to the model, in characters. An HTML template
# over a 100 KB example renders to more than a turn should carry.
_RENDERED_MAX = 20000


def _match_field(rec: dict, field: str) -> Optional[str]:
    """The record's own spelling of ``field``, matched case-insensitively.
    Live, the model wrote ``outputTemplateJson`` for ``outputTemplateJSON``."""
    if field in rec:
        return field
    low = field.strip().lower()
    return next((k for k in rec if isinstance(k, str) and k.lower() == low), None)


def _jinja_fields(rec: dict) -> list[str]:
    return [k for k, v in rec.items()
            if isinstance(k, str) and not k.startswith("@")
            and isinstance(v, str) and _JINJA_MARK.search(v)]


def _pick_field(rec: dict, field: str, uuid: str) -> tuple[str, Optional[dict]]:
    """Which field of ``rec`` to render, or an error naming the choices.

    A template keeps its body in ``script``, which may be plain text; an
    automation action keeps several output templates and a ``script`` that is
    often not Jinja at all. Rendering ``script`` there tested the wrong thing."""
    found = _jinja_fields(rec)
    if field:
        name = _match_field(rec, field)
        if name is None:
            return "", {"ok": False, "code": "unknown_field",
                        "message": (f"record {uuid} has no field {field!r}. Fields holding "
                                    f"Jinja: {', '.join(found) or 'none'}."),
                        "jinja_fields": found}
        if not isinstance(rec.get(name), str) or not rec[name].strip():
            return "", {"ok": False, "code": "empty_script",
                        "message": f"{name} is empty on record {uuid}; nothing to render.",
                        "jinja_fields": found}
        return name, None
    if _SCRIPT_FIELD in found or (not found and isinstance(rec.get(_SCRIPT_FIELD), str)
                                  and rec[_SCRIPT_FIELD].strip()):
        return _SCRIPT_FIELD, None
    if len(found) == 1:
        return found[0], None
    if not found:
        return "", {"ok": False, "code": "empty_script",
                    "message": f"record {uuid} has no field holding Jinja to render"}
    return "", {"ok": False, "code": "choose_field",
                "message": (f"record {uuid} has {len(found)} fields holding Jinja: "
                            f"{', '.join(found)}. Pass `field` with the one to render "
                            "(call once per field to render several)."),
                "jinja_fields": found}


# Markdown table rows the template's whitespace trims glued onto one line:
# "| a | b || c | d |" or a header separator with cells after it.
_GLUED_ROW = re.compile(r"\|\|(?!\s*$)|\|\s*:?-{3,}:?\s*\|[^\n]*\|\s*[^\s|:-][^|\n]*\|")


def _markdown_findings(rendered: Any) -> list[dict]:
    """Table rows run together. Live, a Markdown output template's `{%- -%}`
    trims removed every newline between rows and the table did not render."""
    if not isinstance(rendered, str) or "|" not in rendered:
        return []
    bad = [i + 1 for i, ln in enumerate(rendered.splitlines()) if _GLUED_ROW.search(ln)]
    if not bad:
        return []
    return [{"severity": "warning", "code": "table_rows_run_together",
             "message": (f"rendered line(s) {bad[:5]} hold more than one table row; a "
                         "`-%}` or `{%-` whitespace trim removed the newline between "
                         "them. Each row must end the line.")}]


def _capped(rendered: Any) -> Any:
    if isinstance(rendered, str) and len(rendered) > _RENDERED_MAX:
        return (rendered[:_RENDERED_MAX]
                + f"\n... [{len(rendered) - _RENDERED_MAX} more characters not shown]")
    return rendered


def _json_findings(field: Optional[str], rendered: Any) -> list[dict]:
    """A JSON field's output must parse. Live, a JSON output template saved
    with a broken loop and nothing said so until the analyst saw it."""
    if not field or not field.lower().endswith("json") or not isinstance(rendered, str):
        return []
    try:
        json.loads(rendered)
        return []
    except ValueError as e:
        pos = getattr(e, "pos", 0) or 0
        near = rendered[max(0, pos - 60):pos + 60]
        return [{"severity": "error", "code": "rendered_not_json",
                 "message": (f"{field} rendered output that is not valid JSON: {e}. "
                             f"Around it: {near!r}")}]


def _undefined_findings(script: str, ctx: dict) -> list[dict]:
    """Values the template printed from something the vars lack.

    The runtime prints a missing value as empty, so the render looks clean and
    the output carries blanks or "undefined" (live: an interface table's
    `allowaccess`). Re-render offline with an Undefined that records what was
    printed. Guards like `{% if d.x %}` and `| default()` stay silent, unlike
    StrictUndefined, which raises on them. Fails silent: the live render
    already answered; this only adds what it could not see."""
    import jinja2
    seen: list[str] = []

    class _Recording(jinja2.Undefined):
        def __str__(self) -> str:
            name = self._undefined_name or "a value"
            if name not in seen:
                seen.append(name)
            return ""

    try:
        env = _jinja_env(_Recording)
        env.filters.update(_OFFLINE_FILTERS)
        env.from_string(script).render(ctx)
    except Exception:  # noqa: BLE001 - offline gaps etc.: nothing to add
        return []
    if not seen:
        return []
    return [{"severity": "warning", "code": "printed_undefined",
             "message": (f"the render printed {', '.join(repr(n) for n in seen[:10])} "
                         "but the example vars do not have it, so it came out empty. "
                         "Guard it with `is defined` or `| default(...)`, or fix the name.")}]


def test_template(module: str = "ztpf_templates", uuid: str = "",
                  script: Optional[str] = None,
                  context: Optional[dict] = None,
                  field: str = "") -> dict[str, Any]:
    """Render Jinja on FortiSOAR's own engine and lint it: any template field of a record (a template's script, an automation action's output templates, any field holding Jinja) against that record's example vars, or an unsaved body. Use it whenever the analyst asks to render, preview or test Jinja, and before saving a template edit: pass the edited body as ``script`` with the record's ``module`` and ``uuid`` so it renders against the record's example vars. Read-only.

    Three ways to call it:
      - ``{module, uuid}`` -- renders the record's Jinja field: ``script`` on a
        template, or the record's only field holding Jinja. A record with
        several (an automation action's output templates) needs ``field``.
      - ``{module, uuid, field}`` -- renders that field (the name is matched
        case-insensitively, e.g. ``outputTemplateJSON``).
      - ``{script}`` with ``{module, uuid}`` or ``{context}`` -- an unsaved
        body, against the record's example vars or the given context.

    It runs two checks and never writes anything:
      1. LINT -- an authoritative syntax check (FSR `{% do %}` / loopcontrols
         extensions enabled, so valid ztpf bodies aren't false-flagged) plus
         unknown-filter warnings from the full FSR + Ansible filter catalog.
      2. RENDER -- on the box, renders through FSR's own Jinja editor so every
         runtime filter (`regex_replace`, `ipaddr`, `b64encode`...) and `{% do %}`
         resolve (``render_via: "live"``). Off-box it falls back to a StrictUndefined
         offline env (``render_via: "offline"``) that surfaces undefined-variable
         refs; a runtime-only filter it can't resolve is reported as an
         ``offline_gap`` (render on the box), not a false render_error.
    A JSON field's output must parse as JSON, and a value the render printed
    from something missing in the vars is a finding.

    When called with ``uuid``, the fetched record is seeded into the render
    context as ``record`` (unless the example vars already set it), so a template
    referencing ``record.*`` resolves against the real record.

    Args:
      module: module the record lives in (default ``ztpf_templates``).
      uuid: the record's UUID (omit when passing ``script`` with ``context``).
      script: an unsaved Jinja body to render instead of the record's.
      context: render vars; overrides the record's ``exampleJinjaVars``.
      field: which of the record's fields holds the Jinja to render.

    Returns:
      ``{ok, field, findings, rendered, render_error, offline_gap, render_via,
      has_errors, note}``: ``findings`` is the lint list; ``rendered`` is the rendered output
      (or None on a render error/gap); ``render_error`` is a genuine render failure
      (or None); ``offline_gap`` is set when the offline fallback couldn't resolve a
      runtime-only filter (NOT a defect); ``render_via`` is ``"live"`` or
      ``"offline"``; ``has_errors`` is True only if a lint finding is severity
      ``error`` OR a genuine render_error occurred (an offline_gap does NOT set it).
      On a transport/arg problem: ``{ok: false, code, message}``; a record
      with several Jinja fields and no ``field`` returns ``code: choose_field``
      with ``jinja_fields``.
    """
    if not isinstance(module, str) or not module:
        return {"ok": False, "code": "missing_module",
                "message": "test_template requires a module name"}

    note: Optional[str] = None
    ctx: dict = {}
    ctx_warn: Optional[str] = None
    rendered_field: Optional[str] = None

    if script is not None and (not isinstance(script, str) or not script.strip()):
        return {"ok": False, "code": "empty_script",
                "message": "test_template requires a non-empty script"}
    if script is None and (not uuid or not isinstance(uuid, str)):
        return {"ok": False, "code": "missing_uuid",
                "message": "test_template needs a uuid (or a script to test)"}

    if uuid and isinstance(uuid, str):
        # The record is its own fixture: its example vars are the render
        # context, for its own Jinja and for an unsaved edit of it alike.
        rec, err = _read_record(module, uuid)
        if err is not None:
            return err
        rec = rec or {}
        if script is None:
            rendered_field, err = _pick_field(rec, field, uuid)
            if err is not None:
                return err
            script = rec[rendered_field]
        else:
            rendered_field = _match_field(rec, field) if field else None
        # Caller context wins over the record's example vars.
        if context is not None:
            ctx, ctx_warn = _coerce_context(context)
        else:
            ctx, ctx_warn = _coerce_context(rec.get(_VARS_FIELD))
        # Seed the grounding record so a template referencing `record.*` resolves
        # (the example vars usually cover data.*/input.* but not the record).
        if isinstance(ctx, dict) and "record" not in ctx:
            ctx = {**ctx, "record": rec}
    else:
        ctx, ctx_warn = _coerce_context(context)
    assert isinstance(script, str)

    ctx, filled, data_findings = _fill_from_sources(script, ctx)
    findings = _lint(script) + data_findings
    rendered, render_error, offline_gap, render_via = _render(script, ctx)
    findings += _cli_findings(rendered)
    if render_error is None and rendered is not None:
        findings += _json_findings(rendered_field, rendered)
        findings += _markdown_findings(rendered)
        findings += _undefined_findings(script, ctx)

    # A genuine render failure or a hard lint finding is an error; a runtime-only
    # filter gap is NOT (the template may be perfectly valid).
    has_errors = bool(render_error) or any(
        isinstance(f, dict) and f.get("severity") == "error" for f in findings)
    # Surface the bad-vars note and the offline gap together if both apply.
    note = "; ".join(n for n in (ctx_warn, offline_gap) if n) or None

    return {
        "ok": True,
        "module": module,
        "field": rendered_field,
        "findings": findings,
        "rendered": _capped(rendered),
        "render_error": render_error,
        "offline_gap": offline_gap,
        "render_via": render_via,
        "has_errors": has_errors,
        "note": note,
        "context_filled": filled or None,
    }
