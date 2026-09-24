"""Conservative root-cause eligibility derived from causal-role evidence.

Eligibility is deliberately separate from both epistemic assessment and causal
role.  It only removes a hypothesis from root-cause competition when a
propagated effect is positively established and the complete hypothesis
episode has no source-capable initiating evidence, or when the ended
manifestation episode rule (``packages.rca.episode_end``) positively applies.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.rca.causal_roles import (
    HypothesisCausalRole,
    HypothesisCausalRoleAssessment,
    HypothesisCausalRoles,
)
from packages.rca.model import (
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
)

if TYPE_CHECKING:
    from packages.rca.episode_end import EndedEpisode


class RootCauseEligibilityState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE_PROPAGATED_EFFECT = "INELIGIBLE_PROPAGATED_EFFECT"
    UNDETERMINED = "UNDETERMINED"


class RootCauseEligibilityBasis(StrEnum):
    SOURCE_CAPABLE_CAUSAL_ROLE = "SOURCE_CAPABLE_CAUSAL_ROLE"
    PROPAGATED_EFFECT_CAUSAL_ROLE = "PROPAGATED_EFFECT_CAUSAL_ROLE"
    MANIFESTATION_CAUSAL_ROLE = "MANIFESTATION_CAUSAL_ROLE"
    VERIFIED_INCOMING_PROPAGATION = "VERIFIED_INCOMING_PROPAGATION"
    EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE = "EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE"
    NO_EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE = "NO_EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE"
    MIXED_OR_UNKNOWN_CAUSAL_ROLE = "MIXED_OR_UNKNOWN_CAUSAL_ROLE"


_SOURCE_CAPABLE_INITIATING_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.OBJECT_CREATED,
        FindingKind.OBJECT_DELETED,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.ROLLOUT_RESTART,
        FindingKind.FAULT_INJECTION,
        FindingKind.FAULT_SCHEDULE,
        FindingKind.POLICY_CREATED,
        FindingKind.QUOTA_EXCEEDED,
        FindingKind.QUOTA_EXHAUSTED,
        FindingKind.NETWORK_RESTRICTION,
        FindingKind.AUTOSCALING_FAILURE,
        FindingKind.TRAFFIC_INCREASE,
    }
)
_MAX_PROVENANCE = 32
_MIN_TIMESTAMP = datetime.min.replace(tzinfo=UTC)


def episode_source_capable_initiating_findings(
    hypothesis: Hypothesis,
) -> tuple[Finding, ...]:
    """Return source-capable initiating findings from the entire episode."""
    return tuple(
        finding
        for finding in hypothesis.findings
        if finding.kind in _SOURCE_CAPABLE_INITIATING_KINDS
        and finding.temporal_role is EvidenceTemporalRole.INITIATING
    )


def _bounded_ids(candidates: Sequence[tuple[tuple[object, ...], str]]) -> tuple[str, ...]:
    ordered: dict[str, tuple[object, ...]] = {}
    for order, evidence_id in candidates:
        if evidence_id not in ordered or order < ordered[evidence_id]:
            ordered[evidence_id] = order
    return tuple(
        evidence_id
        for _order, evidence_id in sorted(
            ((order, evidence_id) for evidence_id, order in ordered.items()),
            key=lambda item: (item[0], item[1]),
        )[:_MAX_PROVENANCE]
    )


def _initiating_ids(findings: Sequence[Finding]) -> tuple[str, ...]:
    candidates: list[tuple[tuple[object, ...], str]] = []
    for finding in findings:
        at = finding.at if finding.at is not None else _MIN_TIMESTAMP
        for evidence_id in finding.evidence_ids:
            candidates.append(
                ((at, finding.entity.canonical, finding.kind.value, evidence_id), evidence_id)
            )
    return _bounded_ids(candidates)


class HypothesisRootCauseEligibility(BaseModel):
    model_config = ConfigDict(frozen=True)

    hypothesis_id: str = Field(min_length=1)
    causal_actor: EntityRef
    causal_role: HypothesisCausalRole
    state: RootCauseEligibilityState
    episode_source_capable_initiating_findings: int = Field(ge=0)
    verified_incoming_edges: int = Field(ge=0)
    verified_incoming_pairs: int = Field(ge=0)
    initiating_evidence_ids: tuple[str, ...] = ()
    propagation_evidence_ids: tuple[str, ...] = ()
    basis: tuple[RootCauseEligibilityBasis, ...] = ()
    rationale: str

    @model_validator(mode="after")
    def _bounded_provenance(self) -> HypothesisRootCauseEligibility:
        if len(self.initiating_evidence_ids) > _MAX_PROVENANCE:
            raise ValueError("initiating eligibility provenance is unbounded")
        if len(self.propagation_evidence_ids) > _MAX_PROVENANCE:
            raise ValueError("propagation eligibility provenance is unbounded")
        return self


def assess_root_cause_eligibility(
    hypothesis: Hypothesis,
    causal_role: HypothesisCausalRoleAssessment,
) -> HypothesisRootCauseEligibility:
    if (
        causal_role.hypothesis_id != hypothesis.hypothesis_id
        or causal_role.causal_actor != hypothesis.causal_actor
    ):
        raise ValueError("causal role assessment does not match hypothesis")

    initiating = episode_source_capable_initiating_findings(hypothesis)
    initiating_count = len(initiating)
    initiating_ids = _initiating_ids(initiating)
    propagation_ids = tuple(causal_role.incoming_propagation_evidence_ids)
    basis: list[RootCauseEligibilityBasis] = []
    role = causal_role.role
    if causal_role.mixed_role_evidence:
        basis.append(RootCauseEligibilityBasis.MIXED_OR_UNKNOWN_CAUSAL_ROLE)
        if initiating_count:
            basis.append(RootCauseEligibilityBasis.EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE)
        return HypothesisRootCauseEligibility(
            hypothesis_id=hypothesis.hypothesis_id,
            causal_actor=hypothesis.causal_actor,
            causal_role=role,
            state=RootCauseEligibilityState.UNDETERMINED,
            episode_source_capable_initiating_findings=initiating_count,
            verified_incoming_edges=causal_role.verified_incoming_edges,
            verified_incoming_pairs=causal_role.verified_incoming_pairs,
            initiating_evidence_ids=initiating_ids,
            propagation_evidence_ids=propagation_ids,
            basis=tuple(basis),
            rationale=(
                "the hypothesis contains source-capable initiating evidence but current "
                "causal-role evidence is not sufficient to make an exclusion"
                if initiating_count
                else "current positive evidence does not safely determine root-cause eligibility"
            ),
        )
    if role is HypothesisCausalRole.SOURCE_CAPABLE:
        if causal_role.source_capable_initiating_findings <= 0:
            raise ValueError("SOURCE_CAPABLE role lacks actor initiating evidence")
        basis.extend(
            (
                RootCauseEligibilityBasis.SOURCE_CAPABLE_CAUSAL_ROLE,
                RootCauseEligibilityBasis.EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE,
            )
        )
        return HypothesisRootCauseEligibility(
            hypothesis_id=hypothesis.hypothesis_id,
            causal_actor=hypothesis.causal_actor,
            causal_role=role,
            state=RootCauseEligibilityState.ELIGIBLE,
            episode_source_capable_initiating_findings=initiating_count,
            verified_incoming_edges=causal_role.verified_incoming_edges,
            verified_incoming_pairs=causal_role.verified_incoming_pairs,
            initiating_evidence_ids=initiating_ids,
            propagation_evidence_ids=propagation_ids,
            basis=tuple(basis),
            rationale=(
                "actor-local source-capable initiating evidence makes this hypothesis "
                "eligible for root-cause competition; eligibility does not select it"
            ),
        )

    if role in {HypothesisCausalRole.PROPAGATED_EFFECT, HypothesisCausalRole.MANIFESTATION}:
        basis.append(
            RootCauseEligibilityBasis.PROPAGATED_EFFECT_CAUSAL_ROLE
            if role is HypothesisCausalRole.PROPAGATED_EFFECT
            else RootCauseEligibilityBasis.MANIFESTATION_CAUSAL_ROLE
        )
        if causal_role.verified_incoming_edges <= 0 or causal_role.verified_incoming_pairs <= 0:
            raise ValueError("propagated role lacks verified incoming propagation")
        basis.append(RootCauseEligibilityBasis.VERIFIED_INCOMING_PROPAGATION)
        if initiating_count == 0:
            basis.append(RootCauseEligibilityBasis.NO_EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE)
            return HypothesisRootCauseEligibility(
                hypothesis_id=hypothesis.hypothesis_id,
                causal_actor=hypothesis.causal_actor,
                causal_role=role,
                state=RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT,
                episode_source_capable_initiating_findings=0,
                verified_incoming_edges=causal_role.verified_incoming_edges,
                verified_incoming_pairs=causal_role.verified_incoming_pairs,
                initiating_evidence_ids=initiating_ids,
                propagation_evidence_ids=propagation_ids,
                basis=tuple(basis),
                rationale=(
                    "the exact causal actor is a VERIFIED propagated non-success effect and "
                    "the hypothesis episode contains no source-capable initiating evidence"
                ),
            )
        basis.append(RootCauseEligibilityBasis.EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE)
        return HypothesisRootCauseEligibility(
            hypothesis_id=hypothesis.hypothesis_id,
            causal_actor=hypothesis.causal_actor,
            causal_role=role,
            state=RootCauseEligibilityState.UNDETERMINED,
            episode_source_capable_initiating_findings=initiating_count,
            verified_incoming_edges=causal_role.verified_incoming_edges,
            verified_incoming_pairs=causal_role.verified_incoming_pairs,
            initiating_evidence_ids=initiating_ids,
            propagation_evidence_ids=propagation_ids,
            basis=tuple(basis),
            rationale=(
                "the hypothesis contains source-capable initiating evidence but current "
                "causal-role evidence is not sufficient to make an exclusion"
            ),
        )

    basis.append(RootCauseEligibilityBasis.MIXED_OR_UNKNOWN_CAUSAL_ROLE)
    if initiating_count:
        basis.append(RootCauseEligibilityBasis.EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE)
        rationale = (
            "the hypothesis contains source-capable initiating evidence but current "
            "causal-role evidence is not sufficient to make an exclusion"
        )
    else:
        rationale = "current positive evidence does not safely determine root-cause eligibility"
    return HypothesisRootCauseEligibility(
        hypothesis_id=hypothesis.hypothesis_id,
        causal_actor=hypothesis.causal_actor,
        causal_role=role,
        state=RootCauseEligibilityState.UNDETERMINED,
        episode_source_capable_initiating_findings=initiating_count,
        verified_incoming_edges=causal_role.verified_incoming_edges,
        verified_incoming_pairs=causal_role.verified_incoming_pairs,
        initiating_evidence_ids=initiating_ids,
        propagation_evidence_ids=propagation_ids,
        basis=tuple(basis),
        rationale=rationale,
    )


class RootCauseEligibilityStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    hypotheses: int = Field(ge=0)
    eligible: int = Field(ge=0)
    ineligible_propagated_effect: int = Field(ge=0)
    undetermined: int = Field(ge=0)
    ineligible_supported: int = Field(ge=0)
    ineligible_unresolved: int = Field(ge=0)
    ineligible_contradicted: int = Field(ge=0)


class RootCauseEligibilities:
    """Root-cause eligibility per hypothesis.

    ``ended_episodes`` holds hypotheses excluded by the ended-manifestation
    episode rule (contract section 17); they are ineligible independently of
    their propagation-based assessment.
    """

    __slots__ = ("assessments", "ended_episodes", "stats")

    def __init__(
        self,
        assessments: Sequence[HypothesisRootCauseEligibility],
        ended_episodes: Mapping[str, EndedEpisode] | None = None,
    ) -> None:
        self.assessments = tuple(sorted(assessments, key=lambda item: item.hypothesis_id))
        self.ended_episodes: Mapping[str, EndedEpisode] = dict(ended_episodes or {})
        states = [item.state for item in self.assessments]
        self.stats = RootCauseEligibilityStats(
            hypotheses=len(states),
            eligible=states.count(RootCauseEligibilityState.ELIGIBLE),
            ineligible_propagated_effect=states.count(
                RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT
            ),
            undetermined=states.count(RootCauseEligibilityState.UNDETERMINED),
            ineligible_supported=0,
            ineligible_unresolved=0,
            ineligible_contradicted=0,
        )

    @classmethod
    def empty(cls) -> RootCauseEligibilities:
        return cls(())

    def for_hypothesis(self, hypothesis_id: str) -> HypothesisRootCauseEligibility | None:
        return next(
            (item for item in self.assessments if item.hypothesis_id == hypothesis_id), None
        )

    def with_ended_episodes(self, ended: Mapping[str, EndedEpisode]) -> RootCauseEligibilities:
        return RootCauseEligibilities(self.assessments, {**self.ended_episodes, **ended})

    def ended_episode(self, hypothesis_id: str) -> EndedEpisode | None:
        return self.ended_episodes.get(hypothesis_id)

    def is_root_cause_selectable(self, hypothesis_id: str) -> bool:
        if hypothesis_id in self.ended_episodes:
            return False
        item = self.for_hypothesis(hypothesis_id)
        return (
            item is None or item.state is not RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT
        )


def derive_root_cause_eligibilities(
    hypotheses: Sequence[Hypothesis],
    causal_roles: HypothesisCausalRoles,
) -> RootCauseEligibilities:
    assessments: list[HypothesisRootCauseEligibility] = []
    role_ids = [item.hypothesis_id for item in causal_roles.assessments]
    if len(role_ids) != len(set(role_ids)):
        raise ValueError("duplicate causal role assessment")
    for hypothesis in hypotheses:
        role = causal_roles.for_hypothesis(hypothesis.hypothesis_id)
        if role is None:
            assessments.append(
                HypothesisRootCauseEligibility(
                    hypothesis_id=hypothesis.hypothesis_id,
                    causal_actor=hypothesis.causal_actor,
                    causal_role=HypothesisCausalRole.UNKNOWN,
                    state=RootCauseEligibilityState.UNDETERMINED,
                    episode_source_capable_initiating_findings=len(
                        episode_source_capable_initiating_findings(hypothesis)
                    ),
                    verified_incoming_edges=0,
                    verified_incoming_pairs=0,
                    initiating_evidence_ids=_initiating_ids(
                        episode_source_capable_initiating_findings(hypothesis)
                    ),
                    basis=(RootCauseEligibilityBasis.MIXED_OR_UNKNOWN_CAUSAL_ROLE,),
                    rationale="current positive evidence does not safely determine root-cause eligibility",
                )
            )
        else:
            assessments.append(assess_root_cause_eligibility(hypothesis, role))
    return RootCauseEligibilities(assessments)


__all__ = [
    "HypothesisRootCauseEligibility",
    "RootCauseEligibilities",
    "RootCauseEligibilityBasis",
    "RootCauseEligibilityState",
    "assess_root_cause_eligibility",
    "derive_root_cause_eligibilities",
    "episode_source_capable_initiating_findings",
]
