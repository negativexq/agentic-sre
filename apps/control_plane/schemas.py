"""HTTP error contracts for the control plane."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """Stable machine-readable API error."""

    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)


class ErrorResponse(BaseModel):
    """Envelope used by all typed control-plane errors."""

    model_config = ConfigDict(extra="forbid", strict=True)

    error: ErrorDetail


class DiagnosisRevisionSummary(BaseModel):
    """Persisted, factual metadata identifying one diagnosis revision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    diagnosis_id: int
    revision_number: int
    previous_diagnosis_id: int | None
    trigger: str
    created_at: datetime
    run_id: str | None
    window_end: datetime | None
    manifest_digest: str | None
    tape_digest: str | None
    epistemic_digest: str | None
    engine_version: str | None
    config_digest: str | None
    root_cause: str | None
    confidence: str
    mode: str
    resolution: str | None


class ResolutionTransitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    previous: str
    current: str
    changed: bool


class ManifestDiffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    previous_digest: str | None
    current_digest: str | None
    status: Literal["UNCHANGED", "CHANGED", "UNKNOWN"]


class HypothesisSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    hypothesis_key: str | None
    hypothesis_id: str
    causal_actor: str | None
    state: str | None
    root_eligible: bool


class HypothesisChangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    hypothesis_key: str
    previous: HypothesisSnapshotResponse
    current: HypothesisSnapshotResponse


class NewEliminationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    hypothesis_key: str | None
    hypothesis_id: str
    code: str
    rule_id: str
    rule_version: str
    consequence: str | None
    targets: tuple[str, ...]
    mechanism: str
    evidence_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    time_basis: tuple[str, ...]
    coverage_basis: str
    preconditions: tuple[str, ...]
    decisive_evidence_ids: tuple[str, ...]


class RevisionDiffResponse(BaseModel):
    """HTTP serialization of the pure M19-4.3 revision diff."""

    model_config = ConfigDict(extra="forbid", strict=True)

    resolution_transition: ResolutionTransitionResponse
    hypothesis_changes: tuple[HypothesisChangeResponse, ...]
    new_eliminations: tuple[NewEliminationResponse, ...]
    new_decisive_evidence_ids: tuple[str, ...]
    appeared: tuple[HypothesisSnapshotResponse, ...]
    disappeared: tuple[HypothesisSnapshotResponse, ...]
    manifest_diff: ManifestDiffResponse


class DiagnosisRevisionDetail(DiagnosisRevisionSummary):
    """One persisted diagnosis revision and its factual chain diff, if linked."""

    diagnosis: dict[str, Any]
    diff: RevisionDiffResponse | None


class BaselineProbeRequest(BaseModel):
    """Input of the incident-free clean-baseline probe (M19-6.8)."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=1)
    baseline_reference_at: datetime
    collector_started_at: datetime
