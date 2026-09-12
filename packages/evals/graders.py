"""Deterministic graders for hypothesis quality and evidence authority."""

from pydantic import BaseModel, ConfigDict, Field

from packages.evals.dataset import FrozenIncident
from packages.investigation.contracts import HypothesisSubmission, InvestigationResult


class GradeModel(BaseModel):
    """Strict grade result shared by benchmark metrics."""

    model_config = ConfigDict(extra="forbid", strict=True)


class HypothesisGrade(GradeModel):
    """Weighted root-cause score with its component metrics."""

    service_accuracy: float = Field(ge=0, le=1)
    mechanism_accuracy: float = Field(ge=0, le=1)
    trigger_accuracy: float = Field(ge=0, le=1)
    composite_rca: float = Field(ge=0, le=1)


class EvidenceGrade(GradeModel):
    """Evidence reference integrity metrics."""

    valid_reference_rate: float = Field(ge=0, le=1)
    fabricated_evidence_accepted: bool
    cross_incident_evidence_accepted: bool


def grade_hypothesis(
    hypothesis: HypothesisSubmission | None,
    expected: FrozenIncident,
) -> HypothesisGrade:
    """Score exact normalized component, mechanism, and trigger matches."""
    if hypothesis is None:
        return HypothesisGrade(
            service_accuracy=0,
            mechanism_accuracy=0,
            trigger_accuracy=0,
            composite_rca=0,
        )
    service = float(hypothesis.affected_component == expected.affected_component)
    mechanism = float(hypothesis.mechanism is expected.mechanism)
    trigger = float(hypothesis.suspected_trigger == expected.suspected_trigger)
    return HypothesisGrade(
        service_accuracy=service,
        mechanism_accuracy=mechanism,
        trigger_accuracy=trigger,
        composite_rca=0.25 * service + 0.50 * mechanism + 0.25 * trigger,
    )


def grade_evidence(result: InvestigationResult) -> EvidenceGrade:
    """Verify every submitted reference points to runtime-owned evidence."""
    if result.hypothesis is None:
        return EvidenceGrade(
            valid_reference_rate=0,
            fabricated_evidence_accepted=False,
            cross_incident_evidence_accepted=False,
        )
    available = {item.evidence_id for item in result.evidence}
    references = set(result.hypothesis.evidence_ids)
    valid = references.issubset(available)
    return EvidenceGrade(
        valid_reference_rate=float(valid),
        fabricated_evidence_accepted=not valid,
        cross_incident_evidence_accepted=not valid,
    )
