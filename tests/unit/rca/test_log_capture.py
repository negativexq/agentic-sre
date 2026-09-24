"""Bounded capture of the incident log window (Loki window reconciliation)."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.rca.live import (
    LOKI_MAX_QUERY_SPAN,
    LokiLogReader,
    bounded_slices,
    capture_error_logs,
)
from packages.rca.model import LogRecord

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _record(minute: int, message: str = "timeout") -> LogRecord:
    return LogRecord(
        service="order-service",
        at=T0 + timedelta(minutes=minute),
        severity="ERROR",
        message=message,
        evidence_id=f"loki:order-service:{minute}:0",
    )


class WindowReader:
    """Returns the fixed records that fall inside each requested [start, end)."""

    def __init__(self, records: list[LogRecord], fail_on: set[int] | None = None) -> None:
        self.records = records
        self.fail_on = fail_on or set()
        self.windows: list[tuple[datetime, datetime]] = []

    def error_logs(
        self, services: Sequence[str], starts_at: datetime, ends_at: datetime
    ) -> list[LogRecord]:
        del services
        index = len(self.windows)
        self.windows.append((starts_at, ends_at))
        if index in self.fail_on:
            raise ConnectionError("loki refused")
        return [r for r in self.records if r.at is not None and starts_at <= r.at < ends_at]


def test_slices_are_newest_first_contiguous_and_bounded() -> None:
    start, end = T0, T0 + timedelta(hours=2, minutes=5)
    slices = bounded_slices(start, end)
    assert len(slices) == 3
    assert slices[0][1] == end and slices[-1][0] == start
    assert all(b - a <= LOKI_MAX_QUERY_SPAN for a, b in slices)
    for newer, older in zip(slices, slices[1:], strict=False):
        assert older[1] == newer[0]


def test_exact_two_hours_is_two_slices_and_empty_window_is_none() -> None:
    assert len(bounded_slices(T0, T0 + timedelta(hours=2))) == 2
    assert bounded_slices(T0, T0) == ()
    with pytest.raises(ValueError):
        bounded_slices(T0 + timedelta(minutes=1), T0)


def test_capture_reads_the_whole_window_and_keeps_every_slice_record() -> None:
    reader = WindowReader([_record(10), _record(70), _record(115)])
    capture = capture_error_logs(reader, ["order-service"], T0, T0 + timedelta(hours=2))
    assert capture.succeeded and not capture.failed and not capture.skipped
    assert len(capture.queried) == 2
    # Newest first, like a single backward Loki query.
    assert [r.at for r in capture.records] == [
        T0 + timedelta(minutes=115),
        T0 + timedelta(minutes=70),
        T0 + timedelta(minutes=10),
    ]


def test_one_failed_slice_is_recorded_and_does_not_drop_the_others() -> None:
    reader = WindowReader([_record(10), _record(70), _record(115)], fail_on={0})
    capture = capture_error_logs(reader, ["order-service"], T0, T0 + timedelta(hours=2))
    assert capture.succeeded
    assert [(f.error_type, f.ends_at) for f in capture.failed] == [
        ("ConnectionError", T0 + timedelta(hours=2))
    ]
    assert [r.at for r in capture.records] == [T0 + timedelta(minutes=10)]


def test_all_slices_failing_is_not_success() -> None:
    reader = WindowReader([_record(10)], fail_on={0, 1})
    capture = capture_error_logs(reader, ["order-service"], T0, T0 + timedelta(hours=2))
    assert not capture.succeeded
    assert capture.records == () and len(capture.failed) == 2


def test_record_budget_stops_older_reads_and_reports_them_skipped() -> None:
    reader = WindowReader([_record(100), _record(110)])
    capture = capture_error_logs(
        reader, ["order-service"], T0, T0 + timedelta(hours=2), max_records=2
    )
    assert len(reader.windows) == 1
    assert capture.skipped == ((T0, T0 + timedelta(hours=1)),)
    assert len(capture.records) == 2


def test_slice_budget_reports_older_slices_skipped() -> None:
    reader = WindowReader([])
    capture = capture_error_logs(
        reader, ["order-service"], T0, T0 + timedelta(hours=5), max_slices=3
    )
    assert len(reader.windows) == 3
    assert len(capture.skipped) == 2
    assert capture.skipped[-1][0] == T0


def test_identical_records_from_several_slices_are_kept_once() -> None:
    class SameAnswer:
        def error_logs(
            self, services: Sequence[str], starts_at: datetime, ends_at: datetime
        ) -> list[LogRecord]:
            del services, starts_at, ends_at
            return [_record(30)]

    capture = capture_error_logs(SameAnswer(), ["order-service"], T0, T0 + timedelta(hours=2))
    assert len(capture.records) == 1


def test_skewed_window_reads_nothing_and_never_raises() -> None:
    reader = WindowReader([_record(10)])
    capture = capture_error_logs(reader, ["order-service"], T0 + timedelta(hours=1), T0)
    assert reader.windows == [] and not capture.succeeded and not capture.failed


def test_real_reader_bound_holds_and_capture_stays_within_it() -> None:
    spans: list[int] = []

    def opener(request: Any, timeout: float) -> io.BytesIO:
        del timeout
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(request.full_url).query)
        spans.append(int(query["end"][0]) - int(query["start"][0]))
        return io.BytesIO(json.dumps({"data": {"result": []}}).encode())

    reader = LokiLogReader("http://loki:3100", opener=opener)
    with pytest.raises(ValueError):
        reader.error_logs(["order-service"], T0, T0 + timedelta(hours=2))
    capture = capture_error_logs(reader, ["order-service"], T0, T0 + timedelta(hours=2))
    assert capture.succeeded and not capture.failed
    assert spans and max(spans) <= int(LOKI_MAX_QUERY_SPAN.total_seconds() * 1e9)
