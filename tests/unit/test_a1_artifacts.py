"""Offline tests for bounded A1 evidence and run artifact contracts."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

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

    restored = A1RunArtifact.from_json(artifact.model_dump_json())
    assert restored == artifact
    assert json.loads(restored.model_dump_json()) == json.loads(artifact.model_dump_json())
    assert restored.hypothesis is not None
    assert restored.hypothesis.causal_resource is DependencyResourceId.POSTGRESQL


def test_historical_r1_smoke_artifact_uses_json_wire_reader() -> None:
    """The persisted R1 shape reloads without changing the historical file."""
    payload = json.loads(Path("docs/benchmarks/a1-r1-live-smoke.json").read_text())
    restored = A1RunArtifact.from_json(
        json.dumps(payload["artifact"], sort_keys=True, separators=(",", ":"))
    )

    assert len(restored.evidence) == 10
    assert restored.termination_reason is TerminationReason.TOOL_CALL_LIMIT
    assert all(len(item.bounded_observation_summary) <= 1000 for item in restored.evidence)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("run_id",), "not-a-uuid"),
        (("observation_window", "starts_at"), "not-a-datetime"),
        (("termination_reason",), "NOT_A_TERMINATION"),
        (("run_id",), 42),
        (("evidence", 0, "target_workload"), "unknown-service"),
        (("evidence", 0, "target_resource"), "unknown-resource"),
        (("evidence", 0, "evidence_id"), "not-a-uuid"),
        (("evidence", 0, "unexpected"), True),
    ),
)
def test_artifact_json_wire_reader_rejects_invalid_values(
    path: tuple[object, ...], value: object
) -> None:
    """Wire parsing remains exact and rejects malformed or extra values."""
    payload = json.loads(Path("docs/benchmarks/a1-r1-live-smoke.json").read_text())["artifact"]
    current: object = payload
    for part in path[:-1]:
        current = current[part]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ValidationError):
        A1RunArtifact.from_json(json.dumps(payload, sort_keys=True, separators=(",", ":")))
