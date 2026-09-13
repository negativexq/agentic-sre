"""Bounded, serializable A1 run and evidence artifact contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from packages.contracts import Evidence, TimeWindow
from packages.investigation.bounds import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    bounded_observation_summary,
)
from packages.investigation.causal_contracts import CausalHypothesis, CausalStopDecision
from packages.investigation.contracts import (
    InvestigationLimits,
    InvestigationModel,
    InvestigationResult,
    InvestigationUsage,
    TerminationReason,
)
from packages.investigation.topology import (
    DependencyResourceId,
    WorkloadComponentId,
    target_from_tool_arguments,
)


class EvidenceSummary(InvestigationModel):
    """Safe bounded evidence projection for future benchmark artifacts."""

    incident_id: UUID
    evidence_id: UUID
    tool: str = Field(min_length=1, max_length=100)
    target_workload: WorkloadComponentId | None = None
    target_resource: DependencyResourceId | None = None
    source_type: str = Field(min_length=1, max_length=32)
    source_system: str = Field(min_length=1, max_length=100)
    time_window: TimeWindow
    temporal_mode: str = Field(min_length=1, max_length=64)
    collected_at: datetime
    bounded_observation_summary: str = Field(min_length=1, max_length=MAX_EVIDENCE_SUMMARY_CHARS)

    @classmethod
    def from_evidence(cls, evidence: Evidence, *, tool: str) -> "EvidenceSummary":
        """Create a bounded summary without storing raw backend payloads."""
        summary = bounded_observation_summary(evidence.observation)
        return cls(
            incident_id=evidence.incident_id,
            evidence_id=evidence.evidence_id,
            tool=tool,
            target_workload=(
                WorkloadComponentId(evidence.target_workload)
                if evidence.target_workload is not None
                else None
            ),
            target_resource=(
                DependencyResourceId(evidence.target_resource)
                if evidence.target_resource is not None
                else None
            ),
            source_type=evidence.source_type.value,
            source_system=evidence.source_system,
            time_window=evidence.time_window,
            temporal_mode=evidence.observation.get("temporal_mode", "INCIDENT_WINDOW"),
            collected_at=evidence.collected_at,
            bounded_observation_summary=summary,
        )


class TurnRecord(InvestigationModel):
    """Non-secret per-turn decision and execution observability."""

    turn: int = Field(gt=0, le=8)
    decision: str = Field(min_length=1, max_length=64)
    requested_tools: list[str] = Field(default_factory=list, max_length=20)
    canonical_arguments: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    request_statuses: list[str] = Field(default_factory=list, max_length=20)
    target_workloads: list[WorkloadComponentId] = Field(default_factory=list, max_length=20)
    target_resources: list[DependencyResourceId] = Field(default_factory=list, max_length=20)
    new_evidence_ids: list[UUID] = Field(default_factory=list, max_length=12)
    workloads_queried_so_far: list[WorkloadComponentId] = Field(
        default_factory=list, max_length=InvestigationLimits.HARD_MAX_TOOL_CALLS
    )
    resources_queried_so_far: list[DependencyResourceId] = Field(
        default_factory=list, max_length=InvestigationLimits.HARD_MAX_TOOL_CALLS
    )
    remaining_model_calls: int = Field(ge=0, le=8)
    remaining_tool_calls: int = Field(ge=0, le=20)


class A1SafetyCounters(InvestigationModel):
    """Typed safety counters required by future A1 artifacts."""

    fabricated_evidence: int = Field(default=0, ge=0)
    cross_incident_evidence: int = Field(default=0, ge=0)
    infrastructure_writes: int = Field(default=0, ge=0)
    kubernetes_write_verbs: int = Field(default=0, ge=0)
    budget_bypass: int = Field(default=0, ge=0)
    secret_leakage: int = Field(default=0, ge=0)


class A1RunArtifact(InvestigationModel):
    """Complete bounded A1 result without hidden chain-of-thought."""

    experiment_id: str = Field(min_length=1, max_length=100)
    run_id: UUID
    incident_id: UUID
    configuration_hashes: dict[str, str] = Field(default_factory=dict, max_length=20)
    observation_window: TimeWindow
    turns: list[TurnRecord] = Field(default_factory=list, max_length=8)
    evidence: list[EvidenceSummary] = Field(default_factory=list, max_length=12)
    hypothesis: CausalHypothesis | None = None
    stop: CausalStopDecision | None = None
    termination_reason: TerminationReason
    error_code: str | None = None
    usage: InvestigationUsage
    safety: A1SafetyCounters = Field(default_factory=A1SafetyCounters)

    @classmethod
    def from_result(
        cls,
        result: InvestigationResult,
        *,
        experiment_id: str,
        observation_window: TimeWindow,
        configuration_hashes: dict[str, str] | None = None,
    ) -> "A1RunArtifact":
        """Populate a bounded A1 artifact from one offline runtime result."""
        tool_by_evidence: dict[str, str] = {}
        for turn in result.turns:
            for summary in turn.get("summaries", []):
                if not isinstance(summary, dict):
                    continue
                tool = summary.get("tool")
                if not isinstance(tool, str):
                    continue
                for evidence_id in summary.get("evidence_ids", []):
                    if isinstance(evidence_id, str):
                        tool_by_evidence[evidence_id] = tool

        evidence = [
            EvidenceSummary.from_evidence(
                item,
                tool=tool_by_evidence.get(str(item.evidence_id), item.source_system),
            )
            for item in result.evidence
        ]
        workloads: set[WorkloadComponentId] = set()
        resources: set[DependencyResourceId] = set()
        turns: list[TurnRecord] = []
        tool_calls_used = 0
        for turn in result.turns:
            summaries = [item for item in turn.get("summaries", []) if isinstance(item, dict)]
            arguments = [
                item["arguments"] for item in summaries if isinstance(item.get("arguments"), dict)
            ]
            request_statuses = [
                item["status"] for item in summaries if isinstance(item.get("status"), str)
            ]
            target_workloads: set[WorkloadComponentId] = set()
            target_resources: set[DependencyResourceId] = set()
            new_evidence_ids: list[UUID] = []
            for summary in summaries:
                target = target_from_tool_arguments(summary.get("arguments", {}))
                if target.workload is not None:
                    target_workloads.add(target.workload)
                    workloads.add(target.workload)
                if target.resource is not None:
                    target_resources.add(target.resource)
                    resources.add(target.resource)
                new_evidence_ids.extend(
                    UUID(item) for item in summary.get("evidence_ids", []) if isinstance(item, str)
                )
            tool_calls_used += int(turn.get("tool_calls_attempted", 0))
            turns.append(
                TurnRecord(
                    turn=int(turn.get("turn", 1)),
                    decision=str(turn.get("selected_decision") or "UNKNOWN"),
                    requested_tools=[
                        item
                        for item in turn.get("requested_tool_names", [])
                        if isinstance(item, str)
                    ],
                    canonical_arguments=arguments,
                    request_statuses=request_statuses,
                    target_workloads=sorted(target_workloads, key=str),
                    target_resources=sorted(target_resources, key=str),
                    new_evidence_ids=new_evidence_ids,
                    workloads_queried_so_far=sorted(workloads, key=str),
                    resources_queried_so_far=sorted(resources, key=str),
                    remaining_model_calls=max(
                        result.usage.model_calls_limit - int(turn.get("turn", 1)), 0
                    ),
                    remaining_tool_calls=max(result.usage.tool_calls_limit - tool_calls_used, 0),
                )
            )
        return cls(
            experiment_id=experiment_id,
            run_id=result.run_id,
            incident_id=result.incident_id,
            configuration_hashes=configuration_hashes or {},
            observation_window=observation_window,
            turns=turns,
            evidence=evidence,
            hypothesis=(
                CausalHypothesis.model_validate(result.causal_hypothesis, strict=False)
                if result.causal_hypothesis is not None
                else None
            ),
            stop=(
                CausalStopDecision.model_validate(result.causal_stop, strict=False)
                if result.causal_stop is not None
                else None
            ),
            termination_reason=result.termination_reason,
            error_code=result.error_code,
            usage=result.usage,
            safety=A1SafetyCounters(
                fabricated_evidence=int(result.error_code == "FABRICATED_EVIDENCE_REFERENCE")
            ),
        )


__all__ = ["A1RunArtifact", "A1SafetyCounters", "EvidenceSummary", "TurnRecord"]
