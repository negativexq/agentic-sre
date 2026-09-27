"""M20.3 F1: an explicitly named declared dependency is not discarded as shared infrastructure."""

from __future__ import annotations

from rca_builders import microservice, ref

from packages.rca.model import LogRecord
from packages.rca.signals import dependency_findings
from packages.rca.source import InMemorySource
from packages.rca.topology import Topology, derive_edges

HOSTS = {"PAY_ADDR": "payment:1", "MQ_ADDR": "kafka:9092", "CACHE_ADDR": "redis:6379"}
ALERTING = {ref("shop/Deployment/order")}


def _shared_topology() -> Topology:
    """Every caller declares the same hosts, so payment, kafka and redis all look shared."""
    source = InMemorySource(
        name="shared",
        versions=[
            *microservice("order", 0, {**HOSTS, "DB_ADDR": "order-db:5432"}),
            *[
                item
                for name in ("worker", "admin", "billing")
                for item in microservice(name, 0, HOSTS)
            ],
            *microservice("order-db", 0),
            *microservice("payment", 0),
            *microservice("kafka", 0),
            *microservice("redis", 0),
        ],
    )
    history = source.object_history()
    latest = {entity: versions[-1] for entity, versions in history.items()}
    return Topology(derive_edges(latest, list(source.events())), latest)


def _log(message: str, evidence_id: str) -> LogRecord:
    return LogRecord(
        service="order", at=None, severity="ERROR", message=message, evidence_id=evidence_id
    )


def test_the_fixture_declares_three_shared_and_one_private_dependency() -> None:
    topology = _shared_topology()
    declared = set(topology.outgoing(ref("shop/Deployment/order"), "calls"))
    assert declared == {
        ref("shop/Service/order-db"),
        ref("shop/Service/payment"),
        ref("shop/Service/kafka"),
        ref("shop/Service/redis"),
    }


def test_a_named_shared_dependency_is_blamed_with_only_its_own_records() -> None:
    logs = [
        _log("dependency.request error: payment URLError: Connection refused", "loki:1"),
        _log("connection reset by peer", "loki:2"),
        _log("dependency.request error: payment timed out", "loki:3"),
    ]
    findings = dependency_findings(logs, _shared_topology(), ALERTING)
    assert {finding.related for finding in findings} == {(ref("shop/Service/payment"),)}
    assert all(finding.evidence_ids == ("loki:1", "loki:3") for finding in findings)
    assert all(finding.details["errors"] == 2 for finding in findings)


def test_shared_and_unnamed_stays_excluded() -> None:
    """Unnamed errors keep today's rule: shared ones drop out, the single private one is blamed."""
    logs = [_log("connection refused", "loki:1"), _log("dial tcp: i/o timeout", "loki:2")]
    findings = dependency_findings(logs, _shared_topology(), ALERTING)
    assert findings
    assert {finding.related for finding in findings} == {(ref("shop/Service/order-db"),)}
    assert all(finding.evidence_ids == ("loki:1", "loki:2") for finding in findings)


def test_log_text_never_invents_an_undeclared_dependency() -> None:
    logs = [_log("inventory: connection refused", "loki:1")]
    findings = dependency_findings(logs, _shared_topology(), ALERTING)
    # Nothing declared is named, so today's fallback applies: the private dependency only.
    assert {finding.related for finding in findings} == {(ref("shop/Service/order-db"),)}
    assert all("inventory" not in finding.entity.name for finding in findings)


def test_each_named_dependency_keeps_its_own_records() -> None:
    logs = [
        _log("payment: connection refused", "loki:1"),
        _log("redis: connection refused", "loki:2"),
    ]
    findings = dependency_findings(logs, _shared_topology(), ALERTING)
    by_service = {finding.related[0].name: finding.evidence_ids for finding in findings}
    assert by_service == {"payment": ("loki:1",), "redis": ("loki:2",)}


def test_a_non_error_log_naming_a_dependency_is_not_evidence() -> None:
    logs = [_log("payment request succeeded", "loki:1")]
    assert dependency_findings(logs, _shared_topology(), ALERTING) == []
