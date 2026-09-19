from datetime import UTC, datetime, timedelta
from random import Random
from typing import Any

import pytest

from packages.rca.engine import build_case, diagnose_case
from packages.rca.investigation.environment import initial_view
from packages.rca.model import (
    EntityRef,
    Lifecycle,
    ObjectVersion,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.runtime_evidence import (
    RuntimeBindingQuality,
    RuntimeKubernetesBinding,
    RuntimeOutcomeBasis,
    RuntimeOutcomeState,
    RuntimeProtocol,
)
from packages.rca.runtime_graph import canonicalize_trace_spans
from packages.rca.runtime_propagation import (
    RuntimeBindingVerificationState,
    RuntimeBoundaryKind,
    RuntimeEntityState,
    RuntimeEntityStateBasis,
    RuntimePropagationMechanism,
    classify_runtime_boundary,
    derive_runtime_propagation,
    observed_entity_state_at,
    verify_runtime_binding,
)
from packages.rca.source import InMemorySource

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _span(
    *,
    trace: str,
    span: str,
    service: str,
    kind: str,
    at: int = 0,
    parent: str | None = None,
    status: TraceSpanStatus = TraceSpanStatus.UNSET,
    code: str | None = None,
    namespace: str | None = "shop",
    deployment: str | None = "workload",
    pod: str | None = "pod-1",
    evidence: str | None = None,
) -> TraceSpanObservation:
    attrs: dict[str, str] = {}
    if code is not None:
        attrs["http.response.status_code"] = code
    if namespace is not None:
        attrs["k8s.namespace.name"] = namespace
    if deployment is not None:
        attrs["k8s.deployment.name"] = deployment
    if pod is not None:
        attrs["k8s.pod.name"] = pod
    return TraceSpanObservation(
        trace_id=trace,
        span_id=span,
        parent_span_id=parent,
        service=service,
        span_kind=kind,
        start_at=T0 + timedelta(seconds=at),
        status=status,
        semantic_attributes=attrs,
        evidence_id=evidence or f"e:{trace}:{span}",
    )


def _version(
    entity: EntityRef,
    at: int,
    lifecycle: Lifecycle = Lifecycle.OBSERVED,
    evidence: str | None = None,
) -> ObjectVersion:
    return ObjectVersion(
        entity=entity,
        observed_at=T0 + timedelta(seconds=at),
        body={},
        evidence_id=evidence or f"object:{entity.name}:{at}",
        lifecycle=lifecycle,
    )


def _binding(
    namespace: str = "shop", deployment: str | None = "workload", pod: str | None = "pod-1"
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


def _history_for(*entities: EntityRef) -> dict[EntityRef, list[ObjectVersion]]:
    return {entity: [_version(entity, 0)] for entity in entities}


@pytest.mark.parametrize(
    ("history", "query", "state", "basis"),
    [
        ({}, 1, RuntimeEntityState.UNKNOWN, RuntimeEntityStateBasis.NO_HISTORY),
        ("observed", 3, RuntimeEntityState.PRESENT, RuntimeEntityStateBasis.LATEST_NON_DELETED),
        ("updated", 6, RuntimeEntityState.PRESENT, RuntimeEntityStateBasis.LATEST_NON_DELETED),
        ("deleted", 6, RuntimeEntityState.ABSENT, RuntimeEntityStateBasis.LATEST_DELETED),
        (
            "created",
            -1,
            RuntimeEntityState.ABSENT,
            RuntimeEntityStateBasis.BEFORE_OBSERVED_CREATION,
        ),
        (
            "before-observed",
            -1,
            RuntimeEntityState.UNKNOWN,
            RuntimeEntityStateBasis.BEFORE_FIRST_OBSERVATION,
        ),
    ],
)
def test_observed_entity_state_at(
    history: Any,
    query: int,
    state: RuntimeEntityState,
    basis: RuntimeEntityStateBasis,
) -> None:
    entity = EntityRef(namespace="shop", kind="Pod", name="p")
    versions = {
        "observed": [_version(entity, 0), _version(entity, 5, Lifecycle.UPDATED)],
        "updated": [_version(entity, 0), _version(entity, 5, Lifecycle.UPDATED)],
        "deleted": [_version(entity, 0), _version(entity, 5, Lifecycle.DELETED)],
        "created": [_version(entity, 0, Lifecycle.CREATED)],
        "before-observed": [_version(entity, 0, Lifecycle.OBSERVED)],
    }
    selected = {} if history == {} else {entity: versions[history]}
    result = observed_entity_state_at(selected, entity, T0 + timedelta(seconds=query))
    assert result.state is state
    assert result.basis is basis


def test_entity_history_is_not_mutated_and_exact_timestamp_is_inclusive() -> None:
    entity = EntityRef(namespace="shop", kind="Pod", name="p")
    history = {
        entity: [_version(entity, 5, evidence="late"), _version(entity, 0, evidence="early")]
    }
    original = tuple(history[entity])
    result = observed_entity_state_at(history, entity, T0 + timedelta(seconds=5))
    assert result.evidence_id == "late"
    assert tuple(history[entity]) == original


@pytest.mark.parametrize(
    ("deployment", "pod", "expected"),
    [
        (None, None, RuntimeBindingVerificationState.UNRESOLVED),
        ("workload", None, RuntimeBindingVerificationState.VERIFIED),
        (None, "pod-1", RuntimeBindingVerificationState.VERIFIED),
        ("workload", "missing", RuntimeBindingVerificationState.CONTRADICTED),
    ],
)
def test_verify_runtime_binding_states(
    deployment: str | None,
    pod: str | None,
    expected: RuntimeBindingVerificationState,
) -> None:
    binding = _binding(deployment=deployment, pod=pod)
    refs = []
    if deployment is not None:
        refs.append(EntityRef(namespace="shop", kind="Deployment", name=deployment))
    if pod is not None and pod != "missing":
        refs.append(EntityRef(namespace="shop", kind="Pod", name=pod))
    history = _history_for(*refs)
    if pod == "missing":
        missing = EntityRef(namespace="shop", kind="Pod", name=pod)
        history[missing] = [_version(missing, 0), _version(missing, 2, Lifecycle.DELETED)]
    query_at = 3 if pod == "missing" else 1
    verification = verify_runtime_binding(
        binding, at=T0 + timedelta(seconds=query_at), history=history
    )
    assert verification is not None
    assert verification.state is expected


@pytest.mark.parametrize(
    ("caller", "callee", "expected"),
    [
        (RuntimeOutcomeState.SUCCESS, RuntimeOutcomeState.SUCCESS, None),
        (RuntimeOutcomeState.SUCCESS, RuntimeOutcomeState.UNKNOWN, None),
        (RuntimeOutcomeState.UNKNOWN, RuntimeOutcomeState.SUCCESS, None),
        (RuntimeOutcomeState.UNKNOWN, RuntimeOutcomeState.UNKNOWN, None),
        (
            RuntimeOutcomeState.SUCCESS,
            RuntimeOutcomeState.NON_OK,
            RuntimeBoundaryKind.REMOTE_NON_SUCCESS_CONTAINED,
        ),
        (
            RuntimeOutcomeState.ERROR,
            RuntimeOutcomeState.SUCCESS,
            RuntimeBoundaryKind.CALLER_NON_SUCCESS_WITH_SUCCESSFUL_CALLEE,
        ),
        (
            RuntimeOutcomeState.ERROR,
            RuntimeOutcomeState.NON_OK,
            RuntimeBoundaryKind.REMOTE_NON_SUCCESS_PROPAGATED,
        ),
        (
            RuntimeOutcomeState.ERROR,
            RuntimeOutcomeState.UNKNOWN,
            RuntimeBoundaryKind.REMOTE_OUTCOME_UNRESOLVED,
        ),
        (
            RuntimeOutcomeState.UNKNOWN,
            RuntimeOutcomeState.ERROR,
            RuntimeBoundaryKind.CALLER_OUTCOME_UNRESOLVED,
        ),
    ],
)
def test_boundary_classifier_matrix(
    caller: RuntimeOutcomeState,
    callee: RuntimeOutcomeState,
    expected: RuntimeBoundaryKind | None,
) -> None:
    from packages.rca.runtime_evidence import RuntimeSpanOutcome

    caller_outcome = RuntimeSpanOutcome(
        protocol=RuntimeProtocol.HTTP, state=caller, basis=(RuntimeOutcomeBasis.NONE,)
    )
    callee_outcome = RuntimeSpanOutcome(
        protocol=RuntimeProtocol.HTTP, state=callee, basis=(RuntimeOutcomeBasis.NONE,)
    )
    assert classify_runtime_boundary(caller_outcome, callee_outcome) is expected


def test_propagation_reverses_direct_request_direction_and_verifies_each_endpoint_at_own_time() -> (
    None
):
    caller = EntityRef(namespace="shop", kind="Deployment", name="checkout")
    callee = EntityRef(namespace="shop", kind="Deployment", name="payment")
    caller_pod = EntityRef(namespace="shop", kind="Pod", name="checkout-pod")
    callee_pod = EntityRef(namespace="shop", kind="Pod", name="payment-pod")
    history = {
        caller: [_version(caller, 0)],
        caller_pod: [_version(caller_pod, 0), _version(caller_pod, 30, Lifecycle.DELETED)],
        callee: [_version(callee, 0)],
        callee_pod: [_version(callee_pod, 0)],
    }
    spans = (
        _span(
            trace="t",
            span="c",
            service="checkout",
            kind="CLIENT",
            at=0,
            code="500",
            deployment="checkout",
            pod="checkout-pod",
        ),
        _span(
            trace="t",
            span="s",
            service="payment",
            kind="SERVER",
            at=60,
            parent="c",
            code="500",
            deployment="payment",
            pod="payment-pod",
        ),
    )
    propagation = derive_runtime_propagation(
        canonicalize_trace_spans(spans), history=history, incident_onset=T0
    )
    assert propagation.stats.propagated_non_success_pairs == 1
    edge = propagation.edges[0]
    assert (edge.source_service, edge.affected_service) == ("payment", "checkout")
    assert edge.mechanism is RuntimePropagationMechanism.REMOTE_NON_SUCCESS_RETURN
    assert propagation.boundaries[0].caller_service == "checkout"
    assert propagation.boundaries[0].callee_service == "payment"
    assert edge.first_onset_delta_seconds == 60.0
    assert edge.affected_binding_state is RuntimeBindingVerificationState.VERIFIED


def test_contained_and_caller_only_failures_do_not_create_propagation_edges() -> None:
    base = _span(trace="t", span="c", service="a", kind="CLIENT", code="200")
    contained = _span(trace="t", span="s", service="b", kind="SERVER", parent="c", code="500")
    caller_only = (
        _span(trace="u", span="c", service="a", kind="CLIENT", code="500"),
        _span(trace="u", span="s", service="b", kind="SERVER", parent="c", code="200"),
    )
    first = derive_runtime_propagation(
        canonicalize_trace_spans((base, contained)), history={}, incident_onset=None
    )
    second = derive_runtime_propagation(
        canonicalize_trace_spans(caller_only), history={}, incident_onset=None
    )
    assert first.stats.contained_non_success_pairs == 1
    assert not first.edges
    assert second.stats.caller_non_success_successful_callee_pairs == 1
    assert not second.edges


def test_unknown_and_async_pairs_are_not_propagation() -> None:
    async_pair = (
        _span(trace="a", span="p", service="a", kind="PRODUCER", code="500"),
        _span(trace="a", span="c", service="b", kind="CONSUMER", parent="p", code="500"),
    )
    unknown_pair = (
        _span(trace="u", span="p", service="a", kind="CLIENT"),
        _span(trace="u", span="c", service="b", kind="SERVER", parent="p", code="500"),
    )
    result = derive_runtime_propagation(
        canonicalize_trace_spans(async_pair + unknown_pair), history={}, incident_onset=None
    )
    assert result.stats.async_pairs_uninterpreted == 1
    assert result.stats.caller_outcome_unresolved_pairs == 1
    assert not result.edges


def test_aggregation_provenance_and_determinism() -> None:
    spans = tuple(
        item
        for index in range(40)
        for item in (
            _span(trace=f"t{index}", span="c", service="a", kind="CLIENT", at=index, code="500"),
            _span(
                trace=f"t{index}",
                span="s",
                service="b",
                kind="SERVER",
                at=index + 1,
                parent="c",
                code="500",
            ),
        )
    )
    first = derive_runtime_propagation(
        canonicalize_trace_spans(spans), history={}, incident_onset=None
    )
    shuffled = list(spans)
    Random(7).shuffle(shuffled)
    second = derive_runtime_propagation(
        canonicalize_trace_spans(tuple(shuffled)), history={}, incident_onset=None
    )
    assert first.edges[0].model_dump(mode="json") == second.edges[0].model_dump(mode="json")
    assert first.edges[0].observed_pairs == 40
    assert len(first.edges[0].evidence_ids) <= 32
    assert len(first.edges[0].trace_ids) <= 32


def test_case_runtime_propagation_is_observational_and_initial_view_is_empty() -> None:
    spans = [
        _span(trace="t", span="c", service="a", kind="CLIENT", code="500"),
        _span(trace="t", span="s", service="b", kind="SERVER", parent="c", code="500"),
    ]
    source = InMemorySource(name="same", trace_items=spans)
    full = build_case(source)
    seed = build_case(initial_view(source))
    assert full.runtime_propagation.edges
    assert full.runtime_propagation.stats.propagated_non_success_pairs == 1
    assert seed.runtime_propagation.stats.canonical_spans == 0
    assert not seed.runtime_propagation.edges
    assert diagnose_case(full).model_dump(mode="json") == diagnose_case(
        build_case(InMemorySource(name="same"))
    ).model_dump(mode="json")
