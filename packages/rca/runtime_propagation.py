"""Deterministic temporal verification and direct runtime propagation evidence.

This module is deliberately observational.  A propagation edge proves only a
directly paired non-success return across one observed strict runtime
boundary.  It does not identify the initiating cause of either endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.rca.model import EntityRef, Lifecycle, ObjectVersion
from packages.rca.runtime_evidence import (
    RuntimeKubernetesBinding,
    RuntimeOutcomeState,
    RuntimeProtocol,
    RuntimeSpanOutcome,
    classify_runtime_span_outcome,
    trace_kubernetes_binding,
)
from packages.rca.runtime_graph import (
    CanonicalTraceIndex,
    RuntimeEdgeEvidence,
    classify_runtime_pair,
)


class RuntimeEntityState(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class RuntimeEntityStateBasis(StrEnum):
    LATEST_NON_DELETED = "LATEST_NON_DELETED"
    LATEST_DELETED = "LATEST_DELETED"
    BEFORE_OBSERVED_CREATION = "BEFORE_OBSERVED_CREATION"
    BEFORE_FIRST_OBSERVATION = "BEFORE_FIRST_OBSERVATION"
    NO_HISTORY = "NO_HISTORY"


class RuntimeBindingVerificationState(StrEnum):
    VERIFIED = "VERIFIED"
    UNRESOLVED = "UNRESOLVED"
    CONTRADICTED = "CONTRADICTED"


class RuntimeBoundaryKind(StrEnum):
    REMOTE_NON_SUCCESS_PROPAGATED = "REMOTE_NON_SUCCESS_PROPAGATED"
    REMOTE_NON_SUCCESS_CONTAINED = "REMOTE_NON_SUCCESS_CONTAINED"
    CALLER_NON_SUCCESS_WITH_SUCCESSFUL_CALLEE = "CALLER_NON_SUCCESS_WITH_SUCCESSFUL_CALLEE"
    REMOTE_OUTCOME_UNRESOLVED = "REMOTE_OUTCOME_UNRESOLVED"
    CALLER_OUTCOME_UNRESOLVED = "CALLER_OUTCOME_UNRESOLVED"


class RuntimePropagationMechanism(StrEnum):
    REMOTE_NON_SUCCESS_RETURN = "REMOTE_NON_SUCCESS_RETURN"


class RuntimeEntityStateObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity: EntityRef
    at: datetime
    state: RuntimeEntityState
    basis: RuntimeEntityStateBasis
    evidence_id: str | None = None


class RuntimeBindingVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    binding: RuntimeKubernetesBinding
    at: datetime
    deployment: RuntimeEntityStateObservation | None = None
    pod: RuntimeEntityStateObservation | None = None
    state: RuntimeBindingVerificationState


def observed_entity_state_at(
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    entity: EntityRef,
    at: datetime,
) -> RuntimeEntityStateObservation:
    """Read an entity's state from object history at an exact timestamp."""
    versions = history.get(entity, ())
    if not versions:
        return RuntimeEntityStateObservation(
            entity=entity,
            at=at,
            state=RuntimeEntityState.UNKNOWN,
            basis=RuntimeEntityStateBasis.NO_HISTORY,
        )

    ordered = sorted(versions, key=lambda item: (item.observed_at, item.evidence_id))
    eligible = [version for version in ordered if version.observed_at <= at]
    if eligible:
        version = eligible[-1]
        if version.lifecycle is Lifecycle.DELETED:
            state = RuntimeEntityState.ABSENT
            basis = RuntimeEntityStateBasis.LATEST_DELETED
        else:
            state = RuntimeEntityState.PRESENT
            basis = RuntimeEntityStateBasis.LATEST_NON_DELETED
        return RuntimeEntityStateObservation(
            entity=entity,
            at=at,
            state=state,
            basis=basis,
            evidence_id=version.evidence_id,
        )

    first = ordered[0]
    if first.lifecycle is Lifecycle.CREATED:
        state = RuntimeEntityState.ABSENT
        basis = RuntimeEntityStateBasis.BEFORE_OBSERVED_CREATION
    else:
        state = RuntimeEntityState.UNKNOWN
        basis = RuntimeEntityStateBasis.BEFORE_FIRST_OBSERVATION
    return RuntimeEntityStateObservation(
        entity=entity,
        at=at,
        state=state,
        basis=basis,
        evidence_id=first.evidence_id,
    )


def _entity_ref(binding: RuntimeKubernetesBinding, kind: str, name: str) -> EntityRef:
    return EntityRef(namespace=binding.namespace, kind=kind, name=name)


def verify_runtime_binding(
    binding: RuntimeKubernetesBinding | None,
    *,
    at: datetime,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> RuntimeBindingVerification | None:
    """Verify only directly observed deployment and pod identities."""
    if binding is None:
        return None
    deployment = (
        observed_entity_state_at(
            history,
            _entity_ref(binding, "Deployment", binding.deployment),
            at,
        )
        if binding.deployment is not None
        else None
    )
    pod = (
        observed_entity_state_at(
            history,
            _entity_ref(binding, "Pod", binding.pod),
            at,
        )
        if binding.pod is not None
        else None
    )
    observations = tuple(item for item in (deployment, pod) if item is not None)
    if not observations or any(item.state is RuntimeEntityState.ABSENT for item in observations):
        state = (
            RuntimeBindingVerificationState.UNRESOLVED
            if not observations
            else RuntimeBindingVerificationState.CONTRADICTED
        )
    elif any(item.state is RuntimeEntityState.UNKNOWN for item in observations):
        state = RuntimeBindingVerificationState.UNRESOLVED
    else:
        state = RuntimeBindingVerificationState.VERIFIED
    return RuntimeBindingVerification(
        binding=binding,
        at=at,
        deployment=deployment,
        pod=pod,
        state=state,
    )


def is_runtime_non_success(state: RuntimeOutcomeState) -> bool:
    return state in {RuntimeOutcomeState.NON_OK, RuntimeOutcomeState.ERROR}


def classify_runtime_boundary(
    caller: RuntimeSpanOutcome,
    callee: RuntimeSpanOutcome,
) -> RuntimeBoundaryKind | None:
    """Classify one synchronous endpoint outcome pair without causal meaning."""
    caller_abnormal = is_runtime_non_success(caller.state)
    callee_abnormal = is_runtime_non_success(callee.state)
    if caller_abnormal and callee_abnormal:
        return RuntimeBoundaryKind.REMOTE_NON_SUCCESS_PROPAGATED
    if caller.state is RuntimeOutcomeState.SUCCESS and callee_abnormal:
        return RuntimeBoundaryKind.REMOTE_NON_SUCCESS_CONTAINED
    if caller_abnormal and callee.state is RuntimeOutcomeState.SUCCESS:
        return RuntimeBoundaryKind.CALLER_NON_SUCCESS_WITH_SUCCESSFUL_CALLEE
    if caller_abnormal and callee.state is RuntimeOutcomeState.UNKNOWN:
        return RuntimeBoundaryKind.REMOTE_OUTCOME_UNRESOLVED
    if caller.state is RuntimeOutcomeState.UNKNOWN and callee_abnormal:
        return RuntimeBoundaryKind.CALLER_OUTCOME_UNRESOLVED
    return None


@dataclass
class _Accumulator:
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    count: int = 0
    evidence: list[tuple[tuple[object, ...], str]] = field(default_factory=list)
    traces: list[tuple[tuple[object, ...], str]] = field(default_factory=list)

    def add(
        self,
        *,
        at: datetime,
        order: tuple[object, ...],
        evidence_ids: Sequence[str] = (),
        trace_ids: Sequence[str] = (),
    ) -> None:
        self.first_seen = at if self.first_seen is None else min(self.first_seen, at)
        self.last_seen = at if self.last_seen is None else max(self.last_seen, at)
        self.count += 1
        for position, value in enumerate(evidence_ids):
            _bounded_insert(self.evidence, (*order, position), value)
        for position, value in enumerate(trace_ids):
            _bounded_insert(self.traces, (*order, position), value)


def _bounded_insert(
    candidates: list[tuple[tuple[object, ...], str]],
    order: tuple[object, ...],
    value: str,
) -> None:
    if any(existing == value for _order, existing in candidates):
        return
    candidates.append((order, value))
    candidates.sort(key=lambda item: (item[0], item[1]))
    del candidates[32:]


def _values(candidates: list[tuple[tuple[object, ...], str]]) -> tuple[str, ...]:
    return tuple(value for _order, value in candidates)


def _times(accumulator: _Accumulator) -> tuple[datetime, datetime]:
    assert accumulator.first_seen is not None
    assert accumulator.last_seen is not None
    return accumulator.first_seen, accumulator.last_seen


def _binding_key(binding: RuntimeKubernetesBinding | None) -> tuple[str, str, str]:
    if binding is None:
        return ("", "", "")
    return (binding.namespace, binding.deployment or "", binding.pod or "")


def _verification_key(
    service: str,
    verification: RuntimeBindingVerification,
) -> tuple[object, ...]:
    deployment = verification.deployment
    pod = verification.pod
    return (
        service,
        *_binding_key(verification.binding),
        verification.state,
        deployment.state if deployment is not None else "",
        deployment.basis if deployment is not None else "",
        pod.state if pod is not None else "",
        pod.basis if pod is not None else "",
    )


def _verification_evidence(verification: RuntimeBindingVerification) -> tuple[str, ...]:
    return tuple(
        item.evidence_id
        for item in (verification.deployment, verification.pod)
        if item is not None and item.evidence_id is not None
    )


class RuntimeBindingVerificationSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    service: str = Field(min_length=1)
    binding: RuntimeKubernetesBinding
    verification_state: RuntimeBindingVerificationState
    deployment_state: RuntimeEntityState | None = None
    deployment_basis: RuntimeEntityStateBasis | None = None
    pod_state: RuntimeEntityState | None = None
    pod_basis: RuntimeEntityStateBasis | None = None
    first_seen: datetime
    last_seen: datetime
    observed_endpoints: int = Field(gt=0)
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_summary(self) -> RuntimeBindingVerificationSummary:
        if self.first_seen > self.last_seen:
            raise ValueError("binding verification has inverted observation range")
        if len(self.evidence_ids) > 32:
            raise ValueError("binding verification provenance is unbounded")
        return self


def _boundary_key(
    caller_service: str,
    callee_service: str,
    kind: RuntimeBoundaryKind,
    caller_binding: RuntimeKubernetesBinding | None,
    callee_binding: RuntimeKubernetesBinding | None,
    caller: RuntimeSpanOutcome,
    callee: RuntimeSpanOutcome,
    caller_binding_state: RuntimeBindingVerificationState | None,
    callee_binding_state: RuntimeBindingVerificationState | None,
) -> tuple[object, ...]:
    return (
        caller_service,
        callee_service,
        kind,
        *_binding_key(caller_binding),
        *_binding_key(callee_binding),
        caller_binding_state or "",
        callee_binding_state or "",
        caller.state,
        callee.state,
        caller.protocol,
        callee.protocol,
        caller.protocol_code or "",
        callee.protocol_code or "",
    )


class RuntimeBoundarySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    caller_service: str = Field(min_length=1)
    callee_service: str = Field(min_length=1)
    kind: RuntimeBoundaryKind
    caller_binding: RuntimeKubernetesBinding | None
    callee_binding: RuntimeKubernetesBinding | None
    caller_binding_state: RuntimeBindingVerificationState | None
    callee_binding_state: RuntimeBindingVerificationState | None
    caller_state: RuntimeOutcomeState
    callee_state: RuntimeOutcomeState
    caller_protocol: RuntimeProtocol
    callee_protocol: RuntimeProtocol
    caller_code: str | None = None
    callee_code: str | None = None
    first_seen: datetime
    last_seen: datetime
    first_onset_delta_seconds: float | None = None
    last_onset_delta_seconds: float | None = None
    observed_pairs: int = Field(gt=0)
    evidence_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_summary(self) -> RuntimeBoundarySummary:
        if self.caller_service == self.callee_service:
            raise ValueError("runtime boundary must be cross-service")
        if self.first_seen > self.last_seen:
            raise ValueError("runtime boundary has inverted observation range")
        if len(self.evidence_ids) > 32 or len(self.trace_ids) > 32:
            raise ValueError("runtime boundary provenance is unbounded")
        return self


def _propagation_key(
    source_service: str,
    affected_service: str,
    source_binding: RuntimeKubernetesBinding | None,
    affected_binding: RuntimeKubernetesBinding | None,
    source_binding_state: RuntimeBindingVerificationState | None,
    affected_binding_state: RuntimeBindingVerificationState | None,
    source: RuntimeSpanOutcome,
    affected: RuntimeSpanOutcome,
) -> tuple[object, ...]:
    return (
        source_service,
        affected_service,
        RuntimePropagationMechanism.REMOTE_NON_SUCCESS_RETURN,
        *_binding_key(source_binding),
        *_binding_key(affected_binding),
        source_binding_state or "",
        affected_binding_state or "",
        source.state,
        affected.state,
        source.protocol,
        affected.protocol,
        source.protocol_code or "",
        affected.protocol_code or "",
    )


class RuntimePropagationEdge(BaseModel):
    """One direct observed non-success return across a strict runtime call.

    A propagation edge proves only a directly paired non-success return across
    one observed strict runtime boundary. It does not identify the initiating
    cause of either endpoint.
    """

    model_config = ConfigDict(frozen=True)

    source_service: str = Field(min_length=1)
    affected_service: str = Field(min_length=1)
    mechanism: RuntimePropagationMechanism
    source_binding: RuntimeKubernetesBinding | None
    affected_binding: RuntimeKubernetesBinding | None
    source_binding_state: RuntimeBindingVerificationState | None
    affected_binding_state: RuntimeBindingVerificationState | None
    source_state: RuntimeOutcomeState
    affected_state: RuntimeOutcomeState
    source_protocol: RuntimeProtocol
    affected_protocol: RuntimeProtocol
    source_code: str | None = None
    affected_code: str | None = None
    first_seen: datetime
    last_seen: datetime
    first_onset_delta_seconds: float | None = None
    last_onset_delta_seconds: float | None = None
    observed_pairs: int = Field(gt=0)
    evidence_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_edge(self) -> RuntimePropagationEdge:
        if self.source_service == self.affected_service:
            raise ValueError("propagation edge must be cross-service")
        if self.first_seen > self.last_seen:
            raise ValueError("propagation edge has inverted observation range")
        if len(self.evidence_ids) > 32 or len(self.trace_ids) > 32:
            raise ValueError("propagation provenance is unbounded")
        if not is_runtime_non_success(self.source_state) or not is_runtime_non_success(
            self.affected_state
        ):
            raise ValueError("propagation endpoints must both be non-success")
        return self


class RuntimePropagationStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_spans: int = Field(ge=0)
    client_server_pairs: int = Field(ge=0)
    async_pairs_uninterpreted: int = Field(ge=0)
    healthy_pairs: int = Field(ge=0)
    no_abnormal_boundary_pairs: int = Field(ge=0)
    abnormal_boundary_pairs: int = Field(ge=0)
    propagated_non_success_pairs: int = Field(ge=0)
    contained_non_success_pairs: int = Field(ge=0)
    caller_non_success_successful_callee_pairs: int = Field(ge=0)
    remote_outcome_unresolved_pairs: int = Field(ge=0)
    caller_outcome_unresolved_pairs: int = Field(ge=0)
    binding_endpoint_observations: int = Field(ge=0)
    verified_binding_endpoints: int = Field(ge=0)
    unresolved_binding_endpoints: int = Field(ge=0)
    contradicted_binding_endpoints: int = Field(ge=0)
    unbound_endpoints: int = Field(ge=0)
    binding_verification_summaries: int = Field(ge=0)
    boundary_summaries: int = Field(ge=0)
    propagation_edges: int = Field(ge=0)


class RuntimePropagation:
    """Bounded immutable lookup view over direct temporal propagation evidence."""

    __slots__ = ("binding_verifications", "boundaries", "edges", "stats")

    def __init__(
        self,
        *,
        binding_verifications: Sequence[RuntimeBindingVerificationSummary],
        boundaries: Sequence[RuntimeBoundarySummary],
        edges: Sequence[RuntimePropagationEdge],
        stats: RuntimePropagationStats,
    ) -> None:
        self.binding_verifications = tuple(
            sorted(
                binding_verifications,
                key=lambda item: (
                    item.service,
                    *_binding_key(item.binding),
                    item.verification_state,
                    item.deployment_state or "",
                    item.deployment_basis or "",
                    item.pod_state or "",
                    item.pod_basis or "",
                ),
            )
        )
        self.boundaries = tuple(
            sorted(
                boundaries,
                key=lambda item: (
                    item.caller_service,
                    item.callee_service,
                    item.kind,
                    *_binding_key(item.caller_binding),
                    *_binding_key(item.callee_binding),
                    item.caller_state,
                    item.callee_state,
                    item.caller_protocol,
                    item.callee_protocol,
                    item.caller_code or "",
                    item.callee_code or "",
                ),
            )
        )
        self.edges = tuple(
            sorted(
                edges,
                key=lambda item: (
                    item.source_service,
                    item.affected_service,
                    item.mechanism,
                    *_binding_key(item.source_binding),
                    *_binding_key(item.affected_binding),
                    item.source_state,
                    item.affected_state,
                    item.source_protocol,
                    item.affected_protocol,
                    item.source_code or "",
                    item.affected_code or "",
                ),
            )
        )
        self.stats = stats

    @classmethod
    def empty(cls) -> RuntimePropagation:
        return cls(
            binding_verifications=(),
            boundaries=(),
            edges=(),
            stats=RuntimePropagationStats(
                **{field: 0 for field in RuntimePropagationStats.model_fields}
            ),
        )

    def boundaries_from_caller(self, service: str) -> tuple[RuntimeBoundarySummary, ...]:
        return tuple(item for item in self.boundaries if item.caller_service == service)

    def boundaries_to_callee(self, service: str) -> tuple[RuntimeBoundarySummary, ...]:
        return tuple(item for item in self.boundaries if item.callee_service == service)

    def propagations_from(self, service: str) -> tuple[RuntimePropagationEdge, ...]:
        return tuple(item for item in self.edges if item.source_service == service)

    def propagations_to(self, service: str) -> tuple[RuntimePropagationEdge, ...]:
        return tuple(item for item in self.edges if item.affected_service == service)

    def binding_verifications_for_service(
        self, service: str
    ) -> tuple[RuntimeBindingVerificationSummary, ...]:
        return tuple(item for item in self.binding_verifications if item.service == service)


def _delta(at: datetime, onset: datetime | None) -> float | None:
    return None if onset is None else (at - onset).total_seconds()


def _endpoint_state(
    binding: RuntimeKubernetesBinding | None,
    *,
    at: datetime,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> RuntimeBindingVerification | None:
    return verify_runtime_binding(binding, at=at, history=history)


def _add_verification(
    service: str,
    verification: RuntimeBindingVerification,
    *,
    at: datetime,
    span_evidence: str,
    accumulators: dict[tuple[object, ...], _Accumulator],
    models: dict[tuple[object, ...], RuntimeBindingVerificationSummary],
) -> None:
    key = _verification_key(service, verification)
    accumulator = accumulators.setdefault(key, _Accumulator())
    accumulator.add(
        at=at,
        order=(at, service, span_evidence),
        evidence_ids=(span_evidence, *_verification_evidence(verification)),
    )
    deployment = verification.deployment
    pod = verification.pod
    models.setdefault(
        key,
        RuntimeBindingVerificationSummary(
            service=service,
            binding=verification.binding,
            verification_state=verification.state,
            deployment_state=deployment.state if deployment is not None else None,
            deployment_basis=deployment.basis if deployment is not None else None,
            pod_state=pod.state if pod is not None else None,
            pod_basis=pod.basis if pod is not None else None,
            first_seen=at,
            last_seen=at,
            observed_endpoints=1,
            evidence_ids=(span_evidence, *_verification_evidence(verification)),
        ),
    )


def derive_runtime_propagation(
    index: CanonicalTraceIndex,
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    incident_onset: datetime | None,
) -> RuntimePropagation:
    """Derive direct synchronous propagation observations from one canonical index."""
    verification_accumulators: dict[tuple[object, ...], _Accumulator] = {}
    verification_models: dict[tuple[object, ...], RuntimeBindingVerificationSummary] = {}
    boundary_accumulators: dict[tuple[object, ...], _Accumulator] = {}
    boundary_models: dict[tuple[object, ...], RuntimeBoundarySummary] = {}
    edge_accumulators: dict[tuple[object, ...], _Accumulator] = {}
    edge_models: dict[tuple[object, ...], RuntimePropagationEdge] = {}

    stats: dict[str, int] = {field: 0 for field in RuntimePropagationStats.model_fields}
    stats["canonical_spans"] = len(index.spans)

    for child in index.spans.values():
        if child.parent_span_id is None:
            continue
        parent_key = (child.trace_id, child.parent_span_id)
        if parent_key in index.conflicting_keys:
            continue
        parent = index.spans.get(parent_key)
        if parent is None:
            continue
        evidence = classify_runtime_pair(parent, child)
        if evidence is RuntimeEdgeEvidence.PAIRED_PRODUCER_CONSUMER:
            stats["async_pairs_uninterpreted"] += 1
            continue
        if evidence is not RuntimeEdgeEvidence.PAIRED_CLIENT_SERVER:
            continue
        stats["client_server_pairs"] += 1

        caller = classify_runtime_span_outcome(parent)
        callee = classify_runtime_span_outcome(child)
        boundary_kind = classify_runtime_boundary(caller, callee)
        if boundary_kind is None:
            if (
                caller.state is RuntimeOutcomeState.SUCCESS
                and callee.state is RuntimeOutcomeState.SUCCESS
            ):
                stats["healthy_pairs"] += 1
            else:
                stats["no_abnormal_boundary_pairs"] += 1
            continue

        stats["abnormal_boundary_pairs"] += 1
        counter_name = {
            RuntimeBoundaryKind.REMOTE_NON_SUCCESS_PROPAGATED: "propagated_non_success_pairs",
            RuntimeBoundaryKind.REMOTE_NON_SUCCESS_CONTAINED: "contained_non_success_pairs",
            RuntimeBoundaryKind.CALLER_NON_SUCCESS_WITH_SUCCESSFUL_CALLEE: (
                "caller_non_success_successful_callee_pairs"
            ),
            RuntimeBoundaryKind.REMOTE_OUTCOME_UNRESOLVED: "remote_outcome_unresolved_pairs",
            RuntimeBoundaryKind.CALLER_OUTCOME_UNRESOLVED: "caller_outcome_unresolved_pairs",
        }[boundary_kind]
        stats[counter_name] += 1

        caller_binding = trace_kubernetes_binding(parent)
        callee_binding = trace_kubernetes_binding(child)
        caller_verification = _endpoint_state(caller_binding, at=parent.start_at, history=history)
        callee_verification = _endpoint_state(callee_binding, at=child.start_at, history=history)
        for service, binding, verification, at, span_evidence in (
            (
                parent.service.strip(),
                caller_binding,
                caller_verification,
                parent.start_at,
                parent.evidence_id,
            ),
            (
                child.service.strip(),
                callee_binding,
                callee_verification,
                child.start_at,
                child.evidence_id,
            ),
        ):
            if binding is None or verification is None:
                stats["unbound_endpoints"] += 1
                continue
            stats["binding_endpoint_observations"] += 1
            if verification.state is RuntimeBindingVerificationState.VERIFIED:
                stats["verified_binding_endpoints"] += 1
            elif verification.state is RuntimeBindingVerificationState.UNRESOLVED:
                stats["unresolved_binding_endpoints"] += 1
            else:
                stats["contradicted_binding_endpoints"] += 1
            _add_verification(
                service,
                verification,
                at=at,
                span_evidence=span_evidence,
                accumulators=verification_accumulators,
                models=verification_models,
            )

        caller_state = caller_verification.state if caller_verification is not None else None
        callee_state = callee_verification.state if callee_verification is not None else None
        at = child.start_at
        evidence_ids = (
            parent.evidence_id,
            child.evidence_id,
            *(_verification_evidence(caller_verification) if caller_verification else ()),
            *(_verification_evidence(callee_verification) if callee_verification else ()),
        )
        caller_service = parent.service.strip()
        callee_service = child.service.strip()
        key = _boundary_key(
            caller_service,
            callee_service,
            boundary_kind,
            caller_binding,
            callee_binding,
            caller,
            callee,
            caller_state,
            callee_state,
        )
        boundary_accumulators.setdefault(key, _Accumulator()).add(
            at=at,
            order=(at, caller_service, callee_service, parent.evidence_id, child.evidence_id),
            evidence_ids=evidence_ids,
            trace_ids=(child.trace_id,),
        )
        boundary_models.setdefault(
            key,
            RuntimeBoundarySummary(
                caller_service=caller_service,
                callee_service=callee_service,
                kind=boundary_kind,
                caller_binding=caller_binding,
                callee_binding=callee_binding,
                caller_binding_state=caller_state,
                callee_binding_state=callee_state,
                caller_state=caller.state,
                callee_state=callee.state,
                caller_protocol=caller.protocol,
                callee_protocol=callee.protocol,
                caller_code=caller.protocol_code,
                callee_code=callee.protocol_code,
                first_seen=at,
                last_seen=at,
                first_onset_delta_seconds=_delta(at, incident_onset),
                last_onset_delta_seconds=_delta(at, incident_onset),
                observed_pairs=1,
                evidence_ids=tuple(evidence_ids[:32]),
                trace_ids=(child.trace_id,),
            ),
        )

        if boundary_kind is RuntimeBoundaryKind.REMOTE_NON_SUCCESS_PROPAGATED:
            edge_key = _propagation_key(
                callee_service,
                caller_service,
                callee_binding,
                caller_binding,
                callee_state,
                caller_state,
                callee,
                caller,
            )
            edge_accumulators.setdefault(edge_key, _Accumulator()).add(
                at=at,
                order=(at, callee_service, caller_service, parent.evidence_id, child.evidence_id),
                evidence_ids=evidence_ids,
                trace_ids=(child.trace_id,),
            )
            edge_models.setdefault(
                edge_key,
                RuntimePropagationEdge(
                    source_service=callee_service,
                    affected_service=caller_service,
                    mechanism=RuntimePropagationMechanism.REMOTE_NON_SUCCESS_RETURN,
                    source_binding=callee_binding,
                    affected_binding=caller_binding,
                    source_binding_state=callee_state,
                    affected_binding_state=caller_state,
                    source_state=callee.state,
                    affected_state=caller.state,
                    source_protocol=callee.protocol,
                    affected_protocol=caller.protocol,
                    source_code=callee.protocol_code,
                    affected_code=caller.protocol_code,
                    first_seen=at,
                    last_seen=at,
                    first_onset_delta_seconds=_delta(at, incident_onset),
                    last_onset_delta_seconds=_delta(at, incident_onset),
                    observed_pairs=1,
                    evidence_ids=tuple(evidence_ids[:32]),
                    trace_ids=(child.trace_id,),
                ),
            )

    binding_summaries = [
        model.model_copy(
            update={
                "first_seen": _times(accumulator)[0],
                "last_seen": _times(accumulator)[1],
                "observed_endpoints": accumulator.count,
                "evidence_ids": _values(accumulator.evidence),
            }
        )
        for key, accumulator in verification_accumulators.items()
        for model in (verification_models[key],)
    ]
    boundaries: list[RuntimeBoundarySummary] = []
    for key, accumulator in boundary_accumulators.items():
        model = boundary_models[key]
        first, last = _times(accumulator)
        boundaries.append(
            model.model_copy(
                update={
                    "first_seen": first,
                    "last_seen": last,
                    "first_onset_delta_seconds": _delta(first, incident_onset),
                    "last_onset_delta_seconds": _delta(last, incident_onset),
                    "observed_pairs": accumulator.count,
                    "evidence_ids": _values(accumulator.evidence),
                    "trace_ids": _values(accumulator.traces),
                }
            )
        )
    edges: list[RuntimePropagationEdge] = []
    for key, accumulator in edge_accumulators.items():
        edge_model: RuntimePropagationEdge = edge_models[key]
        first, last = _times(accumulator)
        edges.append(
            edge_model.model_copy(
                update={
                    "first_seen": first,
                    "last_seen": last,
                    "first_onset_delta_seconds": _delta(first, incident_onset),
                    "last_onset_delta_seconds": _delta(last, incident_onset),
                    "observed_pairs": accumulator.count,
                    "evidence_ids": _values(accumulator.evidence),
                    "trace_ids": _values(accumulator.traces),
                }
            )
        )
    stats["binding_verification_summaries"] = len(binding_summaries)
    stats["boundary_summaries"] = len(boundaries)
    stats["propagation_edges"] = len(edges)
    return RuntimePropagation(
        binding_verifications=binding_summaries,
        boundaries=boundaries,
        edges=edges,
        stats=RuntimePropagationStats(**stats),
    )


__all__ = [
    "RuntimeBindingVerification",
    "RuntimeBindingVerificationState",
    "RuntimeBindingVerificationSummary",
    "RuntimeBoundaryKind",
    "RuntimeBoundarySummary",
    "RuntimeEntityState",
    "RuntimeEntityStateBasis",
    "RuntimeEntityStateObservation",
    "RuntimePropagation",
    "RuntimePropagationEdge",
    "RuntimePropagationMechanism",
    "RuntimePropagationStats",
    "classify_runtime_boundary",
    "derive_runtime_propagation",
    "is_runtime_non_success",
    "observed_entity_state_at",
    "verify_runtime_binding",
]
