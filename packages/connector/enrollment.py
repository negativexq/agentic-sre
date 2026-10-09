"""Connector registry and enrollment with a one-time token (connector-install-design.md §A8.2, §7).

An operator creates a Connector id and receives a token ``<id>.<secret>.<CA>``: the secret is one-time, valid one
hour and stored only as a hash; the CA (base64url DER) is the control plane's, so the Connector trusts exactly it and
nothing else has to be handed over. On first start the Connector generates its key, sends a signing request with the
token to the **enrollment port** (server-authenticated TLS, this one method only), and receives its certificate.
From then on it connects to the session port over mutual TLS as before; the registry is the allow-list.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
from collections.abc import Callable
from concurrent import futures
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from packages.connector.pki import (
    CONNECTOR_SAN_PREFIX,
    DEFAULT_VALIDITY_DAYS,
    Identity,
    new_key_and_request,
    sign_request,
)

logger = logging.getLogger(__name__)

ENROLL_METHOD = "/connector.v1.Enrollment/Enroll"
TOKEN_TTL = timedelta(hours=1)
_ID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
Status = Literal["pending", "active", "disabled"]


class EnrollmentError(Exception):
    """An enrollment refused; the reason is logged on the control plane, never told to the caller."""


@dataclass(frozen=True)
class ConnectorRecord:
    connector_id: str
    status: Status
    created_at: datetime
    token_hash: str | None = None
    token_expires_at: datetime | None = None
    token_used_at: datetime | None = None
    cert_serial: str | None = None
    cert_not_after: datetime | None = None


class RegistryStore(Protocol):
    def get(self, connector_id: str) -> ConnectorRecord | None: ...

    def put(self, record: ConnectorRecord) -> None: ...

    def all(self) -> list[ConnectorRecord]: ...


class MemoryStore:
    """A registry store in memory (tests, and a control plane without a database)."""

    def __init__(self) -> None:
        self._rows: dict[str, ConnectorRecord] = {}
        self._lock = threading.Lock()

    def get(self, connector_id: str) -> ConnectorRecord | None:
        with self._lock:
            return self._rows.get(connector_id)

    def put(self, record: ConnectorRecord) -> None:
        with self._lock:
            self._rows[record.connector_id] = record

    def all(self) -> list[ConnectorRecord]:
        with self._lock:
            return sorted(self._rows.values(), key=lambda r: r.connector_id)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True)
class Token:
    connector_id: str
    secret: str
    ca_pem: bytes

    def __str__(self) -> str:
        der = x509.load_pem_x509_certificate(self.ca_pem).public_bytes(serialization.Encoding.DER)
        return f"{self.connector_id}.{self.secret}.{_b64(der)}"

    @classmethod
    def parse(cls, text: str) -> Token:
        parts = text.strip().split(".")
        if len(parts) != 3 or not _ID.match(parts[0]) or not parts[1]:
            raise ValueError("not an enrollment token")
        try:
            ca = x509.load_der_x509_certificate(_unb64(parts[2]))
        except ValueError as error:
            raise ValueError("the token's CA cannot be read") from error
        return cls(parts[0], parts[1], ca.public_bytes(serialization.Encoding.PEM))


class Registry:
    """The control plane's Connectors: who may connect, and the one-time enrollment of each."""

    def __init__(
        self,
        store: RegistryStore,
        ca: Identity,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        validity_days: int = DEFAULT_VALIDITY_DAYS,
    ) -> None:
        self.store, self.ca, self.clock, self.validity_days = store, ca, clock, validity_days

    def create(self, connector_id: str, *, ttl: timedelta = TOKEN_TTL) -> Token:
        """Register a Connector id (or re-issue its token while pending) and return its one-time token."""
        if not _ID.match(connector_id):
            raise ValueError("a connector id is a DNS label: lowercase letters, digits and '-'")
        existing = self.store.get(connector_id)
        if existing is not None and existing.status != "pending":
            raise ValueError(
                f"connector {connector_id} is {existing.status}; it cannot be enrolled again"
            )
        now = self.clock()
        secret = secrets.token_urlsafe(32)
        self.store.put(
            ConnectorRecord(
                connector_id=connector_id,
                status="pending",
                created_at=existing.created_at if existing else now,
                token_hash=_hash(secret),
                token_expires_at=now + ttl,
            )
        )
        return Token(connector_id, secret, self.ca.certificate)

    def enroll(self, token_text: str, request_pem: bytes) -> bytes:
        """Check the token and sign the Connector's request; returns its certificate (PEM)."""
        try:
            token = Token.parse(token_text)
        except ValueError as error:
            raise EnrollmentError(f"malformed token: {error}") from error
        record = self.store.get(token.connector_id)
        now = self.clock()
        if record is None:
            raise EnrollmentError(f"unknown connector {token.connector_id}")
        if (
            record.status != "pending"
            or record.token_used_at is not None
            or record.token_hash is None
        ):
            raise EnrollmentError(f"connector {token.connector_id} has no unused token")
        if record.token_expires_at is None or now > record.token_expires_at:
            raise EnrollmentError(f"the token of {token.connector_id} has expired")
        if not hmac.compare_digest(record.token_hash, _hash(token.secret)):
            raise EnrollmentError(f"wrong secret for {token.connector_id}")
        if token.ca_pem.strip() != self.ca.certificate.strip():
            raise EnrollmentError("the token names another CA")
        try:
            certificate, serial, expiry = sign_request(
                self.ca, request_pem, token.connector_id, days=self.validity_days, now=now
            )
        except ValueError as error:
            raise EnrollmentError(f"bad signing request: {error}") from error
        self.store.put(
            replace(
                record,
                status="active",
                token_hash=None,
                token_used_at=now,
                cert_serial=format(serial, "x"),
                cert_not_after=expiry,
            )
        )
        return certificate

    def is_allowed(self, connector_id: str) -> bool:
        record = self.store.get(connector_id)
        return record is not None and record.status == "active"


class EnrollmentServer:
    """The enrollment port: server-authenticated TLS, one method, no session (decision 1 as amended, §7)."""

    def __init__(self, address: str, *, server: Identity, registry: Registry) -> None:
        self._address, self._identity, self._registry = address, server, registry
        self._server: grpc.Server | None = None
        self.port = 0

    def _enroll(self, request: bytes, context: grpc.ServicerContext) -> bytes:
        try:
            body = json.loads(request)
            certificate = self._registry.enroll(str(body["token"]), str(body["request"]).encode())
        except (EnrollmentError, ValueError, KeyError, TypeError) as error:
            logger.warning("refused an enrollment: %s", error)
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "enrollment refused")
        return json.dumps(
            {"certificate": certificate.decode(), "ca": self._registry.ca.certificate.decode()}
        ).encode()

    def start(self) -> int:
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        service, method = ENROLL_METHOD.strip("/").split("/")
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    service, {method: grpc.unary_unary_rpc_method_handler(self._enroll)}
                ),
            )
        )
        credentials = grpc.ssl_server_credentials(
            ((self._identity.private_key, self._identity.certificate),)
        )
        self.port = server.add_secure_port(self._address, credentials)
        server.start()
        self._server = server
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop(grace=1).wait(5)
            self._server = None


def enroll(
    endpoint: str, token_text: str, *, server_name: str | None = None, timeout: float = 15.0
) -> tuple[Identity, bytes]:
    """The Connector's side: generate a key, enroll with the token, verify the answer.

    Returns the Connector's identity and the CA (PEM). The channel trusts only the CA the token carries.
    """
    token = Token.parse(token_text)
    key, request = new_key_and_request(token.connector_id)
    options = [("grpc.ssl_target_name_override", server_name)] if server_name else []
    channel = grpc.secure_channel(
        endpoint, grpc.ssl_channel_credentials(root_certificates=token.ca_pem), options=options
    )
    try:
        answer = json.loads(
            channel.unary_unary(ENROLL_METHOD)(
                json.dumps({"token": token_text, "request": request.decode()}).encode(),
                timeout=timeout,
            )
        )
    finally:
        channel.close()
    certificate = answer["certificate"].encode()
    ca = answer["ca"].encode()
    if ca.strip() != token.ca_pem.strip():
        raise EnrollmentError("the control plane answered with another CA")
    issued = x509.load_pem_x509_certificate(certificate)
    sans = issued.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if f"{CONNECTOR_SAN_PREFIX}{token.connector_id}" not in sans.get_values_for_type(
        x509.UniformResourceIdentifier
    ):
        raise EnrollmentError("the certificate does not name this connector")
    ours = serialization.load_pem_private_key(key, password=None).public_key()
    if issued.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != ours.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        raise EnrollmentError("the certificate is not for this connector's key")
    return Identity(certificate, key), ca


__all__ = [
    "ENROLL_METHOD",
    "ConnectorRecord",
    "EnrollmentError",
    "EnrollmentServer",
    "MemoryStore",
    "Registry",
    "RegistryStore",
    "Token",
    "enroll",
]
