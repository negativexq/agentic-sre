"""Bounded, serializable A1 run and evidence artifact contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from packages.contracts import Evidence, TimeWindow
from packages.investigation.causal_contracts import CausalHypothesis, CausalStopDecision
from packages.investigation.contracts import (
    InvestigationLimits,
    InvestigationModel,
    InvestigationUsage,
)
from packages.investigation.topology import DependencyResourceId, WorkloadComponentId


class EvidenceSummary(InvestigationModel):
    """Safe bounded evidence projection for future benchmark artifacts."""

    evidence_id: UUID
    tool: str = Field(min_length=1, max_length=100)
    target_workload: WorkloadComponentId | None = None
    target_resource: DependencyResourceId | None = None
    source_type: str = Field(min_length=1, max_length=32)
    source_system: str = Field(min_length=1, max_length=100)
    time_window: TimeWindow
    temporal_mode: str = Field(min_length=1, max_length=64)
    collected_at: datetime
    bounded_observation_summary: str = Field(min_length=1, max_length=1_000)

    @classmethod
    def from_evidence(cls, evidence: Evidence, *, tool: str) -> "EvidenceSummary":
        """Create a bounded summary without storing raw backend payloads."""
        import json

        summary = json.dumps(evidence.observation, sort_keys=True, default=str)
        if len(summary) > 1_000:
            summary = f"{summary[:1_000]}…"
        return cls(
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
    usage: InvestigationUsage
    safety: A1SafetyCounters = Field(default_factory=A1SafetyCounters)


__all__ = ["A1RunArtifact", "A1SafetyCounters", "EvidenceSummary", "TurnRecord"]
