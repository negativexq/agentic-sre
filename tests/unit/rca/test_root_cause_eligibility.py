"""Unit tests for conservative P2G-6 root-cause eligibility."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from packages.rca.causal_roles import (
    HypothesisCausalRole,
    HypothesisCausalRoleAssessment,
    HypothesisCausalRoles,
    HypothesisCausalRoleStats,
)
from packages.rca.model import (
    CausalHop,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Resolution,
    ResolutionReasonCode,
)
from packages.rca.resolution import resolve_hypotheses
from packages.rca.root_cause_eligibility import (
    RootCauseEligibilities,
    RootCauseEligibilityBasis,
    RootCauseEligibilityState,
    assess_root_cause_eligibility,
    derive_root_cause_eligibilities,
    episode_source_capable_initiating_findings,
)

_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _finding(
    entity: EntityRef,
    kind: FindingKind,
    evidence: str,
    *,
    role: EvidenceTemporalRole = EvidenceTemporalRole.INITIATING,
    at: datetime | None = _AT,
) -> Finding:
    return Finding(
        kind=kind,
        entity=entity,
        at=at,
        temporal_role=role,
        summary=f"{kind.value} on {entity.name}",
        evidence_ids=(evidence,),
    )


def _hypothesis(
    actor: EntityRef,
    findings: tuple[Finding, ...],
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=f"hypothesis:{actor.name}",
        causal_actor=actor,
        members=(actor,),
        findings=findings,
        initiating_findings=tuple(
            item for item in findings if item.temporal_role is EvidenceTemporalRole.INITIATING
        ),
        causal_paths=((CausalHop(source=actor, relation="causes", target=actor),),),
        causal_explanation="PATH",
    )


def _role(
    hypothesis: Hypothesis,
    role: HypothesisCausalRole,
    *,
    incoming: int = 0,
    pairs: int = 0,
    incoming_ids: tuple[str, ...] = (),
    actor_findings: int = 0,
    mixed: bool = False,
) -> HypothesisCausalRoleAssessment:
    return HypothesisCausalRoleAssessment(
        hypothesis_id=hypothesis.hypothesis_id,
        causal_actor=hypothesis.causal_actor,
        role=role,
        source_capable_initiating_findings=actor_findings,
        verified_incoming_edges=incoming,
        verified_incoming_pairs=pairs,
        verified_outgoing_edges=0,
        verified_outgoing_pairs=0,
        unresolved_incoming_edges=0,
        contradicted_incoming_edges=0,
        incoming_propagation_evidence_ids=incoming_ids,
        mixed_role_evidence=mixed,
        rationale="test",
    )


def test_episode_initiating_evidence_is_not_actor_limited() -> None:
    actor = _entity("Pod", "payment")
    member = _entity("ConfigMap", "payment-config")
    finding = _finding(member, FindingKind.CONFIG_CHANGE, "config-change")
    hypothesis = _hypothesis(actor, (finding,))

    assert episode_source_capable_initiating_findings(hypothesis) == (finding,)
    role = _role(hypothesis, HypothesisCausalRole.PROPAGATED_EFFECT, incoming=1, pairs=1)
    assessment = assess_root_cause_eligibility(hypothesis, role)
    assert assessment.state is RootCauseEligibilityState.UNDETERMINED


def test_propagated_effect_without_episode_initiator_is_ineligible() -> None:
    actor = _entity("Pod", "payment")
    hypothesis = _hypothesis(
        actor,
        (
            _finding(
                actor, FindingKind.FAILURE_EVENT, "failure", role=EvidenceTemporalRole.SUPPORTING
            ),
        ),
    )
    assessment = assess_root_cause_eligibility(
        hypothesis,
        _role(
            hypothesis,
            HypothesisCausalRole.PROPAGATED_EFFECT,
            incoming=1,
            pairs=3,
            incoming_ids=("trace:1",),
        ),
    )
    assert assessment.state is RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT
    assert (
        RootCauseEligibilityBasis.NO_EPISODE_SOURCE_CAPABLE_INITIATING_EVIDENCE in assessment.basis
    )


def test_source_capable_is_eligible_and_unknown_is_fail_open() -> None:
    actor = _entity("Deployment", "payment")
    source = _finding(actor, FindingKind.CONFIG_CHANGE, "config")
    hypothesis = _hypothesis(actor, (source,))
    eligible = assess_root_cause_eligibility(
        hypothesis,
        _role(hypothesis, HypothesisCausalRole.SOURCE_CAPABLE, actor_findings=1),
    )
    assert eligible.state is RootCauseEligibilityState.ELIGIBLE

    unknown = assess_root_cause_eligibility(
        _hypothesis(actor, ()), _role(_hypothesis(actor, ()), HypothesisCausalRole.UNKNOWN)
    )
    assert unknown.state is RootCauseEligibilityState.UNDETERMINED


def test_selectable_predicate_fails_open_except_for_positive_exclusion() -> None:
    actor = _entity("Pod", "payment")
    hypothesis = _hypothesis(actor, ())
    role = _role(hypothesis, HypothesisCausalRole.UNKNOWN)
    index = derive_root_cause_eligibilities(
        (hypothesis,),
        HypothesisCausalRoles(
            (role,),
            HypothesisCausalRoleStats(
                **{name: 0 for name in HypothesisCausalRoleStats.model_fields}
            ),
        ),
    )
    assert index.is_root_cause_selectable(hypothesis.hypothesis_id)
    assert RootCauseEligibilities.empty().is_root_cause_selectable("missing")


def test_eligibility_mismatch_and_invariant_fail_loudly() -> None:
    actor = _entity("Pod", "payment")
    hypothesis = _hypothesis(actor, ())
    other = _hypothesis(_entity("Pod", "checkout"), ())
    with pytest.raises(ValueError):
        assess_root_cause_eligibility(hypothesis, _role(other, HypothesisCausalRole.UNKNOWN))
    with pytest.raises(ValueError):
        assess_root_cause_eligibility(
            hypothesis,
            _role(hypothesis, HypothesisCausalRole.PROPAGATED_EFFECT),
        )


def test_resolver_excludes_only_ineligible_propagated_effects() -> None:
    source_actor = _entity("Deployment", "source")
    source = _hypothesis(
        source_actor,
        (_finding(source_actor, FindingKind.CONFIG_CHANGE, "source-change"),),
    )
    affected_actor = _entity("Pod", "affected")
    affected = _hypothesis(
        affected_actor,
        (_finding(affected_actor, FindingKind.FAILURE_EVENT, "affected-failure"),),
    )
    roles = HypothesisCausalRoles(
        (
            _role(source, HypothesisCausalRole.SOURCE_CAPABLE, actor_findings=1),
            _role(
                affected,
                HypothesisCausalRole.PROPAGATED_EFFECT,
                incoming=1,
                pairs=1,
                incoming_ids=("trace:propagated",),
            ),
        ),
        HypothesisCausalRoleStats(**{name: 0 for name in HypothesisCausalRoleStats.model_fields}),
    )
    eligibility = derive_root_cause_eligibilities((source, affected), roles)
    trace = resolve_hypotheses((source, affected), root_cause_eligibilities=eligibility)

    assert trace.state is Resolution.RESOLVED
    assert trace.leading_hypothesis_ids == (source.hypothesis_id,)
    assert trace.decision_basis == "ROOT_CAUSE_ELIGIBILITY"
    assert affected.hypothesis_id in trace.considered_hypotheses
    assert affected.hypothesis_id in trace.eliminated_hypotheses
    assert any(
        item.hypothesis_id == affected.hypothesis_id
        and item.code is ResolutionReasonCode.ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT
        for item in trace.eliminations
    )
