"""Deterministic protocol and trace-resource evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.rca.model import RuntimeTraceCallFact, TraceSpanObservation, TraceSpanStatus
from packages.rca.runtime_graph import (
    CanonicalTraceIndex,
    RuntimeEdgeEvidence,
    RuntimeSpanKind,
    canonicalize_trace_spans,
    classify_runtime_pair,
    normalize_runtime_span_kind,
)


class RuntimeProtocol(StrEnum):
    GRPC = "GRPC"
    HTTP = "HTTP"
    RPC_OTHER = "RPC_OTHER"
    OTHER = "OTHER"


class RuntimeOutcomeState(StrEnum):
    SUCCESS = "SUCCESS"
    NON_OK = "NON_OK"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class RuntimeOutcomeBasis(StrEnum):
    ERROR_TYPE = "ERROR_TYPE"
    GRPC_STATUS = "GRPC_STATUS"
    HTTP_STATUS = "HTTP_STATUS"
    SPAN_STATUS = "SPAN_STATUS"
    CONFLICTING_GRPC_STATUS = "CONFLICTING_GRPC_STATUS"
    CONFLICTING_HTTP_STATUS = "CONFLICTING_HTTP_STATUS"
    NONE = "NONE"


class RuntimeBindingQuality(StrEnum):
    NAMESPACE_ONLY = "NAMESPACE_ONLY"
    NAMESPACE_POD = "NAMESPACE_POD"
    NAMESPACE_DEPLOYMENT = "NAMESPACE_DEPLOYMENT"
    NAMESPACE_DEPLOYMENT_POD = "NAMESPACE_DEPLOYMENT_POD"


class RuntimeSpanOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    protocol: RuntimeProtocol
    state: RuntimeOutcomeState
    protocol_code: str | None = None
    error_type: str | None = None
    basis: tuple[RuntimeOutcomeBasis, ...]

    @model_validator(mode="after")
    def _basis_present(self) -> RuntimeSpanOutcome:
        if not self.basis:
            raise ValueError("runtime outcome basis must not be empty")
        return self


class RuntimeKubernetesBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    namespace: str = Field(min_length=1)
    deployment: str | None = None
    pod: str | None = None
    quality: RuntimeBindingQuality

    @model_validator(mode="after")
    def _quality_matches_fields(self) -> RuntimeKubernetesBinding:
        if self.deployment is not None and self.pod is not None:
            expected = RuntimeBindingQuality.NAMESPACE_DEPLOYMENT_POD
        elif self.deployment is not None:
            expected = RuntimeBindingQuality.NAMESPACE_DEPLOYMENT
        elif self.pod is not None:
            expected = RuntimeBindingQuality.NAMESPACE_POD
        else:
            expected = RuntimeBindingQuality.NAMESPACE_ONLY
        if self.quality is not expected:
            raise ValueError("binding quality does not match observed fields")
        return self


class RuntimeServiceBindingSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    service: str = Field(min_length=1)
    binding: RuntimeKubernetesBinding
    first_seen: datetime
    last_seen: datetime
    observed_spans: int = Field(gt=0)
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_summary(self) -> RuntimeServiceBindingSummary:
        if self.first_seen > self.last_seen:
            raise ValueError("binding summary has inverted observation range")
        if len(self.evidence_ids) > 32:
            raise ValueError("binding summary provenance is unbounded")
        return self


class RuntimeServiceOutcomeSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    service: str = Field(min_length=1)
    binding: RuntimeKubernetesBinding | None
    span_kind: RuntimeSpanKind
    protocol: RuntimeProtocol
    state: RuntimeOutcomeState
    protocol_code: str | None = None
    error_type: str | None = None
    first_seen: datetime
    last_seen: datetime
    observed_spans: int = Field(gt=0)
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_summary(self) -> RuntimeServiceOutcomeSummary:
        if self.first_seen > self.last_seen:
            raise ValueError("service outcome has inverted observation range")
        if len(self.evidence_ids) > 32:
            raise ValueError("service outcome provenance is unbounded")
        return self


class RuntimeCallOutcomeSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    caller_service: str = Field(min_length=1)
    callee_service: str = Field(min_length=1)
    edge_evidence: RuntimeEdgeEvidence
    caller_binding: RuntimeKubernetesBinding | None
    callee_binding: RuntimeKubernetesBinding | None
    caller_protocol: RuntimeProtocol
    callee_protocol: RuntimeProtocol
    caller_state: RuntimeOutcomeState
    callee_state: RuntimeOutcomeState
    caller_code: str | None = None
    callee_code: str | None = None
    first_seen: datetime
    last_seen: datetime
    observed_calls: int = Field(gt=0)
    evidence_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_summary(self) -> RuntimeCallOutcomeSummary:
        if self.caller_service == self.callee_service:
            raise ValueError("call outcome must be cross-service")
        if self.first_seen > self.last_seen:
            raise ValueError("call outcome has inverted observation range")
        if len(self.evidence_ids) > 32 or len(self.trace_ids) > 32:
            raise ValueError("call outcome provenance is unbounded")
        return self


class RuntimeEvidenceStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_spans: int = Field(ge=0)
    bound_spans: int = Field(ge=0)
    unbound_spans: int = Field(ge=0)
    binding_summaries: int = Field(ge=0)
    grpc_spans: int = Field(ge=0)
    http_spans: int = Field(ge=0)
    rpc_other_spans: int = Field(ge=0)
    other_protocol_spans: int = Field(ge=0)
    success_spans: int = Field(ge=0)
    non_ok_spans: int = Field(ge=0)
    error_spans: int = Field(ge=0)
    unknown_spans: int = Field(ge=0)
    explicit_error_type_spans: int = Field(ge=0)
    grpc_status_spans: int = Field(ge=0)
    http_status_spans: int = Field(ge=0)
    span_status_evidence_spans: int = Field(ge=0)
    strict_call_pairs: int = Field(ge=0)
    client_server_pairs: int = Field(ge=0)
    producer_consumer_pairs: int = Field(ge=0)
    skipped_fallback_pairs: int = Field(ge=0)
    all_success_call_pairs: int = Field(ge=0)
    non_success_call_pairs: int = Field(ge=0)
    unknown_call_pairs: int = Field(ge=0)
    service_outcome_summaries: int = Field(ge=0)
    call_outcome_summaries: int = Field(ge=0)


_GRPC_CODES = {
    0: "OK",
    1: "CANCELLED",
    2: "UNKNOWN",
    3: "INVALID_ARGUMENT",
    4: "DEADLINE_EXCEEDED",
    5: "NOT_FOUND",
    6: "ALREADY_EXISTS",
    7: "PERMISSION_DENIED",
    8: "RESOURCE_EXHAUSTED",
    9: "FAILED_PRECONDITION",
    10: "ABORTED",
    11: "OUT_OF_RANGE",
    12: "UNIMPLEMENTED",
    13: "INTERNAL",
    14: "UNAVAILABLE",
    15: "DATA_LOSS",
    16: "UNAUTHENTICATED",
}
_GRPC_BY_NAME = {value: value for value in _GRPC_CODES.values()}


def normalize_grpc_status(value: Any) -> str | None:
    """Normalize an exact numeric or canonical gRPC status."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return _GRPC_CODES.get(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return _GRPC_CODES.get(int(text))
    return _GRPC_BY_NAME.get(text.upper())


def _normalize_http_status(value: Any) -> int | None:
    if isinstance(value, bool) or isinstance(value, int):
        result = value if isinstance(value, int) else None
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        result = None
    return result if result is not None and 100 <= result <= 599 else None


def _add_basis(basis: list[RuntimeOutcomeBasis], value: RuntimeOutcomeBasis) -> None:
    if value not in basis:
        basis.append(value)


def _first_nonempty(attrs: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


def _protocol_signal(attrs: dict[str, str]) -> tuple[RuntimeProtocol, str | None]:
    rpc_system = _first_nonempty(attrs, "rpc.system.name", "rpc.system")
    grpc_fields = any(key in attrs for key in ("rpc.response.status_code", "rpc.grpc.status_code"))
    if (rpc_system is not None and rpc_system.casefold() == "grpc") or grpc_fields:
        return RuntimeProtocol.GRPC, rpc_system
    http_fields = any(key in attrs for key in ("http.response.status_code", "http.status_code"))
    if http_fields:
        return RuntimeProtocol.HTTP, rpc_system
    if rpc_system is not None:
        return RuntimeProtocol.RPC_OTHER, rpc_system
    return RuntimeProtocol.OTHER, None


def classify_runtime_span_outcome(
    span: TraceSpanObservation,
) -> RuntimeSpanOutcome:
    attrs = span.semantic_attributes
    protocol, _rpc_system = _protocol_signal(attrs)
    basis: list[RuntimeOutcomeBasis] = []
    protocol_code: str | None = None
    state = RuntimeOutcomeState.UNKNOWN

    if protocol is RuntimeProtocol.GRPC:
        modern = normalize_grpc_status(attrs.get("rpc.response.status_code"))
        legacy = normalize_grpc_status(attrs.get("rpc.grpc.status_code"))
        if modern is not None and legacy is not None and modern != legacy:
            protocol_code = f"{modern}|{legacy}"
            _add_basis(basis, RuntimeOutcomeBasis.CONFLICTING_GRPC_STATUS)
        else:
            protocol_code = modern or legacy
            if protocol_code is not None:
                _add_basis(basis, RuntimeOutcomeBasis.GRPC_STATUS)
                state = (
                    RuntimeOutcomeState.SUCCESS
                    if protocol_code == "OK"
                    else RuntimeOutcomeState.NON_OK
                )
    elif protocol is RuntimeProtocol.HTTP:
        modern_http = _normalize_http_status(attrs.get("http.response.status_code"))
        legacy_http = _normalize_http_status(attrs.get("http.status_code"))
        if modern_http is not None and legacy_http is not None and modern_http != legacy_http:
            protocol_code = f"{modern_http}|{legacy_http}"
            _add_basis(basis, RuntimeOutcomeBasis.CONFLICTING_HTTP_STATUS)
        else:
            selected = modern_http if modern_http is not None else legacy_http
            if selected is not None:
                protocol_code = str(selected)
                _add_basis(basis, RuntimeOutcomeBasis.HTTP_STATUS)
                if selected < 400:
                    state = RuntimeOutcomeState.SUCCESS
                elif selected < 500:
                    state = RuntimeOutcomeState.NON_OK
                else:
                    state = RuntimeOutcomeState.ERROR

    error_type = _first_nonempty(attrs, "error.type", "exception.type")
    if error_type is not None:
        error_type = error_type[:256]
        state = RuntimeOutcomeState.ERROR
        _add_basis(basis, RuntimeOutcomeBasis.ERROR_TYPE)

    if span.status is TraceSpanStatus.ERROR:
        state = RuntimeOutcomeState.ERROR
        _add_basis(basis, RuntimeOutcomeBasis.SPAN_STATUS)
    elif (
        span.status is TraceSpanStatus.OK
        and state is RuntimeOutcomeState.UNKNOWN
        and RuntimeOutcomeBasis.CONFLICTING_GRPC_STATUS not in basis
        and RuntimeOutcomeBasis.CONFLICTING_HTTP_STATUS not in basis
    ):
        state = RuntimeOutcomeState.SUCCESS
        _add_basis(basis, RuntimeOutcomeBasis.SPAN_STATUS)

    if not basis:
        _add_basis(basis, RuntimeOutcomeBasis.NONE)
    return RuntimeSpanOutcome(
        protocol=protocol,
        state=state,
        protocol_code=protocol_code,
        error_type=error_type,
        basis=tuple(basis),
    )


def _duration_seconds(span: TraceSpanObservation) -> float | None:
    if span.end_at is None or span.end_at < span.start_at:
        return None
    return (span.end_at - span.start_at).total_seconds()


def derive_runtime_trace_call_facts(
    spans: Sequence[TraceSpanObservation],
) -> tuple[RuntimeTraceCallFact, ...]:
    """Expose bounded direct cross-service span direction and typed outcomes."""
    index = canonicalize_trace_spans(spans)
    facts: list[RuntimeTraceCallFact] = []
    for child in sorted(
        index.spans.values(),
        key=lambda item: (item.start_at, item.trace_id, item.span_id),
    ):
        if child.parent_span_id is None:
            continue
        parent_key = (child.trace_id, child.parent_span_id)
        if parent_key in index.conflicting_keys:
            continue
        parent = index.spans.get(parent_key)
        if parent is None:
            continue
        relation = classify_runtime_pair(parent, child)
        if relation is None:
            continue
        caller = classify_runtime_span_outcome(parent)
        callee = classify_runtime_span_outcome(child)
        direction = cast(
            Literal["CLIENT_SERVER_SPANS", "PRODUCER_CONSUMER_SPANS", "DIRECT_PARENT_CHILD"],
            {
                RuntimeEdgeEvidence.PAIRED_CLIENT_SERVER: "CLIENT_SERVER_SPANS",
                RuntimeEdgeEvidence.PAIRED_PRODUCER_CONSUMER: "PRODUCER_CONSUMER_SPANS",
                RuntimeEdgeEvidence.CROSS_SERVICE_PARENT: "DIRECT_PARENT_CHILD",
            }[relation],
        )
        facts.append(
            RuntimeTraceCallFact(
                trace_id=child.trace_id,
                caller_span_id=parent.span_id,
                callee_span_id=child.span_id,
                caller_service=parent.service.strip(),
                callee_service=child.service.strip(),
                direction_basis=direction,
                caller_start_at=parent.start_at,
                caller_end_at=parent.end_at,
                caller_duration_seconds=_duration_seconds(parent),
                caller_status=parent.status,
                caller_outcome=caller.state.value,
                callee_start_at=child.start_at,
                callee_end_at=child.end_at,
                callee_duration_seconds=_duration_seconds(child),
                callee_status=child.status,
                callee_outcome=callee.state.value,
                evidence_ids=(parent.evidence_id, child.evidence_id),
            )
        )
    return tuple(facts)


def trace_kubernetes_binding(
    span: TraceSpanObservation,
) -> RuntimeKubernetesBinding | None:
    def value(key: str) -> str | None:
        raw = span.semantic_attributes.get(key)
        if raw is None:
            return None
        text = raw.strip()
        return text or None

    namespace = value("k8s.namespace.name")
    if namespace is None:
        return None
    deployment = value("k8s.deployment.name")
    pod = value("k8s.pod.name")
    if deployment is not None and pod is not None:
        quality = RuntimeBindingQuality.NAMESPACE_DEPLOYMENT_POD
    elif deployment is not None:
        quality = RuntimeBindingQuality.NAMESPACE_DEPLOYMENT
    elif pod is not None:
        quality = RuntimeBindingQuality.NAMESPACE_POD
    else:
        quality = RuntimeBindingQuality.NAMESPACE_ONLY
    return RuntimeKubernetesBinding(
        namespace=namespace,
        deployment=deployment,
        pod=pod,
        quality=quality,
    )


def _binding_key(binding: RuntimeKubernetesBinding | None) -> tuple[str, str, str] | None:
    if binding is None:
        return None
    return (binding.namespace, binding.deployment or "", binding.pod or "")


def _binding_sort_key(binding: RuntimeKubernetesBinding | None) -> tuple[str, str, str]:
    key = _binding_key(binding)
    return key if key is not None else ("", "", "")


@dataclass
class _SummaryAccumulator:
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    count: int = 0
    evidence_candidates: list[tuple[tuple[object, ...], str]] = field(default_factory=list)
    trace_candidates: list[tuple[tuple[object, ...], str]] = field(default_factory=list)

    def add(
        self,
        *,
        at: datetime,
        order: tuple[object, ...],
        evidence_ids: Sequence[str],
        trace_ids: Sequence[str] = (),
    ) -> None:
        self.first_seen = at if self.first_seen is None else min(self.first_seen, at)
        self.last_seen = at if self.last_seen is None else max(self.last_seen, at)
        self.count += 1
        for position, value in enumerate(evidence_ids):
            _bounded_insert(self.evidence_candidates, (*order, position), value)
        for position, value in enumerate(trace_ids):
            _bounded_insert(self.trace_candidates, (*order, position), value)


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


def _candidate_values(
    candidates: list[tuple[tuple[object, ...], str]],
) -> tuple[str, ...]:
    return tuple(value for _order, value in candidates)


def _summary_times(accumulator: _SummaryAccumulator) -> tuple[datetime, datetime]:
    assert accumulator.first_seen is not None
    assert accumulator.last_seen is not None
    return accumulator.first_seen, accumulator.last_seen


def _binding_summary_key(
    service: str, binding: RuntimeKubernetesBinding
) -> tuple[str, str, str, str]:
    return (service, *_binding_sort_key(binding))


def _outcome_key(
    service: str,
    binding: RuntimeKubernetesBinding | None,
    kind: RuntimeSpanKind,
    outcome: RuntimeSpanOutcome,
) -> tuple[object, ...]:
    return (
        service,
        *_binding_sort_key(binding),
        kind,
        outcome.protocol,
        outcome.state,
        outcome.protocol_code or "",
        outcome.error_type or "",
    )


def _call_key(
    caller_service: str,
    callee_service: str,
    edge_evidence: RuntimeEdgeEvidence,
    caller_binding: RuntimeKubernetesBinding | None,
    callee_binding: RuntimeKubernetesBinding | None,
    caller_outcome: RuntimeSpanOutcome,
    callee_outcome: RuntimeSpanOutcome,
) -> tuple[object, ...]:
    return (
        caller_service,
        callee_service,
        *_binding_sort_key(caller_binding),
        *_binding_sort_key(callee_binding),
        edge_evidence,
        caller_outcome.protocol,
        callee_outcome.protocol,
        caller_outcome.state,
        callee_outcome.state,
        caller_outcome.protocol_code or "",
        callee_outcome.protocol_code or "",
    )


class RuntimeEvidence:
    """Bounded immutable lookup view over protocol-aware trace observations."""

    __slots__ = ("bindings", "service_outcomes", "call_outcomes", "stats")

    def __init__(
        self,
        *,
        bindings: Sequence[RuntimeServiceBindingSummary],
        service_outcomes: Sequence[RuntimeServiceOutcomeSummary],
        call_outcomes: Sequence[RuntimeCallOutcomeSummary],
        stats: RuntimeEvidenceStats,
    ) -> None:
        self.bindings = tuple(
            sorted(
                bindings,
                key=lambda item: (
                    item.service,
                    item.binding.namespace,
                    item.binding.deployment or "",
                    item.binding.pod or "",
                ),
            )
        )
        self.service_outcomes = tuple(
            sorted(
                service_outcomes,
                key=lambda item: (
                    item.service,
                    *_binding_sort_key(item.binding),
                    item.span_kind,
                    item.protocol,
                    item.state,
                    item.protocol_code or "",
                    item.error_type or "",
                ),
            )
        )
        self.call_outcomes = tuple(
            sorted(
                call_outcomes,
                key=lambda item: (
                    item.caller_service,
                    item.callee_service,
                    *_binding_sort_key(item.caller_binding),
                    *_binding_sort_key(item.callee_binding),
                    item.edge_evidence,
                    item.caller_protocol,
                    item.callee_protocol,
                    item.caller_state,
                    item.callee_state,
                    item.caller_code or "",
                    item.callee_code or "",
                ),
            )
        )
        self.stats = stats

    @classmethod
    def empty(cls) -> RuntimeEvidence:
        return cls(
            bindings=(),
            service_outcomes=(),
            call_outcomes=(),
            stats=RuntimeEvidenceStats(**{field: 0 for field in _STAT_FIELDS}),
        )

    def bindings_for_service(self, service: str) -> tuple[RuntimeServiceBindingSummary, ...]:
        return tuple(item for item in self.bindings if item.service == service)

    def outcomes_for_service(self, service: str) -> tuple[RuntimeServiceOutcomeSummary, ...]:
        return tuple(item for item in self.service_outcomes if item.service == service)

    def calls_from(self, service: str) -> tuple[RuntimeCallOutcomeSummary, ...]:
        return tuple(item for item in self.call_outcomes if item.caller_service == service)

    def calls_to(self, service: str) -> tuple[RuntimeCallOutcomeSummary, ...]:
        return tuple(item for item in self.call_outcomes if item.callee_service == service)


_STAT_FIELDS = tuple(RuntimeEvidenceStats.model_fields)


def derive_runtime_evidence(index: CanonicalTraceIndex) -> RuntimeEvidence:
    """Aggregate bounded protocol, binding, and strict call observations."""
    binding_accumulators: dict[tuple[str, str, str, str], _SummaryAccumulator] = {}
    outcome_accumulators: dict[tuple[object, ...], _SummaryAccumulator] = {}
    call_accumulators: dict[tuple[object, ...], _SummaryAccumulator] = {}
    binding_models: dict[tuple[str, str, str, str], RuntimeKubernetesBinding] = {}
    outcome_models: dict[
        tuple[object, ...],
        tuple[str, RuntimeKubernetesBinding | None, RuntimeSpanKind, RuntimeSpanOutcome],
    ] = {}
    call_models: dict[
        tuple[object, ...],
        tuple[
            str,
            str,
            RuntimeEdgeEvidence,
            RuntimeKubernetesBinding | None,
            RuntimeKubernetesBinding | None,
            RuntimeSpanOutcome,
            RuntimeSpanOutcome,
        ],
    ] = {}

    bound_spans = 0
    grpc_spans = http_spans = rpc_other_spans = other_protocol_spans = 0
    success_spans = non_ok_spans = error_spans = unknown_spans = 0
    explicit_error_type_spans = grpc_status_spans = http_status_spans = 0
    span_status_evidence_spans = 0

    canonical_spans = index.spans.values()
    for span in canonical_spans:
        service = span.service.strip()
        binding = trace_kubernetes_binding(span)
        outcome = classify_runtime_span_outcome(span)
        kind = normalize_runtime_span_kind(span.span_kind)
        if binding is None:
            pass
        else:
            bound_spans += 1
            key = _binding_summary_key(service, binding)
            binding_models[key] = binding
            accumulator = binding_accumulators.setdefault(key, _SummaryAccumulator())
            accumulator.add(
                at=span.start_at,
                order=(span.start_at, service, span.evidence_id),
                evidence_ids=(span.evidence_id,),
            )

        if outcome.protocol is RuntimeProtocol.GRPC:
            grpc_spans += 1
        elif outcome.protocol is RuntimeProtocol.HTTP:
            http_spans += 1
        elif outcome.protocol is RuntimeProtocol.RPC_OTHER:
            rpc_other_spans += 1
        else:
            other_protocol_spans += 1
        if outcome.state is RuntimeOutcomeState.SUCCESS:
            success_spans += 1
        elif outcome.state is RuntimeOutcomeState.NON_OK:
            non_ok_spans += 1
        elif outcome.state is RuntimeOutcomeState.ERROR:
            error_spans += 1
        else:
            unknown_spans += 1
        if outcome.error_type is not None:
            explicit_error_type_spans += 1
        if any(
            basis in (RuntimeOutcomeBasis.GRPC_STATUS, RuntimeOutcomeBasis.CONFLICTING_GRPC_STATUS)
            for basis in outcome.basis
        ):
            grpc_status_spans += 1
        if any(
            basis in (RuntimeOutcomeBasis.HTTP_STATUS, RuntimeOutcomeBasis.CONFLICTING_HTTP_STATUS)
            for basis in outcome.basis
        ):
            http_status_spans += 1
        if RuntimeOutcomeBasis.SPAN_STATUS in outcome.basis:
            span_status_evidence_spans += 1

        outcome_key = _outcome_key(service, binding, kind, outcome)
        outcome_models[outcome_key] = (service, binding, kind, outcome)
        outcome_accumulators.setdefault(outcome_key, _SummaryAccumulator()).add(
            at=span.start_at,
            order=(span.start_at, service, span.evidence_id),
            evidence_ids=(span.evidence_id,),
        )

    strict_call_pairs = client_server_pairs = producer_consumer_pairs = 0
    skipped_fallback_pairs = 0
    all_success_call_pairs = non_success_call_pairs = unknown_call_pairs = 0
    for child in canonical_spans:
        if child.parent_span_id is None:
            continue
        parent_key = (child.trace_id, child.parent_span_id)
        if parent_key in index.conflicting_keys:
            continue
        parent = index.spans.get(parent_key)
        if parent is None:
            continue
        evidence = classify_runtime_pair(parent, child)
        if evidence is None:
            continue
        if evidence is RuntimeEdgeEvidence.CROSS_SERVICE_PARENT:
            skipped_fallback_pairs += 1
            continue
        strict_call_pairs += 1
        if evidence is RuntimeEdgeEvidence.PAIRED_CLIENT_SERVER:
            client_server_pairs += 1
        else:
            producer_consumer_pairs += 1
        caller_outcome = classify_runtime_span_outcome(parent)
        callee_outcome = classify_runtime_span_outcome(child)
        caller_binding = trace_kubernetes_binding(parent)
        callee_binding = trace_kubernetes_binding(child)
        if caller_outcome.state in (
            RuntimeOutcomeState.NON_OK,
            RuntimeOutcomeState.ERROR,
        ) or callee_outcome.state in (
            RuntimeOutcomeState.NON_OK,
            RuntimeOutcomeState.ERROR,
        ):
            non_success_call_pairs += 1
        elif (
            caller_outcome.state is RuntimeOutcomeState.UNKNOWN
            or callee_outcome.state is RuntimeOutcomeState.UNKNOWN
        ):
            unknown_call_pairs += 1
        else:
            all_success_call_pairs += 1
        caller_service = parent.service.strip()
        callee_service = child.service.strip()
        call_key = _call_key(
            caller_service,
            callee_service,
            evidence,
            caller_binding,
            callee_binding,
            caller_outcome,
            callee_outcome,
        )
        call_models[call_key] = (
            caller_service,
            callee_service,
            evidence,
            caller_binding,
            callee_binding,
            caller_outcome,
            callee_outcome,
        )
        call_accumulators.setdefault(call_key, _SummaryAccumulator()).add(
            at=child.start_at,
            order=(
                child.start_at,
                caller_service,
                callee_service,
                parent.evidence_id,
                child.evidence_id,
            ),
            evidence_ids=(parent.evidence_id, child.evidence_id),
            trace_ids=(child.trace_id,),
        )

    bindings = [
        RuntimeServiceBindingSummary(
            service=key[0],
            binding=binding_models[key],
            first_seen=_summary_times(accumulator)[0],
            last_seen=_summary_times(accumulator)[1],
            observed_spans=accumulator.count,
            evidence_ids=_candidate_values(accumulator.evidence_candidates),
        )
        for key, accumulator in binding_accumulators.items()
    ]
    service_outcomes = [
        RuntimeServiceOutcomeSummary(
            service=service,
            binding=binding,
            span_kind=kind,
            protocol=outcome.protocol,
            state=outcome.state,
            protocol_code=outcome.protocol_code,
            error_type=outcome.error_type,
            first_seen=_summary_times(accumulator)[0],
            last_seen=_summary_times(accumulator)[1],
            observed_spans=accumulator.count,
            evidence_ids=_candidate_values(accumulator.evidence_candidates),
        )
        for key, accumulator in outcome_accumulators.items()
        for service, binding, kind, outcome in (outcome_models[key],)
    ]
    call_outcomes = [
        RuntimeCallOutcomeSummary(
            caller_service=caller_service,
            callee_service=callee_service,
            edge_evidence=evidence,
            caller_binding=caller_binding,
            callee_binding=callee_binding,
            caller_protocol=caller_outcome.protocol,
            callee_protocol=callee_outcome.protocol,
            caller_state=caller_outcome.state,
            callee_state=callee_outcome.state,
            caller_code=caller_outcome.protocol_code,
            callee_code=callee_outcome.protocol_code,
            first_seen=_summary_times(accumulator)[0],
            last_seen=_summary_times(accumulator)[1],
            observed_calls=accumulator.count,
            evidence_ids=_candidate_values(accumulator.evidence_candidates),
            trace_ids=_candidate_values(accumulator.trace_candidates),
        )
        for key, accumulator in call_accumulators.items()
        for (
            caller_service,
            callee_service,
            evidence,
            caller_binding,
            callee_binding,
            caller_outcome,
            callee_outcome,
        ) in (call_models[key],)
    ]
    stats = RuntimeEvidenceStats(
        canonical_spans=len(index.spans),
        bound_spans=bound_spans,
        unbound_spans=len(index.spans) - bound_spans,
        binding_summaries=len(bindings),
        grpc_spans=grpc_spans,
        http_spans=http_spans,
        rpc_other_spans=rpc_other_spans,
        other_protocol_spans=other_protocol_spans,
        success_spans=success_spans,
        non_ok_spans=non_ok_spans,
        error_spans=error_spans,
        unknown_spans=unknown_spans,
        explicit_error_type_spans=explicit_error_type_spans,
        grpc_status_spans=grpc_status_spans,
        http_status_spans=http_status_spans,
        span_status_evidence_spans=span_status_evidence_spans,
        strict_call_pairs=strict_call_pairs,
        client_server_pairs=client_server_pairs,
        producer_consumer_pairs=producer_consumer_pairs,
        skipped_fallback_pairs=skipped_fallback_pairs,
        all_success_call_pairs=all_success_call_pairs,
        non_success_call_pairs=non_success_call_pairs,
        unknown_call_pairs=unknown_call_pairs,
        service_outcome_summaries=len(service_outcomes),
        call_outcome_summaries=len(call_outcomes),
    )
    return RuntimeEvidence(
        bindings=bindings,
        service_outcomes=service_outcomes,
        call_outcomes=call_outcomes,
        stats=stats,
    )


__all__ = [
    "RuntimeBindingQuality",
    "RuntimeCallOutcomeSummary",
    "RuntimeEvidence",
    "RuntimeEvidenceStats",
    "RuntimeKubernetesBinding",
    "RuntimeOutcomeBasis",
    "RuntimeOutcomeState",
    "RuntimeProtocol",
    "RuntimeServiceBindingSummary",
    "RuntimeServiceOutcomeSummary",
    "RuntimeSpanOutcome",
    "classify_runtime_span_outcome",
    "derive_runtime_evidence",
    "derive_runtime_trace_call_facts",
    "normalize_grpc_status",
    "trace_kubernetes_binding",
]
