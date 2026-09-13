"""Offline tests for A1 causal identity and structured decision contracts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.contracts import EvidenceSourceType, TimeWindow
from packages.investigation import (
    CausalHypothesis,
    CausalStopDecision,
    DependencyResourceId,
    EvidenceCategory,
    HypothesisMechanism,
    StopReason,
    StructuredTrigger,
    TriggerType,
    WorkloadComponentId,
)


def test_structured_trigger_supports_workload_and_resource_targets() -> None:
    trigger = StructuredTrigger(
        trigger_type=TriggerType.DB_CONNECTION_PRESSURE,
        trigger_component=WorkloadComponentId.PAYMENT_SERVICE,
        trigger_resource=DependencyResourceId.POSTGRESQL,
    )

    assert trigger.model_validate_json(trigger.model_dump_json()) == trigger


def test_structured_trigger_rejects_unknown_or_targetless_values() -> None:
    with pytest.raises(ValidationError):
        StructuredTrigger.model_validate(
            {"trigger_type": "not-a-trigger", "trigger_component": "payment-service"}
        )
    with pytest.raises(ValidationError):
        StructuredTrigger(trigger_type=TriggerType.ERROR_RATE_INCREASE)


def test_causal_hypothesis_has_bounded_strict_structured_fields() -> None:
    hypothesis = CausalHypothesis(
        symptom_component=WorkloadComponentId.PAYMENT_SERVICE,
        causal_component=WorkloadComponentId.PAYMENT_SERVICE,
        causal_resource=DependencyResourceId.POSTGRESQL,
        mechanism=HypothesisMechanism.DATABASE_CONNECTION_PRESSURE,
        structured_trigger=StructuredTrigger(
            trigger_type=TriggerType.DB_CONNECTION_PRESSURE,
            trigger_component=WorkloadComponentId.PAYMENT_SERVICE,
            trigger_resource=DependencyResourceId.POSTGRESQL,
        ),
        causal_summary="Payment connection acquisition latency increased.",
        evidence_ids=[uuid4()],
    )

    legacy = hypothesis.to_legacy_submission()
    assert legacy.affected_component == "payment-service"
    assert len(hypothesis.model_dump_json()) > 0

    with pytest.raises(ValidationError):
        CausalHypothesis.model_validate({**hypothesis.model_dump(mode="json"), "extra": True})
    with pytest.raises(ValidationError):
        CausalHypothesis.model_validate(
            {**hypothesis.model_dump(mode="json"), "causal_summary": "x" * 1_001}
        )


def test_structured_stop_is_bounded_and_round_trips() -> None:
    stop = CausalStopDecision(
        stop_reason=StopReason.INSUFFICIENT_EVIDENCE,
        considered_components=[WorkloadComponentId.ORDER_SERVICE],
        considered_resources=[DependencyResourceId.KAFKA],
        missing_evidence_categories=[EvidenceCategory.MESSAGING],
    )
    assert CausalStopDecision.model_validate_json(stop.model_dump_json()) == stop
    with pytest.raises(ValidationError):
        CausalStopDecision.model_validate(
            {**stop.model_dump(mode="json"), "considered_components": ["unknown"]}
        )


def test_evidence_target_fields_round_trip_without_causal_claim() -> None:
    from packages.contracts import Evidence

    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    evidence = Evidence(
        incident_id=uuid4(),
        source_type=EvidenceSourceType.METRIC,
        source_system="service_latency",
        observation={"records": [{"p95": 1.2}]},
        time_window=TimeWindow(starts_at=now, ends_at=now),
        tool_call_id=uuid4(),
        raw_result_reference="service_latency://call",
        collected_at=now,
        target_workload=WorkloadComponentId.PAYMENT_SERVICE.value,
    )

    assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence
    assert evidence.target_workload == "payment-service"
