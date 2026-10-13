"""Development-only evidence contract for recorded episode relations.

Checks explicit frozen assertions, not authenticity or causal origin. Never
called by the resolver; a recorded request relation is not root-cause support.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FrozenRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    evidence_id: str = Field(min_length=1)
    source_read_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    at: datetime
    request_id: str | None = None
    operation_id: str | None = None
    outcome: Literal["FAILED", "SUCCEEDED", "UNKNOWN"] = "UNKNOWN"


class DependencyAttempt(FrozenRecord):
    caller_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    parent_operation_id: str | None = None
    target_uid: str | None = None
    mechanism_fingerprint: str | None = None
    fault_generation_id: str | None = None


class SymptomOperation(FrozenRecord):
    actor_id: str = Field(min_length=1)


class EpisodeBindingBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    claim_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    caller_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    episode_start: datetime
    episode_end: datetime
    current_attempt: DependencyAttempt
    symptom_operation: SymptomOperation
    historical_attempt: DependencyAttempt | None = None


class BindingAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)
    contract_version: str = "episode-binding.dev.v1"
    authority: str = "EXPLANATORY_ONLY"
    claim_id: str
    request_relation: Literal["RECORDED", "UNASSESSED"]
    same_fault_at_two_observations: Literal["RECORDED", "UNASSESSED"]
    deficits: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    causal_origin: str = "UNASSESSED"
    uninterrupted_continuation: str = "UNASSESSED"
    recovery: str = "UNASSESSED"
    admission_change: bool = False


def _zoned(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def assess_episode_binding(bundle: EpisodeBindingBundle) -> BindingAssessment:
    """Validate a narrow direct-parent request relation from supplied records.

    Optional recurrence needs exact target UID and an explicitly recorded fault
    generation and mechanism. Matching messages/names are never substitutes.
    No absence check can produce an exclusion; no success check proves recovery.
    """
    current, symptom, historical = (
        bundle.current_attempt,
        bundle.symptom_operation,
        bundle.historical_attempt,
    )
    deficits: list[str] = []
    zoned = all(
        _zoned(t) for t in (bundle.episode_start, bundle.episode_end, current.at, symptom.at)
    )
    if not zoned:
        deficits.append("TIME_MISSING_TIMEZONE")
    elif bundle.episode_end < bundle.episode_start:
        deficits.append("INVALID_EPISODE_INTERVAL")
    elif not all(bundle.episode_start <= t <= bundle.episode_end for t in (current.at, symptom.at)):
        deficits.append("CURRENT_RECORD_OUTSIDE_EPISODE")
    if not all(r.incident_id == bundle.incident_id for r in (current, symptom)):
        deficits.append("CURRENT_INCIDENT_ID_MISMATCH")
    if (
        current.caller_id != bundle.caller_id
        or symptom.actor_id != bundle.caller_id
        or current.target_id != bundle.target_id
    ):
        deficits.append("RELATION_IDENTITY_MISMATCH")
    if not current.request_id or current.request_id != symptom.request_id:
        deficits.append("REQUEST_ID_MISSING_OR_MISMATCHED")
    if not symptom.operation_id or current.parent_operation_id != symptom.operation_id:
        deficits.append("DIRECT_PARENT_OPERATION_LINK_MISSING_OR_MISMATCHED")
    if not current.operation_id or current.operation_id == symptom.operation_id:
        deficits.append("DEPENDENCY_OPERATION_ID_MISSING_OR_NOT_DISTINCT")
    if current.evidence_id == symptom.evidence_id:
        deficits.append("DEPENDENCY_AND_SYMPTOM_RECORD_NOT_DISTINCT")
    if current.outcome != "FAILED" or symptom.outcome != "FAILED":
        deficits.append("FAILED_DEPENDENCY_AND_SYMPTOM_NOT_RECORDED")
    request_recorded = not deficits
    recurrence_deficits: list[str] = []
    if historical is None:
        recurrence_deficits.append("HISTORICAL_ATTEMPT_NOT_SUPPLIED")
    else:
        if not _zoned(historical.at) or not _zoned(bundle.episode_start):
            recurrence_deficits.append("HISTORICAL_TIME_MISSING_TIMEZONE")
        elif historical.at >= bundle.episode_start:
            recurrence_deficits.append("HISTORICAL_RECORD_NOT_BEFORE_EPISODE")
        if historical.evidence_id in (current.evidence_id, symptom.evidence_id):
            recurrence_deficits.append("HISTORICAL_RECORD_NOT_DISTINCT")
        if historical.outcome != "FAILED":
            recurrence_deficits.append("HISTORICAL_FAILURE_NOT_RECORDED")
        if historical.caller_id != bundle.caller_id or historical.target_id != bundle.target_id:
            recurrence_deficits.append("HISTORICAL_RELATION_IDENTITY_MISMATCH")
        for field, code in (
            ("target_uid", "EXACT_TARGET_UID_MISSING_OR_MISMATCHED"),
            ("mechanism_fingerprint", "MECHANISM_IDENTITY_MISSING_OR_MISMATCHED"),
            ("fault_generation_id", "FAULT_GENERATION_MISSING_OR_MISMATCHED"),
        ):
            value = getattr(current, field)
            if not value or value != getattr(historical, field):
                recurrence_deficits.append(code)
    if not request_recorded:
        recurrence_deficits.append("CURRENT_REQUEST_RELATION_NOT_ESTABLISHED")
    return BindingAssessment(
        claim_id=bundle.claim_id,
        request_relation="RECORDED" if request_recorded else "UNASSESSED",
        same_fault_at_two_observations="RECORDED" if not recurrence_deficits else "UNASSESSED",
        deficits=tuple(deficits + recurrence_deficits),
        evidence_ids=tuple(r.evidence_id for r in (current, symptom, historical) if r is not None),
    )
