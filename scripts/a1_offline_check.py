"""Qualify A1 grading and artifact wiring with scripted fake-provider runs."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from packages.contracts import (
    Alert,
    AlertSource,
    AlertStatus,
    EvidenceSourceType,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.evals import (
    A1_TARGET_BY_SCENARIO,
    aggregate_a1_grades,
    grade_a1_run,
)
from packages.investigation import (
    DEFAULT_TOPOLOGY,
    A1RunArtifact,
    DecisionType,
    ReadOnlyToolRegistry,
    RegisteredTool,
    StopReason,
    TerminationReason,
    ToolRepeatPolicy,
)
from packages.investigation.causal_contracts import EvidenceCategory, TriggerType
from packages.investigation.context import derive_observation_window
from packages.investigation.runtime import InvestigationRuntime
from packages.investigation.tool_contracts import ServiceArgs
from packages.provider import FakeModelProvider, ModelRequest
from packages.tools import metrics_tool

ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION_PATH = ROOT / "docs/benchmarks/a1-evaluator-qualification.json"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _incident() -> Incident:
    """Create an operationally shaped incident without evaluator fields."""
    return Incident(
        incident_id=uuid4(),
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="Order dependency latency is elevated",
        created_at=NOW,
        updated_at=NOW,
    )


def _alert() -> Alert:
    """Create the symptom-scope alert used by the scripted cross-component case."""
    return Alert(
        alert_name="OrderDependencyLatencyHigh",
        service="order-service",
        namespace="sre-demo",
        cluster="agentic-sre",
        starts_at=NOW,
        labels={"service": "order-service", "severity": "critical"},
        annotations={"description": "dependency latency is elevated"},
        fingerprint="a1-offline",
        status=AlertStatus.FIRING,
        source=AlertSource.PROMETHEUS,
    )


def _registry() -> ReadOnlyToolRegistry:
    """Build one bounded canonical-target tool backed by deterministic data."""
    tool = metrics_tool(lambda _operation, _parameters: {"records": [{"status": "degraded"}]})
    return ReadOnlyToolRegistry(
        (
            RegisteredTool(
                name="service_latency",
                version="1",
                operation="service_latency",
                source_type=EvidenceSourceType.METRIC,
                tool=tool,
                argument_model=ServiceArgs,
                repeat_policy=ToolRepeatPolicy.FIXED_WINDOW,
                target_argument="service",
                target_topology=DEFAULT_TOPOLOGY,
            ),
        )
    )


def _submit(request: ModelRequest, *, causal: str) -> dict[str, Any]:
    """Submit an A1 hypothesis citing only evidence visible in this request."""
    context = json.loads(request.messages[1].content)
    evidence_ids = [item["evidence_id"] for item in context["evidence"]]
    return {
        "decision": DecisionType.SUBMIT_HYPOTHESIS,
        "hypothesis": {
            "symptom_component": "order-service",
            "causal_component": causal,
            "causal_resource": None,
            "mechanism": "dependency_latency",
            "structured_trigger": {
                "trigger_type": TriggerType.DEPENDENCY_LATENCY_INCREASE,
                "trigger_component": causal,
                "trigger_resource": None,
            },
            "causal_summary": "bounded scripted causal hypothesis",
            "evidence_ids": evidence_ids,
        },
    }


def _run_cross_component(*, explore_payment: bool) -> A1RunArtifact:
    """Run a correct or intentionally incomplete cross-component scripted path."""
    first = {
        "decision": DecisionType.CALL_TOOLS,
        "requests": [{"tool": "service_latency", "arguments": {"service": "order-service"}}],
    }
    if explore_payment:
        second: dict[str, Any] = {
            "decision": DecisionType.CALL_TOOLS,
            "requests": [{"tool": "service_latency", "arguments": {"service": "payment-service"}}],
        }
        provider = FakeModelProvider(
            [first, second, lambda request: _submit(request, causal="payment-service")]
        )
    else:
        provider = FakeModelProvider(
            [first, lambda request: _submit(request, causal="payment-service")]
        )
    incident = _incident()
    alerts = (_alert(),)
    result = InvestigationRuntime(provider, _registry(), a1_protocol=True).run(incident, alerts)
    assert result.usage.actual_api_calls == 0
    return A1RunArtifact.from_result(
        result,
        experiment_id="a1-offline-qualification",
        observation_window=derive_observation_window(incident, alerts).time_window(),
        configuration_hashes={"topology": DEFAULT_TOPOLOGY.topology_hash()},
    )


def _run_stop() -> A1RunArtifact:
    """Run a valid structured STOP without manufacturing evidence."""
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.STOP,
                "stop": {
                    "stop_reason": StopReason.INSUFFICIENT_EVIDENCE,
                    "considered_components": ["order-worker"],
                    "considered_resources": ["kafka"],
                    "missing_evidence_categories": [EvidenceCategory.MESSAGING],
                },
            }
        ]
    )
    incident = _incident()
    alerts = (_alert(),)
    result = InvestigationRuntime(provider, _registry(), a1_protocol=True).run(incident, alerts)
    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.usage.actual_api_calls == 0
    return A1RunArtifact.from_result(
        result,
        experiment_id="a1-offline-qualification",
        observation_window=derive_observation_window(incident, alerts).time_window(),
    )


def main() -> None:
    """Run deterministic checks and write a non-performance qualification artifact."""
    correct = _run_cross_component(explore_payment=True)
    missed = _run_cross_component(explore_payment=False)
    stopped = _run_stop()
    target = A1_TARGET_BY_SCENARIO["V020-003"]
    correct_grade = grade_a1_run(correct, target)
    missed_grade = grade_a1_run(
        missed, target.model_copy(update={"scenario_id": "A1-OFFLINE-MISS"})
    )
    stopped_grade = grade_a1_run(
        stopped,
        A1_TARGET_BY_SCENARIO["V020-008"].model_copy(update={"scenario_id": "A1-OFFLINE-STOP"}),
    )
    assert correct_grade.completion == 1
    assert correct_grade.causal_component == 1
    assert correct_grade.structured_trigger == 1
    assert correct_grade.evidence_reference_integrity == 1
    assert correct_grade.ground_truth_causal_component_evidence == 1
    assert correct_grade.causal_component_explored is True
    assert missed_grade.causal_component_explored is False
    assert stopped_grade.valid_stop is True
    aggregate = aggregate_a1_grades([correct_grade, missed_grade, stopped_grade])
    assert aggregate.cross_component_exploration_recall.numerator == 1
    assert aggregate.cross_component_exploration_recall.denominator == 2
    assert aggregate.evidence_reference_integrity.denominator == 2

    output = {
        "artifact_type": "A1_EVALUATOR_QUALIFICATION",
        "status": "PASS",
        "model_quality_claim": False,
        "openai_calls": 0,
        "network_calls": 0,
        "cases": {
            "correct_structured_hypothesis": "PASS",
            "cross_component_exploration": "PASS",
            "missed_cross_component_exploration": "PASS",
            "structured_stop": "PASS",
            "reference_integrity": "PASS",
            "denominator_transparency": "PASS",
        },
        "aggregate_checks": {
            "cross_component_exploration_numerator": 1,
            "cross_component_exploration_denominator": 2,
            "submitted_hypothesis_integrity_numerator": 2,
            "submitted_hypothesis_integrity_denominator": 2,
        },
        "note": "Scripted FakeModelProvider qualification only; not model performance.",
    }
    QUALIFICATION_PATH.write_text(json.dumps(output, indent=2) + "\n")
    print("A1 evaluator qualification: PASS (offline, fake provider, 0 OpenAI calls)")


if __name__ == "__main__":
    main()
