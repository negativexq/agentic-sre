"""Observed message delivery as a structural path (m21 contract §15): producer -> consumer, per message."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.model import Edge, EntityRef, Lifecycle, ObjectVersion, TraceSpanObservation
from packages.rca.runtime_graph import canonicalize_trace_spans
from packages.rca.topology import Topology, with_message_delivery

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
NS = "shop"
ORDER = EntityRef(kind="Deployment", name="order-service", namespace=NS)
WORKER = EntityRef(kind="Deployment", name="order-worker", namespace=NS)
ORDER_POD = EntityRef(kind="Pod", name="order-service-abc-1", namespace=NS)
WORKER_POD = EntityRef(kind="Pod", name="order-worker-def-1", namespace=NS)
STRESS = EntityRef(kind="StressChaos", name="stress", namespace=NS)


def span(
    span_id: str,
    service: str,
    kind: str,
    pod: str,
    *,
    parent: str | None = None,
    at: float = 60.0,
) -> TraceSpanObservation:
    return TraceSpanObservation(
        trace_id="t1",
        span_id=span_id,
        parent_span_id=parent,
        service=service,
        span_kind=kind,
        start_at=T0 + timedelta(seconds=at),
        end_at=T0 + timedelta(seconds=at + 0.1),
        semantic_attributes={
            "k8s.namespace.name": NS,
            "k8s.deployment.name": service,
            "k8s.pod.name": pod,
        },
        evidence_id=f"span:{span_id}",
    )


def present(ref: EntityRef) -> ObjectVersion:
    return ObjectVersion(
        entity=ref,
        uid=f"uid-{ref.name}",
        observed_at=T0,
        body={"metadata": {"name": ref.name, "uid": f"uid-{ref.name}"}},
        evidence_id=f"object:{ref.name}",
        lifecycle=Lifecycle.OBSERVED,
    )


HISTORY = {ref: [present(ref)] for ref in (ORDER, WORKER, ORDER_POD, WORKER_POD)}
BASE = Topology([Edge(source=STRESS, relation="disrupts", target=ORDER_POD)], {})


def delivered(*spans: TraceSpanObservation) -> Topology:
    return with_message_delivery(BASE, canonicalize_trace_spans(spans), HISTORY)


def deliveries(topology: Topology) -> set[tuple[str, str]]:
    return {
        (edge.source.name, edge.target.name)
        for edge in topology.edges
        if edge.relation == "delivers_to"
    }


def test_a_consumer_span_under_another_services_span_is_a_delivery_from_producer_to_consumer() -> (
    None
):
    topology = delivered(
        span("p", "order-service", "SERVER", ORDER_POD.name),
        span("c", "order-worker", "CONSUMER", WORKER_POD.name, parent="p", at=61),
    )
    assert deliveries(topology) == {
        (a.name, b.name) for a in (ORDER, ORDER_POD) for b in (WORKER, WORKER_POD)
    }
    assert topology.edge_evidence[(ORDER, "delivers_to", WORKER)] == ("span:c", "span:p")


def test_a_fault_on_the_producers_pod_reaches_the_consumer_and_not_the_other_way() -> None:
    topology = delivered(
        span("p", "order-service", "SERVER", ORDER_POD.name),
        span("c", "order-worker", "CONSUMER", WORKER_POD.name, parent="p", at=61),
    )
    path = topology.causal_path(STRESS, {WORKER})
    assert path is not None and [hop.relation for hop in path] == ["disrupts", "delivers_to"]
    assert topology.causal_path(WORKER, {ORDER, ORDER_POD}) is None


def test_no_delivery_within_a_service_before_the_producer_or_from_a_client_server_pair() -> None:
    same = delivered(
        span("p", "order-worker", "INTERNAL", WORKER_POD.name),
        span("c", "order-worker", "CONSUMER", WORKER_POD.name, parent="p", at=61),
    )
    earlier = delivered(
        span("p", "order-service", "SERVER", ORDER_POD.name, at=62),
        span("c", "order-worker", "CONSUMER", WORKER_POD.name, parent="p", at=61),
    )
    synchronous = delivered(
        span("p", "order-service", "CLIENT", ORDER_POD.name),
        span("c", "order-worker", "SERVER", WORKER_POD.name, parent="p", at=61),
    )
    assert deliveries(same) == deliveries(earlier) == deliveries(synchronous) == set()


def test_endpoints_not_observed_present_at_the_spans_time_are_not_admitted() -> None:
    gone = ObjectVersion(
        entity=WORKER_POD,
        uid="uid-gone",
        observed_at=T0 + timedelta(seconds=30),
        body={"metadata": {"name": WORKER_POD.name}},
        evidence_id="object:gone",
        lifecycle=Lifecycle.DELETED,
    )
    history = {**HISTORY, WORKER_POD: [present(WORKER_POD), gone]}
    topology = with_message_delivery(
        BASE,
        canonicalize_trace_spans(
            (
                span("p", "order-service", "SERVER", ORDER_POD.name),
                span("c", "order-worker", "CONSUMER", WORKER_POD.name, parent="p", at=61),
            )
        ),
        history,
    )
    assert ("order-service", WORKER_POD.name) not in deliveries(topology)
    assert ("order-service", "order-worker") in deliveries(topology)
