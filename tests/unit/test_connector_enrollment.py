"""Connector registry and enrollment with a one-time token (connector-install-design §A8.2, §7)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import grpc
import pytest
from connector_harness import GrpcHarness

from packages.connector.client import ConnectorClient
from packages.connector.enrollment import (
    EnrollmentError,
    EnrollmentServer,
    MemoryStore,
    Registry,
    Token,
    enroll,
)
from packages.connector.pki import issue_server, new_ca, new_key_and_request
from packages.connector.service import Connector

T0 = datetime(2026, 10, 9, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def registry() -> Registry:
    return Registry(MemoryStore(), new_ca(), clock=Clock())


def request_for(connector_id: str) -> bytes:
    return new_key_and_request(connector_id)[1]


def test_a_token_carries_the_id_a_secret_and_the_ca_and_round_trips(registry: Registry) -> None:
    token = registry.create("prod-eu")
    parsed = Token.parse(str(token))
    assert (parsed.connector_id, parsed.secret) == ("prod-eu", token.secret)
    assert parsed.ca_pem.strip() == registry.ca.certificate.strip()
    assert len(str(token)) < 1000


def test_enrollment_signs_once_and_activates_the_connector(registry: Registry) -> None:
    token = str(registry.create("prod-eu"))
    assert not registry.is_allowed("prod-eu")  # pending
    certificate = registry.enroll(token, request_for("prod-eu"))
    assert b"BEGIN CERTIFICATE" in certificate and registry.is_allowed("prod-eu")
    record = registry.store.get("prod-eu")
    assert record is not None and record.token_hash is None and record.cert_serial
    with pytest.raises(EnrollmentError, match="no unused token"):
        registry.enroll(token, request_for("prod-eu"))  # one-time


def test_an_expired_token_a_wrong_secret_or_an_unknown_id_is_refused(registry: Registry) -> None:
    token = registry.create("prod-eu")
    tampered = f"prod-eu.{'x' * 43}.{str(token).split('.')[2]}"
    with pytest.raises(EnrollmentError, match="wrong secret"):
        registry.enroll(tampered, request_for("prod-eu"))
    with pytest.raises(EnrollmentError, match="unknown connector"):
        registry.enroll(str(token).replace("prod-eu", "other", 1), request_for("other"))
    registry.clock.now = T0 + timedelta(hours=2)  # type: ignore[attr-defined]
    with pytest.raises(EnrollmentError, match="expired"):
        registry.enroll(str(token), request_for("prod-eu"))


def test_a_token_naming_another_ca_is_refused(registry: Registry) -> None:
    token = registry.create("prod-eu")
    foreign = Token("prod-eu", token.secret, new_ca("rogue").certificate)
    with pytest.raises(EnrollmentError, match="another CA"):
        registry.enroll(str(foreign), request_for("prod-eu"))


def test_an_active_connector_cannot_be_created_again_and_ids_are_dns_labels(
    registry: Registry,
) -> None:
    registry.enroll(str(registry.create("prod-eu")), request_for("prod-eu"))
    with pytest.raises(ValueError, match="active"):
        registry.create("prod-eu")
    for bad in ("Prod", "a.b", "-x", ""):
        with pytest.raises(ValueError):
            registry.create(bad)
    pending = registry.create("staging")
    again = registry.create("staging")  # a pending id may get a fresh token
    with pytest.raises(EnrollmentError, match="wrong secret"):
        registry.enroll(str(pending), request_for("staging"))
    assert registry.enroll(str(again), request_for("staging"))


def test_the_certificate_carries_only_the_tokens_identity(registry: Registry) -> None:
    from cryptography import x509

    token = str(registry.create("prod-eu"))
    certificate = x509.load_pem_x509_certificate(
        registry.enroll(token, request_for("someone-else"))
    )
    sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.UniformResourceIdentifier) == ["connector:prod-eu"]


# ---- end to end over loopback: the enrollment port, then the session port ---------------------------------------


def test_a_connector_enrolls_on_its_port_and_then_connects_over_mutual_tls() -> None:
    ca = new_ca()
    registry = Registry(MemoryStore(), ca)
    server = EnrollmentServer(
        "127.0.0.1:0", server=issue_server(ca, ["localhost", "127.0.0.1"]), registry=registry
    )
    port = server.start()
    harness = GrpcHarness(ca=ca, allowed=[], is_allowed=registry.is_allowed)
    try:
        token = str(registry.create("prod-eu"))
        identity, served_ca = enroll(f"127.0.0.1:{port}", token, server_name="localhost")
        assert served_ca.strip() == ca.certificate.strip()
        harness.agent(Connector(), identity)
        harness.wait_connected("prod-eu")
        answer = ConnectorClient(harness.gateway.transport("prod-eu")).call("capabilities")
        assert "capabilities" in answer
        # the token is spent: a second enrollment with it is refused on the wire
        with pytest.raises(grpc.RpcError) as refused:
            enroll(f"127.0.0.1:{port}", token, server_name="localhost")
        assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        harness.close()
        server.stop()


def test_an_unregistered_identity_is_refused_by_the_session_port() -> None:
    ca = new_ca()
    registry = Registry(MemoryStore(), ca)
    harness = GrpcHarness(ca=ca, allowed=[], is_allowed=registry.is_allowed)
    try:
        from packages.connector.pki import issue_connector

        harness.agent(Connector(), issue_connector(ca, "never-enrolled"))
        time.sleep(0.8)
        assert "never-enrolled" not in harness.gateway.connected()
    finally:
        harness.close()


def test_a_connector_refuses_a_control_plane_that_is_not_the_tokens_ca() -> None:
    ca, impostor = new_ca(), new_ca("impostor")
    registry = Registry(MemoryStore(), ca)
    server = EnrollmentServer(
        "127.0.0.1:0", server=issue_server(impostor, ["localhost"]), registry=registry
    )
    port = server.start()
    try:
        with pytest.raises(grpc.RpcError) as refused:
            enroll(f"127.0.0.1:{port}", str(registry.create("prod-eu")), server_name="localhost")
        assert refused.value.code() is grpc.StatusCode.UNAVAILABLE  # TLS refused: untrusted server
    finally:
        server.stop()


def test_the_agent_enrolls_on_first_start_and_never_again(tmp_path: object) -> None:
    import os
    from pathlib import Path

    from packages.connector.agent import ensure_identity

    base = Path(str(tmp_path))
    ca = new_ca()
    registry = Registry(MemoryStore(), ca)
    server = EnrollmentServer(
        "127.0.0.1:0", server=issue_server(ca, ["localhost"]), registry=registry
    )
    port = server.start()
    env = {
        "SRE_CONNECTOR_TLS_CERT": str(base / "tls" / "client.crt"),
        "SRE_CONNECTOR_TLS_KEY": str(base / "tls" / "client.key"),
        "SRE_CONNECTOR_TLS_CA": str(base / "tls" / "ca.crt"),
        "SRE_CONNECTOR_ENROLL_ENDPOINT": f"127.0.0.1:{port}",
        "SRE_CONNECTOR_SERVER_NAME": "localhost",
        "SRE_CONNECTOR_ENROLLMENT_TOKEN": str(registry.create("prod-eu")),
    }
    try:
        ensure_identity(env)
        key = base / "tls" / "client.key"
        assert key.exists() and oct(os.stat(key).st_mode & 0o777) == "0o600"
        assert (base / "tls" / "ca.crt").read_bytes().strip() == ca.certificate.strip()
        assert registry.is_allowed("prod-eu")
        ensure_identity(env)  # the spent token is not used again: the files are there
    finally:
        server.stop()
    with pytest.raises(RuntimeError, match="no certificate"):
        ensure_identity(
            env
            | {"SRE_CONNECTOR_TLS_CERT": str(base / "x.crt"), "SRE_CONNECTOR_ENROLLMENT_TOKEN": ""}
        )


def test_the_registry_persists_in_the_database(tmp_path: object) -> None:
    from pathlib import Path

    from apps.control_plane.connector_registry import SqlRegistryStore
    from packages.storage import create_database_engine, create_session_factory
    from packages.storage.models import Base

    engine = create_database_engine(f"sqlite:///{Path(str(tmp_path)) / 'cp.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    registry = Registry(SqlRegistryStore(factory), new_ca(), clock=Clock())
    token = str(registry.create("prod-eu"))
    again = Registry(
        SqlRegistryStore(factory), registry.ca, clock=Clock()
    )  # a restarted control plane
    again.enroll(token, request_for("prod-eu"))
    (record,) = SqlRegistryStore(factory).all()
    assert (record.status, record.token_hash, record.cert_not_after is not None) == (
        "active",
        None,
        True,
    )
    assert again.is_allowed("prod-eu") and not again.is_allowed("other")
