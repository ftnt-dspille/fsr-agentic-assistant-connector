#!/usr/bin/env python
"""Copy records of a module from one FortiSOAR box to another over the API.

Purpose-built to sidestep the platform's config-export bug (a module whose
default grid sort is a picklist RELATIONSHIP -- e.g. `queueStatus.orderIndex` --
makes the record export emit invalid DQL: `ORDER BY t0 ASC, o.name`). This tool
pulls records with an EXPLICIT SCALAR sort we control, so no `t0` join is ever
generated, then POSTs them to the destination.

Division of labour: **modules** move via the native UI export/import (that half
works). This tool moves the **records** for modules that already exist on BOTH
boxes.

Usage:

    .venv/bin/python scripts/migrate_records.py \
        --src-env ../../fsr_all_widgets/fortisoar-widget-harness/.env.206 \
        --dst-env ../../fsr_all_widgets/fortisoar-widget-harness/.env.159 \
        --module ztpf_devices --dry-run

Drop --dry-run to actually write. Repeat --module (or comma-separate) for several.

By default the copy is CONSERVATIVE:
  • system/audit identity fields are stripped (@id, uuid, id, create/modify*).
  • picklist IRIs (`/api/3/picklists/...`) are KEPT -- their UUIDs match across
    boxes built from the same content.
  • record-to-record RELATIONSHIPS (IRIs into other modules) are DROPPED -- the
    source IRIs don't resolve on the destination. Use --keep-relationships to
    send them anyway (they'll error if the target records don't exist yet).

Flags:
    --src-env / --dst-env PATH   FSR .env files selecting the two boxes (REQUIRED)
    --module NAME[,NAME...]      module(s) to copy (REQUIRED; repeatable)
    --sort FIELD                 scalar sort field for the source pull (default createDate)
    --page-size N                records per page when paging the source (default 100)
    --limit N                    stop after N records per module (default: all)
    --keep-relationships         send record-to-record relationship IRIs too
    --keep-owners                keep owners/createUser/modifyUser (default: drop)
    --extra-strip F[,F...]       additional field names to drop
    --dry-run                    fetch + transform + print a sample, write nothing
"""
from __future__ import annotations

import argparse
import json

import urllib3

from pyfsr import EnvConfig

# Identity / audit fields that must never be POSTed to the destination.
SYSTEM_FIELDS = {
    "@id", "@type", "@context", "id", "uuid", "createDate", "modifyDate",
    "createUser", "modifyUser", "recordTags", "@self", "__self",
}
OWNER_FIELDS = {"owners", "assignedTo", "createUser", "modifyUser"}


def _is_picklist_iri(v) -> bool:
    if isinstance(v, str):
        return "/api/3/picklists/" in v
    if isinstance(v, dict):
        return "/api/3/picklists/" in str(v.get("@id", ""))
    return False


def _is_record_iri(v) -> bool:
    """A dict/str IRI pointing into a MODULE collection (not a picklist)."""
    if isinstance(v, str):
        return v.startswith("/api/3/") and "/api/3/picklists/" not in v
    if isinstance(v, dict) and "@id" in v:
        a = str(v["@id"])
        return a.startswith("/api/3/") and "/api/3/picklists/" not in a
    return False


def _clean_value(v, keep_rel: bool):
    """Return the value to send, or the sentinel _DROP to omit the field."""
    if _is_picklist_iri(v):
        # normalise to a bare IRI -- FortiSOAR accepts a picklist by @id.
        return v["@id"] if isinstance(v, dict) else v
    if isinstance(v, list):
        if v and all(_is_record_iri(x) or _is_picklist_iri(x) for x in v):
            if not keep_rel and any(_is_record_iri(x) and not _is_picklist_iri(x) for x in v):
                return _DROP
            return [(x["@id"] if isinstance(x, dict) and "@id" in x else x) for x in v]
        return v  # list of scalars / embedded JSON
    if _is_record_iri(v):
        if not keep_rel:
            return _DROP
        return v["@id"] if isinstance(v, dict) and "@id" in v else v
    return v  # scalar / embedded json dict


_DROP = object()


def _clean_record(rec: dict, keep_rel: bool, keep_owners: bool, extra_strip: set) -> dict:
    out = {}
    for k, v in rec.items():
        if k in SYSTEM_FIELDS or k in extra_strip:
            continue
        if not keep_owners and k in OWNER_FIELDS:
            continue
        cv = _clean_value(v, keep_rel)
        if cv is _DROP:
            continue
        out[k] = cv
    return out


def _module_exists(client, module: str) -> bool:
    """True if the module route exists. 'Access Denied' means the module EXISTS
    but the API user's role lacks permission on it -- treat as existing so the
    per-record POST surfaces the real (permission) error rather than aborting."""
    try:
        client.get(f"/api/3/{module}?$limit=1")
        return True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "No route found" in msg or "404" in msg:
            return False
        return True


def _iter_source(client, module: str, sort: str, page_size: int, limit, skip=0):
    """Yield source records, paged, ordered by a SCALAR field via the query
    endpoint. Sorting in the request body (not the module's default grid view)
    is what dodges the relationship-sort `t0` export bug -- we always sort on a
    scalar we choose (default createDate)."""
    body = {"sort": [{"field": sort, "direction": "ASC"}],
            "logic": "AND", "filters": []}
    page = 1
    seen = 0      # records yielded
    scanned = 0   # records walked (incl. skipped)
    while True:
        r = client.post(f"/api/query/{module}?$limit={page_size}&$page={page}",
                        data=body)
        members = r.get("hydra:member") or []
        if not members:
            break
        for rec in members:
            scanned += 1
            if scanned <= skip:
                continue
            yield rec
            seen += 1
            if limit and seen >= limit:
                return
        total = r.get("hydra:totalItems")
        if total is not None and page * page_size >= total:
            break
        page += 1


def _parse_remaps(spec: str) -> dict:
    """'field=parent_module:key,field2=mod2:key2' -> {field:(module,key)}."""
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        field, _, rhs = part.partition("=")
        pmod, _, key = rhs.partition(":")
        out[field.strip()] = (pmod.strip(), (key.strip() or "name"))
    return out


def _build_parent_lookup(dst, parent_module, key) -> dict:
    """{key_value: dest_IRI} for a parent module on the destination."""
    lut = {}
    body = {"logic": "AND", "filters": []}
    page = 1
    while True:
        r = dst.post(f"/api/query/{parent_module}?$limit=200&$page={page}", data=body)
        members = r.get("hydra:member") or []
        if not members:
            break
        for rec in members:
            k = rec.get(key)
            if k is not None and k not in lut:
                lut[k] = rec["@id"]
        total = r.get("hydra:totalItems")
        if total is not None and page * 200 >= total:
            break
        page += 1
    return lut


def migrate_module(src, dst, module, args) -> tuple[int, int, list]:
    extra = set(f.strip() for f in (args.extra_strip or "").split(",") if f.strip())
    remaps = _parse_remaps(getattr(args, "remap", "") or "")
    # Build dest parent lookups once per module.
    lookups = {field: _build_parent_lookup(dst, pmod, key)
               for field, (pmod, key) in remaps.items()}
    if remaps and not args.dry_run:
        for field, (pmod, key) in remaps.items():
            print(f"   remap {field} -> {pmod} by {key}: {len(lookups[field])} parents on dest")
    created = failed = 0
    errors = []
    sample_shown = False
    for rec in _iter_source(src, module, args.sort, args.page_size, args.limit,
                            skip=getattr(args, "skip", 0) or 0):
        payload = _clean_record(rec, args.keep_relationships, args.keep_owners, extra)
        # Re-attach remapped parent relationships by natural key.
        unresolved = None
        for field, (pmod, key) in remaps.items():
            srcval = rec.get(field)
            pkey = srcval.get(key) if isinstance(srcval, dict) else None
            dest_iri = lookups[field].get(pkey) if pkey is not None else None
            if dest_iri:
                payload[field] = dest_iri
            else:
                unresolved = f"{field}: no dest {pmod} matching {key}={pkey!r}"
                break
        name = payload.get("name") or rec.get("uuid", "?")
        if unresolved and not args.dry_run:
            failed += 1
            errors.append((name, unresolved))
            print(f"   ✗ {module} '{name}': {unresolved}")
            continue
        if args.dry_run:
            if not sample_shown:
                print(f"   [dry-run] sample cleaned payload for {module}:")
                print("   " + json.dumps(payload, indent=2)[:1200])
                sample_shown = True
            created += 1
            continue
        try:
            dst.post(f"/api/3/{module}", data=payload)
            created += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            msg = str(e)[:200]
            errors.append((name, msg))
            print(f"   ✗ {module} '{name}': {msg}")
    return created, failed, errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src-env", required=True)
    ap.add_argument("--dst-env", required=True)
    ap.add_argument("--module", required=True, action="append",
                    help="module name (repeatable or comma-separated)")
    ap.add_argument("--sort", default="createDate")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip", type=int, default=0,
                    help="skip the first N source records (resume/avoid dupes)")
    ap.add_argument("--keep-relationships", action="store_true")
    ap.add_argument("--keep-owners", action="store_true")
    ap.add_argument("--extra-strip", default="")
    ap.add_argument("--remap", default="",
                    help="re-attach parent relationships by natural key: "
                         "'field=parent_module:key[,...]' "
                         "(e.g. ztpfDevices=ztpf_devices:name)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    urllib3.disable_warnings()
    modules = [m.strip() for spec in args.module for m in spec.split(",") if m.strip()]

    src = EnvConfig.from_env_file(args.src_env).client(timeout=600)
    dst = EnvConfig.from_env_file(args.dst_env).client(timeout=600)

    print(f"src = {args.src_env}\ndst = {args.dst_env}\nmodules = {modules}\n")

    rc = 0
    for module in modules:
        print(f"{'='*60}\n  {module}\n{'='*60}")
        if not _module_exists(src, module):
            print(f"   ✗ SKIP: module '{module}' does not exist on SOURCE"); rc = 1; continue
        if not args.dry_run and not _module_exists(dst, module):
            print(f"   ✗ SKIP: module '{module}' does not exist on DESTINATION -- "
                  f"create it first (native UI module import works; only RECORD "
                  f"export is broken)."); rc = 1; continue
        created, failed, _ = migrate_module(src, dst, module, args)
        verb = "would copy" if args.dry_run else "copied"
        print(f"   → {verb} {created}, failed {failed}")
        if failed:
            rc = 1

    print("\nDONE" + (" (dry-run -- nothing written)" if args.dry_run else ""))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
