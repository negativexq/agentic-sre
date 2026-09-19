"""Deterministic, actor-scoped causal-role assessments.

These assessments are observational metadata.  They deliberately remain
separate from hypothesis epistemic state and do not grant any resolver or
root-cause eligibility authority.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.rca.model import (
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
)
from packages.rca.runtime_evidence import RuntimeKubernetesBinding
from packages.rca.runtime_propagation import (
    RuntimeBindingVerificationState,
    RuntimePropagation,
    RuntimePropagationEdge,
)


class HypothesisCausalRole(StrEnum):
    SOURCE_CAPABLE = "SOURCE_CAPABLE"
    PROPAGATED_EFFECT = "PROPAGATED_EFFECT"
    MANIFESTATION = "MANIFESTATION"
    UNKNOWN = "UNKNOWN"


class CausalRoleBasis(StrEnum):
    ACTOR_ALIGNED_INITIATING_EVIDENCE = "ACTOR_ALIGNED_INITIATING_EVIDENCE"
    VERIFIED_INCOMING_PROPAGATION = "VERIFIED_INCOMING_PROPAGATION"
    VERIFIED_OUTGOING_PROPAGATION = "VERIFIED_OUTGOING_PROPAGATION"
    UNRESOLVED_INCOMING_PROPAGATION = "UNRESOLVED_INCOMING_PROPAGATION"
    CONTRADICTED_INCOMING_PROPAGATION = "CONTRADICTED_INCOMING_PROPAGATION"
    ACTOR_MANIFESTATION_ONLY = "ACTOR_MANIFESTATION_ONLY"
    MANIFESTATION_TIME_OVERLAP = "MANIFESTATION_TIME_OVERLAP"
    MIXED_SOURCE_AND_PROPAGATED_EVIDENCE = "MIXED_SOURCE_AND_PROPAGATED_EVIDENCE"
    NO_POSITIVE_ROLE_EVIDENCE = "NO_POSITIVE_ROLE_EVIDENCE"


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

_MANIFESTATION_KINDS = frozenset(
    {
        FindingKind.CONTAINER_FAILURE,
        FindingKind.RESOURCE_PRESSURE,
        FindingKind.DEPENDENCY_ERRORS,
        FindingKind.FAILURE_EVENT,
    }
)

_MAX_PROVENANCE = 32
_MIN_TIMESTAMP = datetime.min.replace(tzinfo=UTC)


def runtime_binding_entities(
    binding: RuntimeKubernetesBinding | None,
) -> tuple[EntityRef, ...]:
    """Return only the exact Deployment and Pod identities in a trace binding."""
    if binding is None:
        return ()
    entities: list[EntityRef] = []
    if binding.deployment is not None:
        entities.append(
            EntityRef(namespace=binding.namespace, kind="Deployment", name=binding.deployment)
        )
    if binding.pod is not None:
        entities.append(EntityRef(namespace=binding.namespace, kind="Pod", name=binding.pod))
    return tuple(sorted(entities, key=lambda item: item.canonical))


def actor_findings(hypothesis: Hypothesis) -> tuple[Finding, ...]:
    """Return findings attached to the exact causal actor only."""
    return tuple(item for item in hypothesis.findings if item.entity == hypothesis.causal_actor)


def actor_aligned_initiating_findings(hypothesis: Hypothesis) -> tuple[Finding, ...]:
    """Return actor-local findings with source-capable initiating semantics."""
    return tuple(
        item
        for item in actor_findings(hypothesis)
        if item.kind in _SOURCE_CAPABLE_INITIATING_KINDS
        and item.temporal_role is EvidenceTemporalRole.INITIATING
    )


def actor_manifestation_findings(hypothesis: Hypothesis) -> tuple[Finding, ...]:
    """Return actor-local findings classified as manifestations."""
    return tuple(item for item in actor_findings(hypothesis) if item.kind in _MANIFESTATION_KINDS)


def actor_is_manifestation_only(hypothesis: Hypothesis) -> bool:
    """Whether every actor-local finding is a manifestation and none initiates."""
    findings = actor_findings(hypothesis)
    return bool(
        findings
        and all(item.kind in _MANIFESTATION_KINDS for item in findings)
        and not actor_aligned_initiating_findings(hypothesis)
    )


def propagation_overlaps_actor_manifestation(
    hypothesis: Hypothesis,
    edge: RuntimePropagationEdge,
) -> bool:
    """Check inclusive overlap with an actor-local timestamped manifestation."""
    return any(
        finding.at is not None and edge.first_seen <= finding.at <= edge.last_seen
        for finding in actor_manifestation_findings(hypothesis)
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


def _edge_provenance(
    edges: Sequence[RuntimePropagationEdge],
) -> tuple[str, ...]:
    candidates: list[tuple[tuple[object, ...], str]] = []
    for edge in edges:
        for evidence_id in edge.evidence_ids:
            candidates.append(
                (
                    (edge.first_seen, edge.source_service, edge.affected_service, evidence_id),
                    evidence_id,
                )
            )
    return _bounded_ids(candidates)


def _initiating_provenance(findings: Sequence[Finding]) -> tuple[str, ...]:
    candidates: list[tuple[tuple[object, ...], str]] = []
    for finding in findings:
        at = finding.at if finding.at is not None else _MIN_TIMESTAMP
        for evidence_id in finding.evidence_ids:
            candidates.append(
                ((at, finding.kind.value, finding.entity.canonical, evidence_id), evidence_id)
            )
    return _bounded_ids(candidates)


def _matched_edges(
    actor: EntityRef,
    propagation: RuntimePropagation,
) -> tuple[tuple[RuntimePropagationEdge, ...], tuple[RuntimePropagationEdge, ...]]:
    incoming: list[RuntimePropagationEdge] = []
    outgoing: list[RuntimePropagationEdge] = []
    for edge in propagation.edges:
        if actor in runtime_binding_entities(edge.affected_binding):
            incoming.append(edge)
        if actor in runtime_binding_entities(edge.source_binding):
            outgoing.append(edge)
    return tuple(incoming), tuple(outgoing)


class HypothesisCausalRoleAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    hypothesis_id: str = Field(min_length=1)
    causal_actor: EntityRef
    role: HypothesisCausalRole
    actor_finding_kinds: tuple[str, ...] = ()
    actor_manifestation_only: bool = False
    manifestation_time_overlap: bool = False
    source_capable_initiating_findings: int = Field(ge=0)
    verified_incoming_edges: int = Field(ge=0)
    verified_incoming_pairs: int = Field(ge=0)
    verified_outgoing_edges: int = Field(ge=0)
    verified_outgoing_pairs: int = Field(ge=0)
    unresolved_incoming_edges: int = Field(ge=0)
    contradicted_incoming_edges: int = Field(ge=0)
    incoming_from_services: tuple[str, ...] = ()
    propagates_to_services: tuple[str, ...] = ()
    initiating_evidence_ids: tuple[str, ...] = ()
    incoming_propagation_evidence_ids: tuple[str, ...] = ()
    outgoing_propagation_evidence_ids: tuple[str, ...] = ()
    basis: tuple[CausalRoleBasis, ...] = ()
    mixed_role_evidence: bool = False
    rationale: str

    @model_validator(mode="after")
    def _bounded_provenance(self) -> HypothesisCausalRoleAssessment:
        if any(
            len(values) > _MAX_PROVENANCE
            for values in (
                self.initiating_evidence_ids,
                self.incoming_propagation_evidence_ids,
                self.outgoing_propagation_evidence_ids,
            )
        ):
            raise ValueError("causal role provenance is unbounded")
        return self


class HypothesisCausalRoleStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    hypotheses: int = Field(ge=0)
    source_capable: int = Field(ge=0)
    propagated_effect: int = Field(ge=0)
    manifestation: int = Field(ge=0)
    unknown: int = Field(ge=0)
    mixed_unknown: int = Field(ge=0)
    actor_manifestation_only: int = Field(ge=0)
    actor_manifestation_time_overlap: int = Field(ge=0)
    verified_incoming_actor_edges: int = Field(ge=0)
    verified_incoming_actor_pairs: int = Field(ge=0)
    verified_outgoing_actor_edges: int = Field(ge=0)
    verified_outgoing_actor_pairs: int = Field(ge=0)
    unresolved_incoming_actor_edges: int = Field(ge=0)
    contradicted_incoming_actor_edges: int = Field(ge=0)


class HypothesisCausalRoles:
    """Deterministic, bounded lookup of one causal-role assessment per hypothesis."""

    __slots__ = ("assessments", "stats")

    def __init__(
        self,
        assessments: Sequence[HypothesisCausalRoleAssessment],
        stats: HypothesisCausalRoleStats,
    ) -> None:
        self.assessments = tuple(sorted(assessments, key=lambda item: item.hypothesis_id))
        self.stats = stats

    @classmethod
    def empty(cls) -> HypothesisCausalRoles:
        return cls(
            (),
            HypothesisCausalRoleStats(
                **{name: 0 for name in HypothesisCausalRoleStats.model_fields}
            ),
        )

    def for_hypothesis(self, hypothesis_id: str) -> HypothesisCausalRoleAssessment | None:
        return next(
            (item for item in self.assessments if item.hypothesis_id == hypothesis_id), None
        )

    def for_actor(self, actor: EntityRef) -> tuple[HypothesisCausalRoleAssessment, ...]:
        return tuple(item for item in self.assessments if item.causal_actor == actor)


def _assessment(
    hypothesis: Hypothesis,
    propagation: RuntimePropagation,
) -> HypothesisCausalRoleAssessment:
    actor = hypothesis.causal_actor
    findings = actor_findings(hypothesis)
    initiating = actor_aligned_initiating_findings(hypothesis)
    manifestation_only = actor_is_manifestation_only(hypothesis)
    incoming, outgoing = _matched_edges(actor, propagation)
    verified_incoming = tuple(
        edge
        for edge in incoming
        if edge.affected_binding_state is RuntimeBindingVerificationState.VERIFIED
    )
    verified_outgoing = tuple(
        edge
        for edge in outgoing
        if edge.source_binding_state is RuntimeBindingVerificationState.VERIFIED
    )
    unresolved_incoming = tuple(
        edge
        for edge in incoming
        if edge.affected_binding_state is RuntimeBindingVerificationState.UNRESOLVED
    )
    contradicted_incoming = tuple(
        edge
        for edge in incoming
        if edge.affected_binding_state is RuntimeBindingVerificationState.CONTRADICTED
    )
    overlap = bool(
        manifestation_only
        and any(
            propagation_overlaps_actor_manifestation(hypothesis, edge) for edge in verified_incoming
        )
    )
    source_capable = bool(initiating)
    mixed = source_capable and bool(verified_incoming)

    if mixed:
        role = HypothesisCausalRole.UNKNOWN
        rationale = (
            "the actor has both actor-local initiating evidence and VERIFIED incoming "
            "propagation; P2G-5 does not collapse mixed causal-role evidence"
        )
    elif verified_incoming and manifestation_only and overlap:
        role = HypothesisCausalRole.MANIFESTATION
        rationale = (
            "the exact causal actor has manifestation-only local findings and a VERIFIED "
            "incoming propagation overlaps an observed manifestation time"
        )
    elif verified_incoming:
        role = HypothesisCausalRole.PROPAGATED_EFFECT
        rationale = (
            "the exact causal actor is a VERIFIED affected endpoint of direct runtime "
            "non-success propagation"
        )
    elif source_capable:
        role = HypothesisCausalRole.SOURCE_CAPABLE
        rationale = (
            "actor-local initiating evidence is present; no VERIFIED incoming propagation "
            "was observed for the exact actor binding"
        )
    else:
        role = HypothesisCausalRole.UNKNOWN
        rationale = "current positive evidence does not establish a deterministic causal role for the exact causal actor"

    basis: list[CausalRoleBasis] = []
    if source_capable:
        basis.append(CausalRoleBasis.ACTOR_ALIGNED_INITIATING_EVIDENCE)
    if verified_incoming:
        basis.append(CausalRoleBasis.VERIFIED_INCOMING_PROPAGATION)
    if verified_outgoing:
        basis.append(CausalRoleBasis.VERIFIED_OUTGOING_PROPAGATION)
    if unresolved_incoming:
        basis.append(CausalRoleBasis.UNRESOLVED_INCOMING_PROPAGATION)
    if contradicted_incoming:
        basis.append(CausalRoleBasis.CONTRADICTED_INCOMING_PROPAGATION)
    if manifestation_only:
        basis.append(CausalRoleBasis.ACTOR_MANIFESTATION_ONLY)
    if overlap:
        basis.append(CausalRoleBasis.MANIFESTATION_TIME_OVERLAP)
    if mixed:
        basis.append(CausalRoleBasis.MIXED_SOURCE_AND_PROPAGATED_EVIDENCE)
    if not basis:
        basis.append(CausalRoleBasis.NO_POSITIVE_ROLE_EVIDENCE)

    incoming_services = tuple(sorted({edge.source_service for edge in verified_incoming}))
    outgoing_services = tuple(sorted({edge.affected_service for edge in verified_outgoing}))
    return HypothesisCausalRoleAssessment(
        hypothesis_id=hypothesis.hypothesis_id,
        causal_actor=actor,
        role=role,
        actor_finding_kinds=tuple(sorted({item.kind.value for item in findings})),
        actor_manifestation_only=manifestation_only,
        manifestation_time_overlap=overlap,
        source_capable_initiating_findings=len(initiating),
        verified_incoming_edges=len(verified_incoming),
        verified_incoming_pairs=sum(edge.observed_pairs for edge in verified_incoming),
        verified_outgoing_edges=len(verified_outgoing),
        verified_outgoing_pairs=sum(edge.observed_pairs for edge in verified_outgoing),
        unresolved_incoming_edges=len(unresolved_incoming),
        contradicted_incoming_edges=len(contradicted_incoming),
        incoming_from_services=incoming_services,
        propagates_to_services=outgoing_services,
        initiating_evidence_ids=_initiating_provenance(initiating),
        incoming_propagation_evidence_ids=_edge_provenance(verified_incoming),
        outgoing_propagation_evidence_ids=_edge_provenance(verified_outgoing),
        basis=tuple(basis),
        mixed_role_evidence=mixed,
        rationale=rationale,
    )


def derive_hypothesis_causal_roles(
    hypotheses: Sequence[Hypothesis],
    propagation: RuntimePropagation,
) -> HypothesisCausalRoles:
    """Assess exact causal actors from local findings and direct propagation edges."""
    assessments = tuple(_assessment(item, propagation) for item in hypotheses)
    counts = {name: 0 for name in HypothesisCausalRoleStats.model_fields}
    counts["hypotheses"] = len(assessments)
    for assessment in assessments:
        counts[
            {
                HypothesisCausalRole.SOURCE_CAPABLE: "source_capable",
                HypothesisCausalRole.PROPAGATED_EFFECT: "propagated_effect",
                HypothesisCausalRole.MANIFESTATION: "manifestation",
                HypothesisCausalRole.UNKNOWN: "unknown",
            }[assessment.role]
        ] += 1
        counts["mixed_unknown"] += int(assessment.mixed_role_evidence)
        counts["actor_manifestation_only"] += int(assessment.actor_manifestation_only)
        counts["actor_manifestation_time_overlap"] += int(assessment.manifestation_time_overlap)
        counts["verified_incoming_actor_edges"] += assessment.verified_incoming_edges
        counts["verified_incoming_actor_pairs"] += assessment.verified_incoming_pairs
        counts["verified_outgoing_actor_edges"] += assessment.verified_outgoing_edges
        counts["verified_outgoing_actor_pairs"] += assessment.verified_outgoing_pairs
        counts["unresolved_incoming_actor_edges"] += assessment.unresolved_incoming_edges
        counts["contradicted_incoming_actor_edges"] += assessment.contradicted_incoming_edges
    return HypothesisCausalRoles(assessments, HypothesisCausalRoleStats(**counts))


__all__ = [
    "CausalRoleBasis",
    "HypothesisCausalRole",
    "HypothesisCausalRoleAssessment",
    "HypothesisCausalRoleStats",
    "HypothesisCausalRoles",
    "actor_aligned_initiating_findings",
    "actor_findings",
    "actor_is_manifestation_only",
    "actor_manifestation_findings",
    "derive_hypothesis_causal_roles",
    "propagation_overlaps_actor_manifestation",
    "runtime_binding_entities",
]
