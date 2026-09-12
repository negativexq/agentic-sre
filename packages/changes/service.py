"""Change normalization without causal inference."""

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from packages.contracts import ChangeRecord, ChangeScope, ChangeType


class ChangeService:
    """Record and query captured resource changes."""

    def __init__(self, persist: Callable[[ChangeRecord], None] | None = None) -> None:
        self._records: list[ChangeRecord] = []
        self._persist = persist

    def capture(
        self,
        *,
        timestamp: datetime,
        resource_type: str,
        resource_name: str,
        change_type: ChangeType,
        scope: ChangeScope = ChangeScope.DEPLOYMENT,
        before: dict[str, Any],
        after: dict[str, Any],
        revision: str,
        source: str,
    ) -> ChangeRecord:
        """Capture one normalized fact."""
        record = ChangeRecord(
            timestamp=timestamp,
            resource_type=resource_type,
            resource_name=resource_name,
            change_type=change_type,
            scope=scope,
            before=before,
            after=after,
            revision=revision,
            source=source,
        )
        self._records.append(record)
        if self._persist is not None:
            self._persist(record)
        return record

    def recent(self, *, limit: int = 100) -> Iterable[ChangeRecord]:
        """Return the latest records in deterministic reverse time order."""
        if limit < 1:
            raise ValueError("limit must be positive")
        return sorted(
            self._records, key=lambda item: (item.timestamp, str(item.change_id)), reverse=True
        )[:limit]

    def between(
        self,
        *,
        resource_name: str,
        starts_at: datetime,
        ends_at: datetime,
        limit: int = 100,
    ) -> tuple[ChangeRecord, ...]:
        """Return real captured changes in a bounded incident lookback window."""
        if limit < 1:
            raise ValueError("limit must be positive")
        return tuple(
            item
            for item in self.recent(limit=len(self._records) or 1_000_000)
            if item.resource_name == resource_name and starts_at <= item.timestamp <= ends_at
        )[:limit]
