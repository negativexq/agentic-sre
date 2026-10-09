"""gRPC over mTLS, the Connector dials out (contract §12).

The Control Plane hosts a ``ConnectorGateway``; a ``ConnectorAgent`` (customer side) dials it and
opens one long-lived bidirectional stream. Requests travel Control Plane to Connector over that
stream and the answers return on it. Messages are ``request_id`` (8 bytes) plus the §4 canonical
bytes, untouched; no ``.proto`` is compiled, so the service is served with generic handlers and
identity serializers.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import random
import struct
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from concurrent import futures
from datetime import datetime
from pathlib import Path
from typing import Any

import grpc

from packages.connector import wire
from packages.connector.client import Transport
from packages.connector.pki import CONNECTOR_SAN_PREFIX, Identity
from packages.connector.service import Connector

logger = logging.getLogger(__name__)

SERVICE = "connector.v1.Session"
METHOD = f"/{SERVICE}/Open"
# connector-install-design.md §A8.3: a new certificate for the caller's own identity, over mutual TLS
RENEW_METHOD = f"/{SERVICE}/Renew"
MAX_MESSAGE = wire.MAX_RESPONSE_BYTES + 1_048_576
KEEPALIVE_MS = 20_000
KEEPALIVE_TIMEOUT_MS = 10_000
DEFAULT_TIMEOUT_SECONDS = 30.0


def frame(request_id: int, payload: bytes) -> bytes:
    return struct.pack(">Q", request_id) + payload


def unframe(message: bytes) -> tuple[int, bytes]:
    if len(message) < 8:
        raise ValueError("frame shorter than its request id")
    return int(struct.unpack(">Q", message[:8])[0]), message[8:]


class _Session:
    """One live connector stream: outgoing frames and the requests awaiting an answer."""

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id
        self.outgoing: queue.Queue[bytes | None] = queue.Queue()
        self.closed = threading.Event()
        self._pending: dict[int, list[Any]] = {}
        self._lock = threading.Lock()

    def request(self, request_id: int, payload: bytes, timeout: float) -> bytes:
        slot: list[Any] = [threading.Event(), None]
        with self._lock:
            if self.closed.is_set():
                raise ConnectionError("the connector session is closed")
            self._pending[request_id] = slot
        self.outgoing.put(frame(request_id, payload))
        if not slot[0].wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"no answer from connector {self.connector_id} in {timeout}s")
        if slot[1] is None:
            raise ConnectionError("the connector session dropped before answering")
        return bytes(slot[1])

    def resolve(self, request_id: int, payload: bytes) -> None:
        with self._lock:
            slot = self._pending.pop(request_id, None)
        if slot is not None:  # a late answer to a request that already ended is discarded
            slot[1] = payload
            slot[0].set()

    def close(self) -> None:
        with self._lock:
            self.closed.set()
            waiting = list(self._pending.values())
            self._pending.clear()
        for slot in waiting:
            slot[0].set()  # their answer stays None: the session dropped
        self.outgoing.put(None)


class ConnectorGateway:
    """The control-plane end: accepts connector sessions and turns them into ``Transport``s."""

    def __init__(
        self,
        address: str,
        *,
        server: Identity,
        client_ca: bytes,
        allowed: Collection[str],
        is_allowed: Callable[[str], bool] | None = None,
        renew: Callable[[str, bytes], bytes] | None = None,
        sweep_interval: float = 10.0,
        request_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        on_connect: Callable[[str], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
        max_workers: int = 32,
        max_message: int = MAX_MESSAGE,
    ) -> None:
        self._address = address
        self._server_identity = server
        self._client_ca = client_ca
        self._allowed = frozenset(allowed)
        # connector-install-design.md §A8.2: the registry's active Connectors, beside the fixed allow-list
        self._is_allowed = is_allowed
        # §A8.3: the registry signs renewals; a revoked identity's live session is closed by the sweep
        self._renew = renew
        self._sweep_interval = sweep_interval
        self._sweeping = threading.Event()
        self._expiry: dict[str, datetime] = {}
        self._timeout = request_timeout
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._max_workers = max_workers
        self._max_message = max_message
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._server: grpc.Server | None = None
        self.port = 0

    def start(self) -> int:
        server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=self._max_workers),
            options=[
                ("grpc.max_receive_message_length", self._max_message),
                ("grpc.max_send_message_length", self._max_message),
                ("grpc.keepalive_time_ms", KEEPALIVE_MS),
                ("grpc.keepalive_timeout_ms", KEEPALIVE_TIMEOUT_MS),
                ("grpc.keepalive_permit_without_calls", 1),
                ("grpc.http2.min_ping_interval_without_data_ms", KEEPALIVE_MS // 2),
                ("grpc.http2.max_pings_without_data", 0),
            ],
        )
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    SERVICE,
                    {
                        "Open": grpc.stream_stream_rpc_method_handler(self._open),
                        "Renew": grpc.unary_unary_rpc_method_handler(self._renew_call),
                    },
                ),
            )
        )
        credentials = grpc.ssl_server_credentials(
            ((self._server_identity.private_key, self._server_identity.certificate),),
            root_certificates=self._client_ca,
            require_client_auth=True,
        )
        self.port = server.add_secure_port(self._address, credentials)
        server.start()
        self._server = server
        self._sweeping.clear()
        threading.Thread(target=self._sweep, daemon=True).start()
        return self.port

    def stop(self) -> None:
        self._sweeping.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.close()
        if self._server is not None:
            self._server.stop(grace=1).wait(5)
            self._server = None

    def connected(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._sessions)

    def transport(self, connector_id: str) -> Transport:
        """A ``Transport`` for one connector; it raises ``ConnectionError`` while none is connected."""

        def send(payload: bytes) -> bytes:
            with self._lock:
                session = self._sessions.get(connector_id)
            if session is None:
                raise ConnectionError(f"connector {connector_id} is not connected")
            return session.request(next(self._ids), payload, self._timeout)

        return send

    def certificate_expiry(self, connector_id: str) -> datetime | None:
        """When the certificate a connected Connector presented expires (§A8.3)."""
        with self._lock:
            return self._expiry.get(connector_id) if connector_id in self._sessions else None

    def _sweep(self) -> None:
        """Close the live session of every identity no longer allowed (a revocation, §A8.3)."""
        while not self._sweeping.wait(self._sweep_interval):
            with self._lock:
                revoked = [s for i, s in self._sessions.items() if not self.allows(i)]
            for session in revoked:
                logger.warning("closing the session of revoked connector %s", session.connector_id)
                session.close()

    def _renew_call(self, request: bytes, context: grpc.ServicerContext) -> bytes:
        identity = self._identity(context)
        if identity is None or not self.allows(identity) or self._renew is None:
            logger.warning("refused a renewal for identity %r", identity)
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "renewal refused")
        assert identity is not None and self._renew is not None
        try:
            return self._renew(identity, request)
        except (
            Exception
        ) as error:  # the registry refuses: log the reason, tell the caller nothing more
            logger.warning("refused a renewal for %s: %s", identity, error)
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "renewal refused")
            raise

    def allows(self, identity: str) -> bool:
        return identity in self._allowed or (
            self._is_allowed is not None and self._is_allowed(identity)
        )

    @staticmethod
    def _identity(context: grpc.ServicerContext) -> str | None:
        for value in context.auth_context().get("x509_subject_alternative_name", []):
            text = str(value.decode("utf-8", "replace"))
            if text.startswith(CONNECTOR_SAN_PREFIX):
                return str(text[len(CONNECTOR_SAN_PREFIX) :])
        return None

    def _open(self, requests: Iterator[bytes], context: grpc.ServicerContext) -> Iterator[bytes]:
        identity = self._identity(context)
        if identity is None or not self.allows(identity):
            logger.warning("refused a connector session for identity %r", identity)
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "connector is not allowed")
        assert identity is not None
        session = _Session(identity)
        expiry = _certificate_expiry(context)
        with self._lock:
            previous = self._sessions.get(identity)
            self._sessions[identity] = session
            if expiry is not None:
                self._expiry[identity] = expiry
        if previous is not None:
            previous.close()

        def read() -> None:
            try:
                for message in requests:
                    try:
                        request_id, payload = unframe(message)
                    except ValueError:
                        continue
                    session.resolve(request_id, payload)
            except Exception:  # the stream ended or broke: either way the session is over
                logger.debug("connector stream ended", exc_info=True)
            finally:
                session.close()

        threading.Thread(target=read, daemon=True).start()
        if self.on_connect is not None:
            threading.Thread(target=self.on_connect, args=(identity,), daemon=True).start()
        try:
            while True:
                item = session.outgoing.get()
                if item is None:
                    return
                yield item
        finally:
            session.close()
            with self._lock:
                if self._sessions.get(identity) is session:
                    del self._sessions[identity]
            if self.on_disconnect is not None:
                threading.Thread(target=self.on_disconnect, args=(identity,), daemon=True).start()


def _certificate_expiry(context: grpc.ServicerContext) -> datetime | None:
    from cryptography import x509

    for pem in context.auth_context().get("x509_pem_cert", []):
        try:
            return x509.load_pem_x509_certificate(pem).not_valid_after_utc
        except ValueError:
            return None
    return None


class ConnectorAgent:
    """The customer end: dials out, serves requests with a ``Connector``, reconnects on loss.

    The ``Connector`` object (and so its stream epoch) outlives every session, which is why a
    reconnect is not a restart and declares no ``Gap``.
    """

    def __init__(
        self,
        connector: Connector,
        target: str,
        *,
        identity: Identity,
        server_ca: bytes,
        server_name: str | None = None,
        workers: int = 8,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
        max_message: int = MAX_MESSAGE,
    ) -> None:
        self._connector = connector
        self._target = target
        self._identity = identity
        self._server_ca = server_ca
        self._server_name = server_name
        self._identity_lock = threading.Lock()
        self._restart = threading.Event()
        self._workers = workers
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._max_message = max_message

    @property
    def identity(self) -> Identity:
        with self._identity_lock:
            return self._identity

    def replace_identity(self, identity: Identity) -> None:
        """Use a renewed certificate (§A8.3): the session is reopened with it at once; the epoch is kept."""
        with self._identity_lock:
            self._identity = identity
        self._restart.set()

    def run(self, stop: threading.Event) -> None:
        """Connect, serve until the session ends, back off (1 s to 30 s, jittered), repeat."""
        backoff = self._min_backoff
        while not stop.is_set():
            started = time.monotonic()
            try:
                self._serve_once(stop)
            except grpc.RpcError as error:
                logger.warning("connector session ended: %s", error)
            except Exception:
                logger.warning("connector session failed", exc_info=True)
            if self._restart.is_set():  # a renewal, not a failure: reconnect at once
                self._restart.clear()
                backoff = self._min_backoff
                continue
            if time.monotonic() - started > self._max_backoff:
                backoff = self._min_backoff
            stop.wait(min(backoff, self._max_backoff) * (0.5 + random.random() / 2))
            backoff = min(backoff * 2, self._max_backoff)

    def _serve_once(self, stop: threading.Event) -> None:
        identity = self.identity
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self._server_ca,
            private_key=identity.private_key,
            certificate_chain=identity.certificate,
        )
        options: list[tuple[str, Any]] = [
            ("grpc.max_receive_message_length", self._max_message),
            ("grpc.max_send_message_length", self._max_message),
            ("grpc.keepalive_time_ms", KEEPALIVE_MS),
            ("grpc.keepalive_timeout_ms", KEEPALIVE_TIMEOUT_MS),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
        ]
        if self._server_name is not None:
            options.append(("grpc.ssl_target_name_override", self._server_name))
        outgoing: queue.Queue[bytes | None] = queue.Queue()

        def requests() -> Iterator[bytes]:
            while True:
                item = outgoing.get()
                if item is None:
                    return
                yield item

        pool = futures.ThreadPoolExecutor(max_workers=self._workers)
        ended = threading.Event()
        with grpc.secure_channel(self._target, credentials, options=options) as channel:
            call = channel.stream_stream(METHOD)(requests())

            def watch_stop() -> None:
                while not ended.wait(0.5):
                    if stop.is_set() or self._restart.is_set():
                        call.cancel()
                        return

            threading.Thread(target=watch_stop, daemon=True).start()
            try:
                for message in call:
                    request_id, payload = unframe(message)
                    pool.submit(self._answer, request_id, payload, outgoing)
            finally:
                ended.set()
                outgoing.put(None)
                pool.shutdown(wait=False, cancel_futures=True)

    def _answer(self, request_id: int, payload: bytes, outgoing: queue.Queue[bytes | None]) -> None:
        outgoing.put(frame(request_id, self._connector.handle(payload)))


def gateway_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    is_allowed: Callable[[str], bool] | None = None,
    renew: Callable[[str, bytes], bytes] | None = None,
) -> tuple[ConnectorGateway, str]:
    """The control plane's gateway and the id of the one connector it talks to.

    ``SRE_CONNECTOR_ALLOWED`` lists the accepted identities; with more than one, the one in use is
    named by ``SRE_CONNECTOR_ID`` (one connector per control plane in this version).
    """
    env = os.environ if environ is None else environ
    allowed = tuple(i.strip() for i in env.get("SRE_CONNECTOR_ALLOWED", "").split(",") if i.strip())
    connector_id = env.get("SRE_CONNECTOR_ID") or (allowed[0] if len(allowed) == 1 else "")
    # an id enrolled through the registry (connector-install-design.md §A8.2) need not be listed here
    if not connector_id or (connector_id not in allowed and is_allowed is None):
        raise ValueError("SRE_CONNECTOR_ID must name one identity of SRE_CONNECTOR_ALLOWED")
    gateway = ConnectorGateway(
        env.get("SRE_CONNECTOR_LISTEN", "0.0.0.0:8443"),  # noqa: S104 - the gateway must be reachable
        server=Identity(
            Path(env["SRE_CONNECTOR_TLS_CERT"]).read_bytes(),
            Path(env["SRE_CONNECTOR_TLS_KEY"]).read_bytes(),
        ),
        client_ca=Path(env["SRE_CONNECTOR_TLS_CLIENT_CA"]).read_bytes(),
        allowed=allowed,
        is_allowed=is_allowed,
        renew=renew,
        request_timeout=float(env.get("SRE_CONNECTOR_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    )
    return gateway, connector_id


def renew_identity(
    target: str,
    identity: Identity,
    connector_id: str,
    *,
    server_ca: bytes,
    server_name: str | None = None,
    timeout: float = 15.0,
) -> Identity:
    """A new key and certificate, asked for over mutual TLS with the current identity (§A8.3)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from packages.connector.pki import new_key_and_request

    key, request = new_key_and_request(connector_id)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=server_ca,
        private_key=identity.private_key,
        certificate_chain=identity.certificate,
    )
    options = [("grpc.ssl_target_name_override", server_name)] if server_name else []
    with grpc.secure_channel(target, credentials, options=options) as channel:
        certificate: bytes = channel.unary_unary(RENEW_METHOD)(request, timeout=timeout)
    issued = x509.load_pem_x509_certificate(certificate)
    sans = issued.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if f"{CONNECTOR_SAN_PREFIX}{connector_id}" not in sans.get_values_for_type(
        x509.UniformResourceIdentifier
    ):
        raise ValueError("the renewed certificate does not name this connector")
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    ours = serialization.load_pem_private_key(key, password=None).public_key()
    if issued.public_key().public_bytes(serialization.Encoding.DER, spki) != ours.public_bytes(
        serialization.Encoding.DER, spki
    ):
        raise ValueError("the renewed certificate is not for the new key")
    return Identity(certificate, key)
