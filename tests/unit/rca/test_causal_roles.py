from datetime import UTC, datetime, timedelta
from random import Random

import pytest

from packages.rca.causal_roles import (
    CausalRoleBasis,
    HypothesisCausalRole,
    actor_aligned_initiating_findings,
    actor_is_manifestation_only,
    derive_hypothesis_causal_roles,
    runtime_binding_entities,
)
from packages.rca.model import (
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
)
from packages.rca.runtime_evidence import (
    RuntimeBindingQuality,
    RuntimeKubernetesBinding,
    RuntimeOutcomeState,
    RuntimeProtocol,
)
from packages.rca.runtime_propagation import (
    RuntimeBindingVerificationState,
    RuntimePropagation,
    RuntimePropagationEdge,
    RuntimePropagationMechanism,
)

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _ref(kind: str, name: str, namespace: str = "shop") -> EntityRef:
    return EntityRef(namespace=namespace, kind=kind, name=name)


def _binding(
    deployment: str | None,
    pod: str | None,
    namespace: str = "shop",
) -> RuntimeKubernetesBinding:
    if deployment is not None and pod is not None:
        quality = RuntimeBindingQuality.NAMESPACE_DEPLOYMENT_POD
    elif deployment is not None:
        quality = RuntimeBindingQuality.NAMESPACE_DEPLOYMENT
    elif pod is not None:
        quality = RuntimeBindingQuality.NAMESPACE_POD
    else:
        quality = RuntimeBindingQuality.NAMESPACE_ONLY
    return RuntimeKubernetesBinding(
        namespace=namespace, deployment=deployment, pod=pod, quality=quality
    )


def _finding(
    entity: EntityRef,
    kind: FindingKind,
    *,
    at: datetime | None = T0,
    temporal_role: EvidenceTemporalRole = EvidenceTemporalRole.AMBIGUOUS,
    evidence: str = "finding:1",
) -> Finding:
    return Finding(
        kind=kind,
        entity=entity,
        at=at,
        summary=kind.value,
        evidence_ids=(evidence,),
        temporal_role=temporal_role,
    )


def _hypothesis(
    actor: EntityRef,
    findings: tuple[Finding, ...] = (),
    *,
    hypothesis_id: str = "h1",
) -> Hypothesis:
    return Hypothesis(hypothesis_id=hypothesis_id, causal_actor=actor, findings=findings)


def _edge(
    *,
    source: RuntimeKubernetesBinding | None,
    affected: RuntimeKubernetesBinding | None,
    source_state: RuntimeBindingVerificationState | None = RuntimeBindingVerificationState.VERIFIED,
    affected_state: RuntimeBindingVerificationState
    | None = RuntimeBindingVerificationState.VERIFIED,
    at: int = 0,
    evidence: str = "edge:1",
    pairs: int = 1,
) -> RuntimePropagationEdge:
    return RuntimePropagationEdge(
        source_service="payment",
        affected_service="checkout",
        mechanism=RuntimePropagationMechanism.REMOTE_NON_SUCCESS_RETURN,
        source_binding=source,
        affected_binding=affected,
        source_binding_state=source_state,
        affected_binding_state=affected_state,
        source_state=RuntimeOutcomeState.ERROR,
        affected_state=RuntimeOutcomeState.NON_OK,
        source_protocol=RuntimeProtocol.GRPC,
        affected_protocol=RuntimeProtocol.HTTP,
        source_code="INTERNAL",
        affected_code="500",
        first_seen=T0 + timedelta(seconds=at),
        last_seen=T0 + timedelta(seconds=at + 10),
        observed_pairs=pairs,
        evidence_ids=(evidence,),
        trace_ids=(f"trace:{evidence}",),
    )


def _propagation(*edges: RuntimePropagationEdge) -> RuntimePropagation:
    return RuntimePropagation(
        binding_verifications=(),
        boundaries=(),
        edges=edges,
        stats=RuntimePropagation.empty().stats.model_copy(update={"propagation_edges": len(edges)}),
    )


def test_runtime_binding_entities_are_exact_deployment_and_pod_refs() -> None:
    binding = _binding("payment", "payment-abc")
    assert tuple(item.canonical for item in runtime_binding_entities(binding)) == (
        "shop/Deployment/payment",
        "shop/Pod/payment-abc",
    )
    assert runtime_binding_entities(None) == ()


def test_actor_local_initiating_evidence_does_not_promote_a_member() -> None:
    actor = _ref("Deployment", "payment")
    member = _ref("ConfigMap", "payment-config")
    hypothesis = _hypothesis(
        actor,
        (
            _finding(
                member, FindingKind.CONFIG_CHANGE, temporal_role=EvidenceTemporalRole.INITIATING
            ),
        ),
    )
    assert actor_aligned_initiating_findings(hypothesis) == ()
    result = derive_hypothesis_causal_roles((hypothesis,), RuntimePropagation.empty())
    assert result.assessments[0].role is HypothesisCausalRole.UNKNOWN


def test_manifestation_only_requires_all_actor_findings_to_be_manifestations() -> None:
    actor = _ref("Pod", "payment-abc")
    only = _hypothesis(
        actor,
        (
            _finding(actor, FindingKind.FAILURE_EVENT),
            _finding(actor, FindingKind.CONTAINER_FAILURE),
        ),
    )
    mixed = _hypothesis(
        actor,
        (_finding(actor, FindingKind.FAILURE_EVENT), _finding(actor, FindingKind.ROLLOUT_RESTART)),
    )
    assert actor_is_manifestation_only(only)
    assert not actor_is_manifestation_only(mixed)


@pytest.mark.parametrize(
    ("incoming_state", "expected_basis"),
    [
        (
            RuntimeBindingVerificationState.UNRESOLVED,
            CausalRoleBasis.UNRESOLVED_INCOMING_PROPAGATION,
        ),
        (
            RuntimeBindingVerificationState.CONTRADICTED,
            CausalRoleBasis.CONTRADICTED_INCOMING_PROPAGATION,
        ),
    ],
)
def test_non_verified_incoming_is_diagnostic_only(
    incoming_state: RuntimeBindingVerificationState,
    expected_basis: CausalRoleBasis,
) -> None:
    actor = _ref("Deployment", "checkout")
    result = derive_hypothesis_causal_roles(
        (_hypothesis(actor),),
        _propagation(
            _edge(
                source=_binding("payment", "payment-1"),
                affected=_binding("checkout", "checkout-1"),
                affected_state=incoming_state,
            )
        ),
    )
    assessment = result.assessments[0]
    assert assessment.role is HypothesisCausalRole.UNKNOWN
    assert expected_basis in assessment.basis
    assert assessment.verified_incoming_edges == 0


def test_verified_incoming_assigns_propagated_effect_and_exact_actor_matching() -> None:
    actor = _ref("Deployment", "checkout")
    assessment = derive_hypothesis_causal_roles(
        (_hypothesis(actor),),
        _propagation(
            _edge(
                source=_binding("payment", "payment-1"),
                affected=_binding("checkout", "checkout-1"),
            )
        ),
    ).assessments[0]
    assert assessment.role is HypothesisCausalRole.PROPAGATED_EFFECT
    assert assessment.verified_incoming_edges == 1
    assert assessment.verified_incoming_pairs == 1
    assert assessment.incoming_from_services == ("payment",)


def test_service_name_and_namespace_are_not_fuzzy_actor_matches() -> None:
    service_actor = _ref("Service", "checkout")
    wrong_namespace = _ref("Deployment", "checkout", "other")
    edge = _edge(
        source=_binding("payment", "payment-1"), affected=_binding("checkout", "checkout-1")
    )
    roles = derive_hypothesis_causal_roles(
        (
            _hypothesis(service_actor, hypothesis_id="h-service"),
            _hypothesis(wrong_namespace, hypothesis_id="h-ns"),
        ),
        _propagation(edge),
    )
    assert all(item.verified_incoming_edges == 0 for item in roles.assessments)


def test_outgoing_only_is_not_source_capable() -> None:
    actor = _ref("Deployment", "payment")
    assessment = derive_hypothesis_causal_roles(
        (_hypothesis(actor),),
        _propagation(
            _edge(
                source=_binding("payment", "payment-1"), affected=_binding("checkout", "checkout-1")
            )
        ),
    ).assessments[0]
    assert assessment.role is HypothesisCausalRole.UNKNOWN
    assert assessment.verified_outgoing_edges == 1
    assert CausalRoleBasis.VERIFIED_OUTGOING_PROPAGATION in assessment.basis


def test_source_capable_and_mixed_role_rules() -> None:
    actor = _ref("Deployment", "checkout")
    initiating = _finding(
        actor,
        FindingKind.CONFIG_CHANGE,
        temporal_role=EvidenceTemporalRole.INITIATING,
    )
    no_incoming = derive_hypothesis_causal_roles(
        (_hypothesis(actor, (initiating,)),), RuntimePropagation.empty()
    ).assessments[0]
    assert no_incoming.role is HypothesisCausalRole.SOURCE_CAPABLE

    mixed = derive_hypothesis_causal_roles(
        (_hypothesis(actor, (initiating,)),),
        _propagation(
            _edge(
                source=_binding("payment", "payment-1"), affected=_binding("checkout", "checkout-1")
            )
        ),
    ).assessments[0]
    assert mixed.role is HypothesisCausalRole.UNKNOWN
    assert mixed.mixed_role_evidence
    assert CausalRoleBasis.MIXED_SOURCE_AND_PROPAGATED_EVIDENCE in mixed.basis


def test_manifestation_requires_inclusive_temporal_overlap() -> None:
    actor = _ref("Deployment", "checkout")
    edge = _edge(
        source=_binding("payment", "payment-1"),
        affected=_binding("checkout", "checkout-1"),
        at=0,
    )
    for at in (0, 10):
        result = derive_hypothesis_causal_roles(
            (
                _hypothesis(
                    actor,
                    (_finding(actor, FindingKind.FAILURE_EVENT, at=T0 + timedelta(seconds=at)),),
                ),
            ),
            _propagation(edge),
        ).assessments[0]
        assert result.role is HypothesisCausalRole.MANIFESTATION
        assert result.manifestation_time_overlap

    before = derive_hypothesis_causal_roles(
        (
            _hypothesis(
                actor, (_finding(actor, FindingKind.FAILURE_EVENT, at=T0 - timedelta(seconds=1)),)
            ),
        ),
        _propagation(edge),
    ).assessments[0]
    after = derive_hypothesis_causal_roles(
        (
            _hypothesis(
                actor, (_finding(actor, FindingKind.FAILURE_EVENT, at=T0 + timedelta(seconds=11)),)
            ),
        ),
        _propagation(edge),
    ).assessments[0]
    assert before.role is HypothesisCausalRole.PROPAGATED_EFFECT
    assert after.role is HypothesisCausalRole.PROPAGATED_EFFECT


def test_manifestation_without_timestamp_is_propagated_effect() -> None:
    actor = _ref("Pod", "checkout-1")
    assessment = derive_hypothesis_causal_roles(
        (_hypothesis(actor, (_finding(actor, FindingKind.FAILURE_EVENT, at=None),)),),
        _propagation(
            _edge(
                source=_binding("payment", "payment-1"), affected=_binding("checkout", "checkout-1")
            )
        ),
    ).assessments[0]
    assert assessment.role is HypothesisCausalRole.PROPAGATED_EFFECT
    assert not assessment.manifestation_time_overlap


def test_intermediate_actor_is_propagated_effect_not_source() -> None:
    actor = _ref("Deployment", "payment")
    incoming = _edge(source=_binding("database", "db-1"), affected=_binding("payment", "payment-1"))
    outgoing = _edge(
        source=_binding("payment", "payment-1"), affected=_binding("checkout", "checkout-1")
    )
    result = derive_hypothesis_causal_roles((_hypothesis(actor),), _propagation(incoming, outgoing))
    assessment = result.assessments[0]
    assert assessment.role is HypothesisCausalRole.PROPAGATED_EFFECT
    assert assessment.verified_incoming_edges == 1
    assert assessment.verified_outgoing_edges == 1


def test_role_index_is_sorted_and_provenance_is_bounded_and_deterministic() -> None:
    actor = _ref("Deployment", "checkout")
    edges = tuple(
        _edge(
            source=_binding("payment", f"payment-{index}"),
            affected=_binding("checkout", f"checkout-{index}"),
            at=index,
            evidence=f"edge:{index:03d}",
        )
        for index in range(40)
    )
    hypotheses = tuple(_hypothesis(actor, hypothesis_id=f"h{index:02d}") for index in range(3))
    first = derive_hypothesis_causal_roles(hypotheses, _propagation(*edges))
    shuffled = list(edges)
    Random(7).shuffle(shuffled)
    second = derive_hypothesis_causal_roles(tuple(reversed(hypotheses)), _propagation(*shuffled))
    assert [item.hypothesis_id for item in first.assessments] == ["h00", "h01", "h02"]
    assert first.assessments[0].model_dump(mode="json") == second.assessments[0].model_dump(
        mode="json"
    )
    assert first.assessments[0].verified_incoming_pairs == 40
    assert len(first.assessments[0].incoming_propagation_evidence_ids) <= 32
    assert first.stats.propagated_effect == 3


def test_no_evidence_is_unknown_and_initial_local_evidence_is_allowed() -> None:
    actor = _ref("Deployment", "checkout")
    unknown = derive_hypothesis_causal_roles((_hypothesis(actor),), RuntimePropagation.empty())
    assert unknown.assessments[0].role is HypothesisCausalRole.UNKNOWN
    assert unknown.assessments[0].basis == (CausalRoleBasis.NO_POSITIVE_ROLE_EVIDENCE,)
    local = derive_hypothesis_causal_roles(
        (
            _hypothesis(
                actor,
                (
                    _finding(
                        actor,
                        FindingKind.ROLLOUT_RESTART,
                        temporal_role=EvidenceTemporalRole.INITIATING,
                    ),
                ),
            ),
        ),
        RuntimePropagation.empty(),
    )
    assert local.assessments[0].role is HypothesisCausalRole.SOURCE_CAPABLE


def test_role_derivation_does_not_mutate_hypothesis() -> None:
    actor = _ref("Deployment", "checkout")
    hypothesis = _hypothesis(
        actor,
        (
            _finding(
                actor, FindingKind.CONFIG_CHANGE, temporal_role=EvidenceTemporalRole.INITIATING
            ),
        ),
    )
    before = hypothesis.model_dump(mode="json")
    roles = derive_hypothesis_causal_roles((hypothesis,), RuntimePropagation.empty())
    assert roles.for_hypothesis(hypothesis.hypothesis_id) is not None
    assert hypothesis.model_dump(mode="json") == before
