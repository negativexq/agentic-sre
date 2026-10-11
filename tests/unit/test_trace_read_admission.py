"""live-trace-design §14.3 P1: every incident's capture reads share two slots, first come first served."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from apps.control_plane.trace_admission import (
    ADMISSION_TIMEOUT,
    FifoSlots,
    SliceRead,
    read_slices,
)
from packages.rca.live import TraceCapture, TraceServiceCapture

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LATENCY = 0.02


class _Reads:
    """A fake capture read with fixed latency that records how many reads overlap and when."""

    def __init__(self, *, error: str = "") -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.error = error
        self.started: dict[str, float] = {}

    def __call__(
        self, reader: Any, services: list[tuple[str, str]], start: datetime, stop: datetime
    ) -> TraceCapture:
        ((namespace, service),) = services
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.started.setdefault(namespace, time.monotonic())
        time.sleep(LATENCY)
        with self._lock:
            self.in_flight -= 1
        completeness = "FAILED" if self.error else "BEST_EFFORT"
        return TraceCapture(
            (), (TraceServiceCapture(service, namespace, start, stop, completeness, 0, self.error),)
        )


def _incident_reads(incident: int, n: int = 6) -> list[SliceRead]:
    return [
        (
            f"incident-{incident}",
            f"svc-{i % 3}",
            T0 + timedelta(minutes=i),
            T0 + timedelta(minutes=i + 1),
        )
        for i in range(n)
    ]


@pytest.mark.parametrize("incidents", [5, 10])
def test_concurrent_incidents_share_two_slots_and_interleave(incidents: int) -> None:
    slots = FifoSlots(2)
    reads = _Reads()
    finished: dict[int, float] = {}
    results: dict[int, list[TraceCapture]] = {}

    def capture(incident: int) -> None:
        results[incident] = read_slices(None, _incident_reads(incident), slots, capture=reads)
        finished[incident] = time.monotonic()

    began = time.monotonic()
    threads = [threading.Thread(target=capture, args=(i,)) for i in range(incidents)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    elapsed = time.monotonic() - began

    total = incidents * 6
    assert reads.max_in_flight <= 2 and slots.max_in_flight <= 2
    assert all(
        r.completeness == "BEST_EFFORT"
        for result in results.values()
        for c in result
        for r in c.reads
    )
    assert sum(len(result) for result in results.values()) == total
    # two at a time: no faster than total / 2 reads, and not much slower
    assert total * LATENCY / 2 <= elapsed <= total * LATENCY / 2 + LATENCY + 1.0
    # every incident's first read starts before any incident's capture has finished whole
    assert max(reads.started.values()) < min(finished.values())
    assert slots.in_flight == 0 and slots.waiting == 0


def test_waiters_are_admitted_in_arrival_order() -> None:
    slots = FifoSlots(1)
    assert slots.admit(1.0) is not None
    order: list[int] = []

    def waiter(index: int) -> None:
        assert slots.admit(5.0) is not None
        order.append(index)
        slots.release()

    threads = []
    for index in range(6):
        thread = threading.Thread(target=waiter, args=(index,))
        thread.start()
        threads.append(thread)
        while slots.waiting < index + 1:
            time.sleep(0.001)
    slots.release()
    for thread in threads:
        thread.join(5)
    assert order == list(range(6))


def test_a_read_not_admitted_in_time_is_failed_coverage_and_leaves_no_slot_taken() -> None:
    slots = FifoSlots(2)
    assert slots.admit(1.0) is not None and slots.admit(1.0) is not None
    reads = _Reads()
    results = read_slices(None, _incident_reads(0, 3), slots, wait=0.05, capture=reads)
    failed = [r for result in results for r in result.reads]
    assert [(r.completeness, r.error) for r in failed] == [("FAILED", ADMISSION_TIMEOUT)] * 3
    assert reads.started == {}  # nothing was sent
    assert slots.waiting == 0
    slots.release()
    slots.release()
    # both slots are free again: the timed-out waiters took none
    assert slots.admit(0.0) is not None and slots.admit(0.0) is not None


def test_a_backend_failure_is_not_an_admission_timeout() -> None:
    results = read_slices(
        None, _incident_reads(0, 2), FifoSlots(2), capture=_Reads(error="TempoHttpError")
    )
    assert {r.error for result in results for r in result.reads} == {"TempoHttpError"}


def test_a_read_that_raises_releases_its_slot() -> None:
    slots = FifoSlots(2)

    def broken(*args: object) -> TraceCapture:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        read_slices(None, _incident_reads(0, 4), slots, capture=broken)
    assert slots.in_flight == 0 and slots.waiting == 0
    assert slots.admit(0.0) is not None and slots.admit(0.0) is not None
