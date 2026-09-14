"""ITBench-native investigator contracts, kept separate from internal A1 RCA types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from packages.investigation.contracts import InvestigationModel, ToolRequestSpec

ITBENCH_EXTERNAL_PROTOCOL_VERSION = "itbench_investigation_decision_v1"


class ITBenchDecisionType(StrEnum):
    """The three externally visible, bounded investigator actions."""

    CALL_TOOLS = "CALL_TOOLS"
    SUBMIT_DIAGNOSIS = "SUBMIT_DIAGNOSIS"
    STOP = "STOP"


class ExternalRootCause(InvestigationModel):
    """One independently supported ITBench Kubernetes root-cause entity."""

    entity: str = Field(
        min_length=3,
        max_length=512,
        description=(
            "Canonical ITBench Kubernetes identity in namespace/Kind/name format. "
            "Use _cluster/Kind/name for cluster-scoped resources."
        ),
    )
    causal_summary: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=12)

    @field_validator("entity")
    @classmethod
    def validate_entity_syntax(cls, value: str) -> str:
        """Require namespace/Kind/name without restricting observable kinds."""
        parts = value.split("/")
        if len(parts) != 3 or not all(parts) or any(len(part) > 255 for part in parts):
            raise ValueError("entity must use namespace/Kind/name syntax")
        return value


class ExternalStop(InvestigationModel):
    """Bounded STOP metadata, never private reasoning."""

    stop_reason: str = Field(min_length=1, max_length=128)
    evidence_categories_considered: list[str] = Field(default_factory=list, max_length=12)
    entities_considered: list[str] = Field(default_factory=list, max_length=20)
    missing_evidence_categories: list[str] = Field(default_factory=list, max_length=12)


class ITBenchInvestigationDecisionV1(InvestigationModel):
    """Strict external decision envelope used by the ITBench runtime."""

    decision: ITBenchDecisionType
    requests: list[ToolRequestSpec] = Field(default_factory=list, max_length=12)
    root_causes: list[ExternalRootCause] = Field(default_factory=list, max_length=5)
    stop: ExternalStop | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ITBenchInvestigationDecisionV1:
        if self.decision is ITBenchDecisionType.CALL_TOOLS:
            if not self.requests or self.root_causes or self.stop is not None:
                raise ValueError("CALL_TOOLS requires requests only")
        elif self.decision is ITBenchDecisionType.SUBMIT_DIAGNOSIS:
            if not self.root_causes or self.requests or self.stop is not None:
                raise ValueError("SUBMIT_DIAGNOSIS requires root causes only")
        elif not self.stop or self.requests or self.root_causes:
            raise ValueError("STOP requires stop metadata only")
        return self


class ITBenchExternalResult(InvestigationModel):
    """Native external result envelope before evaluator data is loaded."""

    protocol: str = ITBENCH_EXTERNAL_PROTOCOL_VERSION
    scenario_id: str = Field(pattern=r"^Scenario-[0-9]+$", max_length=32)
    incident_id: UUID
    decision: ITBenchInvestigationDecisionV1 | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    turns: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    usage: dict[str, Any] = Field(default_factory=dict)
    terminal: str = Field(min_length=1, max_length=64)


__all__ = [
    "ExternalRootCause",
    "ExternalStop",
    "ITBenchDecisionType",
    "ITBenchExternalResult",
    "ITBenchInvestigationDecisionV1",
    "ITBENCH_EXTERNAL_PROTOCOL_VERSION",
]
