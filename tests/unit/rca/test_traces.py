from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.rca.model import TraceSpanObservation, TraceSpanStatus
from packages.rca.traces import normalize_trace_status, parse_trace_mapping


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"status.code": "0"}, TraceSpanStatus.UNSET),
        ({"status.code": "1"}, TraceSpanStatus.OK),
        ({"status.code": "2"}, TraceSpanStatus.ERROR),
        ({"StatusCode": "ok"}, TraceSpanStatus.OK),
        ({"StatusCode": "success"}, TraceSpanStatus.OK),
        ({"StatusCode": "failed"}, TraceSpanStatus.ERROR),
        ({"StatusCode": "unknown"}, TraceSpanStatus.UNSET),
        ({"StatusCode": "weird"}, TraceSpanStatus.UNKNOWN),
        ({"StatusCode": "unknown", "exception.type": "Timeout"}, TraceSpanStatus.ERROR),
        ({"StatusCode": "ok", "exception.type": "Timeout"}, TraceSpanStatus.OK),
    ],
)
def test_normalize_trace_status(record: dict[str, object], expected: TraceSpanStatus) -> None:
    status, _reason = normalize_trace_status(record)
    assert status is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"x": 1}, {"x": 1}),
        ('{"x": 1}', {"x": 1}),
        ("{'x': 1}", {"x": 1}),
        ("", {}),
        ("not a mapping", {}),
        ("[1, 2]", {}),
        (42, {}),
        (None, {}),
    ],
)
def test_parse_trace_mapping(value: object, expected: dict[str, object]) -> None:
    assert parse_trace_mapping(value) == expected


def test_trace_observation_is_frozen_and_validates_required_fields() -> None:
    observation = TraceSpanObservation(
        trace_id="trace",
        span_id="span",
        service="checkout",
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        evidence_id="otel_traces_raw.tsv:0",
    )
    with pytest.raises(ValidationError):
        observation.service = "other"
    with pytest.raises(ValidationError):
        TraceSpanObservation(
            trace_id="",
            span_id="span",
            service="checkout",
            start_at=observation.start_at,
            evidence_id="evidence",
        )
