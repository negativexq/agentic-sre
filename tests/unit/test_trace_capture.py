"""The base trace read of a live diagnosis (live-trace-design.md §3): bounded, recorded, never repaired."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.live import LiveSource, capture_traces
from packages.rca.model import ProviderReadFailure, TraceSpanObservation

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def span(trace: str, sid: str, service: str, minutes: float) -> TraceSpanObservation:
    return TraceSpanObservation(
        trace_id=trace,
        span_id=sid,
        service=service,
        start_at=T0 + timedelta(minutes=minutes),
        evidence_id=f"tempo:{trace}:{sid}",
    )


class Reader:
    def __init__(self, by_service: dict[str, Any]) -> None:
        self.by_service = by_service
        self.asked: list[tuple[str, str, datetime, datetime]] = []

    def query_tempo(self, target: Any, query: Any) -> Any:
        self.asked.append((target.namespace, target.name, query.start, query.end))
        result = self.by_service[target.name]
        if isinstance(result, Exception):
            raise result
        return result


def test_one_read_per_service_over_the_newest_hour_with_spans_shared_by_traces_kept_once() -> None:
    shared = span("t1", "a", "order-service", 50)
    reader = Reader(
        {
            "order-service": (shared,),
            "payment-service": (shared, span("t1", "b", "payment-service", 51)),
        }
    )
    capture = capture_traces(
        reader,
        [("shop", "order-service"), ("shop", "payment-service")],
        T0,
        T0 + timedelta(hours=3),
    )
    assert [s.span_id for s in capture.spans] == ["a", "b"]
    assert {(r.service, r.completeness, r.spans) for r in capture.reads} == {
        ("order-service", "BEST_EFFORT", 1),
        ("payment-service", "BEST_EFFORT", 2),
    }
    assert all(end - start == timedelta(hours=1) for _, _, start, end in reader.asked)


def test_a_failed_read_is_recorded_as_failed_and_the_others_still_count() -> None:
    reader = Reader(
        {
            "order-service": ProviderReadFailure("tempo_traces", "TimeoutError", "slow"),
            "payment-service": RuntimeError("down"),
            "order-worker": (span("t2", "c", "order-worker", 5),),
        }
    )
    capture = capture_traces(
        reader,
        [("shop", "order-service"), ("shop", "payment-service"), ("shop", "order-worker")],
        T0,
        T0 + timedelta(minutes=10),
    )
    assert [(r.service, r.completeness, r.error) for r in capture.reads] == [
        ("order-service", "FAILED", "TimeoutError"),
        ("payment-service", "FAILED", "RuntimeError"),
        ("order-worker", "BEST_EFFORT", ""),
    ]
    assert [s.span_id for s in capture.spans] == ["c"]


def test_reads_are_bounded_in_services_and_spans_and_a_cut_is_truncated() -> None:
    many = tuple(span("t", str(i), "order-service", i * 0.01) for i in range(10))
    reader = Reader({f"svc-{i}": many for i in range(5)})
    capture = capture_traces(
        reader,
        [("shop", f"svc-{i}") for i in range(5)],
        T0,
        T0 + timedelta(minutes=5),
        max_services=2,
        max_spans=4,
    )
    assert len(reader.asked) == 2
    assert all(r.completeness == "TRUNCATED" and r.spans == 4 for r in capture.reads)


def test_the_live_source_serves_exactly_the_captured_spans() -> None:
    spans = (span("t1", "a", "order-service", 1),)
    source = LiveSource(
        incident="i",
        alert_items=[],
        journal=[],
        current_objects=[],
        event_bodies=[],
        trace_items=spans,
    )
    assert list(source.trace_observations()) == list(spans)
