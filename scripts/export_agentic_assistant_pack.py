#!/usr/bin/env python3
"""Export ONE FortiSOAR file containing the Agentic Assistant connector + widget + dashboard.

The three artifacts normally ship separately -- the connector as its own tarball,
the widget as a `.tgz`, the dashboard only as whatever you hand-exported. A
customer then has three imports to sequence and get right. This builds a single
**solution pack** export containing all three, so the install is one file.

    python scripts/export_agentic_assistant_pack.py --env <path/to/.env> [-o out.zip]

    # what would go in the pack, without touching the box:
    python scripts/export_agentic_assistant_pack.py --env <...> --dry-run

The dashboard goes in **by uuid** (the export engine takes `options.dashboards`
verbatim -- no name lookup), so this resolves the uuid from the dashboard title
first and fails loudly with the available titles if it cannot. That endpoint is
`/api/3/dashboard` -- SINGULAR; `/api/3/dashboards` 404s.

Connector **configurations are excluded by default** -- they carry encrypted
credential blobs, an oauth client_secret and internal hostnames, none of which
belong in a pack you hand to someone else. `--with-configurations` opts in for a
same-tenant backup.

Leaves no residue: the export template it creates is deleted afterwards unless
--keep-template is passed.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")  # box certs are self-signed

# Defaults describe the shipped Agentic Assistant surface.
#
# The connector is its info.json **label**, NOT its `name`. `add_connector`
# resolves through Content Hub and then asserts
# `connector["label"] == the string you passed` -- so "connector-fsr-agentic-assistant"
# finds the right record and is then REJECTED as not-found, which reads like the
# connector is missing from the box when it is really a name-vs-label mismatch.
CONNECTOR_LABEL = "FSR Agentic Assistant"
# The widgets, by contrast, ARE their info.json `name` (no lookup; verbatim).
#
# TWO of them, and the second is easy to miss: the dashboard renders
# `agenticAssistantMonitor`, so a pack with only the assistant imports "successfully"
# and then shows an empty dashboard. `check_dashboard_widgets()` below derives
# the requirement from the dashboard itself so this cannot be forgotten again.
WIDGET_NAMES = ["agenticAssistant", "agenticAssistantMonitor"]
DASHBOARD_TITLE = "Agentic Assistant Monitor"

DASHBOARD_ENDPOINT = "/api/3/dashboard"  # singular; the plural form 404s


def resolve_dashboard_uuid(client, title: str) -> str:
    """Map a dashboard title to its uuid, or fail with the real candidates.

    An invented uuid would export an EMPTY dashboards list without erroring, so
    this refuses rather than guessing -- and hands back the list it did see,
    which is the affordance that makes a typo self-correcting."""
    resp = client.get(DASHBOARD_ENDPOINT, params={"$limit": 200})
    members = resp.get("hydra:member", []) if isinstance(resp, dict) else []
    by_title = {}
    for d in members:
        # The list payload has no `name`; the human-facing key is `title`.
        t = d.get("title") or d.get("label") or d.get("displayName")
        if t:
            by_title[t] = d.get("uuid")
    if title in by_title:
        return by_title[title]
    raise SystemExit(
        f"dashboard {title!r} not found on this box.\nAvailable:\n  "
        + "\n  ".join(sorted(by_title)))


def check_dashboard_widgets(client, uuid: str, included: list[str]) -> list[str]:
    """Return the widgets the dashboard renders but the pack does not carry.

    The dashboard config nests `rows[].columns[].widgets[].type`, and each type
    is `<widgetName>-<version>` (e.g. "agenticAssistantMonitor-1.0.4"). A missing
    widget is NOT an import error -- the dashboard installs and then renders
    blank -- so this is the only place the omission can be caught."""
    dash = client.get(f"{DASHBOARD_ENDPOINT}/{uuid}")
    needed = set()
    for row in (dash.get("config") or {}).get("rows") or []:
        for col in row.get("columns") or []:
            for w in col.get("widgets") or []:
                wtype = w.get("type") or ""
                if wtype:
                    # strip the trailing -<version>; widget names may contain '-'
                    needed.add(wtype.rsplit("-", 1)[0])
    return sorted(needed - set(included))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True, help="path to a .env with FSR_BASE_URL etc")
    ap.add_argument("-o", "--output", default=None, help="output path for the export file")
    ap.add_argument("--connector", default=CONNECTOR_LABEL,
                    help="connector LABEL (not its name -- see module docstring)")
    ap.add_argument("--widget", action="append", dest="widgets", default=None,
                    help="widget name; repeatable. Defaults to the full set.")
    ap.add_argument("--dashboard", default=DASHBOARD_TITLE,
                    help="dashboard TITLE (resolved to a uuid)")
    ap.add_argument("--dashboard-uuid", default=None,
                    help="skip title lookup and use this uuid verbatim")
    ap.add_argument("--label", default="Agentic Assistant")
    ap.add_argument("--version", default="1.0.0")
    # DEFAULT-EXCLUDE, deliberately. A pack built for distribution must not
    # carry the exporting box's connector configurations: they contain
    # FortiSOAR-encrypted credential blobs (llm/soar api keys), an oauth
    # `client_secret`, and internal lab hostnames. Verified on a real export
    # before this default was chosen -- `include_configurations=True` produced
    # all of the above. Opt back in only for a same-tenant backup/restore.
    ap.add_argument("--with-configurations", action="store_true",
                    help="INCLUDE saved connector configurations. These carry "
                         "credentials and internal hostnames -- never for a pack "
                         "you intend to hand to someone else.")
    ap.add_argument("--keep-template", action="store_true",
                    help="leave the export template on the box for inspection")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the pack definition and exit without exporting")
    args = ap.parse_args()

    from pyfsr import FortiSOAR
    from pyfsr.api.export_config import SolutionPackBuilder

    client = FortiSOAR.from_env_file(args.env)

    dash_uuid = args.dashboard_uuid or resolve_dashboard_uuid(client, args.dashboard)

    widgets = args.widgets or list(WIDGET_NAMES)

    pack = SolutionPackBuilder(args.label, version=args.version)
    pack.add_connector(args.connector,
                       include_configurations=args.with_configurations)
    for w in widgets:
        pack.add_widget(w)
    pack.add_dashboard(dash_uuid).tags("Agentic AI")

    print(f"connector : {args.connector}"
          + ("  (configurations INCLUDED -- contains credentials)"
             if args.with_configurations else "  (configurations excluded)"))
    for w in widgets:
        print(f"widget    : {w}")
    print(f"dashboard : {args.dashboard}  ({dash_uuid})")

    missing = check_dashboard_widgets(client, dash_uuid, widgets)
    if missing:
        raise SystemExit(
            f"\nREFUSING: the dashboard renders {missing}, which this pack does "
            f"not include. It would import cleanly and then render blank.\n"
            f"Add them with --widget, e.g. "
            + " ".join(f"--widget {m}" for m in missing))

    if args.dry_run:
        import json
        print("\n--- pack definition (dry run, nothing sent) ---")
        print(json.dumps(pack.build(), indent=2)[:4000])
        return 0

    created = client.export_config.create_template(pack)
    template_uuid = created.get("uuid") or created.get("id")
    try:
        # `--widget` has dest="widgets" (it is append-style), so `args.widget`
        # does not exist -- omitting -o raised AttributeError *after* the
        # template was already created on the box. Name it for the pack itself.
        out = args.output or f"agentic-assistant-pack-{args.version}.zip"
        path = client.export_config.export_by_template_uuid(
            template_uuid, output_path=out)
        print(f"\nwrote {path}  ({Path(path).stat().st_size / 1024:.0f} KB)")
    finally:
        if not args.keep_template and template_uuid:
            try:
                client.export_config.delete_template(template_uuid)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: could not delete template {template_uuid}: {exc}",
                      file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
