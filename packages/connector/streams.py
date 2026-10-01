"""Connector-side stream buffers (contract §10): bounded, sequenced, epoch-scoped.

A ``StreamBuffer`` numbers items monotonically within one epoch (one Connector run). A reader
resumes with a cursor ``(epoch, seq)``; whatever the buffer can no longer prove is reported as a
``Gap`` rather than guessed. Snapshot baselines (the ``changes`` stream) are tracked here so a new
or lost reader is always served from a complete snapshot.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.connector import wire


@dataclass
class StreamBuffer:
    epoch: str
    max_len: int
    clock: Callable[[], datetime]
    _items: deque[dict[str, Any]] = field(default_factory=deque)
    _next: int = 1
    baseline_seq: int | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def next_seq(self) -> int:
        with self._lock:
            return self._next

    def append(self, item: dict[str, Any]) -> int:
        """Store one already-validated item (its ``seq`` is assigned here)."""
        with self._lock:
            seq = self._next
            self._next += 1
            self._items.append({**item, "seq": seq})
            while len(self._items) > self.max_len:
                self._items.popleft()
            return seq

    def first_seq(self) -> int:
        with self._lock:
            return int(self._items[0]["seq"]) if self._items else self._next

    def has_baseline(self) -> bool:
        with self._lock:
            return self.baseline_seq is not None and self.baseline_seq >= self.first_seq()

    def mark_baseline(self, seq: int) -> None:
        with self._lock:
            self.baseline_seq = seq

    def read(self, cursor: str | None, limit: int, *, needs_baseline: bool) -> dict[str, Any]:
        """One page: items after the cursor, the next cursor, and any gap the buffer must declare.

        ``needs_baseline`` (the ``changes`` stream) serves a fresh, restarted or expired reader from
        the latest snapshot instead of the oldest buffered item.
        """
        with self._lock:
            gaps: list[dict[str, Any]] = []
            first = self.first_seq()
            restart = expired = False
            start_after: int
            if cursor is None:
                start_after = -1
            else:
                epoch, seq = wire.parse_cursor(cursor)
                if epoch != self.epoch:
                    restart = True
                    start_after = -1
                elif seq < first - 1:
                    expired = True
                    start_after = -1
                else:
                    start_after = seq
            if start_after == -1:
                if needs_baseline:
                    if self.baseline_seq is None or self.baseline_seq < first:
                        raise LookupError("no snapshot baseline is available yet")
                    start_after = self.baseline_seq - 1
                else:
                    start_after = first - 1
            if restart or expired:
                gaps.append(
                    {
                        "kind": "gap",
                        "seq": 0,
                        "at": self.clock().isoformat(),
                        "reason": "CONNECTOR_RESTART" if restart else "BUFFER_EXPIRED",
                    }
                )
            served = [i for i in self._items if int(i["seq"]) > start_after][:limit]
            next_seq = int(served[-1]["seq"]) if served else max(start_after, first - 1)
            if not served and cursor is not None and not (restart or expired):
                next_seq = start_after
            return {
                "epoch": self.epoch,
                "items": [i for i in served if i["kind"] != "gap"],
                "gaps": [*gaps, *[i for i in served if i["kind"] == "gap"]],
                "next_cursor": wire.make_cursor(self.epoch, next_seq),
            }
