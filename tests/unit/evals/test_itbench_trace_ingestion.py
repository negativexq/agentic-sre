from datetime import UTC, datetime

import pytest

from packages.evals.itbench.snapshot_backend import normalize_trace_status as backend_status
from packages.evals.itbench.source import parse_trace_span
from packages.rca.model import TraceSpanStatus
from packages.rca.traces import normalize_trace_status


def _row() -> dict[str, object]:
    return {
        "TraceId": " ABCDEF ",
        "SpanId": " 001122 ",
        "ParentSpanId": "0000000000000000",
        "ServiceName": "checkout",
        "SpanName": "GET /orders",
        "SpanKind": "server",
        "Timestamp": "2025-01-01T00:00:01Z",
        "EndTime": "2025-01-01T00:00:02Z",
        "Duration": "123.5",
        "StatusCode": "OK",
        "ResourceAttributes": '{"service.name": "resource", "peer.service": "resource", "random.secret": "no"}',
        "SpanAttributes": '{"peer.service": "span", "rpc.service": "orders", "password": "no"}',
        "peer.service": "direct",
        "k8s.pod.name": "checkout-1",
    }


def test_parse_trace_span_valid_row_and_bounded_attributes() -> None:
    observation = parse_trace_span(_row(), evidence_id="otel_traces_raw.tsv:0")
    assert observation is not None
    assert observation.trace_id == "abcdef"
    assert observation.span_id == "001122"
    assert observation.parent_span_id is None
    assert observation.service == "checkout"
    assert observation.span_name == "GET /orders"
    assert observation.span_kind == "SERVER"
    assert observation.start_at == datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert observation.end_at == datetime(2025, 1, 1, 0, 0, 2, tzinfo=UTC)
    assert observation.duration_raw == 123.5
    assert observation.status is TraceSpanStatus.OK
    assert observation.semantic_attributes == {
        "k8s.pod.name": "checkout-1",
        "peer.service": "direct",
        "rpc.service": "orders",
        "service.name": "resource",
    }
    assert tuple(observation.semantic_attributes) == tuple(sorted(observation.semantic_attributes))


@pytest.mark.parametrize("missing", ["TraceId", "SpanId", "ServiceName", "Timestamp"])
def test_parse_trace_span_rejects_missing_required_fields(missing: str) -> None:
    row = _row()
    row.pop(missing)
    if missing == "ServiceName":
        row.pop("ResourceAttributes")
    assert parse_trace_span(row, evidence_id="evidence") is None


def test_service_falls_back_to_resource_and_direct_value_wins() -> None:
    row = _row()
    row["ServiceName"] = ""
    observation = parse_trace_span(row, evidence_id="evidence")
    assert observation is not None
    assert observation.service == "resource"
    row["ServiceName"] = "frontend"
    observation = parse_trace_span(row, evidence_id="evidence")
    assert observation is not None
    assert observation.service == "frontend"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("StartTime", "2025-01-01T00:00:03Z", datetime(2025, 1, 1, 0, 0, 3, tzinfo=UTC)),
        ("StartTimeUnixNano", "1735689604000000000", datetime(2025, 1, 1, 0, 0, 4, tzinfo=UTC)),
    ],
)
def test_start_time_fallbacks(field: str, value: object, expected: datetime) -> None:
    row = _row()
    row.pop("Timestamp")
    row[field] = value
    observation = parse_trace_span(row, evidence_id="evidence")
    assert observation is not None
    assert observation.start_at == expected


def test_invalid_start_is_skipped_and_end_is_never_derived_from_duration() -> None:
    row = _row()
    row["Timestamp"] = "invalid"
    assert parse_trace_span(row, evidence_id="evidence") is None
    row = _row()
    row.pop("EndTime")
    observation = parse_trace_span(row, evidence_id="evidence")
    assert observation is not None
    assert observation.end_at is None


@pytest.mark.parametrize("value", ["", "nan", "inf", "abc"])
def test_invalid_duration_is_none(value: str) -> None:
    row = _row()
    row["Duration"] = value
    observation = parse_trace_span(row, evidence_id="evidence")
    assert observation is not None
    assert observation.duration_raw is None


def test_shared_status_normalization_matches_snapshot_backend() -> None:
    record = {"StatusCode": "2"}
    status, reason = normalize_trace_status(record)
    backend_value, backend_reason = backend_status(record)
    assert (status.value, reason) == (backend_value, backend_reason)
