"""Strict contracts for the external ITBench-Lite SRE snapshot boundary.

The public snapshot and its ground truth deliberately have different types.  A
caller must opt in to loading :class:`ITBenchGroundTruth`; the investigator
loader only constructs :class:`ITBenchScenario` and :class:`InvestigatorData`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ITBenchEvidenceCategory(StrEnum):
    """Evidence files published in an ITBench-Lite SRE snapshot."""

    ALERTS = "alerts"
    METRICS = "metrics"
    K8S_EVENTS = "k8s_events"
    K8S_OBJECTS = "k8s_objects"
    LOGS = "logs"
    TRACES = "traces"


class ITBenchEntity(BaseModel):
    """Canonical Kubernetes/ITBench entity, without evaluator semantics."""

    model_config = ConfigDict(extra="forbid", strict=True)

    namespace: str | None = Field(default=None, min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)

    @property
    def canonical(self) -> str:
        """Serialize using the official namespace/Kind/name convention."""
        namespace = self.namespace or "_cluster"
        return f"{namespace}/{self.kind}/{self.name}"


def parse_canonical_entity(value: str) -> ITBenchEntity:
    """Parse one observable ITBench Kubernetes identity without lookup or GT."""
    if not isinstance(value, str):
        raise ValueError("entity must be a string")
    parts = value.split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError("entity must use namespace/Kind/name syntax")
    return ITBenchEntity(
        namespace=None if parts[0] == "_cluster" else parts[0],
        kind=parts[1],
        name=parts[2],
    )


class ITBenchScenario(BaseModel):
    """Investigator-safe description of one pinned external scenario."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scenario_id: str = Field(pattern=r"^Scenario-[0-9]+$", max_length=32)
    snapshot_path: str = Field(min_length=1, max_length=500)
    evidence_categories: tuple[ITBenchEvidenceCategory, ...] = Field(min_length=1)
    evidence_files: dict[ITBenchEvidenceCategory, tuple[str, ...]]
    observation_start: str | None = Field(default=None, max_length=64)
    observation_end: str | None = Field(default=None, max_length=64)

    def public_context(self) -> dict[str, Any]:
        """Return only facts permitted in an investigator context."""
        return {
            "benchmark": "ITBench-Lite",
            "domain": "SRE",
            "scenario_id": self.scenario_id,
            "available_evidence": [item.value for item in self.evidence_categories],
            "observation_start": self.observation_start,
            "observation_end": self.observation_end,
        }


class InvestigatorData(BaseModel):
    """The only snapshot data object allowed into an investigation context."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario: ITBenchScenario
    alerts: tuple[dict[str, Any], ...] = Field(max_length=100)
    metrics: tuple[dict[str, Any], ...] = Field(max_length=500)
    k8s_events: tuple[dict[str, Any], ...] = Field(max_length=500)
    k8s_objects: tuple[dict[str, Any], ...] = Field(max_length=500)
    logs: tuple[dict[str, Any], ...] = Field(max_length=500)
    traces: tuple[dict[str, Any], ...] = Field(max_length=500)


class ITBenchGroundTruthGroup(BaseModel):
    """Evaluator-only group definition from ``ground_truth.yaml``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    group_id: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=128)
    namespace: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    filters: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    root_cause: bool = False

    def entity(self) -> ITBenchEntity | None:
        """Return an exact entity when GT supplies a concrete name."""
        if self.name is None:
            return None
        return ITBenchEntity(namespace=self.namespace, kind=self.kind, name=self.name)


class ITBenchGroundTruth(BaseModel):
    """Evaluator-only normalized ground truth, never model-facing."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(pattern=r"^Scenario-[0-9]+$", max_length=32)
    root_cause_groups: tuple[ITBenchGroundTruthGroup, ...] = Field(min_length=1)
    aliases: tuple[tuple[str, ...], ...] = Field(default_factory=tuple, max_length=100)
    alerts: tuple[dict[str, Any], ...] = Field(default_factory=tuple, max_length=100)
    faults: tuple[dict[str, Any], ...] = Field(default_factory=tuple, max_length=100)
    propagations: tuple[dict[str, Any], ...] = Field(default_factory=tuple, max_length=200)

    def root_cause_ids(self) -> frozenset[str]:
        """Return evaluator IDs marked as root causes."""
        return frozenset(item.group_id for item in self.root_cause_groups if item.root_cause)

    def root_cause_entities(self) -> tuple[ITBenchEntity, ...]:
        """Return only concrete root-cause entities, when available."""
        return tuple(
            entity
            for group in self.root_cause_groups
            if group.root_cause
            for entity in (group.entity(),)
            if entity is not None
        )


class ITBenchScenarioQualification(BaseModel):
    """One deterministic E0 qualification record."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str
    loaded: bool
    ground_truth_loaded_separately: bool
    evidence_category_counts: dict[str, int]
    unsupported_evidence_categories: tuple[str, ...] = ()
    generated_evidence_ids: int = Field(ge=0)
    error: str | None = None


class ITBenchEntityPrediction(BaseModel):
    """One model prediction exported for the official evaluator."""

    model_config = ConfigDict(extra="forbid", strict=True)

    entity: ITBenchEntity
    rank: int = Field(gt=0, le=5)
    condition: str = Field(min_length=1, max_length=1_000)


class ITBenchAgentOutput(BaseModel):
    """Deterministic ITBench-compatible export, separate from native artifacts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    incident_id: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(pattern=r"^Scenario-[0-9]+$", max_length=32)
    contributing_factor: tuple[ITBenchEntityPrediction, ...] = Field(max_length=5)
    reasoning: str = Field(default="", max_length=1_000)
    native_terminal: str = Field(min_length=1, max_length=64)
