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


def test_backends_configured_on_the_connector_are_not_reported_as_unconfigured() -> None:
    from apps.control_plane.console.dto import SystemConnector

    row = SystemConnector(name="Connector", status="connected", detail="connected: lab")
    status = build_system_status(
        factory()(),
        reader_configured=False,
        connector_status=row,
        connector_capabilities=frozenset({"changes", "history", "alerts", "logs"}),
    )
    by_name = {c.name: c for c in status.connectors}
    assert by_name["Kubernetes"].status == "connected"
    assert by_name["Alertmanager"].status == "connected"
    assert by_name["Loki"].status == "connected"
    assert by_name["Prometheus"].status == "not_configured"
    assert "backends.prometheus.url" in (
        by_name["Prometheus"].detail or ""
    )  # D4: the chart value to set
    assert "PROMETHEUS_URL" not in " ".join(c.detail or "" for c in status.connectors)


def test_with_no_connector_every_backend_is_unavailable_not_unconfigured() -> None:
    status = build_system_status(
        factory()(),
        reader_configured=False,
        connector_capabilities=frozenset(),
    )
    by_name = {c.name: c.status for c in status.connectors}
    assert {by_name[n] for n in ("Kubernetes", "Alertmanager", "Prometheus", "Loki", "Tempo")} == {
        "unavailable"
    }


def test_a_lost_connector_is_logged_once_and_its_return_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    from packages.connector.client import ConnectorClient

    # Another test's logging configuration can disable existing loggers, so this one attaches its own
    # handler to the module's logger and re-enables it for the duration.
    logger = logging.getLogger("apps.control_plane.connector_intake")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logger.level)  # restored on teardown
    logger.setLevel(logging.INFO)  # setLevel, not an assignment: it clears the enabled-for cache
    messages: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = Collect(level=logging.INFO)
    logger.addHandler(handler)
    state = {"up": False}
    connector = Connector(cluster=FakeCluster(), alert_source=FakeAlerts(), accept_webhook=True)

    def transport(payload: bytes) -> bytes:
        if not state["up"]:
            raise ConnectionError("gone")
        return connector.handle(payload)

    consumer = AlertStreamConsumer(ConnectorClient(transport), factory())
    try:
        for _ in range(5):
            consumer.step()
        assert sum("unavailable" in m for m in messages) == 1
        state["up"] = True
        consumer.step()
        assert any("available again" in m for m in messages)
    finally:
        logger.removeHandler(handler)


def test_the_watch_loop_reports_an_unreachable_connector_once_and_its_return() -> None:
    import logging

    from packages.connector.client import ConnectorUnavailable

    service = DiagnosisService(session_factory=factory(), namespaces=("shop",))
    outcomes: list[Any] = [
        ConnectorUnavailable("gone"),
        ConnectorUnavailable("gone"),
        ConnectorUnavailable("gone"),
        0,
        ConnectorUnavailable("gone again"),
    ]

    def snapshot() -> int:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return int(outcome)

    service.snapshot = snapshot  # type: ignore[method-assign]
    service.apply_retention = lambda: None  # type: ignore[method-assign]
    service.reevaluate = lambda: None  # type: ignore[method-assign]

    class Cycles:
        """A stop flag that lets the loop run for the scripted outcomes, without waiting."""

        def is_set(self) -> bool:
            return not outcomes

        def wait(self, timeout: float | None = None) -> bool:
            return False

    logger = logging.getLogger("apps.control_plane.diagnosis")
    messages: list[tuple[int, bool]] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append((record.levelno, record.exc_info is not None))

    handler = Collect(level=logging.INFO)
    previous = (logger.disabled, logger.level)
    logger.disabled = False
    logger.setLevel(logging.INFO)  # setLevel clears the logger's enabled-for cache
    logger.addHandler(handler)
    try:
        service.watch(Cycles(), 0.0)  # type: ignore[arg-type]
    finally:
        logger.removeHandler(handler)
        logger.disabled = previous[0]
        logger.setLevel(previous[1])
    # one warning for the first loss, one info for the return, one warning for the second loss;
    # none of them carries a traceback
    assert messages == [(logging.WARNING, False), (logging.INFO, False), (logging.WARNING, False)]
