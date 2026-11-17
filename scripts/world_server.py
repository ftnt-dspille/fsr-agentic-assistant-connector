#!/usr/bin/env python3
"""Serve a recorded known-answer world over HTTP, so an OUTSIDE agent can read it.

A known-answer row's evidence lives in a fixture bundle (`FixtureBox`: records,
`ops`, `configured_connectors`). The widget loop reads it through in-process
seams. The toolkit MCP server (the outside agent's tools) speaks pyfsr over
HTTP, so this puts the SAME bundle behind a loopback FortiSOAR-shaped API:

    GET  /api/3/...                    -> FixtureBox.get
    POST /api/query/<module>           -> FixtureBox.post (reads only)
    POST /api/integration/execute/     -> FixtureBox.op (the recorded response)
    POST /api/integration/connector_details/?configured=true
                                       -> the bundle's declared inventory
    anything else                      -> 404/599 and logged as a miss

Nothing is written: a POST that is not one of the above is refused, and the
misses are returned to the caller so a row can show what the agent asked for
that the world could not answer (the same "unserved" list the widget side has).
"""
from __future__ import annotations

import json
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


def _execute_envelope(recorded: Any) -> dict:
    """A recorded op response as the platform's execute envelope. Bundles record
    `{"ok": true, "data": X}`; the platform returns `{"status": "Success",
    "data": X}`."""
    if isinstance(recorded, dict) and "data" in recorded:
        ok = recorded.get("ok", True)
        return {"status": "Success" if ok else "Failed", "data": recorded["data"],
                "message": recorded.get("message", ""), "env": {}, "operation": ""}
    return {"status": "Success", "data": recorded, "message": "", "env": {}}


class WorldServer:
    """`with WorldServer(bundle_name) as w:` -> `w.base_url`, `w.misses`, `w.calls`."""

    def __init__(self, bundle: str):
        from scripts.fixture_box import FixtureBox, load_bundle
        self.box = FixtureBox(load_bundle(bundle))
        self.calls: list[str] = []
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def misses(self) -> list[str]:
        return list(self.box.misses) + list(self.box.http_misses)

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        # pyfsr always speaks https, so the world does too (throwaway self-signed
        # cert on loopback; clients run with FSR_VERIFY_SSL=false).
        return f"https://127.0.0.1:{self._httpd.server_address[1]}"

    def handle(self, method: str, url: str, body: Optional[dict]) -> tuple[int, Any]:
        path = urlparse(url).path
        self.calls.append(f"{method} {url}")
        box = self.box
        if method == "GET" and path.rstrip("/") == "/api/3/people":
            # pyfsr's API-key validation probe; not evidence, never a miss.
            return 200, {"hydra:member": [], "hydra:totalItems": 0}
        if method == "GET" and path.rstrip("/") == "/api/integration/connectors":
            rows = [{"id": i + 1, "name": c["name"], "version": c.get("version"),
                     "label": c.get("label"), "status": c.get("status", "Completed"),
                     "active": True, "configuration_count": c.get("config_count", 1)}
                    for i, c in enumerate(box.configured_connectors or [])]
            return 200, {"data": rows, "totalItems": len(rows)}
        if method == "GET" and path.startswith("/api/integration/connectors/healthcheck/"):
            return 200, {"status": "Available", "message": "Connector is available"}
        if method == "GET" and path.rstrip("/") == "/api/3/staging_model_metadatas":
            return 200, {"hydra:member": [], "hydra:totalItems": 0}  # no pending schema
        if method == "GET" and not path.startswith(("/api/3/keys",)):
            declared = box._declared_inventory("GET", path, {})
            if declared is not None:
                return 200, declared
        if method == "GET":
            replayed = box.replay("GET", url) if box.records_path(url) else None
            if replayed is not None:
                return replayed
            return box.get(url)
        if path.rstrip("/") == "/api/integration/execute":
            body = body or {}
            rec = box.op(str(body.get("connector") or ""), str(body.get("operation") or ""),
                         body.get("params") or {})
            if rec is None:
                box.misses.append(f"OP {body.get('connector')}.{body.get('operation')}")
                return 200, {"status": "Failed", "data": None,
                             "message": f"{body.get('operation')} returned no data"}
            return 200, _execute_envelope(rec)
        if path.startswith("/api/query/"):
            return box.post(url, body)
        replayed = box.replay("POST", url)
        if replayed is not None:
            return replayed
        box.misses.append(f"POST {url}")
        return 599, {"message": "this world serves reads only"}

    def __enter__(self) -> "WorldServer":
        world = self

        class H(BaseHTTPRequestHandler):
            def _go(self, method: str) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                status, payload = world.handle(method, self.path, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._go("GET")

            def do_POST(self):  # noqa: N802
                self._go("POST")

            def log_message(self, *a):  # silence
                pass

        self._tmp = tempfile.TemporaryDirectory(prefix="world-")
        cert, key = Path(self._tmp.name) / "c.pem", Path(self._tmp.name) / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1",
                        "-subj", "/CN=127.0.0.1"], check=True, capture_output=True)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        self._httpd.socket = ctx.wrap_socket(self._httpd.socket, server_side=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        self._tmp.cleanup()
