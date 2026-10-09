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
                    {"Open": grpc.stream_stream_rpc_method_handler(self._open)},
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
        return self.port

    def stop(self) -> None:
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
        with self._lock:
            previous = self._sessions.get(identity)
            self._sessions[identity] = session
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
        self._workers = workers
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._max_message = max_message

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
            if time.monotonic() - started > self._max_backoff:
                backoff = self._min_backoff
            stop.wait(min(backoff, self._max_backoff) * (0.5 + random.random() / 2))
            backoff = min(backoff * 2, self._max_backoff)

    def _serve_once(self, stop: threading.Event) -> None:
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self._server_ca,
            private_key=self._identity.private_key,
            certificate_chain=self._identity.certificate,
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
                    if stop.is_set():
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
        request_timeout=float(env.get("SRE_CONNECTOR_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    )
    return gateway, connector_id
