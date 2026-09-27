"""M20.3 F1.1: a log binds a dependency only by an exact, unambiguous DNS-label mention."""

from __future__ import annotations

import pytest
from rca_builders import microservice, ref

from packages.rca.model import Edge, LogRecord
from packages.rca.signals import dependency_findings, explicitly_names_dependency
from packages.rca.source import InMemorySource
from packages.rca.topology import Topology, derive_edges


@pytest.mark.parametrize(
    ("message", "dependency", "named"),
    [
        pytest.param("payment-service", "payment-service", True, id="bare"),
        pytest.param("GET http://payment-service:8080/pay", "payment-service", True, id="url"),
        pytest.param("dial payment-service.default.svc", "payment-service", True, id="fqdn"),
        pytest.param("order -> payment-service refused", "payment-service", True, id="arrow"),
        pytest.param("PAYMENT-SERVICE unreachable", "payment-service", True, id="case"),
        pytest.param("mypayment-service refused", "payment-service", False, id="prefix"),
        pytest.param("payment-service-v2 refused", "payment-service", False, id="suffix"),
        pytest.param("payment-service refused", "payment", False, id="partial-label"),
        pytest.param("PAYMENT_URL not reachable", "payment", False, id="identifier"),
        pytest.param("api.payment-service refused", "payment-service", False, id="namespace-label"),
        pytest.param("prepayment timed out", "payment", False, id="inside-word"),
        pytest.param("payment.svc (1+1) refused", "payment.svc", True, id="escaped"),
        pytest.param("paymentXsvc refused", "payment.svc", False, id="dot-is-literal"),
    ],
)
def test_explicit_dependency_mentions(message: str, dependency: str, named: bool) -> None:
    assert explicitly_names_dependency(message, dependency) is named


def test_non_ascii_case_folding_does_not_match() -> None:
    # "ſ" (long s) case-folds to "s" under Unicode rules; DNS labels are ASCII.
    assert explicitly_names_dependency("paymentſervice refused", "paymentservice") is False


ALERTING = {ref("shop/Deployment/order")}


def _topology(extra: tuple[Edge, ...] = ()) -> Topology:
    source = InMemorySource(
        name="binding",
        versions=[
            *microservice("order", 0, {"PAY_ADDR": "payment:1", "DB_ADDR": "order-db:5432"}),
            *microservice("payment", 0),
            *microservice("order-db", 0),
        ],
    )
    history = source.object_history()
    latest = {entity: versions[-1] for entity, versions in history.items()}
    return Topology((*derive_edges(latest, list(source.events())), *extra), latest)


def _log(message: str, evidence_id: str = "loki:1") -> LogRecord:
    return LogRecord(
        service="order", at=None, severity="ERROR", message=message, evidence_id=evidence_id
    )


def test_an_exact_mention_blames_the_declared_dependency() -> None:
    findings = dependency_findings([_log("payment: connection refused")], _topology(), ALERTING)
    assert {finding.related for finding in findings} == {(ref("shop/Service/payment"),)}


def test_a_partial_mention_binds_nothing_it_does_not_name() -> None:
    findings = dependency_findings(
        [_log("payment-gateway: connection refused")], _topology(), ALERTING
    )
    assert all(finding.related != (ref("shop/Service/payment"),) for finding in findings)


def test_two_declared_dependencies_with_one_name_bind_neither() -> None:
    other = ref("billing/Service/payment")
    topology = _topology(
        (Edge(source=ref("shop/Deployment/order"), target=other, relation="calls"),)
    )
    assert {ref("shop/Service/payment"), other} <= set(
        topology.outgoing(ref("shop/Deployment/order"), "calls")
    )
    # Fail closed: no finding for either payment, and no fallback to order-db either.
    assert dependency_findings([_log("payment: connection refused")], topology, ALERTING) == []


def test_an_undeclared_exact_looking_name_invents_no_dependency() -> None:
    findings = dependency_findings([_log("inventory: connection refused")], _topology(), ALERTING)
    blamed = {finding.related for finding in findings}
    assert (ref("shop/Service/inventory"),) not in blamed
    assert all(item[0].name in {"payment", "order-db"} for item in blamed)


def test_an_ambiguous_mention_does_not_fall_back_to_a_private_dependency() -> None:
    """The log names 'payment' (two shared declared services); order-db must not inherit it."""
    callers = ("order", "worker", "admin", "billing")
    source = InMemorySource(
        name="ambiguous",
        versions=[
            *microservice("order", 0, {"PAY_ADDR": "payment:1", "DB_ADDR": "order-db:5432"}),
            *[
                item
                for name in callers[1:]
                for item in microservice(name, 0, {"PAY_ADDR": "payment:1"})
            ],
            *microservice("payment", 0),
            *microservice("order-db", 0),
        ],
    )
    history = source.object_history()
    latest = {entity: versions[-1] for entity, versions in history.items()}
    edges = derive_edges(latest, list(source.events()))
    other = ref("billing/Service/payment")
    sources = {edge.source for edge in edges if edge.target == ref("shop/Service/payment")}
    topology = Topology(
        (*edges, *(Edge(source=item, target=other, relation="calls") for item in sources)),
        latest,
    )
    # Without the mention, today's rule blames the single private dependency.
    unnamed = dependency_findings([_log("connection refused")], topology, ALERTING)
    assert {finding.related for finding in unnamed} == {(ref("shop/Service/order-db"),)}
    # With an ambiguous mention nothing is blamed: no payment, and no fallback.
    assert dependency_findings([_log("payment: connection refused")], topology, ALERTING) == []
