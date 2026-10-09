"""Certificate rotation and revocation (connector-install-design §A8.3)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import grpc
import pytest
from connector_harness import GrpcHarness

from packages.connector.agent import renew_when_due
from packages.connector.enrollment import (
    EnrollmentError,
    EnrollmentServer,
    MemoryStore,
    Registry,
    enroll,
)
from packages.connector.pki import issue_connector, issue_server, new_ca, new_key_and_request
from packages.connector.service import Connector
from packages.connector.transport import ConnectorAgent, renew_identity


def enrolled(registry: Registry, connector_id: str) -> None:
    registry.enroll(str(registry.create(connector_id)), new_key_and_request(connector_id)[1])


def test_the_registry_renews_only_an_active_connector() -> None:
    registry = Registry(MemoryStore(), new_ca())
    enrolled(registry, "prod-eu")
    first = registry.store.get("prod-eu")
    assert registry.renew("prod-eu", new_key_and_request("prod-eu")[1])
    second = registry.store.get("prod-eu")
    assert first is not None and second is not None and first.cert_serial != second.cert_serial
    registry.disable("prod-eu")
    assert not registry.is_allowed("prod-eu")
    with pytest.raises(EnrollmentError, match="not active"):
        registry.renew("prod-eu", new_key_and_request("prod-eu")[1])
    with pytest.raises(EnrollmentError):
        registry.renew("nobody", new_key_and_request("nobody")[1])
    with pytest.raises(ValueError, match="unknown"):
        registry.disable("nobody")


@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    ca = new_ca()
    registry = Registry(MemoryStore(), ca)
    enrollment = EnrollmentServer(
        "127.0.0.1:0", server=issue_server(ca, ["localhost", "127.0.0.1"]), registry=registry
    )
    enrollment_port = enrollment.start()
    harness = GrpcHarness(
        ca=ca,
        allowed=[],
        is_allowed=registry.is_allowed,
        renew=registry.renew,
        sweep_interval=0.2,
    )
    identity, served_ca = enroll(
        f"127.0.0.1:{enrollment_port}", str(registry.create("prod-eu")), server_name="localhost"
    )
    files = {
        "SRE_CONNECTOR_TLS_CERT": tmp_path / "client.crt",
        "SRE_CONNECTOR_TLS_KEY": tmp_path / "client.key",
        "SRE_CONNECTOR_TLS_CA": tmp_path / "ca.crt",
    }
    files["SRE_CONNECTOR_TLS_CERT"].write_bytes(identity.certificate)
    files["SRE_CONNECTOR_TLS_KEY"].write_bytes(identity.private_key)
    files["SRE_CONNECTOR_TLS_CA"].write_bytes(served_ca)
    env = {name: str(path) for name, path in files.items()} | {
        "SRE_CONNECTOR_ENDPOINT": f"localhost:{harness.port}",
        "SRE_CONNECTOR_SERVER_NAME": "localhost",
    }
    yield registry, harness, identity, env
    harness.close()
    enrollment.stop()


def agent_for(harness: GrpcHarness, identity, stop_events: list[threading.Event]) -> ConnectorAgent:  # type: ignore[no-untyped-def]
    agent = ConnectorAgent(
        Connector(),
        f"localhost:{harness.port}",
        identity=identity,
        server_ca=harness.ca.certificate,
        server_name="localhost",
        min_backoff=0.1,
        max_backoff=0.4,
    )
    stop = threading.Event()
    threading.Thread(target=agent.run, args=(stop,), daemon=True).start()
    stop_events.append(stop)
    return agent


def test_a_due_certificate_is_renewed_and_the_session_reopens_with_it(world) -> None:  # type: ignore[no-untyped-def]
    registry, harness, identity, env = world
    stops: list[threading.Event] = []
    agent = agent_for(harness, identity, stops)
    try:
        harness.wait_connected("prod-eu")
        assert not renew_when_due(agent, env)  # fresh: nothing to do
        later = datetime.now(UTC) + timedelta(days=61)
        serial_before = registry.store.get("prod-eu").cert_serial
        assert renew_when_due(agent, env, now=later)
        assert agent.identity.certificate != identity.certificate
        assert Path(env["SRE_CONNECTOR_TLS_CERT"]).read_bytes() == agent.identity.certificate
        assert oct(Path(env["SRE_CONNECTOR_TLS_KEY"]).stat().st_mode & 0o777) == "0o600"
        assert registry.store.get("prod-eu").cert_serial != serial_before
        time.sleep(0.5)
        harness.wait_connected("prod-eu")  # the session is back, now with the renewed certificate
        assert harness.gateway.certificate_expiry("prod-eu") is not None
    finally:
        for stop in stops:
            stop.set()


def test_a_revoked_connector_loses_its_live_session_and_cannot_reconnect_or_renew(world) -> None:  # type: ignore[no-untyped-def]
    registry, harness, identity, env = world
    stops: list[threading.Event] = []
    agent_for(harness, identity, stops)
    try:
        harness.wait_connected("prod-eu")
        registry.disable("prod-eu")
        deadline = time.monotonic() + 5
        while "prod-eu" in harness.gateway.connected() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "prod-eu" not in harness.gateway.connected()
        time.sleep(0.8)  # the agent keeps retrying; every attempt is refused
        assert "prod-eu" not in harness.gateway.connected()
        with pytest.raises(grpc.RpcError) as refused:
            renew_identity(
                env["SRE_CONNECTOR_ENDPOINT"],
                identity,
                "prod-eu",
                server_ca=harness.ca.certificate,
                server_name="localhost",
            )
        assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        for stop in stops:
            stop.set()


def test_an_identity_on_the_fixed_allow_list_has_no_renewal() -> None:
    ca = new_ca()
    registry = Registry(MemoryStore(), ca)
    harness = GrpcHarness(
        ca=ca, allowed=["lab"], is_allowed=registry.is_allowed, renew=registry.renew
    )
    try:
        with pytest.raises(grpc.RpcError) as refused:
            renew_identity(
                f"localhost:{harness.port}",
                issue_connector(ca, "lab"),
                "lab",
                server_ca=ca.certificate,
                server_name="localhost",
            )
        assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        harness.close()


def test_the_gateway_knows_when_a_connected_certificate_expires() -> None:
    harness = GrpcHarness()
    stops: list[threading.Event] = []
    try:
        agent_for(harness, issue_connector(harness.ca, "lab-0", days=5), stops)
        harness.wait_connected("lab-0")
        expiry = harness.gateway.certificate_expiry("lab-0")
        assert expiry is not None and expiry - datetime.now(UTC) < timedelta(days=6)
        assert harness.gateway.certificate_expiry("lab-1") is None
    finally:
        for stop in stops:
            stop.set()
        harness.close()


def test_the_system_status_degrades_fourteen_days_before_a_certificate_expires() -> None:
    from apps.control_plane.main import gateway_status

    class Gateway:
        def __init__(self, expiry: datetime | None) -> None:
            self.expiry = expiry

        def connected(self) -> frozenset[str]:
            return frozenset({"prod-eu"})

        def certificate_expiry(self, _: str) -> datetime | None:
            return self.expiry

    now = datetime(2026, 10, 9, tzinfo=UTC)
    far = gateway_status(Gateway(now + timedelta(days=60)), now)  # type: ignore[arg-type]
    near = gateway_status(Gateway(now + timedelta(days=10)), now)  # type: ignore[arg-type]
    assert far.status == "connected"
    assert near.status == "degraded" and "expires in 10 days" in (near.detail or "")
