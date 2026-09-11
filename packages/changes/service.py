"""Change normalization without causal inference."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from packages.contracts import ChangeRecord, ChangeType


class ChangeService:
    """Record and query captured resource changes."""

    def __init__(self) -> None:
        self._records: list[ChangeRecord] = []

    def capture(
        self,
        *,
        timestamp: datetime,
        resource_type: str,
        resource_name: str,
        change_type: ChangeType,
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
            before=before,
            after=after,
            revision=revision,
            source=source,
        )
        self._records.append(record)
        return record

    def recent(self, *, limit: int = 100) -> Iterable[ChangeRecord]:
        """Return the latest records in deterministic reverse time order."""
        if limit < 1:
            raise ValueError("limit must be positive")
        return sorted(
            self._records, key=lambda item: (item.timestamp, str(item.change_id)), reverse=True
        )[:limit]
