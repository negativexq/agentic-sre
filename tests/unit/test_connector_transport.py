"""What only a real transport can get wrong (contract §12): identity, sessions, loss, limits."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest
from connector_harness import GrpcHarness

from packages.connector import wire
from packages.connector.client import (
    ConnectorClient,
    ConnectorClusterReader,
    ConnectorReadError,
    ConnectorUnavailable,
)
from packages.connector.pki import issue_connector, new_ca
from packages.connector.service import Connector
from packages.connector.transport import METHOD, frame, unframe
from packages.rca.live import ListingScope, ObjectListing


class Cluster:
    def __init__(self, *, block: threading.Event | None = None, delay: float = 0.0) -> None:
        self.block = block
        self.delay = delay
        self.entered = threading.Event()
        self.pods = [{"kind": "Pod", "metadata": {"name": "a", "namespace": "shop"}}]

    def list_objects(self, namespaces: Sequence[str]) -> ObjectListing:
        return ObjectListing(tuple(self.pods), frozenset({ListingScope("shop", "Pod")}), ())

    def list_events(self, namespaces: Sequence[str]) -> list[dict[str, Any]]:
        self.entered.set()
        if self.block is not None:
            self.block.wait(20)
        if self.delay:
            time.sleep(self.delay)
        return [{"kind": "Event"}]


@pytest.fixture
def harness() -> Iterator[GrpcHarness]:
    h = GrpcHarness(timeout=5.0)
    yield h
    h.close()


def refused_for(harness: GrpcHarness, connector_id: str, **agent: Any) -> bool:
    """True when an agent with that identity never gets a session in ~1.5 s."""
    harness.agent(Connector(cluster=Cluster()), **agent)
    time.sleep(1.5)
    return connector_id not in harness.gateway.connected()


def test_an_allowed_connector_is_served_over_mtls(harness: GrpcHarness) -> None:
    transport = harness.attach(Connector(cluster=Cluster()))
    assert harness.gateway.connected() == {"lab-0"}
    assert ConnectorClient(transport).capabilities() == ("changes", "events", "history")


def test_a_certificate_for_an_identity_off_the_allow_list_is_refused(
    harness: GrpcHarness,
) -> None:
    intruder = issue_connector(harness.ca, "intruder")
    assert refused_for(harness, "intruder", identity=intruder)
    with pytest.raises(ConnectionError):
        harness.gateway.transport("intruder")(b"{}")


def test_a_certificate_from_another_authority_is_refused(harness: GrpcHarness) -> None:
    foreign = issue_connector(new_ca("some other CA"), "lab-0")
    assert refused_for(harness, "lab-0", identity=foreign)


def test_an_expired_certificate_is_refused(harness: GrpcHarness) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=30)
    expired = issue_connector(harness.ca, "lab-0", days=1, now=long_ago)
    assert refused_for(harness, "lab-0", identity=expired)


def test_a_client_without_a_certificate_is_refused(harness: GrpcHarness) -> None:
    credentials = grpc.ssl_channel_credentials(root_certificates=harness.ca.certificate)
    with grpc.secure_channel(
        f"localhost:{harness.port}",
        credentials,
        options=[("grpc.ssl_target_name_override", "localhost")],
    ) as channel:
        call = channel.stream_stream(METHOD)(iter(()))
        with pytest.raises(grpc.RpcError):
            next(iter(call))
    assert not harness.gateway.connected()


def _raw_session(harness: GrpcHarness, identity: Any) -> tuple[Any, Any, queue.Queue[bytes | None]]:
    """A hand-driven connector stream: (channel, call, outbox)."""
    credentials = grpc.ssl_channel_credentials(
        root_certificates=harness.ca.certificate,
        private_key=identity.private_key,
        certificate_chain=identity.certificate,
    )
    channel = grpc.secure_channel(
        f"localhost:{harness.port}",
        credentials,
        options=[("grpc.ssl_target_name_override", "localhost")],
    )
    outbox: queue.Queue[bytes | None] = queue.Queue()

    def requests() -> Iterator[bytes]:
        while True:
            item = outbox.get()
            if item is None:
                return
            yield item

    return channel, channel.stream_stream(METHOD)(requests()), outbox


def test_a_new_session_replaces_the_old_one_and_takes_the_requests(
    harness: GrpcHarness,
) -> None:
    identity = issue_connector(harness.ca, "lab-0")
    channel_one, first, box_one = _raw_session(harness, identity)
    harness.wait_connected("lab-0")
    channel_two, second, box_two = _raw_session(harness, identity)
    deadline = time.monotonic() + 5
    while not first.done() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert first.done()  # the replaced stream was closed by the gateway
    answer: list[bytes] = []
    asker = threading.Thread(
        target=lambda: answer.append(harness.gateway.transport("lab-0")(b"ping"))
    )
    asker.start()
    request_id, payload = unframe(next(iter(second)))
    assert payload == b"ping"
    box_two.put(frame(request_id, b"pong"))
    asker.join(5)
    assert answer == [b"pong"]
    box_one.put(None)
    box_two.put(None)
    channel_one.close()
    channel_two.close()


def test_a_drop_in_the_middle_of_a_request_fails_it(harness: GrpcHarness) -> None:
    block = threading.Event()
    cluster = Cluster(block=block)
    connector = Connector(cluster=cluster)
    stop, _ = harness.agent(connector, issue_connector(harness.ca, "lab-0"))
    harness.wait_connected("lab-0")
    client = ConnectorClient(harness.gateway.transport("lab-0"))
    outcome: list[BaseException] = []

    def ask() -> None:
        try:
            ConnectorClusterReader(client).list_events(["shop"])
        except BaseException as error:  # noqa: BLE001
            outcome.append(error)

    thread = threading.Thread(target=ask)
    thread.start()
    assert cluster.entered.wait(5)
    stop.set()  # the connector goes away with the request still in flight
    thread.join(10)
    block.set()
    assert outcome and isinstance(outcome[0], ConnectorUnavailable)


def test_a_reconnect_keeps_the_epoch_so_the_cursor_resumes_without_a_gap(
    harness: GrpcHarness,
) -> None:
    connector = Connector(cluster=Cluster(), watch_namespaces=("shop",))
    stop, thread = harness.agent(connector, issue_connector(harness.ca, "lab-0"))
    harness.wait_connected("lab-0")
    client = ConnectorClient(harness.gateway.transport("lab-0"))
    first = client.read("read_changes", None)
    stop.set()
    thread.join(5)
    deadline = time.monotonic() + 5
    while "lab-0" in harness.gateway.connected() and time.monotonic() < deadline:
        time.sleep(0.02)
    harness.agent(connector, issue_connector(harness.ca, "lab-0"))
    harness.wait_connected("lab-0")
    again = ConnectorClient(harness.gateway.transport("lab-0")).read(
        "read_changes", first.next_cursor
    )
    assert again.epoch == first.epoch and not again.gaps


def test_an_oversize_message_is_refused_and_the_request_fails() -> None:
    small = GrpcHarness(timeout=5.0, max_message=4096)
    try:
        cluster = Cluster()
        cluster.pods = [
            {"kind": "Pod", "metadata": {"name": f"p{i}", "namespace": "shop", "pad": "x" * 200}}
            for i in range(100)
        ]
        transport = small.attach(Connector(cluster=cluster))
        with pytest.raises(ConnectorUnavailable):
            ConnectorClusterReader(ConnectorClient(transport)).list_objects(["shop"])
    finally:
        small.close()


def test_a_slow_backend_ends_in_a_timeout_and_the_next_request_still_works() -> None:
    quick = GrpcHarness(timeout=0.5)
    try:
        cluster = Cluster(delay=1.5)
        transport = quick.attach(Connector(cluster=cluster))
        client = ConnectorClient(transport)
        with pytest.raises(ConnectorUnavailable):
            ConnectorClusterReader(client).list_events(["shop"])
        cluster.delay = 0
        time.sleep(1.5)  # the late answer arrives and is discarded
        assert ConnectorClusterReader(client).list_events(["shop"]) == [{"kind": "Event"}]
    finally:
        quick.close()


def test_a_control_plane_without_a_connector_reports_it_and_does_not_fail(
    harness: GrpcHarness,
) -> None:
    client = ConnectorClient(harness.gateway.transport("lab-0"))
    with pytest.raises(ConnectorUnavailable):
        client.capabilities()
    assert harness.gateway.connected() == frozenset()


def test_a_backend_error_crosses_the_stream_as_a_failure_not_a_lost_session(
    harness: GrpcHarness,
) -> None:
    transport = harness.attach(Connector())
    client = ConnectorClient(transport)
    with pytest.raises(ConnectorReadError):
        ConnectorClusterReader(client).list_objects(["shop"])
    assert harness.gateway.connected() == {"lab-0"}
    assert wire.WIRE_VERSION == "connector.v1"


def test_frames_carry_the_request_id_and_the_payload_untouched() -> None:
    payload = b'{"a":1}'
    assert unframe(frame(2**40 + 7, payload)) == (2**40 + 7, payload)
    with pytest.raises(ValueError):
        unframe(b"short")
