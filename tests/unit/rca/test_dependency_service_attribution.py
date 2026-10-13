"""A caller's Service error must not invent failures on every selected endpoint."""

import json
from pathlib import Path
from typing import Any

import pytest
from rca_builders import alert, at, event, microservice, pod, ref, version

from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import Edge, EntityRef, FindingKind, LogRecord, Resolution
from packages.rca.signals import dependency_findings
from packages.rca.source import InMemorySource
from packages.rca.topology import Topology, derive_edges

RECORDED_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "dependency_service_attribution.json").read_text()
)


@pytest.mark.parametrize("recorded", RECORDED_CASES, ids=lambda row: row["case"])
def test_holdout23_caller_evidence_is_not_copied_to_three_pod_subjects(
    recorded: dict[str, Any],
) -> None:
    # Distilled attribution inputs from the three final diagnoses, not an RCA replay.
    caller, service = ref(recorded["caller"]), ref(recorded["service"])
    pods = [EntityRef.model_validate(value) for value in recorded["pod_subjects"]]
    assert len(pods) == 3
    topology = Topology(
        (
            Edge(source=caller, target=service, relation="calls"),
            *(Edge(source=service, target=pod, relation="selects") for pod in pods),
        ),
        {},
    )
    logs = [
        LogRecord(
            service=caller.name,
            at=None,
            severity="ERROR",
            message=recorded["message_excerpt"],
            evidence_id=evidence_id,
        )
        for evidence_id in recorded["evidence_ids"]
    ]
    findings = dependency_findings(logs, topology, {caller})
    assert [finding.entity for finding in findings] == [service]
    assert findings[0].evidence_ids == tuple(recorded["evidence_ids"])


def source(replicas: int, *, pod_failure: bool = False) -> InMemorySource:
    payment = [item for item in microservice("payment", 0) if item.entity.kind != "Pod"]
    payment.extend(
        version(
            f"shop/Pod/payment-5d8f7c9b4-replica-{index}",
            0,
            pod(f"payment-5d8f7c9b4-replica-{index}", "payment", "payment-5d8f7c9b4"),
        )
        for index in range(replicas)
    )
    return InMemorySource(
        name="service-errors-with-replicas",
        alert_items=[alert("OrderErrorRateHigh", "order", 1)],
        versions=[*microservice("order", 0, {"PAYMENT_URL": "payment:8080"}), *payment],
        error_items=[
            LogRecord(
                service="order",
                at=at(1),
                severity="ERROR",
                message="payment: connection refused",
                evidence_id="loki:order:refusal",
            )
        ],
        event_items=[event("shop/Pod/payment-5d8f7c9b4-replica-0", "Failed", 1, type_="Warning")]
        if pod_failure
        else [],
    )


@pytest.mark.parametrize("replicas", [0, 1, 3])
def test_a_service_error_has_one_service_subject_regardless_of_endpoint_count(
    replicas: int,
) -> None:
    observed = source(replicas)
    latest = {entity: versions[-1] for entity, versions in observed.object_history().items()}
    topology = Topology(derive_edges(latest, []), latest)
    assert len(topology.outgoing(ref("shop/Service/payment"), "selects")) == replicas

    findings = dependency_findings(observed.error_items, topology, {ref("shop/Deployment/order")})
    assert len(findings) == 1
    assert findings[0].entity == ref("shop/Service/payment")
    assert findings[0].entity_instance is None
    assert findings[0].evidence_ids == ("loki:order:refusal",)


def test_service_evidence_does_not_create_pod_rivals_or_establish_a_cause() -> None:
    case = build_case(source(3))
    log_claims = [
        claim
        for claim in case.hypotheses
        if any("loki:order:refusal" in finding.evidence_ids for finding in claim.findings)
    ]
    assert log_claims
    assert {claim.causal_actor for claim in log_claims} == {ref("shop/Service/payment")}
    assert diagnose_case(case).resolution is not Resolution.RESOLVED


def test_an_independent_pod_failure_keeps_its_own_claim_and_provenance() -> None:
    case = build_case(source(3, pod_failure=True))
    failed_pod = ref("shop/Pod/payment-5d8f7c9b4-replica-0")
    claims = [claim for claim in case.hypotheses if claim.causal_actor == failed_pod]
    assert claims
    assert any(
        finding.kind is FindingKind.FAILURE_EVENT and finding.entity == failed_pod
        for claim in claims
        for finding in claim.findings
    )
    assert all(
        "loki:order:refusal" not in finding.evidence_ids
        for claim in claims
        for finding in claim.findings
    )
