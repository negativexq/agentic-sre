"""M20.5 P2b: Pod-level runtime authority needs the exact instance, end to end.

span k8s.pod.uid == history ObjectVersion.uid (at the span time) == the hypothesis's
single actor-local Finding UID. Deployment authority is verified separately and is
never lost because a Pod UID is missing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.causal_roles import HypothesisCausalRole, derive_hypothesis_causal_roles
from packages.rca.model import (
    EntityInstanceRef,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Lifecycle,
    ObjectVersion,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.runtime_graph import canonicalize_trace_spans
from packages.rca.runtime_propagation import (
    RuntimeBindingVerificationState,
    derive_runtime_propagation,
)

T0 = datetime(2025, 1, 1, tzinfo=UTC)
NS = "shop"
POD = EntityRef(namespace=NS, kind="Pod", name="cart-0")
DEPLOYMENT = EntityRef(namespace=NS, kind="Deployment", name="cart")
VERIFIED = RuntimeBindingVerificationState.VERIFIED
UNRESOLVED = RuntimeBindingVerificationState.UNRESOLVED
CONTRADICTED = RuntimeBindingVerificationState.CONTRADICTED

_counter = iter(range(10_000))


def _version(
    entity: EntityRef, minute: int, uid: str | None, lifecycle: Lifecycle = Lifecycle.UPDATED
) -> ObjectVersion:
    metadata = {"name": entity.name, "namespace": NS, **({"uid": uid} if uid else {})}
    return ObjectVersion(
        entity=entity,
        uid=uid,
        observed_at=T0 + timedelta(minutes=minute),
        body={"kind": entity.kind, "metadata": metadata},
        lifecycle=lifecycle,
        evidence_id=f"journal:{next(_counter)}",
    )


def _history(*pod_versions: ObjectVersion) -> dict[EntityRef, list[ObjectVersion]]:
    history: dict[EntityRef, list[ObjectVersion]] = {DEPLOYMENT: [_version(DEPLOYMENT, 0, "d-1")]}
    for version in pod_versions:
        history.setdefault(version.entity, []).append(version)
    return history


def _pair(
    span_uid: str | None, *, minute: int = 5, trace: str = "t1", with_pod: bool = True
) -> list[TraceSpanObservation]:
    """cart (CLIENT, error) → payment (SERVER, error): cart is the affected endpoint."""
    at = T0 + timedelta(minutes=minute)
    caller = {"k8s.namespace.name": NS, "k8s.deployment.name": "cart"}
    if with_pod:
        caller["k8s.pod.name"] = "cart-0"
    if span_uid is not None:
        caller["k8s.pod.uid"] = span_uid
    return [
        TraceSpanObservation(
            trace_id=trace,
            span_id=f"{trace}-c",
            service="cart",
            span_kind="CLIENT",
            start_at=at,
            end_at=at + timedelta(seconds=1),
            status=TraceSpanStatus.ERROR,
            semantic_attributes=caller,
            evidence_id=f"tempo:{trace}:c",
        ),
        TraceSpanObservation(
            trace_id=trace,
            span_id=f"{trace}-s",
            parent_span_id=f"{trace}-c",
            service="payment",
            span_kind="SERVER",
            start_at=at,
            end_at=at + timedelta(milliseconds=500),
            status=TraceSpanStatus.ERROR,
            semantic_attributes={"k8s.namespace.name": NS, "k8s.deployment.name": "payment"},
            evidence_id=f"tempo:{trace}:s",
        ),
    ]


def _hypothesis(actor: EntityRef, *uids: str | None) -> Hypothesis:
    findings = tuple(
        Finding(
            kind=FindingKind.FAILURE_EVENT,
            entity=actor,
            entity_instance=(EntityInstanceRef(entity=actor, uid=uid) if uid else None),
            at=T0 + timedelta(minutes=5),
            summary="Unhealthy",
            evidence_ids=(f"event:{index}",),
            temporal_role=EvidenceTemporalRole.AMBIGUOUS,
        )
        for index, uid in enumerate(uids or (None,))
    )
    return Hypothesis(hypothesis_id=f"h-{actor.kind}", causal_actor=actor, findings=findings)


def _roles(
    spans: list[TraceSpanObservation],
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    *hypotheses: Hypothesis,
) -> dict[str, Any]:
    propagation = derive_runtime_propagation(
        canonicalize_trace_spans(spans), history=history, incident_onset=None
    )
    roles = derive_hypothesis_causal_roles(hypotheses, propagation)
    return {item.causal_actor.kind: item for item in roles.assessments} | {"_p": propagation}


# Roles through which root eligibility may exclude the actor as a propagated effect.
_EFFECT_ROLES = {HypothesisCausalRole.PROPAGATED_EFFECT, HypothesisCausalRole.MANIFESTATION}


def _pod_authority(result: dict[str, Any]) -> bool:
    item = result["Pod"]
    return item.verified_incoming_edges > 0 and item.role in _EFFECT_ROLES


def test_exact_uid_chain_gives_pod_authority() -> None:
    result = _roles(_pair("uid-A"), _history(_version(POD, 0, "uid-A")), _hypothesis(POD, "uid-A"))
    assert _pod_authority(result)


def test_a_different_hypothesis_uid_gives_no_pod_authority() -> None:
    result = _roles(_pair("uid-A"), _history(_version(POD, 0, "uid-A")), _hypothesis(POD, "uid-X"))
    assert not _pod_authority(result)
    assert result["Pod"].unresolved_incoming_edges == 1


def test_a_missing_span_uid_leaves_the_pod_unresolved() -> None:
    result = _roles(_pair(None), _history(_version(POD, 0, "uid-A")), _hypothesis(POD, "uid-A"))
    (edge,) = result["_p"].edges
    assert edge.affected_pod_verification is UNRESOLVED
    assert not _pod_authority(result)


def test_history_without_the_exact_instance_leaves_the_pod_unresolved() -> None:
    for history in (_history(_version(POD, 0, None)), _history(_version(POD, 0, "uid-OTHER"))):
        result = _roles(_pair("uid-A"), history, _hypothesis(POD, "uid-A"))
        (edge,) = result["_p"].edges
        assert edge.affected_pod_verification is UNRESOLVED
        assert not _pod_authority(result)


def test_history_that_refutes_the_span_uid_contradicts_the_pod() -> None:
    history = _history(
        _version(POD, 0, "uid-A", Lifecycle.CREATED),
        _version(POD, 3, "uid-A", Lifecycle.DELETED),
    )
    result = _roles(_pair("uid-A"), history, _hypothesis(POD, "uid-A"))
    (edge,) = result["_p"].edges
    assert edge.affected_pod_verification is CONTRADICTED
    assert result["Pod"].contradicted_incoming_edges == 1
    assert not _pod_authority(result)


def test_deployment_authority_survives_a_missing_pod_uid() -> None:
    result = _roles(
        _pair(None),
        _history(_version(POD, 0, "uid-A")),
        _hypothesis(DEPLOYMENT),
        _hypothesis(POD, "uid-A"),
    )
    (edge,) = result["_p"].edges
    assert edge.affected_deployment_verification is VERIFIED
    assert result["Deployment"].verified_incoming_edges == 1
    assert result["Deployment"].role in _EFFECT_ROLES
    assert not _pod_authority(result)


def test_a_reused_pod_name_binds_only_the_matching_instance() -> None:
    history = _history(
        _version(POD, 0, "uid-A", Lifecycle.CREATED),
        _version(POD, 3, "uid-A", Lifecycle.DELETED),
        _version(POD, 4, "uid-B", Lifecycle.CREATED),
    )
    trace_b = _pair("uid-B", minute=5)
    assert not _pod_authority(_roles(trace_b, history, _hypothesis(POD, "uid-A")))
    assert _pod_authority(_roles(trace_b, history, _hypothesis(POD, "uid-B")))


def test_instances_of_one_pod_name_are_separate_edges() -> None:
    history = _history(
        _version(POD, 0, "uid-A", Lifecycle.CREATED),
        _version(POD, 3, "uid-A", Lifecycle.DELETED),
        _version(POD, 4, "uid-B", Lifecycle.CREATED),
    )
    spans = _pair("uid-A", minute=1, trace="ta") + _pair("uid-B", minute=5, trace="tb")
    result = _roles(spans, history, _hypothesis(POD, "uid-B"))
    assert sorted(edge.affected_binding.pod_uid for edge in result["_p"].edges) == [
        "uid-A",
        "uid-B",
    ]
    assert result["Pod"].verified_incoming_edges == 1


def test_a_hypothesis_without_an_exact_uid_gets_no_pod_authority() -> None:
    result = _roles(_pair("uid-A"), _history(_version(POD, 0, "uid-A")), _hypothesis(POD, None))
    assert not _pod_authority(result)


def test_a_hypothesis_with_several_actor_uids_fails_closed() -> None:
    result = _roles(
        _pair("uid-A"), _history(_version(POD, 0, "uid-A")), _hypothesis(POD, "uid-A", "uid-B")
    )
    assert not _pod_authority(result)


def test_a_span_reported_with_conflicting_pod_uids_carries_no_authority() -> None:
    spans = _pair("uid-A")
    conflicting = spans[0].model_copy(
        update={
            "semantic_attributes": {**spans[0].semantic_attributes, "k8s.pod.uid": "uid-B"},
            "evidence_id": "tempo:t1:c-dup",
        }
    )
    index = canonicalize_trace_spans([*spans, conflicting])
    assert index.conflicting_span_keys == 1
    result = _roles(
        [*spans, conflicting], _history(_version(POD, 0, "uid-A")), _hypothesis(POD, "uid-A")
    )
    assert result["_p"].edges == ()
    assert not _pod_authority(result)
