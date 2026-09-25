"""Single durable boundary for Prometheus, Loki, and Tempo reads."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from typing import Any, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy.orm import Session

from packages.rca.investigation.tempo import TempoTraceBatch
from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)

ProviderCallerClass = Literal["CAPTURE", "ENGINE", "INVESTIGATION"]
_T = TypeVar("_T")


class ProviderReadPersistenceError(RuntimeError):
    """A provider succeeded but its successful response could not be taped."""


class PrometheusReader(Protocol):
    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]: ...

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]: ...


class LokiReader(Protocol):
    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]: ...


class TempoReader(Protocol):
    def query(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch: ...


@dataclass(frozen=True)
class ProviderReaders:
    """Configured provider implementations owned by ``ProviderAdapter``."""

    prometheus: PrometheusReader | None = None
    loki: LokiReader | None = None
    tempo: TempoReader | None = None

    @classmethod
    def from_environment(cls) -> ProviderReaders:
        """Construct configured readers inside the provider boundary."""
        from packages.rca.investigation.prometheus import PrometheusConfig, PrometheusMetricsReader
        from packages.rca.investigation.tempo import TempoConfig, TempoTraceReader
        from packages.rca.live import LokiLogReader

        prometheus_config = PrometheusConfig.from_environment()
        tempo_config = TempoConfig.from_environment()
        loki_url = os.getenv("SRE_LOKI_URL")
        return cls(
            prometheus=(
                PrometheusMetricsReader(prometheus_config)
                if prometheus_config is not None
                else None
            ),
            loki=LokiLogReader(loki_url) if loki_url else None,
            tempo=TempoTraceReader(tempo_config) if tempo_config is not None else None,
        )

    @classmethod
    def from_urls(
        cls,
        *,
        prometheus_url: str | None = None,
        loki_url: str | None = None,
        tempo_url: str | None = None,
    ) -> ProviderReaders:
        """Construct explicitly configured readers inside the provider boundary."""
        from packages.rca.investigation.prometheus import PrometheusConfig, PrometheusMetricsReader
        from packages.rca.investigation.tempo import TempoConfig, TempoTraceReader
        from packages.rca.live import LokiLogReader

        return cls(
            prometheus=(
                PrometheusMetricsReader(PrometheusConfig(prometheus_url))
                if prometheus_url is not None
                else None
            ),
            loki=LokiLogReader(loki_url) if loki_url is not None else None,
            tempo=TempoTraceReader(TempoConfig(tempo_url)) if tempo_url is not None else None,
        )


def provider_query_key(
    descriptor: Mapping[str, object],
    *,
    descriptor_id: str | None = None,
    observation_identity: str | None = None,
) -> str:
    """Choose an existing identity first, otherwise hash canonical JSON."""
    if descriptor_id is not None:
        return descriptor_id
    if observation_identity is not None:
        return observation_identity
    canonical = json.dumps(
        descriptor,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    """Normalize known response values into deterministic JSON primitives."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("provider observation mapping keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported provider observation value: {type(value).__name__}")


def _evidence_ids(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, BaseModel):
            evidence_id = getattr(item, "evidence_id", None)
            if isinstance(evidence_id, str) and evidence_id not in found:
                found.append(evidence_id)
            for name in type(item).model_fields:
                visit(getattr(item, name))
        elif is_dataclass(item) and not isinstance(item, type):
            for field in fields(item):
                visit(getattr(item, field.name))
        elif isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (tuple, list)):
            for nested in item:
                visit(nested)

    visit(value)
    return found


@dataclass(frozen=True)
class ProviderAdapter:
    """Pass-through provider boundary with run and caller identity."""

    run_id: str
    caller_class: ProviderCallerClass
    session_factory: Callable[[], Session]
    readers: ProviderReaders

    def for_caller(self, caller_class: ProviderCallerClass) -> ProviderAdapter:
        """Share configured readers while assigning a caller class for one path."""
        return ProviderAdapter(self.run_id, caller_class, self.session_factory, self.readers)

    def supports(self, capability: str) -> bool:
        if capability in {"resource_pressure", "traffic"}:
            return self.readers.prometheus is not None
        if capability == "runtime_traces":
            return self.readers.tempo is not None
        if capability == "logs":
            return self.readers.loki is not None
        return False

    def query_resource_pressure(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[ResourcePressure, ...]:
        reader = self.readers.prometheus
        if reader is None:
            raise RuntimeError("Prometheus reader is not configured")
        descriptor = {
            "provider": "prometheus",
            "operation": "resource_pressure",
            "target": target.canonical,
            "query": query.model_dump(mode="json"),
        }
        query_key = provider_query_key(
            descriptor,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return cast(
            tuple[ResourcePressure, ...],
            self._provider_call(
                capability="resource_pressure",
                query_key=query_key,
                descriptor=descriptor,
                call=lambda: reader.query_resource_pressure(target, query),
            ),
        )

    def query_traffic(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[TrafficObservation, ...]:
        reader = self.readers.prometheus
        if reader is None:
            raise RuntimeError("Prometheus reader is not configured")
        descriptor = {
            "provider": "prometheus",
            "operation": "traffic",
            "target": target.canonical,
            "query": query.model_dump(mode="json"),
        }
        query_key = provider_query_key(
            descriptor,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return cast(
            tuple[TrafficObservation, ...],
            self._provider_call(
                capability="traffic",
                query_key=query_key,
                descriptor=descriptor,
                call=lambda: reader.query_traffic(target, query),
            ),
        )

    def query_tempo(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch:
        reader = self.readers.tempo
        if reader is None:
            raise RuntimeError("Tempo reader is not configured")
        descriptor = {
            "provider": "tempo",
            "operation": "query",
            "target": target.canonical,
            "query": query.model_dump(mode="json"),
        }
        query_key = provider_query_key(
            descriptor,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return cast(
            tuple[TraceSpanObservation, ...] | TempoTraceBatch,
            self._provider_call(
                capability="tempo_traces",
                query_key=query_key,
                descriptor=descriptor,
                call=lambda: reader.query(target, query),
            ),
        )

    def query_loki(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> list[LogRecord]:
        records, _read_id = self.query_loki_with_read_id(
            services,
            starts_at,
            ends_at,
            limit=limit,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return records

    def query_loki_with_read_id(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[list[LogRecord], int]:
        """Return capture records with the read row committed before return."""
        reader = self.readers.loki
        if reader is None:
            raise RuntimeError("Loki reader is not configured")
        descriptor = {
            "provider": "loki",
            "operation": "error_logs",
            "services": sorted(services),
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "limit": limit,
        }
        query_key = provider_query_key(
            descriptor,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        if limit is None:

            def call() -> list[LogRecord]:
                return reader.error_logs(services, starts_at, ends_at)
        else:

            def call() -> list[LogRecord]:
                return reader.error_logs(services, starts_at, ends_at, limit=limit)

        records, read_id = cast(
            tuple[list[LogRecord], int],
            self._provider_call(
                capability="loki_logs",
                query_key=query_key,
                descriptor=descriptor,
                call=call,
                include_read_id=True,
            ),
        )
        return records, read_id

    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]:
        """LogReader-compatible capture method routed through this adapter."""
        return self.query_loki(services, starts_at, ends_at, limit=limit)

    def error_logs_with_read_id(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> tuple[list[LogRecord], int]:
        return self.query_loki_with_read_id(services, starts_at, ends_at, limit=limit)

    def _provider_call(
        self,
        *,
        capability: str,
        query_key: str,
        descriptor: dict[str, Any],
        call: Callable[[], _T],
        include_read_id: bool = False,
    ) -> _T | tuple[_T, int]:
        started_at = datetime.now(UTC)
        result = call()
        observation = _json_value(result)
        finished_at = datetime.now(UTC)
        from packages.storage.repositories import InvestigationReadRepository

        try:
            with self.session_factory() as session:
                read_id = InvestigationReadRepository(session).append_success(
                    run_id=self.run_id,
                    caller_class=self.caller_class,
                    capability=capability,
                    query_key=query_key,
                    query_descriptor=descriptor,
                    started_at=started_at,
                    finished_at=finished_at,
                    observation=observation,
                    evidence_ids=_evidence_ids(result),
                )
        except Exception as error:
            raise ProviderReadPersistenceError(
                f"successful {capability} read could not be persisted"
            ) from error
        if include_read_id:
            return result, read_id
        return result


__all__ = [
    "ProviderAdapter",
    "ProviderCallerClass",
    "ProviderReaders",
    "ProviderReadPersistenceError",
    "provider_query_key",
]
