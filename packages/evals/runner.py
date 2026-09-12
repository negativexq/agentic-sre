"""Zero-credit benchmark runner using the real runtime and fake model."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.contracts import (
    EvidenceSourceType,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.evals.dataset import FROZEN_DATASET, FrozenIncident, frozen_dataset_hash
from packages.evals.graders import grade_evidence, grade_hypothesis
from packages.investigation import ReadOnlyToolRegistry, RegisteredTool
from packages.investigation.runtime import InvestigationRuntime
from packages.provider import FakeModelProvider, ModelRequest
from packages.tools import metrics_tool


class OfflineBenchmarkReport(BaseModel):
    """Compact report suitable for local and CI artifacts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    dataset_hash: str = Field(min_length=1)
    scenario_count: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    service_accuracy: float = Field(ge=0, le=1)
    mechanism_accuracy: float = Field(ge=0, le=1)
    trigger_accuracy: float = Field(ge=0, le=1)
    composite_rca: float = Field(ge=0, le=1)
    valid_evidence_reference_rate: float = Field(ge=0, le=1)
    model_calls_per_incident: float = Field(ge=0)
    tool_requests_per_incident: float = Field(ge=0)
    tool_calls_per_incident: float = Field(ge=0)
    duplicate_requests_suppressed_per_incident: float = Field(ge=0)
    total_live_api_calls: int = Field(ge=0)


def run_offline_benchmark(
    scenarios: tuple[FrozenIncident, ...] = FROZEN_DATASET,
) -> OfflineBenchmarkReport:
    """Run one scripted fake-provider investigation per frozen scenario."""
    if not scenarios:
        return OfflineBenchmarkReport(
            dataset_hash=frozen_dataset_hash(),
            scenario_count=0,
            completion_rate=0,
            service_accuracy=0,
            mechanism_accuracy=0,
            trigger_accuracy=0,
            composite_rca=0,
            valid_evidence_reference_rate=0,
            model_calls_per_incident=0,
            tool_requests_per_incident=0,
            tool_calls_per_incident=0,
            duplicate_requests_suppressed_per_incident=0,
            total_live_api_calls=0,
        )

    results: list[tuple[FrozenIncident, Any]] = []
    for scenario in scenarios:
        provider = _provider_for(scenario)
        runtime = InvestigationRuntime(provider, _registry())
        results.append((scenario, runtime.run(_incident_for(scenario))))

    grades = [grade_hypothesis(result.hypothesis, scenario) for scenario, result in results]
    evidence_grades = [grade_evidence(result) for _, result in results]
    completed = sum(result.hypothesis is not None for _, result in results)
    return OfflineBenchmarkReport(
        dataset_hash=frozen_dataset_hash(),
        scenario_count=len(results),
        completion_rate=completed / len(results),
        service_accuracy=sum(item.service_accuracy for item in grades) / len(grades),
        mechanism_accuracy=sum(item.mechanism_accuracy for item in grades) / len(grades),
        trigger_accuracy=sum(item.trigger_accuracy for item in grades) / len(grades),
        composite_rca=sum(item.composite_rca for item in grades) / len(grades),
        valid_evidence_reference_rate=(
            sum(item.valid_reference_rate for item in evidence_grades) / len(evidence_grades)
        ),
        model_calls_per_incident=sum(result.usage.model_calls for _, result in results)
        / len(results),
        tool_requests_per_incident=sum(result.usage.tool_requests_total for _, result in results)
        / len(results),
        tool_calls_per_incident=sum(result.usage.tool_calls for _, result in results)
        / len(results),
        duplicate_requests_suppressed_per_incident=sum(
            result.usage.duplicate_requests_suppressed for _, result in results
        )
        / len(results),
        total_live_api_calls=sum(result.usage.actual_api_calls for _, result in results),
    )


def _incident_for(scenario: FrozenIncident) -> Incident:
    """Construct public incident facts without exposing benchmark truth fields."""
    now = datetime.now(UTC)
    return Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title=f"{scenario.category}: {scenario.affected_component}",
        created_at=now,
        updated_at=now,
    )


def _registry() -> ReadOnlyToolRegistry:
    """Use a deterministic metric backend to exercise real tool dispatch."""
    tool = metrics_tool(lambda _operation, _parameters: {"records": [{"signal": "degraded"}]})
    return ReadOnlyToolRegistry(
        (
            RegisteredTool(
                name="service_latency",
                version="1",
                operation="service_latency",
                source_type=EvidenceSourceType.METRIC,
                tool=tool,
            ),
        )
    )


def _provider_for(scenario: FrozenIncident) -> FakeModelProvider:
    """Create a scripted two-turn provider; truth stays in the benchmark harness."""

    def submit(request: ModelRequest) -> dict[str, Any]:
        import json

        context = json.loads(request.messages[1].content)
        evidence_id = context["evidence"][0]["evidence_id"]
        return {
            "decision": "SUBMIT_HYPOTHESIS",
            "hypothesis": {
                "affected_component": scenario.affected_component,
                "mechanism": scenario.mechanism.value,
                "suspected_trigger": scenario.suspected_trigger,
                "evidence_ids": [evidence_id],
            },
        }

    return FakeModelProvider(
        [
            {"decision": "CALL_TOOLS", "requests": [{"tool": "service_latency"}]},
            submit,
        ]
    )
