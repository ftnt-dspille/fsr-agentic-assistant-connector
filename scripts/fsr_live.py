"""Shared live-FSR driver for the connector integration harness.

Drives the INSTALLED connector remotely through FortiSOAR's
`/api/integration/execute/` endpoint -- i.e. the real on-platform code
path where `integrations.crudhub` is available -- and provides the small
set of helpers the CLI runner (`live_integration.py`) and the pytest
wrapper (`tests/test_live_integration.py`) both need:

- auth (Bearer via FSR_USERNAME/FSR_PASSWORD, or API-KEY via FSR_API_KEY)
- `execute(op, params)` → ExecResult(status, ok, data, raw, elapsed)
- golden capture under tests/golden/live/
- cleanup (purge a pushed workflow_collection by uuid)
- a self-contained pure-logic playbook YAML (no connector resolution, so
  it compiles against the slim on-box reference DB)

Env:
  FSR_BASE_URL, FSR_USERNAME/FSR_PASSWORD (or FSR_API_KEY), FSR_VERIFY_SSL
  FSR_BUILDER_CONFIG_NAME  (default: fsrpb-live)
  FSR_BUILDER_VERSION      (default: read from info.json)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyfsr.exceptions import FortiSOARException

ROOT = Path(__file__).resolve().parent.parent
CONNECTOR = "connector-fsr-agentic-assistant"
GOLDEN_DIR = ROOT / "tests" / "golden" / "live"


# A pure-logic playbook: start → set_variable → decision → set_variable / end.
# No connector operations, so it compiles & runs without a warmed catalog.
PURE_LOGIC_YAML = """\
collection: FSRPB Live Harness Pure Logic
description: Live integration harness fixture -- branches on severity.
visible: true

playbooks:
  - name: Route Severity Live
    description: Reads severity from trigger input, branches, stamps a tier.
    is_active: true
    parameters: [severity]
    steps:
      - name: start
        type: start
        next: Read severity
      - name: Read severity
        type: set_variable
        next: Branch on severity
        vars:
          severity: "{{ vars.input.params.severity }}"
      - name: Branch on severity
        type: decision
        conditions:
          - display: high
            when: "{{ vars.severity in ['critical', 'high'] }}"
            next: Escalate to Tier 2
          - display: low
            when: "{{ vars.severity not in ['critical', 'high'] }}"
            next: No action low severity
      - name: Escalate to Tier 2
        type: set_variable
        vars:
          tier: "tier2"
      - name: No action low severity
        type: end
"""


# A linear playbook (no decision) -- exercises validate_yaml's clean path.
# (PURE_LOGIC_YAML's decision compiles fine but trips a validate_yaml gap:
# validate runs before the resolver normalizes `conditions[].next` into
# branch targets, so it falsely flags "no branch target". Tracked as a
# real finding; use this linear fixture for the clean-validate assertion.)
LINEAR_YAML = """\
collection: FSRPB Live Harness Linear
description: Live integration harness fixture -- linear set_variable chain.
visible: true

playbooks:
  - name: Stamp Tier Linear
    description: Reads severity from trigger input and stamps a tier.
    is_active: true
    parameters: [severity]
    steps:
      - name: start
        type: start
        next: Read severity
      - name: Read severity
        type: set_variable
        next: Stamp tier
        vars:
          severity: "{{ vars.input.params.severity }}"
      - name: Stamp tier
        type: set_variable
        next: Done
        vars:
          tier: "tier2"
      - name: Done
        type: end
"""


@dataclass
class ExecResult:
    op: str
    status: int
    ok: bool | None
    data: dict
    raw: dict
    elapsed: float
    error: Any = None


@dataclass
class LiveFSR:
    """Drives the installed connector through pyfsr's ConnectorsAPI.execute,
    adapting it to the harness's ExecResult contract."""

    config: str = ""
    version: str = ""
    client: Any = None

    @property
    def base(self) -> str:
        return getattr(self.client, "base_url", "")

    @classmethod
    def from_env(cls) -> "LiveFSR":
        # Imported HERE, not at module scope. `EnvConfig` is needed only on the
        # live path -- every offline caller injects its own client. As a
        # module-level import it made `pyfsr.config` a hard dependency of merely
        # IMPORTING this module, so an older installed pyfsr (0.2.2 ships no
        # `config` module) broke collection of tests/test_eval_harness.py, which
        # is deterministic and box-free by design. That took down the whole
        # offline suite -- `make verify` could not even collect.
        from pyfsr.config import EnvConfig
        try:
            client = EnvConfig.from_env().client(timeout=int(os.environ.get("FSR_BUILDER_TIMEOUT", "180")))
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        config = os.environ.get("FSR_BUILDER_CONFIG_NAME", "fsrpb-live")
        version = os.environ.get("FSR_BUILDER_VERSION") or _info_version()
        return cls(config=config, version=version, client=client)

    def execute(self, op: str, params: dict | None = None,
                *, timeout: int = 180) -> ExecResult:
        # config_name lets pyfsr resolve the named config's UUID (falling back to
        # the connector's default config if the name isn't found) -- so a missing
        # FSR_BUILDER_CONFIG_NAME still dispatches against the active backend.
        t0 = time.time()
        try:
            res = self.client.connectors.execute(
                CONNECTOR, op, version=self.version or None,
                config=self.config or None, params=params or {})
            elapsed = time.time() - t0
            data = res.data if isinstance(res.data, dict) else (
                {} if res.data is None else {"result": res.data})
            ok = data.get("ok") if isinstance(data, dict) else None
            err = data.get("error") if isinstance(data, dict) else None
            # The execute envelope wraps the handler return in {status, data}; a
            # non-Success status with no embedded error is itself a failure.
            if str(res.status or "").strip().lower() not in ("success", "") and err is None:
                err = {"code": f"execute_status_{res.status}", "message": res.message or ""}
                ok = False if ok is None else ok
            return ExecResult(op=op, status=200, ok=ok, data=data,
                              raw=(res.model_dump() if hasattr(res, "model_dump") else {}),
                              elapsed=elapsed, error=err)
        except FortiSOARException as e:
            elapsed = time.time() - t0
            code = e.status_code or 500
            body: dict = {}
            if e.response is not None:
                try:
                    body = e.response.json()
                except Exception:  # noqa: BLE001
                    body = {}
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                data = body if isinstance(body, dict) else {}
            msg = (body.get("message") if isinstance(body, dict) else None) or e.message or str(e)
            err = (data.get("error") if isinstance(data, dict) else None) or {
                "code": f"execute_http_{code}", "message": str(msg)[:500]}
            return ExecResult(op=op, status=code, ok=False, data=data,
                              raw=body, elapsed=elapsed, error=err)

    # ----- helpers -------------------------------------------------------

    def purge_collection(self, coll_uuid: str) -> bool:
        """Hard-delete a pushed workflow_collection by uuid (frees the reserved
        uuid so a re-push of the same deterministic-uuid collection doesn't 409).
        Idempotent: a 404 (already gone) counts as success. Uses pyfsr's
        ``client.workflow_collections.delete(uuid, hard=True)``."""
        if not coll_uuid:
            return False
        try:
            self.client.workflow_collections.delete(coll_uuid, hard=True)
            return True
        except FortiSOARException as e:
            return e.status_code == 404  # already gone
        except Exception:  # noqa: BLE001
            return False

    def collection_exists(self, coll_uuid: str) -> bool:
        """Whether a workflow_collection really landed in FSR (not just that push
        echoed a uuid). Uses pyfsr's ``client.workflow_collections.exists``."""
        if not coll_uuid:
            return False
        try:
            return bool(self.client.workflow_collections.exists(coll_uuid))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def collection_uuid(workflow_json: dict) -> str:
        """Pull the collection uuid out of a compiled export wrap so the
        harness can pre-purge a prior push of the same (deterministic-uuid)
        playbook before re-pushing."""
        data = (workflow_json or {}).get("data") if isinstance(workflow_json, dict) else None
        if isinstance(data, list) and data:
            return data[0].get("uuid") or ""
        if isinstance(workflow_json, dict):
            return workflow_json.get("uuid") or ""
        return ""

    def save_golden(self, name: str, obj: Any) -> Path:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path = GOLDEN_DIR / f"{name}.json"
        path.write_text(json.dumps(obj, indent=2, default=str))
        return path


def _info_version() -> str:
    info = json.loads((ROOT / CONNECTOR / "info.json").read_text())
    return info["version"]
