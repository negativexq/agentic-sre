"""The injector journal (testbed contract §4.4): every API call the injector makes, appended once.

The injector-sourced timeline fields are derived from it; nothing is back-filled. Each entry is
flushed and synced before the call's result is used, so a crash cannot leave a call unrecorded.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

ROLE_CAUSE_CREATED = "cause_created"
ROLE_EXECUTION_OBSERVED = "execution_observed"
ROLE_ALERT_OBSERVED = "alert_observed"
ROLE_CAUSE_REMOVED = "cause_removed"


class JournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    at: datetime
    verb: str
    object: str
    ok: bool
    response: str = ""
    uid: str | None = None
    resource_version: str | None = None
    role: str | None = None
    payload: dict[str, Any] = {}

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("journal instants must be timezone-aware")
        return value.astimezone(UTC)


class InjectorJournal:
    """Append-only JSON lines with microsecond UTC instants."""

    def __init__(
        self, path: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.path = path
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = len(self.entries())

    def record(
        self,
        *,
        verb: str,
        object: str,  # noqa: A002 - the journal's own field name
        ok: bool = True,
        response: str = "",
        uid: str | None = None,
        resource_version: str | None = None,
        role: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> JournalEntry:
        self._seq += 1
        entry = JournalEntry(
            seq=self._seq,
            at=self._clock(),
            verb=verb,
            object=object,
            ok=ok,
            response=response[:2000],
            uid=uid,
            resource_version=resource_version,
            role=role,
            payload=payload or {},
        )
        line = json.dumps(entry.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def entries(self) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        return [
            JournalEntry.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def first(self, role: str) -> JournalEntry | None:
        return next((e for e in self.entries() if e.role == role and e.ok), None)


def parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def injector_stamps(entries: Sequence[JournalEntry]) -> dict[str, datetime]:
    """The three injector-sourced fields, from the journal only.

    ``cause_created_at`` is when the creating API call returned; ``execution_started_at`` is the
    controller's own first-applied instant that the injector observed; ``alert_fired_at`` is the
    ``startsAt`` Alertmanager reported. A missing observation leaves the field out.
    """
    stamps: dict[str, datetime] = {}
    for entry in entries:
        if not entry.ok or entry.role is None:
            continue
        if entry.role == ROLE_CAUSE_CREATED and "cause_created_at" not in stamps:
            stamps["cause_created_at"] = entry.at
        elif entry.role == ROLE_EXECUTION_OBSERVED and "execution_started_at" not in stamps:
            at = parse_instant(entry.payload.get("applied_at"))
            if at is not None:
                stamps["execution_started_at"] = at
        elif entry.role == ROLE_ALERT_OBSERVED and "alert_fired_at" not in stamps:
            at = parse_instant(entry.payload.get("starts_at"))
            if at is not None:
                stamps["alert_fired_at"] = at
    return stamps
