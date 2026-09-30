"""Remote mode end to end: a control plane that starts alone, a connector that dials in (§12.6)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_connector_streams import FakeAlerts, FakeCluster, firing

from apps.control_plane.connector_intake import AlertStreamConsumer
from apps.control_plane.diagnosis import DiagnosisService, service_from_environment
from apps.control_plane.main import build_system_status
from packages.connector.agent import build_webhook_server
from packages.connector.client import StreamedClusterReader
from packages.connector.pki import issue_connector, issue_server, new_ca, write_identity
from packages.connector.service import Connector
from packages.connector.transport import ConnectorAgent, gateway_from_environment
from packages.storage.models import Base, IncidentRow


def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[DiagnosisService, Any, Connector, sessionmaker[Session]]]:
    ca = new_ca()
    server = issue_server(ca, ["localhost", "127.0.0.1"])
    cert, key = write_identity(server, tmp_path, "server")
    (tmp_path / "ca.crt").write_bytes(ca.certificate)
    for name, value in {
        "SRE_CONNECTOR_MODE": "remote",
        "SRE_CONNECTOR_ALLOWED": "lab-0",
        "SRE_CONNECTOR_LISTEN": "127.0.0.1:0",
        "SRE_CONNECTOR_TLS_CERT": str(cert),
        "SRE_CONNECTOR_TLS_KEY": str(key),
        "SRE_CONNECTOR_TLS_CLIENT_CA": str(tmp_path / "ca.crt"),
        "SRE_WATCH_NAMESPACES": "shop",
    }.items():
        monkeypatch.setenv(name, value)
    sessions = factory()
    service = service_from_environment(sessions)
    assert service.gateway is not None
    service.gateway.start()
    connector = Connector(
        cluster=FakeCluster(),
        alert_source=FakeAlerts(),
        accept_webhook=True,
        watch_namespaces=("shop", "chaos-mesh"),
    )
    stop = threading.Event()
    agent = ConnectorAgent(
        connector,
        f"localhost:{service.gateway.port}",
        identity=issue_connector(ca, "lab-0"),
        server_ca=ca.certificate,
        server_name="localhost",
        min_backoff=0.1,
        max_backoff=0.4,
    )
    thread = threading.Thread(target=agent.run, args=(stop,), daemon=True)
    yield service, thread, connector, sessions
    stop.set()
    thread.join(5)
    service.gateway.stop()


def test_the_control_plane_starts_without_a_connector_and_attaches_readers_on_connect(
    remote: tuple[DiagnosisService, Any, Connector, sessionmaker[Session]],
) -> None:
    service, thread, _, _ = remote
    assert service.reader is None and service.gateway is not None
    assert service.gateway.connected() == frozenset()
    thread.start()
    deadline = time.monotonic() + 10
    while service.reader is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert isinstance(service.reader, StreamedClusterReader)
    listing = service.reader.list_objects(["shop"])
    assert [o["metadata"]["name"] for o in listing.objects] == ["a", "b"]


def test_an_alert_delivered_to_the_local_receiver_becomes_an_incident_here(
    remote: tuple[DiagnosisService, Any, Connector, sessionmaker[Session]],
) -> None:
    service, thread, connector, sessions = remote
    thread.start()
    assert service.gateway is not None
    deadline = time.monotonic() + 10
    while not service.gateway.connected() and time.monotonic() < deadline:
        time.sleep(0.05)
    server = build_webhook_server(connector, ("127.0.0.1", 0), "secret-token")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        delivery = json.dumps(
            {
                "receiver": "sre",
                "status": "firing",
                "alerts": [{**firing("HighLatency", "f9"), "status": "firing"}],
            }
        ).encode()

        def post(path: str, token: str | None, body: bytes = delivery) -> int:
            request = urllib.request.Request(f"{url}{path}", data=body, method="POST")
            if token:
                request.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return int(response.status)
            except urllib.error.HTTPError as error:
                return int(error.code)

        assert post("/webhook", None) == 401
        assert post("/webhook", "wrong") == 401
        assert post("/elsewhere", "secret-token") == 404
        assert post("/webhook", "secret-token", b"{not json") == 400
        assert post("/webhook", "secret-token") == 202
    finally:
        server.shutdown()
    assert service.connector_client is not None
    consumer = AlertStreamConsumer(service.connector_client, sessions)
    assert consumer.step() >= 1
    with sessions() as session:
        assert session.query(IncidentRow).count() == 1


def test_the_status_says_when_no_connector_is_connected() -> None:
    from apps.control_plane.console.dto import SystemConnector

    status = build_system_status(
        factory()(),
        reader_configured=False,
        connector_status=SystemConnector(
            name="Connector", status="unavailable", detail="no connector is connected"
        ),
    )
    assert status.connectors[-1].name == "Connector"
    assert status.connectors[-1].status == "unavailable"


def test_the_gateway_needs_one_named_connector_among_the_allowed(tmp_path: Path) -> None:
    env = {"SRE_CONNECTOR_ALLOWED": "a,b"}
    with pytest.raises(ValueError):
        gateway_from_environment(env)
    with pytest.raises(ValueError):
        gateway_from_environment({**env, "SRE_CONNECTOR_ID": "c"})
