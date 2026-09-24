from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.rca.causal_roles import HypothesisCausalRoles
from packages.rca.frontier import apply_frontier_progress, covered_frontier_dimensions
from packages.rca.information_gap import (
    InformationGapContext,
    _capability_allows_target,
    derive_information_gaps,
)
from packages.rca.investigation.actions import observation_identity
from packages.rca.mechanism_bridge import RuntimeMechanismBridges
from packages.rca.model import (
    AuthorizedQuery,
    Confidence,
    Diagnosis,
    EntityRef,
    FrontierStatus,
    GapDimension,
    GapResolvability,
    Hypothesis,
    HypothesisEpistemicState,
    HypothesisResolutionAudit,
    HypothesisSignature,
    Resolution,
    ResolutionReasonCode,
    ResolutionTrace,
    StructuralAlternative,
    Symptoms,
)
from packages.rca.root_cause_eligibility import RootCauseEligibilities
from packages.rca.runtime_propagation import RuntimePropagation
from packages.rca.source import InMemorySource


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _context() -> InformationGapContext:
    return InformationGapContext(
        causal_roles=HypothesisCausalRoles.empty(),
        root_cause_eligibilities=RootCauseEligibilities.empty(),
        runtime_propagation=RuntimePropagation.empty(),
        runtime_mechanism_bridges=RuntimeMechanismBridges.empty(),
    )


def _unresolved_trace(hypothesis_id: str) -> ResolutionTrace:
    return ResolutionTrace(
        state=Resolution.INSUFFICIENT_EVIDENCE,
        unresolved_hypotheses=(hypothesis_id,),
        hypothesis_audits=(
            HypothesisResolutionAudit(
                hypothesis_id=hypothesis_id,
                signature=HypothesisSignature(),
                epistemic_state=HypothesisEpistemicState.UNRESOLVED,
                plausible=False,
                plausibility_reasons=(ResolutionReasonCode.NO_ONSET_CAPABLE_INITIATING_EVIDENCE,),
            ),
        ),
    )


def test_unresolved_deployment_is_actor_local() -> None:
    actor = _entity("Deployment", "payment")
    member = _entity("Pod", "payment-0")
    config = _entity("ConfigMap", "payment-config")
    hypothesis = Hypothesis(
        hypothesis_id="h-deployment",
        causal_actor=actor,
        members=(actor, member, config),
    )
    gaps = derive_information_gaps(
        (hypothesis,),
        _unresolved_trace(hypothesis.hypothesis_id),
        InMemorySource(name="deployment-gap"),
        runtime_context=_context(),
    )
    queries = [query for gap in gaps for query in gap.authorized_queries]
    assert {(query.capability, query.target) for query in queries} == {("history", actor)}


def test_pod_contract_has_events_pressure_and_traces_but_no_traffic() -> None:
    actor = _entity("Pod", "payment-0")
    hypothesis = Hypothesis(hypothesis_id="h-pod", causal_actor=actor, members=(actor,))
    gaps = derive_information_gaps(
        (hypothesis,),
        _unresolved_trace(hypothesis.hypothesis_id),
        InMemorySource(name="pod-gap"),
        runtime_context=_context(),
    )
    pairs = {(query.capability, query.target) for gap in gaps for query in gap.authorized_queries}
    assert {capability for capability, _target in pairs} == {
        "events",
        "resource_pressure",
        "runtime_traces",
    }
    assert all(target == actor for _capability, target in pairs)


def test_failure_onset_contract_admits_bounded_runtime_traces_for_pod() -> None:
    actor = _entity("Pod", "payment-0")
    hypothesis = Hypothesis(hypothesis_id="h-pod-onset", causal_actor=actor, members=(actor,))

    gaps = derive_information_gaps(
        (hypothesis,),
        _unresolved_trace(hypothesis.hypothesis_id),
        InMemorySource(name="pod-onset-gap"),
        runtime_context=_context(),
    )

    onset_gap = next(gap for gap in gaps if gap.dimension is GapDimension.FAILURE_ONSET)
    assert ("runtime_traces", actor) in {
        (query.capability, query.target) for query in onset_gap.authorized_queries
    }


def test_failure_onset_source_only_contract_excludes_unavailable_runtime_traces() -> None:
    class RuntimeUnavailableSource:
        def __init__(self, source: InMemorySource) -> None:
            self.source = source

        def supports(self, capability: str) -> bool:
            if capability == "runtime_traces":
                return False
            methods = {
                "history": "object_history",
                "events": "events",
                "incident_events": "events",
                "incident_changes": "object_history",
                "logs": "error_logs",
                "resource_pressure": "resource_pressure",
                "traffic": "traffic_observations",
            }
            method = methods.get(capability)
            return method is not None and callable(getattr(self.source, method, None))

        def __getattr__(self, name: str) -> Any:
            return getattr(self.source, name)

    actor = _entity("Pod", "payment-0")
    hypothesis = Hypothesis(hypothesis_id="h-pod-onset", causal_actor=actor, members=(actor,))
    source = RuntimeUnavailableSource(InMemorySource(name="source-only-pod-onset"))

    gaps = derive_information_gaps(
        (hypothesis,),
        _unresolved_trace(hypothesis.hypothesis_id),
        source,
        runtime_context=_context(),
    )

    onset_gap = next(gap for gap in gaps if gap.dimension is GapDimension.FAILURE_ONSET)
    assert tuple(query.capability for query in onset_gap.authorized_queries) == ("events",)


def test_service_contract_is_logs_and_traffic_only() -> None:
    actor = _entity("Service", "payment")
    hypothesis = Hypothesis(hypothesis_id="h-service", causal_actor=actor, members=(actor,))
    gaps = derive_information_gaps(
        (hypothesis,),
        _unresolved_trace(hypothesis.hypothesis_id),
        InMemorySource(name="service-gap"),
        runtime_context=_context(),
    )
    capabilities = {query.capability for gap in gaps for query in gap.authorized_queries}
    assert capabilities == {"logs", "traffic"}
    assert all(query.target == actor for gap in gaps for query in gap.authorized_queries)


def test_secret_is_never_authorized_and_traffic_is_service_only() -> None:
    secret = _entity("Secret", "payment-secret")
    assert not _capability_allows_target("history", secret)
    assert _capability_allows_target("traffic", _entity("Service", "payment"))
    assert not _capability_allows_target("traffic", _entity("Deployment", "payment"))
    assert not _capability_allows_target("traffic", _entity("Pod", "payment-0"))
    assert not _capability_allows_target("traffic", _entity("ConfigMap", "payment-config"))


def test_dependency_trace_prefers_controller_over_replica_pods() -> None:
    service = _entity("Service", "postgres")
    deployment = _entity("Deployment", "payment")
    pod_a = _entity("Pod", "payment-a")
    pod_b = _entity("Pod", "payment-b")
    alternative = StructuralAlternative(
        alternative_id="alternative:dependency",
        actor=service,
        role="dependency",
        queryable_dimensions=(GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN),
        observation_targets=(service, deployment, pod_a, pod_b),
    )
    trace = ResolutionTrace(state=Resolution.RESOLVED)
    gaps = derive_information_gaps(
        (),
        trace,
        InMemorySource(name="dependency-gap"),
        structural_alternatives=(alternative,),
        runtime_context=_context(),
    )
    trace_targets = {
        query.target
        for gap in gaps
        for query in gap.authorized_queries
        if query.capability == "runtime_traces"
    }
    assert trace_targets == {deployment}
    assert any(
        query.capability == "logs" and query.target == service
        for gap in gaps
        for query in gap.authorized_queries
    )


def test_autoscaler_events_cover_both_structural_dimensions() -> None:
    hpa = _entity("HorizontalPodAutoscaler", "payment-hpa")
    alternative = StructuralAlternative(
        alternative_id="alternative:autoscaler",
        actor=hpa,
        role="autoscaler",
        queryable_dimensions=(
            GapDimension.AUTOSCALING_TARGET_STATE,
            GapDimension.EVENT_SEQUENCE,
        ),
        observation_targets=(hpa,),
    )
    gaps = derive_information_gaps(
        (),
        ResolutionTrace(state=Resolution.RESOLVED),
        InMemorySource(name="autoscaler-gap"),
        structural_alternatives=(alternative,),
        runtime_context=_context(),
    )
    assert {
        (gap.dimension, query.capability, query.target)
        for gap in gaps
        for query in gap.authorized_queries
    } == {
        (GapDimension.AUTOSCALING_TARGET_STATE, "events", hpa),
        (GapDimension.EVENT_SEQUENCE, "events", hpa),
    }
    assert all(
        query.target.kind not in {"Pod", "Deployment"}
        and query.capability not in {"traffic", "logs", "runtime_traces"}
        for gap in gaps
        for query in gap.authorized_queries
    )

    diagnosis = Diagnosis(
        incident_id="autoscaler-frontier",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        resolution=Resolution.AMBIGUOUS,
        summary="autoscaler frontier",
        symptoms=Symptoms(
            onset=datetime(2026, 1, 1, tzinfo=UTC),
            last_seen=None,
            services=(),
            namespaces=(),
            alert_names=(),
        ),
        information_gaps=gaps,
    )
    covered = covered_frontier_dimensions(
        diagnosis,
        capability="events",
        target=hpa,
        evidence_acquired=True,
    )
    assert covered == {
        alternative.alternative_id: (
            GapDimension.AUTOSCALING_TARGET_STATE,
            GapDimension.EVENT_SEQUENCE,
        )
    }
    updated = apply_frontier_progress(
        (alternative,), hypotheses=(), queried_dimensions_by_alternative=covered
    )
    assert updated[0].status is FrontierStatus.QUERIED_NO_CAUSAL_FINDING


def test_network_policy_has_actor_local_history_frontier_contract() -> None:
    policy = _entity("NetworkPolicy", "deny-egress")
    alternative = StructuralAlternative(
        alternative_id="alternative:network-policy",
        actor=policy,
        role="network_policy",
        queryable_dimensions=(GapDimension.CHANGE_TIMING,),
        observation_targets=(policy,),
    )
    gaps = derive_information_gaps(
        (),
        ResolutionTrace(state=Resolution.RESOLVED),
        InMemorySource(name="network-policy-gap"),
        structural_alternatives=(alternative,),
        runtime_context=_context(),
    )
    assert len(gaps) == 1
    assert gaps[0].dimension is GapDimension.CHANGE_TIMING
    assert [(query.capability, query.target) for query in gaps[0].authorized_queries] == [
        ("history", policy)
    ]

    diagnosis = Diagnosis(
        incident_id="network-policy-frontier",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        resolution=Resolution.AMBIGUOUS,
        summary="network policy frontier",
        symptoms=Symptoms(
            onset=datetime(2026, 1, 1, tzinfo=UTC),
            last_seen=None,
            services=(),
            namespaces=(),
            alert_names=(),
        ),
        information_gaps=gaps,
    )
    covered = covered_frontier_dimensions(
        diagnosis,
        capability="history",
        target=policy,
        evidence_acquired=True,
    )
    updated = apply_frontier_progress(
        (alternative,), hypotheses=(), queried_dimensions_by_alternative=covered
    )
    assert updated[0].status is FrontierStatus.QUERIED_NO_CAUSAL_FINDING


def test_logical_history_gaps_share_one_physical_observation_identity() -> None:
    deployment = _entity("Deployment", "payment")
    first = AuthorizedQuery(capability="history", target=deployment)
    second = AuthorizedQuery(capability="history", target=deployment)
    assert observation_identity(first.capability, first.target, None) == observation_identity(
        second.capability, second.target, None
    )
    assert observation_identity(first.capability, first.target, None) == (
        "history|shop/Deployment/payment|{}"
    )


def test_supported_structural_roles_have_no_orphan_queryable_dimensions() -> None:
    roles = (
        (
            "configuration_source",
            _entity("ConfigMap", "payment-config"),
            (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
        ),
        (
            "workload_controller",
            _entity("Deployment", "payment"),
            (GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING),
        ),
        (
            "autoscaler",
            _entity("HorizontalPodAutoscaler", "payment-hpa"),
            (GapDimension.AUTOSCALING_TARGET_STATE, GapDimension.EVENT_SEQUENCE),
        ),
        (
            "fault_actor",
            _entity("PodChaos", "payment-chaos"),
            (GapDimension.EVENT_SEQUENCE, GapDimension.FAILURE_ONSET),
        ),
        (
            "dependency",
            _entity("Service", "postgres"),
            (GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN),
        ),
        (
            "network_policy",
            _entity("NetworkPolicy", "deny-egress"),
            (GapDimension.CHANGE_TIMING,),
        ),
    )
    for role, actor, dimensions in roles:
        alternative = StructuralAlternative(
            alternative_id=f"alternative:{role}",
            actor=actor,
            role=role,
            queryable_dimensions=dimensions,
            observation_targets=(actor,),
        )
        gaps = derive_information_gaps(
            (),
            ResolutionTrace(state=Resolution.RESOLVED),
            InMemorySource(name=f"role-{role}"),
            structural_alternatives=(alternative,),
            runtime_context=_context(),
        )
        assert {gap.dimension for gap in gaps} == set(dimensions)
        assert all(gap.authorized_queries for gap in gaps), role


def test_frontier_coverage_requires_novel_evidence_and_excludes_traces() -> None:
    target = _entity("Deployment", "payment")
    query = AuthorizedQuery(capability="history", target=target, alternative_ids=("a",))
    from packages.rca.model import (
        Confidence,
        Diagnosis,
        InformationGap,
        Symptoms,
    )

    diagnosis = Diagnosis(
        incident_id="frontier",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        resolution=Resolution.AMBIGUOUS,
        summary="frontier",
        symptoms=Symptoms(
            onset=datetime(2026, 1, 1, tzinfo=UTC),
            last_seen=None,
            services=(),
            namespaces=(),
            alert_names=(),
        ),
        information_gaps=(
            InformationGap(
                gap_id="g",
                dimension=GapDimension.CHANGE_TIMING,
                missing_fact="change",
                authorized_queries=(query,),
                resolvability=GapResolvability.RESOLVABLE,
            ),
        ),
    )
    assert (
        covered_frontier_dimensions(
            diagnosis, capability="history", target=target, evidence_acquired=False
        )
        == {}
    )
    assert (
        covered_frontier_dimensions(
            diagnosis, capability="runtime_traces", target=target, evidence_acquired=True
        )
        == {}
    )
    assert covered_frontier_dimensions(
        diagnosis, capability="history", target=target, evidence_acquired=True
    ) == {"a": (GapDimension.CHANGE_TIMING,)}
