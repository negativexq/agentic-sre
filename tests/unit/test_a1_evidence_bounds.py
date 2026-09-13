"""Regression tests for bounded A1 evidence context and artifact summaries."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.contracts import (
    Evidence,
    EvidenceSourceType,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
    TimeWindow,
)
from packages.investigation import (
    A1RunArtifact,
    CausalHypothesis,
    CompactContextBuilder,
    HypothesisMechanism,
    InvestigationResult,
    InvestigationUsage,
    StructuredTrigger,
    TerminationReason,
    TriggerType,
    WorkloadComponentId,
    bound_text,
    bounded_observation_summary,
)


@pytest.mark.parametrize(
    ("length", "truncated"),
    ((0, False), (1, False), (999, False), (1000, False), (1001, True), (5000, True)),
)
def test_bound_text_has_exact_character_boundary(length: int, truncated: bool) -> None:
    value = bound_text("x" * length)
    assert len(value) <= 1000
    assert value == "x" * length if not truncated else value.endswith("\n...[truncated]")


def test_bound_text_handles_unicode_and_multiline_without_byte_truncation() -> None:
    value = bound_text("ödeme 🚦\n" * 300)
    assert len(value) <= 1000
    assert value.endswith("\n...[truncated]")
    assert "ödeme" in value
    assert "🚦" in value
    assert value.encode("utf-8").decode("utf-8") == value


def _long_evidence(incident_id: UUID, *, source: str, observation: dict[str, Any]) -> Evidence:
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    return Evidence(
        incident_id=incident_id,
        source_type=EvidenceSourceType.LOG,
        source_system=source,
        observation=observation,
        time_window=TimeWindow(starts_at=now, ends_at=now),
        tool_call_id=uuid4(),
        raw_result_reference=f"{source}://call",
        collected_at=now,
        target_workload="payment-service",
    )


def test_realistic_long_observations_round_trip_through_context_and_artifact() -> None:
    incident_id = uuid4()
    evidence = [
        _long_evidence(
            incident_id,
            source="service_logs",
            observation={"records": [{"message": "timeout " + ("payment=" * 180)}]},
        ),
        _long_evidence(
            incident_id,
            source="kubernetes_events",
            observation={
                "events": [
                    {
                        "reason": "BackOff",
                        "message": "container restart details " + ("pod=" * 180),
                    }
                ]
            },
        ),
        _long_evidence(
            incident_id,
            source="trace_detail",
            observation={
                "traces": [{"attributes": {"dependency": "postgresql", "detail": "x" * 1500}}]
            },
        ),
    ]
    incident = Incident(
        incident_id=incident_id,
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="Payment latency",
        created_at=evidence[0].collected_at,
        updated_at=evidence[0].collected_at,
    )
    context = json.loads(
        CompactContextBuilder().build(incident, evidence, (), current_model_call=1)
    )
    hypothesis = CausalHypothesis(
        symptom_component=WorkloadComponentId.PAYMENT_SERVICE,
        causal_component=WorkloadComponentId.PAYMENT_SERVICE,
        mechanism=HypothesisMechanism.DATABASE_CONNECTION_PRESSURE,
        structured_trigger=StructuredTrigger(
            trigger_type=TriggerType.DB_CONNECTION_PRESSURE,
            trigger_component=WorkloadComponentId.PAYMENT_SERVICE,
        ),
        causal_summary="Database pressure is supported by bounded evidence.",
        evidence_ids=[item.evidence_id for item in evidence],
    )
    result = InvestigationResult(
        incident_id=incident_id,
        causal_hypothesis=hypothesis.model_dump(mode="json"),
        evidence=evidence,
        usage=InvestigationUsage(
            model_calls=1,
            tool_calls=3,
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
        ),
        termination_reason=TerminationReason.HYPOTHESIS_SUBMITTED,
        turns=[
            {
                "turn": 1,
                "selected_decision": "CALL_TOOLS",
                "requested_tool_names": ["service_logs", "kubernetes_events", "trace_detail"],
                "summaries": [
                    {
                        "tool": item.source_system,
                        "arguments": {"service": "payment-service"},
                        "status": "SUCCESS",
                        "evidence_ids": [str(item.evidence_id)],
                    }
                    for item in evidence
                ],
                "tool_calls_attempted": 3,
            }
        ],
    )
    artifact = A1RunArtifact.from_result(
        result,
        experiment_id="a1-recovery-test",
        observation_window=evidence[0].time_window,
    )
    restored = A1RunArtifact.model_validate_json(artifact.model_dump_json())

    assert len(restored.evidence) == 3
    assert all(len(item.bounded_observation_summary) <= 1000 for item in restored.evidence)
    assert restored.hypothesis is not None
    assert restored.hypothesis.evidence_ids == hypothesis.evidence_ids
    for item, visible in zip(restored.evidence, context["evidence"], strict=True):
        assert visible["observation_summary"] == item.bounded_observation_summary


def test_bounded_observation_summary_is_deterministic() -> None:
    observation = {"records": [{"message": "event" * 500}], "ordered": [3, 2, 1]}
    assert bounded_observation_summary(observation) == bounded_observation_summary(observation)
