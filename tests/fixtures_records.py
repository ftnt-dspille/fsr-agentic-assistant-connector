"""Record fixtures for the PL harness grounded-triage path.

A realistic-enough FortiSOAR record so `triage_preflight` (fetch → normalize →
classify → scenario) runs end-to-end in-process against a cassette, without a
box. Kept deterministic (fixed ids/timestamps) so the assembled grounded prompt
is stable enough to golden.
"""
from __future__ import annotations


def incident_beaconing(uuid: str = "11111111-2222-3333-4444-555555555555") -> dict:
    """An external-C2 / beaconing incident: an internal host talking to a
    suspicious external IP. A natural target for a hunt/enrich ask (the T2 case)."""
    iri = f"/api/3/incidents/{uuid}"
    return {
        "@id": iri,
        "@type": "Incident",
        "uuid": uuid,
        "id": 4242,
        "name": "Possible C2 beaconing from 10.20.30.40",
        "description": ("Repeated outbound connections from host WIN-DEV-07 "
                        "(10.20.30.40) to external 203.0.113.66 on a fixed "
                        "interval — possible command-and-control beaconing."),
        "severity": {"itemValue": "High"},
        "status": {"itemValue": "Open"},
        "source": "FortiSIEM",
        "sourceData": {
            "incident_data": {"incidentId": "980123"},
            "srcIp": "10.20.30.40",
            "destIp": "203.0.113.66",
            "hostName": "WIN-DEV-07",
            "user": "j.doe",
            "eventType": "PH_RULE_C2_BEACON",
        },
        "sourcedata": {
            "incident_data": {"incidentId": "980123"},
            "srcIp": "10.20.30.40",
            "destIp": "203.0.113.66",
            "hostName": "WIN-DEV-07",
            "user": "j.doe",
        },
        "createDate": 1700000000,
        "modifyDate": 1700000600,
    }


def cassette_rule_for(record: dict) -> tuple[str, dict]:
    """(url-substring, body) rule that serves this record for both the preflight
    fetch and any get_record the model makes (`/api/3/<module>/<uuid>`)."""
    return (record["@id"], record)


# --- T2 reproduction: a critical botnet blast radius ----------------------
# The T2 defect (triage self-assigns containment on a HUNT ask) only fires when
# the hunt surfaces something CRITICAL — the model then reasonably reaches for
# find_containment_actions, and with containment unconfigured the imperative
# machinery (system_prompt_triage.md L100-132) forces the turn to close on a
# containment capability_gap card instead of delivering the findings. So the
# repro must serve a populated blast radius, not an empty one.

def _related_botnet_alerts(c2_ip: str = "203.0.113.66") -> list[dict]:
    hosts = ["WIN-DEV-07", "WIN-FIN-02", "WIN-HR-11", "SRV-DB-01",
             "WIN-DEV-19", "LNX-BUILD-03"]
    out = []
    for i, h in enumerate(hosts):
        out.append({
            "@id": f"/api/3/alerts/aaaa{i:04d}-0000-0000-0000-000000000000",
            "@type": "Alert",
            "name": f"C2 beacon to {c2_ip} from {h}",
            "severity": {"itemValue": "High"},
            "status": {"itemValue": "Open"},
            "hostName": h,
            "sourcedata": {"srcIp": f"10.20.30.{40 + i}", "destIp": c2_ip,
                           "hostName": h},
        })
    return out


def _hydra(members: list[dict]) -> dict:
    return {"hydra:member": members, "hydra:totalItems": len(members)}


def t2_blast_radius_rules(record: dict) -> list[tuple[str, dict]]:
    """Cassette rules that reproduce the T2 scenario: the mounted incident PLUS
    a populated blast radius returned by search_module_records on BOTH modules
    (`GET /api/3/alerts?…` / `/api/3/incidents?…`). The record fetch
    (`/api/3/incidents/<uuid>?…`) is served by the record rule (checked first;
    its '/incidents/<uuid>' substring differs from the '/incidents?' search).
    find_containment_actions is left to return nothing (offline catalog), which
    is what forces the capability_gap drift."""
    alerts = _related_botnet_alerts()
    return [
        cassette_rule_for(record),
        ("/api/3/alerts?", _hydra(alerts)),
        ("/api/3/incidents?", _hydra(alerts[:2])),
    ]
