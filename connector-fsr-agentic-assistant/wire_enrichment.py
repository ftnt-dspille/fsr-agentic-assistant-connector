"""IOC / threat-intel enrichment card synthesis.

Extracted verbatim from operations.py: pure, self-contained transcript →
``ioc_card`` conversion (dict in, dict out -- no platform I/O). ``operations``
re-imports ``_ioc_extract`` / ``_ioc_cards`` from here; the rest are internal.
"""
from __future__ import annotations

from typing import Any


# (connector-name substring, op-name) → IOC enrichment extractor. Covers the
# demo threat-intel connectors; unknown enrichment ops are simply not carded.
# FortiGuard web-filter categories that are a verdict on their own.
_FGD_MALICIOUS_WF = frozenset({"Malicious Websites", "Phishing"})


def _ioc_extract(connector: str, op: str, params: dict,
                 content: dict) -> dict | None:
    """Pull a normalized enrichment record from a successful enrichment
    `run_op` result. Returns {indicator, source, verdict, score, score_str,
    severity, tags[], rows[]} or None when this op isn't a known enricher."""
    if not content.get("ok"):
        return None
    data = content.get("data")
    # data may be a dict (VT/Shodan/AbuseIPDB) or a list (FortiGuard).
    if not isinstance(data, (dict, list)) or not data:
        return None
    # The recorded tool-input `params` may be the raw JSON STRING the model sent
    # (run_op accepts a stringified object; see the dispatch-layer coercion), so
    # this card builder must tolerate it rather than crash on `.get` (a live
    # enrich-then-block turn aborted finalization here with 'str' has no 'get').
    if isinstance(params, str):
        import json as _json
        try:
            parsed = _json.loads(params)
        except (ValueError, TypeError):
            parsed = None
        params = parsed if isinstance(parsed, dict) else {}
    elif not isinstance(params, dict):
        params = {}
    ind = (params.get("ip") or params.get("ip_address") or params.get("url")
           or params.get("domain") or params.get("file_hash")
           or params.get("hash") or params.get("indicator")
           or params.get("indicators") or params.get("value"))
    if isinstance(ind, (list, tuple)):
        ind = ind[0] if ind else None
    ind = str(ind) if ind else "indicator"
    cn, opn = connector.lower(), op.lower()

    if "virustotal" in cn and opn.startswith("query") and isinstance(data, dict):
        # The raw VT API wraps the object once more ({"data": {"attributes"}});
        # the summarized run_op result is flat. Read both.
        if "attributes" not in data and isinstance(data.get("data"), dict):
            data = data["data"]
        attrs = data.get("attributes") or {}
        stats = attrs.get("last_analysis_stats") or {}
        mal = int(stats.get("malicious") or 0)
        total = sum(int(v or 0) for v in stats.values()
                    if isinstance(v, (int, float))) or 0
        if not total:
            # No engine results is no rating, not "clean": an unread shape
            # once rated a 47-engine C2 address clean, and the autonomy policy
            # reads this verdict.
            return None
        sev = "error" if mal >= 3 else "warning" if mal else "ok"
        rows = [{"label": "Type", "value": data.get("type") or "--"}]
        if attrs.get("country"):
            rows.append({"label": "Country", "value": attrs["country"]})
        if attrs.get("as_owner"):
            rows.append({"label": "ASN", "value": attrs["as_owner"]})
        return {"indicator": ind, "source": "VirusTotal",
                "verdict": "malicious" if mal else "clean",
                "score": (round(100 * mal / total) if total else None),
                "score_str": f"{mal}/{total}" if total else "--", "severity": sev,
                "tags": list(attrs.get("tags") or []), "rows": rows}

    if "shodan" in cn and "ip" in opn and isinstance(data, dict):
        hosts = data.get("hostnames") or []
        is_tor = any("tor-exit" in str(h).lower() for h in hosts)
        tags = list(data.get("tags") or [])
        if is_tor:
            tags.append("Tor exit node")
        rows = []
        if data.get("org"):
            rows.append({"label": "Org", "value": data["org"]})
        if data.get("country_name"):
            rows.append({"label": "Country", "value": data["country_name"]})
        ports = data.get("ports") or []
        if ports:
            rows.append({"label": "Open ports", "value": str(len(ports))})
        return {"indicator": ind, "source": "Shodan",
                "verdict": "tor exit" if is_tor else "seen",
                "score": None, "score_str": "--",
                "severity": "warning" if is_tor else "info",
                "tags": tags, "rows": rows}

    if "abuseipdb" in cn and isinstance(data, dict):
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        conf = int(d.get("abuseConfidenceScore") or 0)
        sev = "error" if conf >= 75 else "warning" if conf >= 25 else "ok"
        rows = []
        if d.get("countryCode"):
            rows.append({"label": "Country", "value": d["countryCode"]})
        if d.get("totalReports") is not None:
            rows.append({"label": "Reports", "value": str(d.get("totalReports"))})
        return {"indicator": ind, "source": "AbuseIPDB",
                "verdict": "abusive" if conf >= 25 else "clean",
                "score": conf, "score_str": f"{conf}%", "severity": sev,
                "tags": (["Tor"] if d.get("isTor") else []), "rows": rows}

    if ("alienvault" in cn or "otx" in cn) and isinstance(data, dict) \
            and isinstance(data.get("general"), dict):
        return _otx_record(ind, data["general"])

    if "fortiguard" in cn:
        # data is either the bare record list or {"data":[...]} (post-summary).
        if isinstance(data, list):
            recs = data
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            recs = data["data"]
        else:
            recs = None
        rec0 = (recs[0] if recs else (data if isinstance(data, dict) else {})) or {}
        # The IOC-search record carries threatinfo/riskinfo/geoip/asn at the TOP
        # level; older threat-intel-feed responses nested them under `values`.
        # Fall back to the record itself so both shapes resolve.
        vals = rec0.get("values") or rec0
        ti = vals.get("threatinfo") or {}
        ri = vals.get("riskinfo") or {}
        score = ti.get("risk_score")
        if score is None:
            score = ri.get("risk_score")
        if score is None:
            score = rec0.get("risk_score")
        score = int(score) if isinstance(score, (int, float)) else None
        sv = score or 0
        sev = "error" if sv >= 75 else "warning" if sv >= 25 else "ok"
        verdict = ("malicious" if sv >= 75 else "suspicious" if sv >= 25
                   else "clean")
        if score is None:
            # threat_intel_search carries no score, only FortiGuard's own
            # categories -- and a missing score read as 0 called an address
            # FortiGuard files under "Malicious Websites" with C2 tags "clean",
            # so the autonomy policy refused a confirmed block. Rate it from
            # the categories instead: a malicious web-filter category is
            # strong; IOC/spam/AV hits alone are weak (a Microsoft address
            # carries stale "Zbot" IOC tags at Low confidence); a rated benign
            # category is clean; "Not Rated" with nothing else is unrated.
            wf_cat = str(ti.get("wf_cate") or rec0.get("wf_cate") or "")
            hits = (rec0.get("ioc_tags") or rec0.get("ioc_cate")
                    or rec0.get("spam_cates") or rec0.get("av_cate"))
            if wf_cat in _FGD_MALICIOUS_WF:
                sev, verdict = "error", "malicious"
            elif hits:
                sev, verdict = "warning", "suspicious"
            elif wf_cat and wf_cat != "Not Rated":
                sev, verdict = "ok", "clean"
            else:
                sev, verdict = "info", "unrated"
        # Prefer the record's own tags (recordTags/ioc_tags), then the
        # threatinfo tags; de-dupe while preserving order.
        tags = list(rec0.get("recordTags") or ti.get("ioc_tags")
                    or rec0.get("ioc_tags") or [])
        wf = ti.get("wf_cate") or rec0.get("wf_cate")
        if wf and wf not in tags:
            tags.append(wf)
        rows = []
        geo = vals.get("geoip") or {}
        if geo.get("country"):
            rows.append({"label": "Country", "value": geo["country"]})
        asinfo = (vals.get("asn") or {}).get("as_info") or {}
        if asinfo.get("organization"):
            rows.append({"label": "ASN", "value": asinfo["organization"]})
        if ti.get("confidence"):
            rows.append({"label": "Confidence", "value": ti["confidence"]})
        # No threat signal from this FortiGuard feed → skip the row rather than
        # assert a misleading "clean" (the IOC feed vs threat-intel feed return
        # different shapes; an empty one shouldn't contradict a real hit).
        if score is None and not tags and not rows:
            return None
        return {"indicator": ind, "source": "FortiGuard", "verdict": verdict,
                "score": score, "score_str": (f"{score}/100" if score is not None
                                              else "--"),
                "severity": sev, "tags": tags, "rows": rows}

    # Generic fallback -- ANY indicator-lookup against an unrecognized connector
    # still contributes a row, so no enrichment source is silently dropped.
    if any(h in opn for h in _ENRICH_OP_HINTS):
        return _generic_enrichment_record(connector, ind, data)
    return None


def _otx_record(ind: str, general: dict) -> dict:
    """AlienVault OTX indicator lookup (`general` block of the reputation
    result). Verdict from the pulse count, unless OTX itself validated the
    indicator as a known false positive or as whitelisted; those are shown as
    such, and a high pulse count then reads as a warning, not an error."""
    pulse = general.get("pulse_info") if isinstance(general.get("pulse_info"), dict) else {}
    count = int(pulse.get("count") or 0)
    validation = [v for v in (general.get("validation") or []) if isinstance(v, dict)]
    fp = (any(v.get("source") == "false_positive" for v in validation)
          or any(isinstance(f, dict) and f.get("assessment") == "accepted"
                 for f in (general.get("false_positive") or [])))
    allowlisted = fp or any(v.get("source") == "whitelist" for v in validation)
    if allowlisted:
        verdict = "known false positive" if fp else "allowlisted"
        sev = "warning" if count >= 3 else "ok"
    elif count >= 3:
        verdict, sev = "malicious", "error"
    elif count:
        verdict, sev = "suspicious", "warning"
    else:
        verdict, sev = "clean", "ok"
    tags: list[str] = []
    for v in validation:
        name = v.get("name") or v.get("message")
        if isinstance(name, str) and name and name not in tags:
            tags.append(name)
    related = pulse.get("related") if isinstance(pulse.get("related"), dict) else {}
    for family in ((related.get("alienvault") or {}).get("malware_families") or []):
        if isinstance(family, str) and family and family not in tags:
            tags.append(family)
    rows = []
    if general.get("country_name"):
        rows.append({"label": "Country", "value": general["country_name"]})
    if general.get("asn"):
        rows.append({"label": "ASN", "value": general["asn"]})
    rows.append({"label": "Pulses", "value": str(count)})
    rep = general.get("reputation")
    if isinstance(rep, (int, float)) and rep:
        rows.append({"label": "OTX reputation", "value": str(int(rep))})
    return {"indicator": ind, "source": "AlienVault OTX", "verdict": verdict,
            "score": None, "score_str": f"{count} pulse" + ("" if count == 1 else "s"),
            "severity": sev, "tags": tags[:6], "rows": rows}


# Op-name fragments that mark an indicator lookup / enrichment call.
_ENRICH_OP_HINTS = ("search", "query", "lookup", "reputation", "ioc", "check",
                    "enrich", "get_ip", "get_file", "get_url", "get_domain",
                    "get_indicator", "investigate")
# Common field names where connectors stash a 0-100 risk/confidence score.
_SCORE_KEYS = ("risk_score", "score", "threat_score", "confidence_score",
               "abuseconfidencescore", "reputation", "risk", "confidence")
# Field names that carry a textual THREAT verdict.
#
# `status` and `result` were once in this list and were the single worst source
# of nonsense in the IOC card: every native-MCP payload is wrapped as
# `{status:"success", result, error}`, and every FortiSOAR record carries a
# workflow `status` ("Open", "Closed"). The scan below is depth-first over the
# whole payload, so it found those long before any real signal and rendered
# "Verdict: success" / "Verdict: Open" against an indicator. A transport status
# and a case state are not threat verdicts -- never treat them as one.
_VERDICT_KEYS = ("verdict", "threat_level", "classification", "disposition",
                 "category", "malicious", "reputation")
_TAG_KEYS = ("tags", "ioc_tags", "categories", "labels")


def _prettify_connector(name: str) -> str:
    n = name
    for pre in ("fortinet-", "cyops-"):
        if n.startswith(pre):
            n = n[len(pre):]
    return " ".join(p.capitalize() for p in n.replace("_", "-").split("-"))


def _generic_enrichment_record(connector: str, ind: str, data: Any) -> dict | None:
    """Best-effort verdict/score/tags from an unknown enrichment payload by
    scanning common field names (bounded depth).

    Returns None when the payload carried NO threat signal at all (no score, no
    verdict, no tags). A card reading `Verdict: checked / Score: --` asserts
    nothing an analyst can act on while looking exactly like a real finding --
    the same reason the FortiGuard extractor above bails on an empty feed."""
    score = None
    verdict = None
    tags: list = []

    def scan(o, depth=0):
        nonlocal score, verdict, tags
        if depth > 3:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                # MAX across every score-named field, not first-found: a
                # payload often carries several ("reputation": 0 meaning
                # "not scored" next to a real risk_score). Depth-first
                # first-match once rendered "Risk 0/100" on an IP the same
                # payload's text called malicious (live, 2026-08-17).
                # Only 0-100 values are risk scores. VirusTotal's raw
                # `reputation` is community votes (negative = bad); scraped
                # as-is it rendered "Risk -12 / ok" against a live C2 IP.
                if (kl in _SCORE_KEYS and isinstance(v, (int, float))
                        and 0 <= v <= 100):
                    score = int(v) if score is None else max(score, int(v))
                # A VT-shaped analysis-stats block is a REAL threat signal;
                # ground the verdict in it rather than the generic keys.
                if kl == "last_analysis_stats" and isinstance(v, dict):
                    mal = int(v.get("malicious") or 0)
                    total = sum(int(x or 0) for x in v.values()
                                if isinstance(x, (int, float)))
                    if total:
                        vt_score = round(100 * mal / total)
                        score = vt_score if score is None else max(score, vt_score)
                        if verdict is None or verdict == "clean":
                            verdict = "malicious" if mal else "clean"
                if verdict is None and kl in _VERDICT_KEYS and isinstance(v, str) and v:
                    verdict = v
                if kl in _TAG_KEYS and isinstance(v, list):
                    tags.extend(str(t) for t in v if isinstance(t, (str, int)))
                scan(v, depth + 1)
        elif isinstance(o, list):
            for x in o[:5]:
                scan(x, depth + 1)

    scan(data)
    if score is None and not verdict and not tags:
        return None
    if not verdict and not tags and score == 0:
        # A lone zero from an unknown field is "not scored", not "clean".
        # Asserting Risk 0/100 against an indicator the prose may call
        # malicious is worse than no card at all.
        return None
    sv = score or 0
    if score is not None:
        sev = "error" if sv >= 75 else "warning" if sv >= 25 else "ok"
    else:
        sev = "info"
    # The verdict can only RAISE severity, never lower it: a "malicious"
    # verdict with a low engine ratio still rendered "ok" (live C2 IP,
    # 2026-08-18) because the score branch never consulted the verdict.
    if verdict and any(w in verdict.lower() for w in
                       ("malic", "abus", "high", "critical", "bad")):
        sev = "error"
    elif verdict and any(w in verdict.lower() for w in ("susp", "medium", "warn")):
        sev = sev if sev == "error" else "warning"
    return {"indicator": ind, "source": _prettify_connector(connector),
            "verdict": (verdict or "checked")[:24],
            "score": score,
            "score_str": (f"{score}" if score is not None else "--"),
            "severity": sev, "tags": tags[:6],
            "rows": []}


_SEV_RANK = {"info": 0, "ok": 1, "warning": 2, "error": 3}

# Input-param keys where the indicator(s) a native-MCP investigation tool was
# called on live. Ordered widest-net first; the value may be a scalar or list.
_MCP_INDICATOR_KEYS = ("values", "indicators", "ip", "ip_address", "entity",
                       "value", "url", "domain", "file_hash", "hash",
                       "indicator", "indicator_value", "host", "hostname")


# Native-MCP tool-name fragments that mark a genuine INDICATOR-REPUTATION
# lookup. Every `mcp_*` result used to be carded, so pivots like
# `get_alerts_linked_to_indicators` (a record search -- it returns alerts, not
# reputation) rendered as an IOC verdict card, one bogus row per indicator.
# A pivot's output belongs in prose or a record card, never in a verdict table.
_MCP_ENRICH_NAME_HINTS = ("enrich", "reputation", "indicator_detail",
                          "get_indicators", "lookup", "threat_intel",
                          "ioc", "whois", "geoip")
# ...unless the name says it is a pivot to OTHER records keyed by an indicator.
_MCP_PIVOT_NAME_HINTS = ("alerts_linked", "incidents_linked", "linked_to",
                         "related_records", "get_alerts", "get_incidents",
                         "by_entity")


def _is_mcp_enrichment_tool(tool_name: str) -> bool:
    n = tool_name.lower()
    if any(h in n for h in _MCP_PIVOT_NAME_HINTS):
        return False
    return any(h in n for h in _MCP_ENRICH_NAME_HINTS)


def _prettify_mcp(tool_name: str) -> str:
    """Human 'Source' label for a native MCP tool: `mcp_soc__get_indicators`
    → 'SOC (MCP)', `mcp_fortisiem__get_incidents_by_entity` → 'FortiSIEM (MCP)'."""
    body = tool_name[4:] if tool_name.startswith("mcp_") else tool_name
    surface = body.split("__", 1)[0] if body else tool_name
    fixed = {"soc": "SOC", "fortisiem": "FortiSIEM", "fmg": "FortiManager",
             "faz": "FortiAnalyzer"}
    label = fixed.get(surface.lower()) or surface.replace("_", " ").title()
    return f"{label} (MCP)"


def _native_mcp_ioc_records(tool_name: str, inp, content: dict) -> list[dict]:
    """Enrichment records from a native MCP investigation tool
    (`mcp_soc__*` / `mcp_fortisiem__*` / …). These wrap output as
    ``{status:"success", result, error:null}`` -- NO ``ok`` field, empty
    connector/op -- so `_ioc_extract` (which reads the connector's own `run_op`
    envelope) can't see them. Without this, an agent that enriches via native
    MCP produces a correct investigation but NO ioc_card deliverable. Reuses
    `_generic_enrichment_record` (best-effort score/verdict/tags scan) so any
    result shape yields a card, one per indicator the tool was called on."""
    if not isinstance(content, dict):
        return []
    if not _is_mcp_enrichment_tool(tool_name):
        return []
    # Success only. Native envelope: status=="success" & error null. Tolerate a
    # bare payload with neither field (still treat as data).
    status = content.get("status")
    if status is not None and status != "success":
        return []
    if content.get("error"):
        return []
    data = content.get("result")
    if data is None:
        data = content.get("data")
    # The bridge can DOUBLE-wrap: {status, result: {status, result: {...}}}
    # (live on mcp_soc__enrich_indicator, 2026-08-18). Unwrap envelope layers
    # or the scan spends its bounded depth on transport keys and misses the
    # actual reputation payload underneath.
    for _ in range(3):
        if (isinstance(data, dict) and "result" in data
                and ("status" in data or "error" in data)):
            if data.get("status") not in (None, "success") or data.get("error"):
                return []
            data = data["result"]
        else:
            break
    if not isinstance(data, (dict, list)) or not data:
        return []
    if isinstance(inp, str):
        import json as _json
        try:
            parsed = _json.loads(inp)
        except (ValueError, TypeError):
            parsed = None
        inp = parsed if isinstance(parsed, dict) else {}
    elif not isinstance(inp, dict):
        inp = {}
    # Native MCP tools take their arguments under a `params` wrapper
    # ({'params': {'indicator_value': ...}}); without unwrapping, the card
    # labels the row "indicator" instead of the IP that was looked up.
    if isinstance(inp.get("params"), dict):
        inp = inp["params"]
    inds = None
    for k in _MCP_INDICATOR_KEYS:
        if inp.get(k):
            inds = inp[k]
            break
    if isinstance(inds, (str, int)):
        inds = [inds]
    if not isinstance(inds, list) or not inds:
        # No indicator in the params -- card it once against a generic label so
        # the deliverable still lands (e.g. an entity lookup keyed elsewhere).
        inds = ["indicator"]
    source = _prettify_mcp(tool_name)
    out: list[dict] = []
    seen: set[str] = set()
    for ind in inds:
        ind_s = str(ind) if ind else "indicator"
        if ind_s in seen:
            continue
        seen.add(ind_s)
        rec = _generic_enrichment_record(source, ind_s, data)
        if rec is None:  # no threat signal -- don't fabricate a verdict row
            continue
        rec["source"] = source  # override the connector-prettified label
        out.append(rec)
    return out


def _ioc_cards(wired: list[dict]) -> list[tuple[int, dict]]:
    """Aggregate enrichment results across the transcript into one ioc_card
    per indicator (combined score/tags + per-source table). Returns
    (insert_after_index, card) pairs positioned after each indicator's last
    contributing tool_result so the card rides the same turn."""
    name_by_id = {w.get("id"): (w.get("name"), w.get("input") or {})
                  for w in wired if w.get("type") == "tool_use"}
    # indicator → {records[], last_idx, type}
    agg: dict[str, dict] = {}
    for i, w in enumerate(wired):
        if w.get("type") != "tool_result":
            continue
        nm, inp = name_by_id.get(w.get("tool_use_id"), (None, {}))
        content = w.get("content")
        if not isinstance(content, dict):
            continue
        recs_here: list[dict] = []
        if nm == "run_op":
            connector = inp.get("connector") or ""
            op = inp.get("op") or inp.get("operation") or ""
            rec = _ioc_extract(connector, op, inp.get("params") or {}, content)
            if rec is not None:
                recs_here.append(rec)
        elif isinstance(nm, str) and nm.startswith("mcp_"):
            recs_here.extend(_native_mcp_ioc_records(nm, inp, content))
        for rec in recs_here:
            slot = agg.setdefault(rec["indicator"], {"records": [], "last_idx": i})
            slot["records"].append(rec)
            slot["last_idx"] = i

    out: list[tuple[int, dict]] = []
    for n, (ind, slot) in enumerate(agg.items()):
        recs = slot["records"]
        worst = max(recs, key=lambda r: _SEV_RANK.get(r["severity"], 0))
        sev = worst["severity"]
        verdict = worst["verdict"]
        tags = []
        for r in recs:
            for t in r["tags"]:
                if t and t not in tags:
                    tags.append(t)
        blocks: list[dict] = []
        scored = [r for r in recs if r.get("score") is not None]
        if scored:
            top = max(scored, key=lambda r: r["score"])
            blocks.append({"kind": "score", "label": "Risk", "value": top["score"],
                           "max": 100, "severity": top["severity"]})
        if tags:
            blocks.append({"kind": "tags", "tags": tags[:10]})
        # merge key/value rows (dedupe by label, first wins)
        seen_lbl, merged_rows = set(), []
        for r in recs:
            for row in r["rows"]:
                if row["label"] not in seen_lbl:
                    seen_lbl.add(row["label"])
                    merged_rows.append(row)
        if merged_rows:
            blocks.append({"kind": "rows", "rows": merged_rows})
        blocks.append({"kind": "table", "columns": ["Source", "Verdict", "Score"],
                       "rows": [[r["source"], r["verdict"], r["score_str"]]
                                for r in recs]})
        card = {"type": "info_card", "id": f"ioc-{n}", "variant": "ioc_enrichment",
                "severity": sev,
                "title": f"IOC: {ind} -- {verdict.title()}", "blocks": blocks}
        out.append((slot["last_idx"], card))
    return out
