from datetime import UTC, datetime, timedelta
from random import Random

import pytest

from packages.rca.model import TraceSpanObservation, TraceSpanStatus
from packages.rca.runtime_evidence import (
    RuntimeBindingQuality,
    RuntimeEvidence,
    RuntimeOutcomeBasis,
    RuntimeOutcomeState,
    RuntimeProtocol,
    classify_runtime_span_outcome,
    derive_runtime_evidence,
    normalize_grpc_status,
    trace_kubernetes_binding,
)
from packages.rca.runtime_graph import (
    RuntimeEdgeEvidence,
    canonicalize_trace_spans,
)

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _span(
    *,
    span: str = "s",
    trace: str = "t",
    service: str = "svc",
    kind: str | None = "INTERNAL",
    at: int = 0,
    parent: str | None = None,
    status: TraceSpanStatus = TraceSpanStatus.UNSET,
    attributes: dict[str, str] | None = None,
    duration: float | None = None,
) -> TraceSpanObservation:
    return TraceSpanObservation(
        trace_id=trace,
        span_id=span,
        parent_span_id=parent,
        service=service,
        span_kind=kind,
        start_at=T0 + timedelta(seconds=at),
        status=status,
        duration_raw=duration,
        semantic_attributes=attributes or {},
        evidence_id=f"evidence:{trace}:{span}:{at}",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (str(code), name)
        for code, name in {
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
        }.items()
    ]
    + [
        (" ok ", "OK"),
        ("unavailable", "UNAVAILABLE"),
    ],
)
def test_normalize_grpc_status(value: str, expected: str) -> None:
    assert normalize_grpc_status(value) == expected


@pytest.mark.parametrize("value", ["", "17", "-1", "foobar", "1.0", None])
def test_invalid_grpc_status(value: str | None) -> None:
    assert normalize_grpc_status(value) is None


def test_grpc_outcomes_and_precedence() -> None:
    success = classify_runtime_span_outcome(
        _span(attributes={"rpc.system": "grpc", "rpc.grpc.status_code": "0"})
    )
    assert success.protocol is RuntimeProtocol.GRPC
    assert success.state is RuntimeOutcomeState.SUCCESS
    assert success.protocol_code == "OK"
    assert RuntimeOutcomeBasis.GRPC_STATUS in success.basis

    non_ok = classify_runtime_span_outcome(
        _span(attributes={"rpc.system": "grpc", "rpc.grpc.status_code": "14"})
    )
    assert non_ok.state is RuntimeOutcomeState.NON_OK

    explicit_error = classify_runtime_span_outcome(
        _span(
            status=TraceSpanStatus.OK,
            attributes={
                "rpc.system": "grpc",
                "rpc.grpc.status_code": "0",
                "error.type": " timeout ",
            },
        )
    )
    assert explicit_error.state is RuntimeOutcomeState.ERROR
    assert explicit_error.error_type == "timeout"
    assert explicit_error.protocol_code == "OK"

    status_error = classify_runtime_span_outcome(
        _span(
            status=TraceSpanStatus.ERROR,
            attributes={"rpc.system": "grpc", "rpc.grpc.status_code": "0"},
        )
    )
    assert status_error.state is RuntimeOutcomeState.ERROR
    assert RuntimeOutcomeBasis.SPAN_STATUS in status_error.basis

    protocol_wins_over_ok = classify_runtime_span_outcome(
        _span(
            status=TraceSpanStatus.OK,
            attributes={"rpc.system": "grpc", "rpc.grpc.status_code": "14"},
        )
    )
    assert protocol_wins_over_ok.state is RuntimeOutcomeState.NON_OK


def test_grpc_alias_agreement_and_conflict() -> None:
    agreed = classify_runtime_span_outcome(
        _span(
            attributes={
                "rpc.system.name": "grpc",
                "rpc.response.status_code": "UNAVAILABLE",
                "rpc.grpc.status_code": "14",
            }
        )
    )
    assert agreed.protocol_code == "UNAVAILABLE"
    assert agreed.state is RuntimeOutcomeState.NON_OK

    conflict = classify_runtime_span_outcome(
        _span(
            status=TraceSpanStatus.OK,
            attributes={
                "rpc.system.name": "grpc",
                "rpc.response.status_code": "UNAVAILABLE",
                "rpc.grpc.status_code": "13",
            },
        )
    )
    assert conflict.state is RuntimeOutcomeState.UNKNOWN
    assert conflict.protocol_code == "UNAVAILABLE|INTERNAL"
    assert RuntimeOutcomeBasis.CONFLICTING_GRPC_STATUS in conflict.basis


@pytest.mark.parametrize(
    ("code", "state"),
    [
        ("200", RuntimeOutcomeState.SUCCESS),
        ("204", RuntimeOutcomeState.SUCCESS),
        ("301", RuntimeOutcomeState.SUCCESS),
        ("404", RuntimeOutcomeState.NON_OK),
        ("429", RuntimeOutcomeState.NON_OK),
        ("500", RuntimeOutcomeState.ERROR),
        ("503", RuntimeOutcomeState.ERROR),
    ],
)
def test_http_outcome_classes(code: str, state: RuntimeOutcomeState) -> None:
    result = classify_runtime_span_outcome(_span(attributes={"http.response.status_code": code}))
    assert result.protocol is RuntimeProtocol.HTTP
    assert result.state is state
    assert result.protocol_code == code


def test_http_alias_conflict_and_error_override() -> None:
    conflict = classify_runtime_span_outcome(
        _span(attributes={"http.response.status_code": "200", "http.status_code": "500"})
    )
    assert conflict.state is RuntimeOutcomeState.UNKNOWN
    assert conflict.protocol_code == "200|500"
    assert RuntimeOutcomeBasis.CONFLICTING_HTTP_STATUS in conflict.basis

    error = classify_runtime_span_outcome(
        _span(
            status=TraceSpanStatus.ERROR,
            attributes={"http.response.status_code": "200", "http.status_code": "500"},
        )
    )
    assert error.state is RuntimeOutcomeState.ERROR
    assert RuntimeOutcomeBasis.SPAN_STATUS in error.basis


def test_generic_span_status_and_unknown() -> None:
    assert (
        classify_runtime_span_outcome(_span(status=TraceSpanStatus.OK)).state
        is RuntimeOutcomeState.SUCCESS
    )
    assert (
        classify_runtime_span_outcome(_span(status=TraceSpanStatus.ERROR)).state
        is RuntimeOutcomeState.ERROR
    )
    unknown = classify_runtime_span_outcome(_span())
    assert unknown.state is RuntimeOutcomeState.UNKNOWN
    assert unknown.basis == (RuntimeOutcomeBasis.NONE,)


@pytest.mark.parametrize(
    ("attrs", "quality"),
    [
        ({"k8s.namespace.name": "shop"}, RuntimeBindingQuality.NAMESPACE_ONLY),
        (
            {"k8s.namespace.name": "shop", "k8s.pod.name": "p"},
            RuntimeBindingQuality.NAMESPACE_POD,
        ),
        (
            {"k8s.namespace.name": "shop", "k8s.deployment.name": "d"},
            RuntimeBindingQuality.NAMESPACE_DEPLOYMENT,
        ),
        (
            {
                "k8s.namespace.name": "shop",
                "k8s.deployment.name": "d",
                "k8s.pod.name": "p",
            },
            RuntimeBindingQuality.NAMESPACE_DEPLOYMENT_POD,
        ),
    ],
)
def test_trace_kubernetes_binding_quality(
    attrs: dict[str, str], quality: RuntimeBindingQuality
) -> None:
    binding = trace_kubernetes_binding(_span(attributes=attrs))
    assert binding is not None
    assert binding.quality is quality
    assert binding.namespace == "shop"


def test_binding_requires_namespace_and_does_not_fuzzy_match() -> None:
    assert (
        trace_kubernetes_binding(
            _span(service="payment-v2", attributes={"k8s.pod.name": "payment-abc"})
        )
        is None
    )


def test_runtime_evidence_aggregates_strict_calls_and_excludes_fallback() -> None:
    attrs = {
        "rpc.system": "grpc",
        "rpc.grpc.status_code": "0",
        "k8s.namespace.name": "shop",
        "k8s.deployment.name": "checkout",
        "k8s.pod.name": "checkout-a",
    }
    spans = (
        _span(span="parent", service="checkout", kind="CLIENT", attributes=attrs),
        _span(
            span="child",
            service="payment",
            kind="SERVER",
            parent="parent",
            at=1,
            attributes={
                **attrs,
                "k8s.deployment.name": "payment",
                "k8s.pod.name": "payment-a",
                "rpc.grpc.status_code": "14",
            },
        ),
        _span(
            span="fallback",
            service="inventory",
            kind="INTERNAL",
            parent="parent",
            at=2,
            attributes={**attrs, "k8s.deployment.name": "inventory", "k8s.pod.name": "inventory-a"},
        ),
    )
    evidence = derive_runtime_evidence(canonicalize_trace_spans(spans))
    assert evidence.stats.strict_call_pairs == 1
    assert evidence.stats.client_server_pairs == 1
    assert evidence.stats.skipped_fallback_pairs == 1
    assert evidence.stats.non_success_call_pairs == 1
    assert len(evidence.call_outcomes) == 1
    assert evidence.call_outcomes[0].edge_evidence is RuntimeEdgeEvidence.PAIRED_CLIENT_SERVER
    assert evidence.call_outcomes[0].caller_state is RuntimeOutcomeState.SUCCESS
    assert evidence.call_outcomes[0].callee_state is RuntimeOutcomeState.NON_OK


def test_duplicate_conflict_orphan_and_same_service_do_not_create_calls() -> None:
    spans = (
        _span(span="p", service="a", kind="CLIENT"),
        _span(span="p", service="a", kind="CLIENT", at=1),
        _span(span="p", service="x", kind="CLIENT", at=2),
        _span(span="same", service="a", kind="SERVER", parent="p", at=3),
        _span(span="orphan", service="b", kind="SERVER", parent="missing", at=4),
    )
    evidence = derive_runtime_evidence(canonicalize_trace_spans(spans))
    assert evidence.stats.strict_call_pairs == 0
    assert evidence.call_outcomes == ()


def test_aggregation_is_bounded_and_order_independent() -> None:
    spans = []
    for index in range(40):
        spans.append(
            _span(
                span=f"p{index}",
                trace=f"t{index}",
                service="a",
                kind="CLIENT",
                at=index,
                attributes={
                    "k8s.namespace.name": "n",
                    "k8s.deployment.name": "d",
                    "k8s.pod.name": "p",
                },
            )
        )
        spans.append(
            _span(
                span=f"c{index}",
                trace=f"t{index}",
                service="b",
                kind="SERVER",
                parent=f"p{index}",
                at=index + 1,
                attributes={
                    "k8s.namespace.name": "n",
                    "k8s.deployment.name": "d",
                    "k8s.pod.name": f"q{index}",
                },
            )
        )
    first = derive_runtime_evidence(canonicalize_trace_spans(tuple(spans)))
    shuffled = list(spans)
    Random(4).shuffle(shuffled)
    second = derive_runtime_evidence(canonicalize_trace_spans(tuple(shuffled)))
    assert first.stats.model_dump() == second.stats.model_dump()
    assert first.bindings == second.bindings
    assert first.service_outcomes == second.service_outcomes
    assert first.call_outcomes == second.call_outcomes
    assert all(len(item.evidence_ids) <= 32 for item in first.bindings)
    assert all(len(item.evidence_ids) <= 32 for item in first.service_outcomes)
    assert all(
        len(item.evidence_ids) <= 32 and len(item.trace_ids) <= 32 for item in first.call_outcomes
    )


def test_runtime_evidence_does_not_use_duration_or_span_name() -> None:
    base = _span(
        attributes={"rpc.system": "grpc", "rpc.grpc.status_code": "0"},
        duration=1.0,
    )
    changed = base.model_copy(update={"span_name": "different", "duration_raw": 999.0})
    first = classify_runtime_span_outcome(base)
    second = classify_runtime_span_outcome(changed)
    assert first == second


def test_empty_runtime_evidence() -> None:
    empty = RuntimeEvidence.empty()
    assert empty.bindings == ()
    assert empty.service_outcomes == ()
    assert empty.call_outcomes == ()
    assert empty.stats.canonical_spans == 0
