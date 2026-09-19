from datetime import UTC, datetime, timedelta

import pytest

from packages.rca.engine import build_case, diagnose_case
from packages.rca.investigation.environment import initial_view
from packages.rca.model import TraceSpanObservation, TraceSpanStatus
from packages.rca.runtime_graph import (
    RuntimeGraph,
    RuntimeSpanKind,
    canonicalize_trace_spans,
    derive_runtime_graph,
    derive_runtime_graph_from_index,
    normalize_runtime_span_kind,
)
from packages.rca.source import InMemorySource

T0 = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, RuntimeSpanKind.UNSPECIFIED),
        ("", RuntimeSpanKind.UNSPECIFIED),
        ("0", RuntimeSpanKind.UNSPECIFIED),
        ("unspecified", RuntimeSpanKind.UNSPECIFIED),
        ("SPAN_KIND_UNSPECIFIED", RuntimeSpanKind.UNSPECIFIED),
        ("1", RuntimeSpanKind.INTERNAL),
        ("internal", RuntimeSpanKind.INTERNAL),
        ("SPAN_KIND_INTERNAL", RuntimeSpanKind.INTERNAL),
        ("2", RuntimeSpanKind.SERVER),
        ("server", RuntimeSpanKind.SERVER),
        ("SPAN_KIND_SERVER", RuntimeSpanKind.SERVER),
        ("3", RuntimeSpanKind.CLIENT),
        (" client ", RuntimeSpanKind.CLIENT),
        ("SPAN_KIND_CLIENT", RuntimeSpanKind.CLIENT),
        ("4", RuntimeSpanKind.PRODUCER),
        ("producer", RuntimeSpanKind.PRODUCER),
        ("SPAN_KIND_PRODUCER", RuntimeSpanKind.PRODUCER),
        ("5", RuntimeSpanKind.CONSUMER),
        ("consumer", RuntimeSpanKind.CONSUMER),
        ("SPAN_KIND_CONSUMER", RuntimeSpanKind.CONSUMER),
        ("http-server", RuntimeSpanKind.UNKNOWN),
    ],
)
def test_normalize_runtime_span_kind(value: str | None, expected: RuntimeSpanKind) -> None:
    assert normalize_runtime_span_kind(value) is expected


def _span(
    *,
    trace: str,
    span: str,
    service: str,
    kind: str | None,
    at: datetime,
    parent: str | None = None,
    evidence: str | None = None,
    status: TraceSpanStatus = TraceSpanStatus.UNSET,
    duration: float | None = None,
    attributes: dict[str, str] | None = None,
) -> TraceSpanObservation:
    return TraceSpanObservation(
        trace_id=trace,
        span_id=span,
        parent_span_id=parent,
        service=service,
        span_kind=kind,
        start_at=at,
        duration_raw=duration,
        status=status,
        semantic_attributes=attributes or {},
        evidence_id=evidence or f"trace:{trace}:{span}",
    )


def test_client_server_edge_is_strict() -> None:
    graph = derive_runtime_graph(
        (
            _span(trace="t1", span="c", service="A", kind="CLIENT", at=T0, evidence="a"),
            _span(
                trace="t1",
                span="s",
                service="B",
                kind="SERVER",
                parent="c",
                at=T0 + timedelta(seconds=1),
                evidence="b",
            ),
        )
    )
    edge = graph.edges[0]
    assert (edge.caller_service, edge.callee_service) == ("A", "B")
    assert edge.client_server_pairs == 1
    assert edge.producer_consumer_pairs == 0
    assert edge.cross_service_parent_pairs == 0
    assert edge.has_strict_evidence is True
    assert edge.fallback_only is False
    assert graph.strict_outgoing("A") == (edge,)
    assert graph.strict_incoming("B") == (edge,)


def test_shared_canonical_index_matches_compatibility_wrapper() -> None:
    spans = (
        _span(trace="t", span="p", service="A", kind="CLIENT", at=T0),
        _span(
            trace="t",
            span="c",
            service="B",
            kind="SERVER",
            parent="p",
            at=T0 + timedelta(seconds=1),
        ),
    )
    direct = derive_runtime_graph(spans)
    indexed = derive_runtime_graph_from_index(canonicalize_trace_spans(spans))
    assert direct.services == indexed.services
    assert direct.edges == indexed.edges
    assert direct.stats == indexed.stats


def test_producer_consumer_edge_is_strict() -> None:
    graph = derive_runtime_graph(
        (
            _span(trace="t", span="p", service="A", kind="PRODUCER", at=T0),
            _span(
                trace="t",
                span="c",
                service="B",
                kind="CONSUMER",
                parent="p",
                at=T0 + timedelta(seconds=1),
            ),
        )
    )
    assert graph.edges[0].producer_consumer_pairs == 1
    assert graph.edges[0].has_strict_evidence is True


def test_fallback_same_service_root_and_orphan_accounting() -> None:
    graph = derive_runtime_graph(
        (
            _span(trace="t", span="root", service="A", kind=None, at=T0),
            _span(
                trace="t",
                span="same",
                service="A",
                kind="SERVER",
                parent="root",
                at=T0 + timedelta(seconds=1),
            ),
            _span(
                trace="t",
                span="fallback",
                service="B",
                kind="SERVER",
                parent="root",
                at=T0 + timedelta(seconds=2),
            ),
            _span(
                trace="t",
                span="orphan",
                service="C",
                kind="SERVER",
                parent="missing",
                at=T0 + timedelta(seconds=3),
            ),
        )
    )
    assert graph.stats.root_spans == 1
    assert graph.stats.child_spans == 3
    assert graph.stats.resolved_parent_links == 2
    assert graph.stats.orphan_parent_links == 1
    assert graph.stats.same_service_parent_links == 1
    assert graph.stats.cross_service_parent_links == 1
    assert graph.stats.fallback_parent_pairs == 1
    assert graph.fallback_edges()[0].fallback_only is True


def test_parent_resolution_is_scoped_to_trace_id() -> None:
    graph = derive_runtime_graph(
        (
            _span(trace="t1", span="parent", service="A", kind="CLIENT", at=T0),
            _span(
                trace="t2",
                span="child",
                service="B",
                kind="SERVER",
                parent="parent",
                at=T0 + timedelta(seconds=1),
            ),
        )
    )
    assert graph.stats.orphan_parent_links == 1
    assert graph.edges == ()


def test_equivalent_duplicates_use_smallest_evidence_and_conflicts_fail_closed() -> None:
    equivalent = (
        _span(
            trace="t",
            span="p",
            service="A",
            kind="CLIENT",
            at=T0,
            evidence="z-parent",
        ),
        _span(
            trace="t",
            span="p",
            service="A",
            kind="CLIENT",
            at=T0 + timedelta(seconds=4),
            evidence="a-parent",
        ),
        _span(
            trace="t",
            span="c",
            service="B",
            kind="SERVER",
            parent="p",
            at=T0 + timedelta(seconds=5),
            evidence="child",
        ),
    )
    graph = derive_runtime_graph(equivalent)
    assert graph.stats.duplicate_equivalent_rows == 1
    assert graph.edges[0].evidence_ids[0] == "a-parent"
    assert graph.edges[0].client_server_pairs == 1

    conflicting = (
        _span(trace="t", span="p", service="A", kind="CLIENT", at=T0, evidence="a"),
        _span(trace="t", span="p", service="X", kind="CLIENT", at=T0, evidence="b"),
        _span(
            trace="t",
            span="c",
            service="B",
            kind="SERVER",
            parent="p",
            at=T0 + timedelta(seconds=1),
        ),
    )
    graph = derive_runtime_graph(conflicting)
    assert graph.stats.conflicting_span_keys == 1
    assert graph.stats.conflicting_parent_links == 1
    assert graph.edges == ()


def test_window_is_child_based_and_inclusive() -> None:
    graph = derive_runtime_graph(
        (
            _span(trace="t", span="p", service="A", kind="CLIENT", at=T0 - timedelta(seconds=1)),
            _span(
                trace="t",
                span="before",
                service="B",
                kind="SERVER",
                parent="p",
                at=T0 - timedelta(microseconds=1),
            ),
            _span(trace="t", span="start", service="B", kind="SERVER", parent="p", at=T0),
            _span(
                trace="t",
                span="end",
                service="B",
                kind="SERVER",
                parent="p",
                at=T0 + timedelta(seconds=1),
            ),
            _span(
                trace="t",
                span="after",
                service="B",
                kind="SERVER",
                parent="p",
                at=T0 + timedelta(seconds=2),
            ),
        ),
        start=T0,
        end=T0 + timedelta(seconds=1),
    )
    assert graph.stats.client_server_pairs == 2
    assert graph.edges[0].first_seen == T0
    assert graph.edges[0].last_seen == T0 + timedelta(seconds=1)


def test_edge_aggregation_provenance_and_determinism() -> None:
    spans: list[TraceSpanObservation] = []
    for index in range(40):
        spans.extend(
            (
                _span(
                    trace=f"t{index}",
                    span="p",
                    service="A",
                    kind="CLIENT",
                    at=T0,
                    evidence=f"p{index:02d}",
                ),
                _span(
                    trace=f"t{index}",
                    span="c",
                    service="B",
                    kind="SERVER",
                    parent="p",
                    at=T0 + timedelta(seconds=index),
                    evidence=f"c{index:02d}",
                ),
            )
        )
    graph = derive_runtime_graph(tuple(reversed(spans)))
    assert graph.stats.client_server_pairs == 40
    assert len(graph.edges) == 1
    assert len(graph.edges[0].evidence_ids) <= 32
    assert len(graph.edges[0].trace_ids) <= 32
    assert graph.edges[0].trace_ids == tuple(f"t{index}" for index in range(32))


def test_mixed_strict_and_fallback_evidence_stays_one_edge() -> None:
    graph = derive_runtime_graph(
        (
            _span(trace="strict", span="p", service="A", kind="CLIENT", at=T0),
            _span(
                trace="strict",
                span="c",
                service="B",
                kind="SERVER",
                parent="p",
                at=T0 + timedelta(seconds=1),
            ),
            _span(trace="fallback", span="p", service="A", kind="INTERNAL", at=T0),
            _span(
                trace="fallback",
                span="c",
                service="B",
                kind="SERVER",
                parent="p",
                at=T0 + timedelta(seconds=2),
            ),
        )
    )
    assert len(graph.edges) == 1
    assert graph.edges[0].client_server_pairs == 1
    assert graph.edges[0].cross_service_parent_pairs == 1
    assert graph.edges[0].has_strict_evidence is True
    assert graph.edges[0].fallback_only is False


def test_status_duration_and_attributes_do_not_change_graph() -> None:
    base = (
        _span(trace="t", span="p", service="A", kind="CLIENT", at=T0),
        _span(
            trace="t",
            span="c",
            service="B",
            kind="SERVER",
            parent="p",
            at=T0 + timedelta(seconds=1),
        ),
    )
    changed = (
        _span(
            trace="t",
            span="p",
            service="A",
            kind="CLIENT",
            at=T0,
            status=TraceSpanStatus.ERROR,
            duration=999,
            attributes={"peer.service": "other"},
        ),
        _span(
            trace="t",
            span="c",
            service="B",
            kind="SERVER",
            parent="p",
            at=T0 + timedelta(seconds=1),
            status=TraceSpanStatus.OK,
            duration=1,
            attributes={"db.system": "postgres"},
        ),
    )
    assert derive_runtime_graph(base).edges == derive_runtime_graph(changed).edges


def test_empty_graph_and_case_integration_do_not_change_diagnosis() -> None:
    assert RuntimeGraph.empty().services == ()
    assert RuntimeGraph.empty().edges == ()
    assert RuntimeGraph.empty().stats.input_spans == 0
    traces = (
        _span(trace="t", span="p", service="A", kind="CLIENT", at=T0),
        _span(
            trace="t",
            span="c",
            service="B",
            kind="SERVER",
            parent="p",
            at=T0 + timedelta(seconds=1),
        ),
    )
    without = InMemorySource(name="same", trace_items=[])
    with_traces = InMemorySource(name="same", trace_items=list(traces))
    without_case = build_case(without)
    with_case = build_case(with_traces)
    assert with_case.runtime_graph.edges
    assert with_case.runtime_evidence.stats.canonical_spans == 2
    seed_case = build_case(initial_view(with_traces))
    assert seed_case.runtime_graph.services == ()
    assert seed_case.runtime_graph.edges == ()
    assert seed_case.runtime_evidence.stats.canonical_spans == 0
    assert diagnose_case(without_case).model_dump(mode="json") == diagnose_case(
        with_case
    ).model_dump(mode="json")
