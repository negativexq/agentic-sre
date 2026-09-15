"""ITBench-native investigator contracts, kept separate from internal A1 RCA types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from packages.investigation.contracts import InvestigationModel, ToolRequestSpec

ITBENCH_EXTERNAL_PROTOCOL_VERSION = "itbench_investigation_decision_v1"
ITBENCH_EXTERNAL_PROTOCOL_V2 = "itbench_investigation_decision_v2"
ITBENCH_EXTERNAL_PROTOCOL_V3 = "itbench_investigation_decision_v3"
ITBENCH_EXTERNAL_PROTOCOL_V4 = "itbench_investigation_decision_v4"
ITBENCH_EXTERNAL_PROTOCOL_V5 = "itbench_investigation_decision_v5"


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


class ExternalRootCauseV2(InvestigationModel):
    """External root cause using short, runtime-issued evidence handles."""

    entity: str = Field(
        min_length=3,
        max_length=512,
        description=(
            "Canonical ITBench Kubernetes identity in namespace/Kind/name format. "
            "Use _cluster/Kind/name for cluster-scoped resources."
        ),
    )
    causal_summary: str = Field(min_length=1, max_length=1_000)
    evidence_refs: list[str] = Field(
        min_length=1,
        max_length=12,
        description="Runtime-issued evidence handles such as E001; never invent handles.",
    )

    @field_validator("entity")
    @classmethod
    def validate_entity_syntax(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 3 or not all(parts) or any(len(part) > 255 for part in parts):
            raise ValueError("entity must use namespace/Kind/name syntax")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if any(len(ref) != 4 or ref[0] != "E" or not ref[1:].isdigit() for ref in value):
            raise ValueError("evidence_refs must use runtime handles such as E001")
        if len(set(value)) != len(value):
            raise ValueError("evidence_refs must not contain duplicates")
        return value


class ITBenchInvestigationDecisionV2(InvestigationModel):
    """Versioned external decision envelope with model-safe evidence handles."""

    decision: ITBenchDecisionType
    requests: list[ToolRequestSpec] = Field(default_factory=list, max_length=3)
    root_causes: list[ExternalRootCauseV2] = Field(default_factory=list, max_length=5)
    stop: ExternalStop | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ITBenchInvestigationDecisionV2:
        if self.decision is ITBenchDecisionType.CALL_TOOLS:
            if not self.requests or self.root_causes or self.stop is not None:
                raise ValueError("CALL_TOOLS requires requests only")
        elif self.decision is ITBenchDecisionType.SUBMIT_DIAGNOSIS:
            if not self.root_causes or self.requests or self.stop is not None:
                raise ValueError("SUBMIT_DIAGNOSIS requires root causes only")
        elif not self.stop or self.requests or self.root_causes:
            raise ValueError("STOP requires stop metadata only")
        return self


class ExternalRootCauseV3(InvestigationModel):
    """External root cause with runtime-enforced, not hidden, bounds."""

    entity: str = Field(
        min_length=3,
        max_length=512,
        description=(
            "Canonical ITBench Kubernetes identity in namespace/Kind/name format. "
            "Use _cluster/Kind/name for cluster-scoped resources."
        ),
    )
    causal_summary: str = Field(min_length=1, max_length=1_000)
    evidence_refs: list[str] = Field(
        min_length=1,
        description="Runtime-issued evidence handles such as E001; never invent handles.",
    )

    @field_validator("entity")
    @classmethod
    def validate_entity_syntax(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 3 or not all(parts) or any(len(part) > 255 for part in parts):
            raise ValueError("entity must use namespace/Kind/name syntax")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if any(len(ref) != 4 or ref[0] != "E" or not ref[1:].isdigit() for ref in value):
            raise ValueError("evidence_refs must use runtime handles such as E001")
        if len(set(value)) != len(value):
            raise ValueError("evidence_refs must not contain duplicates")
        return value


class ITBenchInvestigationDecisionV3(InvestigationModel):
    """External envelope without a hidden provider/local cardinality mismatch."""

    decision: ITBenchDecisionType
    requests: list[ToolRequestSpec] = Field(default_factory=list)
    root_causes: list[ExternalRootCauseV3] = Field(default_factory=list)
    stop: ExternalStop | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ITBenchInvestigationDecisionV3:
        if self.decision is ITBenchDecisionType.CALL_TOOLS:
            if not self.requests or self.root_causes or self.stop is not None:
                raise ValueError("CALL_TOOLS requires requests only")
        elif self.decision is ITBenchDecisionType.SUBMIT_DIAGNOSIS:
            if not self.root_causes or self.requests or self.stop is not None:
                raise ValueError("SUBMIT_DIAGNOSIS requires root causes only")
        elif not self.stop or self.requests or self.root_causes:
            raise ValueError("STOP requires stop metadata only")
        return self


class CandidateStatus(StrEnum):
    """Auditable causal-candidate states exposed by the V4 protocol."""

    ACTIVE = "ACTIVE"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"


class CandidateUpdateV4(InvestigationModel):
    """Bounded candidate-state update; never a private reasoning transcript."""

    entity: str = Field(
        min_length=3,
        max_length=512,
        description="Canonical namespace/Kind/name or _cluster/Kind/name identity.",
    )
    status: CandidateStatus
    supporting_refs: list[str] = Field(default_factory=list, max_length=12)
    contradicting_refs: list[str] = Field(default_factory=list, max_length=12)
    last_tested_question: str | None = Field(default=None, max_length=300)

    @field_validator("entity")
    @classmethod
    def validate_entity_syntax(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 3 or not all(parts) or any(len(part) > 255 for part in parts):
            raise ValueError("entity must use namespace/Kind/name syntax")
        return value

    @field_validator("supporting_refs", "contradicting_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if any(len(ref) != 4 or ref[0] != "E" or not ref[1:].isdigit() for ref in value):
            raise ValueError("candidate evidence refs must use runtime handles such as E001")
        if len(set(value)) != len(value):
            raise ValueError("candidate evidence refs must not contain duplicates")
        return value


class ExternalRootCauseV4(InvestigationModel):
    """Root cause for the fixed-slot V4 provider/local contract."""

    entity: str = Field(
        min_length=3,
        max_length=512,
        description=(
            "Canonical ITBench Kubernetes identity in namespace/Kind/name format. "
            "Use _cluster/Kind/name for cluster-scoped resources."
        ),
    )
    causal_summary: str = Field(min_length=1, max_length=1_000)
    evidence_refs: list[str] = Field(
        min_length=1,
        max_length=12,
        description="Runtime-issued evidence handles such as E001; never invent handles.",
    )

    @field_validator("entity")
    @classmethod
    def validate_entity_syntax(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 3 or not all(parts) or any(len(part) > 255 for part in parts):
            raise ValueError("entity must use namespace/Kind/name syntax")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if any(len(ref) != 4 or ref[0] != "E" or not ref[1:].isdigit() for ref in value):
            raise ValueError("evidence_refs must use runtime handles such as E001")
        if len(set(value)) != len(value):
            raise ValueError("evidence_refs must not contain duplicates")
        return value


class ITBenchInvestigationDecisionV4(InvestigationModel):
    """Fixed-slot external envelope with explicit provider-visible cardinality."""

    decision: ITBenchDecisionType
    primary_request: ToolRequestSpec | None = None
    additional_request_2: ToolRequestSpec | None = None
    additional_request_3: ToolRequestSpec | None = None
    primary_root_cause: ExternalRootCauseV4 | None = None
    additional_root_cause_2: ExternalRootCauseV4 | None = None
    additional_root_cause_3: ExternalRootCauseV4 | None = None
    candidate_update_1: CandidateUpdateV4 | None = None
    candidate_update_2: CandidateUpdateV4 | None = None
    candidate_update_3: CandidateUpdateV4 | None = None
    stop: ExternalStop | None = None

    @property
    def requests(self) -> list[ToolRequestSpec]:
        return [
            item
            for item in (
                self.primary_request,
                self.additional_request_2,
                self.additional_request_3,
            )
            if item is not None
        ]

    @property
    def root_causes(self) -> list[ExternalRootCauseV4]:
        return [
            item
            for item in (
                self.primary_root_cause,
                self.additional_root_cause_2,
                self.additional_root_cause_3,
            )
            if item is not None
        ]

    @property
    def candidate_updates(self) -> list[CandidateUpdateV4]:
        return [
            item
            for item in (
                self.candidate_update_1,
                self.candidate_update_2,
                self.candidate_update_3,
            )
            if item is not None
        ]

    @model_validator(mode="after")
    def validate_payload(self) -> ITBenchInvestigationDecisionV4:
        requests = self.requests
        root_causes = self.root_causes
        if self.decision is ITBenchDecisionType.CALL_TOOLS:
            if not requests or root_causes or self.stop is not None:
                raise ValueError("CALL_TOOLS requires at least one request only")
        elif self.decision is ITBenchDecisionType.SUBMIT_DIAGNOSIS:
            if not root_causes or requests or self.stop is not None:
                raise ValueError("SUBMIT_DIAGNOSIS requires at least one root cause only")
        elif not self.stop or requests or root_causes:
            raise ValueError("STOP requires stop metadata only")
        return self


class E9Action(StrEnum):
    """Small model action vocabulary; workflow state remains runtime-owned."""

    OBSERVE = "OBSERVE"
    HYPOTHESIZE = "HYPOTHESIZE"
    INVESTIGATE = "INVESTIGATE"
    REVISE = "REVISE"
    SUBMIT = "SUBMIT"
    STOP = "STOP"


class ITBenchInvestigationDecisionV5(InvestigationModel):
    """Minimal E9 action contract using scenario-local runtime handles."""

    action: E9Action
    target: str | None = Field(default=None, max_length=16)
    targets: list[str] = Field(default_factory=list, max_length=3)
    operation: str | None = Field(default=None, max_length=64)
    rationale: str | None = Field(default=None, max_length=300)
    stop_reason: str | None = Field(default=None, max_length=128)

    @field_validator("target")
    @classmethod
    def validate_target_handle(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 4 or value[0] != "C" or not value[1:].isdigit()):
            raise ValueError("target must be a runtime candidate handle such as C017")
        return value

    @field_validator("targets")
    @classmethod
    def validate_target_handles(cls, value: list[str]) -> list[str]:
        if any(len(item) != 4 or item[0] != "C" or not item[1:].isdigit() for item in value):
            raise ValueError("targets must use runtime candidate handles such as C017")
        if len(set(value)) != len(value):
            raise ValueError("targets must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_action_shape(self) -> ITBenchInvestigationDecisionV5:
        if self.action is E9Action.SUBMIT and not self.targets:
            raise ValueError("SUBMIT requires at least one candidate target")
        if self.action is E9Action.HYPOTHESIZE and self.target is None:
            raise ValueError("HYPOTHESIZE requires one candidate target")
        if self.action is E9Action.INVESTIGATE and (self.target is None or self.operation is None):
            raise ValueError("INVESTIGATE requires target and operation")
        if self.action is E9Action.STOP and not self.stop_reason:
            raise ValueError("STOP requires stop_reason")
        if self.action is not E9Action.STOP and self.stop_reason is not None:
            raise ValueError("stop_reason is only valid for STOP")
        return self


class ITBenchExternalResult(InvestigationModel):
    """Native external result envelope before evaluator data is loaded."""

    protocol: str = ITBENCH_EXTERNAL_PROTOCOL_VERSION
    scenario_id: str = Field(pattern=r"^Scenario-[0-9]+$", max_length=32)
    incident_id: UUID
    decision: (
        ITBenchInvestigationDecisionV1
        | ITBenchInvestigationDecisionV2
        | ITBenchInvestigationDecisionV3
        | ITBenchInvestigationDecisionV4
        | None
    ) = None
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
    "ITBENCH_EXTERNAL_PROTOCOL_V2",
    "ITBENCH_EXTERNAL_PROTOCOL_V3",
    "ITBENCH_EXTERNAL_PROTOCOL_V4",
    "ITBENCH_EXTERNAL_PROTOCOL_V5",
    "ExternalRootCauseV2",
    "ITBenchInvestigationDecisionV2",
    "ExternalRootCauseV3",
    "ITBenchInvestigationDecisionV3",
    "CandidateStatus",
    "CandidateUpdateV4",
    "ExternalRootCauseV4",
    "ITBenchInvestigationDecisionV4",
    "E9Action",
    "ITBenchInvestigationDecisionV5",
]
