"""A loopback gateway plus agents for Connectors, with generated certificates (tests only)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from packages.connector.client import Transport
from packages.connector.pki import Identity, issue_connector, issue_server, new_ca
from packages.connector.service import Connector
from packages.connector.transport import MAX_MESSAGE, ConnectorAgent, ConnectorGateway

IDS = [f"lab-{i}" for i in range(16)]


class GrpcHarness:
    """One gateway on loopback; ``attach`` runs an agent that serves a Connector over it."""

    def __init__(
        self,
        *,
        ca: Identity | None = None,
        allowed: list[str] | None = None,
        timeout: float = 10.0,
        max_message: int | None = None,
        is_allowed: Callable[[str], bool] | None = None,
        renew: Callable[[str, bytes], bytes] | None = None,
        sweep_interval: float = 10.0,
    ) -> None:
        self.ca = ca or new_ca()
        self.server = issue_server(self.ca, ["localhost", "127.0.0.1"])
        self.max_message = max_message
        self.gateway = ConnectorGateway(
            "127.0.0.1:0",
            server=self.server,
            client_ca=self.ca.certificate,
            allowed=IDS if allowed is None else allowed,
            is_allowed=is_allowed,
            renew=renew,
            sweep_interval=sweep_interval,
            request_timeout=timeout,
            max_message=MAX_MESSAGE if max_message is None else max_message,
        )
        self.port = self.gateway.start()
        self._agents: list[tuple[threading.Event, threading.Thread]] = []
        self._next = 0

    def agent(
        self,
        connector: Connector,
        identity: Identity,
        *,
        ca_certificate: bytes | None = None,
    ) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()
        agent = ConnectorAgent(
            connector,
            f"localhost:{self.port}",
            identity=identity,
            server_ca=ca_certificate or self.ca.certificate,
            server_name="localhost",
            min_backoff=0.1,
            max_backoff=0.4,
            max_message=MAX_MESSAGE if self.max_message is None else self.max_message,
        )
        thread = threading.Thread(target=agent.run, args=(stop,), daemon=True)
        thread.start()
        self._agents.append((stop, thread))
        return stop, thread

    def wait_connected(self, connector_id: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while connector_id not in self.gateway.connected():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{connector_id} never connected")
            time.sleep(0.02)

    def attach(self, connector: Connector) -> Transport:
        connector_id = IDS[self._next]
        self._next += 1
        self.agent(connector, issue_connector(self.ca, connector_id))
        self.wait_connected(connector_id)
        return self.gateway.transport(connector_id)

    def close(self) -> None:
        for stop, _ in self._agents:
            stop.set()
        for _, thread in self._agents:
            thread.join(timeout=5)
        self.gateway.stop()
