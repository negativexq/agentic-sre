"""The customer-side connector process (contract §12.7): ``python -m packages.connector.agent``.

It owns every backend credential, runs the poll loops of §10, receives the Alertmanager webhook
locally (§10.5) and dials the control plane over mTLS. Configuration is by environment.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from packages.connector.enrollment import enroll
from packages.connector.pki import Identity
from packages.connector.service import Connector, connector_from_environment
from packages.connector.transport import ConnectorAgent
from packages.rca.alert_coverage import AlertCoverageConfig

logger = logging.getLogger(__name__)

MAX_WEBHOOK_BYTES = 1_048_576


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


def build_webhook_server(
    connector: Connector, address: tuple[str, int], token: str
) -> ThreadingHTTPServer:
    """A local receiver for Alertmanager deliveries: authenticated POST to ``/webhook`` only."""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self._reply(404, {"error": "not found"})
                return
            presented = self.headers.get("Authorization", "")
            if not hmac.compare_digest(presented, f"Bearer {token}"):
                self._reply(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > MAX_WEBHOOK_BYTES:
                self._reply(400, {"error": "bad length"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                accepted = connector.receive_webhook(payload)
            except (ValueError, LookupError) as error:
                self._reply(400, {"error": str(error)[:200]})
                return
            self._reply(202, {"accepted": accepted})

        def do_GET(self) -> None:
            self._reply(405, {"error": "POST only"})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug(format, *args)

    return ThreadingHTTPServer(address, Handler)


def ensure_identity(env: Mapping[str, str]) -> None:
    """First start (connector-install-design.md §A8.2): enroll when no certificate exists and a token is given.

    The key is generated here and written with owner-only permissions next to the certificate and the CA, at the
    paths the session uses; a Connector that already has its certificate does nothing.
    """
    cert, key, ca = (
        Path(env["SRE_CONNECTOR_TLS_CERT"]),
        Path(env["SRE_CONNECTOR_TLS_KEY"]),
        Path(env["SRE_CONNECTOR_TLS_CA"]),
    )
    token = env.get("SRE_CONNECTOR_ENROLLMENT_TOKEN", "").strip()
    if cert.exists() and key.exists() and ca.exists():
        return
    if not token:
        raise RuntimeError("no certificate and no SRE_CONNECTOR_ENROLLMENT_TOKEN to enroll with")
    identity, served_ca = enroll(
        env["SRE_CONNECTOR_ENROLL_ENDPOINT"],
        token,
        server_name=env.get("SRE_CONNECTOR_SERVER_NAME") or None,
    )
    for path in (cert, key, ca):
        path.parent.mkdir(parents=True, exist_ok=True)
    key.touch(mode=0o600)
    key.write_bytes(identity.private_key)
    cert.write_bytes(identity.certificate)
    ca.write_bytes(served_ca)
    logger.info("enrolled; certificate written to %s", cert)


def identity_from_files(cert: str, key: str) -> Identity:
    return Identity(Path(cert).read_bytes(), Path(key).read_bytes())


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ensure_identity(os.environ)
    namespaces = _csv("SRE_WATCH_NAMESPACES", "sre-demo")
    evidence = _csv("SRE_EVIDENCE_NAMESPACES", "chaos-mesh")
    connector = connector_from_environment(
        evidence, tuple(dict.fromkeys((*namespaces, *evidence))), accept_webhook=True
    )
    stop = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: stop.set())
    threading.Thread(
        target=connector.run,
        args=(stop,),
        kwargs={
            "changes_interval": float(os.getenv("SRE_WATCH_INTERVAL_SECONDS", "15")),
            # contract §15: watch each scope; the full listing is then a reconciliation
            "watch": os.getenv("SRE_CONNECTOR_WATCH", "true").casefold() == "true",
            "reconcile_interval": float(os.getenv("SRE_RECONCILE_INTERVAL_SECONDS", "600")),
            "alerts_interval": AlertCoverageConfig.from_environment().poll_interval.total_seconds(),
        },
        daemon=True,
    ).start()
    listen = os.getenv("SRE_CONNECTOR_WEBHOOK_LISTEN", "")
    token = os.getenv("SRE_CONNECTOR_WEBHOOK_TOKEN", "")
    if listen and token:
        host, _, port = listen.rpartition(":")
        server = build_webhook_server(connector, (host or "0.0.0.0", int(port)), token)  # noqa: S104
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def shutdown_on_stop() -> None:
            stop.wait()
            server.shutdown()

        threading.Thread(target=shutdown_on_stop, daemon=True).start()
    ConnectorAgent(
        connector,
        os.environ["SRE_CONNECTOR_ENDPOINT"],
        identity=identity_from_files(
            os.environ["SRE_CONNECTOR_TLS_CERT"], os.environ["SRE_CONNECTOR_TLS_KEY"]
        ),
        server_ca=Path(os.environ["SRE_CONNECTOR_TLS_CA"]).read_bytes(),
        server_name=os.getenv("SRE_CONNECTOR_SERVER_NAME") or None,
    ).run(stop)


if __name__ == "__main__":
    main()
