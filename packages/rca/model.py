"""Typed data shared by observation sources, signal extraction, and diagnosis."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CLUSTER_SCOPE = "_cluster"


class EntityRef(BaseModel):
    """A Kubernetes object identity rendered as ``namespace/Kind/name``."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    namespace: str = CLUSTER_SCOPE

    @property
    def canonical(self) -> str:
        return f"{self.namespace}/{self.kind}/{self.name}"

    @classmethod
    def parse(cls, value: str) -> EntityRef:
        parts = value.split("/", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"expected namespace/Kind/name, got {value!r}")
        return cls(namespace=parts[0], kind=parts[1], name=parts[2])

    def __str__(self) -> str:
        return self.canonical


class Alert(BaseModel):
    """One firing alert occurrence."""

    model_config = ConfigDict(frozen=True)

    name: str
    service: str | None = None
    namespace: str | None = None
    starts_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)


class ObjectVersion(BaseModel):
    """One observed version of a Kubernetes object."""

    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    observed_at: datetime
    body: dict[str, Any]
    evidence_id: str


class LogRecord(BaseModel):
    """One warning or error log line from a service."""

    model_config = ConfigDict(frozen=True)

    service: str
    at: datetime | None
    severity: str
    message: str
    evidence_id: str


class ClusterEvent(BaseModel):
    """One Kubernetes event about an involved object."""

    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    reason: str
    type: str = "Normal"
    message: str = ""
    first_at: datetime | None = None
    last_at: datetime | None = None
    count: int = 1
    evidence_id: str


class Edge(BaseModel):
    """A directed structural relation between two objects."""

    model_config = ConfigDict(frozen=True)

    source: EntityRef
    target: EntityRef
    relation: str


class FindingKind(StrEnum):
    """Deterministic signal categories, ordered roughly by causal strength."""

    CONFIG_CHANGE = "CONFIG_CHANGE"
    SPEC_CHANGE = "SPEC_CHANGE"
    IMAGE_CHANGE = "IMAGE_CHANGE"
    SCALE_CHANGE = "SCALE_CHANGE"
    ROLLOUT_RESTART = "ROLLOUT_RESTART"
    FAULT_INJECTION = "FAULT_INJECTION"
    FAULT_SCHEDULE = "FAULT_SCHEDULE"
    POLICY_CREATED = "POLICY_CREATED"
    DEPENDENCY_ERRORS = "DEPENDENCY_ERRORS"
    FAILURE_EVENT = "FAILURE_EVENT"


class Finding(BaseModel):
    """One deterministic observation attached to an entity."""

    model_config = ConfigDict(frozen=True)

    kind: FindingKind
    entity: EntityRef
    at: datetime | None
    summary: str
    evidence_ids: tuple[str, ...] = ()
    related: tuple[EntityRef, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


class Symptoms(BaseModel):
    """What the incident looks like from its alerts."""

    model_config = ConfigDict(frozen=True)

    onset: datetime | None
    last_seen: datetime | None
    services: tuple[str, ...]
    namespaces: tuple[str, ...]
    alert_names: tuple[str, ...]
    background_alert_counts: dict[str, int] = Field(default_factory=dict)


class Candidate(BaseModel):
    """A ranked root-cause candidate with the reasons for its score."""

    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    score: float
    findings: tuple[Finding, ...]
    linked_symptoms: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


class Confidence(StrEnum):
    """How well the chosen root cause is supported.

    VERIFIED: a deterministic rule ties the cause to the symptoms.
    LIKELY: strong signal and a structural link, but no verifying rule.
    UNVERIFIED: best available guess.
    """

    VERIFIED = "VERIFIED"
    LIKELY = "LIKELY"
    UNVERIFIED = "UNVERIFIED"


class Remediation(BaseModel):
    """A proposed, never executed, corrective action."""

    model_config = ConfigDict(frozen=True)

    action: str
    command: str
    risk: str
    requires_approval: bool = True


class InvestigationStep(BaseModel):
    """One step of the investigation trace."""

    model_config = ConfigDict(frozen=True)

    actor: str
    action: str
    detail: str


class Diagnosis(BaseModel):
    """The product's answer for one incident."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    root_cause: EntityRef | None
    confidence: Confidence
    summary: str
    symptoms: Symptoms
    evidence: tuple[Finding, ...] = ()
    alternatives: tuple[Candidate, ...] = ()
    remediation: tuple[Remediation, ...] = ()
    steps: tuple[InvestigationStep, ...] = ()
    mode: str = "deterministic"
    model_calls: int = 0


__all__ = [
    "CLUSTER_SCOPE",
    "Alert",
    "Candidate",
    "ClusterEvent",
    "Confidence",
    "Diagnosis",
    "Edge",
    "EntityRef",
    "Finding",
    "FindingKind",
    "InvestigationStep",
    "LogRecord",
    "ObjectVersion",
    "Remediation",
    "Symptoms",
]
