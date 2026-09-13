"""Offline tests for bounded A1 evidence and run artifact contracts."""

from datetime import UTC, datetime
from uuid import uuid4

from packages.contracts import Evidence, EvidenceSourceType, TimeWindow
from packages.investigation import (
    A1RunArtifact,
    A1SafetyCounters,
    CausalHypothesis,
    DependencyResourceId,
    EvidenceSummary,
    HypothesisMechanism,
    InvestigationUsage,
    StructuredTrigger,
    TerminationReason,
    TriggerType,
    TurnRecord,
    WorkloadComponentId,
)


def _usage() -> InvestigationUsage:
    return InvestigationUsage(
        model_calls=1,
        tool_calls=1,
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        prompt_hash="prompt",
        provider="fake",
        model="test-model",
        reasoning_effort="none",
        estimated_api_calls=0,
        actual_api_calls=0,
        model_calls_limit=3,
    )


def test_evidence_summary_and_run_artifact_round_trip() -> None:
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    incident_id = uuid4()
    evidence = Evidence(
        incident_id=incident_id,
        source_type=EvidenceSourceType.METRIC,
        source_system="service_latency",
        observation={"records": [{"p95": 1.4}]},
        time_window=TimeWindow(starts_at=now, ends_at=now),
        tool_call_id=uuid4(),
        raw_result_reference="service_latency://call",
        collected_at=now,
        target_workload="payment-service",
    )
    summary = EvidenceSummary.from_evidence(evidence, tool="service_latency")
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
        causal_summary="Connection acquisition was slow.",
        evidence_ids=[evidence.evidence_id],
    )
    artifact = A1RunArtifact(
        experiment_id="v0.3-exp01-a1-improved-single-agent",
        run_id=uuid4(),
        incident_id=incident_id,
        configuration_hashes={"topology": "hash"},
        observation_window=TimeWindow(starts_at=now, ends_at=now),
        turns=[
            TurnRecord(
                turn=1,
                decision="CALL_TOOLS",
                requested_tools=["service_latency"],
                canonical_arguments=[{"service": "payment-service"}],
                target_workloads=[WorkloadComponentId.PAYMENT_SERVICE],
                new_evidence_ids=[evidence.evidence_id],
                workloads_queried_so_far=[WorkloadComponentId.PAYMENT_SERVICE],
                remaining_model_calls=2,
                remaining_tool_calls=7,
            )
        ],
        evidence=[summary],
        hypothesis=hypothesis,
        termination_reason=TerminationReason.HYPOTHESIS_SUBMITTED,
        usage=_usage(),
        safety=A1SafetyCounters(),
    )

    restored = A1RunArtifact.model_validate_json(artifact.model_dump_json())
    assert restored == artifact
    assert restored.hypothesis is not None
    assert restored.hypothesis.causal_resource is DependencyResourceId.POSTGRESQL
