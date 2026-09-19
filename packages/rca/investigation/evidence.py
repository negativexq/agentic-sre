"""First-class raw evidence storage and bounded source overlays."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    InvestigationObservation,
    LogRecord,
    ObjectVersion,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)
from packages.rca.source import ObservationSource

type EvidenceRecord = (
    ObjectVersion
    | ClusterEvent
    | LogRecord
    | ResourcePressure
    | TrafficObservation
    | TraceSpanObservation
)


@dataclass(frozen=True)
class EvidenceAppendResult:
    """The stable result of appending a batch of raw evidence."""

    new_refs: tuple[str, ...]
    existing_refs: tuple[str, ...]


def _record_id(record: EvidenceRecord) -> str:
    return record.evidence_id


def _canonical_record(record: EvidenceRecord) -> str:
    payload = {
        "record_type": f"{type(record).__module__}.{type(record).__qualname__}",
        "record": record.model_dump(mode="json"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class InMemoryEvidenceStore:
    """An integrity-checked, process-local store keyed by evidence ID."""

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}

    def contains(self, evidence_id: str) -> bool:
        return evidence_id in self._records

    def get(self, evidence_id: str) -> EvidenceRecord:
        return self._records[evidence_id]

    def get_many(self, evidence_ids: Sequence[str]) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records[evidence_id] for evidence_id in evidence_ids)

    def put(self, record: EvidenceRecord) -> bool:
        evidence_id = _record_id(record)
        existing = self._records.get(evidence_id)
        if existing is None:
            self._records[evidence_id] = record
            return True
        if _canonical_record(existing) != _canonical_record(record):
            raise ValueError(f"evidence ID collision for {evidence_id}")
        return False

    def put_many(self, records: Sequence[EvidenceRecord]) -> EvidenceAppendResult:
        """Validate a batch completely before mutating the store."""
        pending: dict[str, EvidenceRecord] = {}
        new_refs: list[str] = []
        existing_refs: list[str] = []
        for record in records:
            evidence_id = _record_id(record)
            prior = pending.get(evidence_id, self._records.get(evidence_id))
            if prior is not None:
                if _canonical_record(prior) != _canonical_record(record):
                    raise ValueError(f"evidence ID collision for {evidence_id}")
                existing_refs.append(evidence_id)
                continue
            pending[evidence_id] = record
            new_refs.append(evidence_id)
        self._records.update(pending)
        return EvidenceAppendResult(tuple(new_refs), tuple(existing_refs))

    def refs(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))


_PAYLOAD_KEYS: tuple[tuple[str, str, type[EvidenceRecord]], ...] = (
    ("history", "versions", ObjectVersion),
    ("events", "events", ClusterEvent),
    ("logs", "logs", LogRecord),
    ("resource_pressure", "resource_pressure", ResourcePressure),
    ("traffic", "traffic", TrafficObservation),
)


def records_from_observation(
    observation: InvestigationObservation,
) -> tuple[EvidenceRecord, ...]:
    """Materialize only known first-party raw-record payloads."""
    payload = observation.payload
    records: list[EvidenceRecord] = []
    for capability, key, model in _PAYLOAD_KEYS:
        if observation.capability != capability or key not in payload:
            continue
        raw = payload[key]
        if not isinstance(raw, list):
            raise ValueError(f"{capability} payload {key!r} must be a list")
        records.extend(model.model_validate(item) for item in raw)
    if "traces" in payload:
        raw_traces = payload["traces"]
        if not isinstance(raw_traces, list):
            raise ValueError("trace payload 'traces' must be a list")
        records.extend(TraceSpanObservation.model_validate(item) for item in raw_traces)
    return tuple(records)


def _record_time(record: EvidenceRecord) -> datetime | None:
    if isinstance(record, ObjectVersion):
        return record.observed_at
    if isinstance(record, ClusterEvent):
        return record.last_at or record.first_at
    if isinstance(record, LogRecord):
        return record.at
    if isinstance(record, ResourcePressure):
        return record.at
    if isinstance(record, TrafficObservation):
        return record.at
    return record.start_at


def _before_cutoff(record: EvidenceRecord, cutoff: datetime | None) -> bool:
    at = _record_time(record)
    return cutoff is None or at is None or at <= cutoff


def _time_key(at: datetime | None) -> tuple[str, str]:
    return ("0", "") if at is None else ("1", at.astimezone(UTC).isoformat())


_RecordT = TypeVar("_RecordT", bound=EvidenceRecord)


@dataclass(frozen=True)
class OverlayObservationSource:
    """A bounded source plus exactly the explicitly acquired raw records."""

    base: ObservationSource
    store: InMemoryEvidenceStore
    acquired_evidence_refs: tuple[str, ...]
    supported_capabilities: frozenset[str]
    initial_observation_bounded: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "initial_observation_bounded",
            bool(getattr(self.base, "initial_observation_bounded", False)),
        )

    def incident_id(self) -> str:
        return self.base.incident_id()

    def observation_cutoff(self) -> datetime | None:
        return self.base.observation_cutoff()

    def alerts(self) -> Sequence[Alert]:
        return self.base.alerts()

    def supports(self, capability: str) -> bool:
        return capability in self.supported_capabilities

    def _acquired(self, model: type[_RecordT]) -> tuple[_RecordT, ...]:
        return tuple(
            record
            for record in self.store.get_many(self.acquired_evidence_refs)
            if isinstance(record, model) and _before_cutoff(record, self.observation_cutoff())
        )

    @staticmethod
    def _merge_records(
        base_records: Sequence[_RecordT],
        acquired_records: Sequence[_RecordT],
        *,
        key: Callable[[_RecordT], tuple[str, ...]],
    ) -> tuple[_RecordT, ...]:
        merged: dict[str, _RecordT] = {}
        for record in (*base_records, *acquired_records):
            merged.setdefault(record.evidence_id, record)
        return tuple(sorted(merged.values(), key=key))

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        result: dict[EntityRef, list[ObjectVersion]] = {
            entity: list(versions) for entity, versions in self.base.object_history().items()
        }
        for version in self._acquired(ObjectVersion):
            result.setdefault(version.entity, []).append(version)
        return {
            entity: tuple(
                sorted(
                    {version.evidence_id: version for version in versions}.values(),
                    key=lambda item: (item.observed_at, item.evidence_id),
                )
            )
            for entity, versions in result.items()
        }

    def events(self) -> tuple[ClusterEvent, ...]:
        return self._merge_records(
            self.base.events(),
            self._acquired(ClusterEvent),
            key=lambda item: (*_time_key(item.last_at or item.first_at), item.evidence_id),
        )

    def error_logs(self) -> tuple[LogRecord, ...]:
        return self._merge_records(
            self.base.error_logs(),
            self._acquired(LogRecord),
            key=lambda item: (*_time_key(item.at), item.evidence_id),
        )

    def logs(self, service: str, *, limit: int = 20) -> tuple[dict[str, object], ...]:
        base_items = tuple(self.base.logs(service, limit=limit))
        acquired = [
            {
                "timestamp": record.at.isoformat() if record.at is not None else None,
                "severity": record.severity,
                "body": record.message,
                "evidence_id": record.evidence_id,
            }
            for record in self._acquired(LogRecord)
            if record.service == service
        ]
        if not acquired:
            return base_items
        merged = [*base_items, *acquired]
        return tuple(
            sorted(
                merged,
                key=lambda item: (
                    str(item.get("timestamp") or ""),
                    str(item.get("evidence_id") or ""),
                ),
            )[:limit]
        )

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> tuple[ResourcePressure, ...]:
        acquired = tuple(
            record
            for record in self._acquired(ResourcePressure)
            if record.pod in pods and record.at is not None and record.at >= since
        )
        return self._merge_records(
            self.base.resource_pressure(pods, since),
            acquired,
            key=lambda item: (*_time_key(item.at), item.evidence_id),
        )

    def traffic_observations(self) -> tuple[TrafficObservation, ...]:
        return self._merge_records(
            self.base.traffic_observations(),
            self._acquired(TrafficObservation),
            key=lambda item: (*_time_key(item.at), item.evidence_id),
        )

    def trace_observations(self) -> tuple[TraceSpanObservation, ...]:
        return self._merge_records(
            self.base.trace_observations(),
            self._acquired(TraceSpanObservation),
            key=lambda item: (
                *_time_key(item.start_at),
                item.trace_id,
                item.span_id,
                item.evidence_id,
            ),
        )


def visible_evidence_refs(source: ObservationSource) -> frozenset[str]:
    """Return IDs visible through source methods, without hidden-source access."""
    refs: set[str] = set()
    refs.update(
        version.evidence_id for versions in source.object_history().values() for version in versions
    )
    refs.update(event.evidence_id for event in source.events())
    refs.update(record.evidence_id for record in source.error_logs())
    refs.update(record.evidence_id for record in source.traffic_observations())
    refs.update(record.evidence_id for record in source.trace_observations())
    return frozenset(refs)


__all__ = [
    "EvidenceAppendResult",
    "EvidenceRecord",
    "InMemoryEvidenceStore",
    "OverlayObservationSource",
    "records_from_observation",
    "visible_evidence_refs",
]
