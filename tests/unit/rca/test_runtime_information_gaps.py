from __future__ import annotations

from datetime import UTC, datetime

from packages.rca.causal_roles import HypothesisCausalRoles
from packages.rca.frontier import covered_frontier_dimensions
from packages.rca.information_gap import (
    InformationGapContext,
    _capability_allows_target,
    derive_information_gaps,
)
from packages.rca.mechanism_bridge import RuntimeMechanismBridges
from packages.rca.model import (
    AuthorizedQuery,
    EntityRef,
    GapDimension,
    Hypothesis,
    HypothesisEpistemicState,
    HypothesisResolutionAudit,
    HypothesisSignature,
    Resolution,
    ResolutionReasonCode,
    ResolutionTrace,
    StructuralAlternative,
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


def test_frontier_coverage_requires_novel_evidence_and_excludes_traces() -> None:
    target = _entity("Deployment", "payment")
    query = AuthorizedQuery(capability="history", target=target, alternative_ids=("a",))
    from packages.rca.model import (
        Confidence,
        Diagnosis,
        GapResolvability,
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
