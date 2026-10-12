"""One shared, first-come-first-served budget for the capture's Tempo reads (live-trace-design.md §14.3)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from packages.rca.live import TraceCapture, TraceServiceCapture, capture_traces

logger = logging.getLogger(__name__)

TRACE_READ_SLOTS = 2
TRACE_READ_ADMISSION_WAIT_SECONDS = 120.0
ADMISSION_TIMEOUT = "TraceReadAdmissionTimeout"

SliceRead = tuple[str, str, datetime, datetime]  # namespace, service, start, stop


class FifoSlots:
    """A counting gate that admits waiters strictly in arrival order.

    ``threading.Semaphore`` promises no order, so a waiter could be passed indefinitely. Here a waiter is
    admitted only at the head of the queue, and one that times out leaves the queue without taking a slot.
    """

    def __init__(self, slots: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._condition = threading.Condition()
        self._free = slots
        self._queue: deque[object] = deque()
        self._clock = clock
        self.in_flight = 0
        self.max_in_flight = 0

    def admit(self, timeout: float) -> float | None:
        """Seconds waited once admitted, or ``None`` if not admitted within ``timeout``."""
        ticket = object()
        with self._condition:
            started = self._clock()
            self._queue.append(ticket)
            try:
                while not (self._queue[0] is ticket and self._free > 0):
                    remaining = started + timeout - self._clock()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
                self._queue.popleft()
                self._free -= 1
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                return self._clock() - started
            finally:
                if ticket in self._queue:  # timed out or interrupted: leave without a slot
                    self._queue.remove(ticket)
                self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            self._free += 1
            self.in_flight -= 1
            self._condition.notify_all()

    @property
    def waiting(self) -> int:
        with self._condition:
            return len(self._queue)


def read_slices(
    reader: Any,
    reads: Sequence[SliceRead],
    slots: FifoSlots,
    *,
    wait: float = TRACE_READ_ADMISSION_WAIT_SECONDS,
    capture: Callable[..., TraceCapture] = capture_traces,
) -> list[TraceCapture]:
    """One capture's reads, one slice at a time per slot, two at a time (§11), through the shared ``slots`` (§14.3).

    A slice's services are read together (live-trace-design.md §15): each keeps its own search and read, and a
    trace several of them found is fetched once. A slice not admitted within ``wait`` is not sent. Each of its
    reads is ``FAILED`` with ``TraceReadAdmissionTimeout``: coverage, never absence, and distinct from every backend
    failure.
    """
    slices: dict[tuple[datetime, datetime], list[tuple[str, str]]] = {}
    for namespace, service, start, stop in reads:
        slices.setdefault((start, stop), []).append((namespace, service))
    waits: list[float] = []

    def read(item: tuple[tuple[datetime, datetime], list[tuple[str, str]]]) -> TraceCapture:
        (start, stop), services = item
        waited = slots.admit(wait)
        if waited is None:
            return TraceCapture(
                (),
                tuple(
                    TraceServiceCapture(
                        service, namespace, start, stop, "FAILED", 0, ADMISSION_TIMEOUT
                    )
                    for namespace, service in services
                ),
            )
        waits.append(waited)
        try:
            return capture(reader, services, start, stop)
        finally:
            slots.release()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(read, slices.items()))
    timed_out = sum(r.error == ADMISSION_TIMEOUT for result in results for r in result.reads)
    logger.info(
        "trace capture: %d reads in %d slices, admission wait total %.1fs max %.1fs, %d not admitted",
        len(reads),
        len(slices),
        sum(waits),
        max(waits, default=0.0),
        timed_out,
    )
    return results
