"""Single pass-through boundary for Prometheus, Loki, and Tempo reads.

This layer assigns stable query identities and caller classes. M19-3.9 will add
the durable read envelope at ``_provider_call``; until then provider responses
are returned immediately with their existing types and error behavior.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)

if TYPE_CHECKING:
    from packages.rca.investigation.tempo import TempoTraceBatch

ProviderCallerClass = Literal["CAPTURE", "ENGINE", "INVESTIGATION"]
_T = TypeVar("_T")


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


@dataclass(frozen=True)
class ProviderAdapter:
    """Pass-through provider boundary with run and caller identity."""

    run_id: str
    caller_class: ProviderCallerClass
    session_factory: Callable[[], object]
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
        query_key = provider_query_key(
            {
                "provider": "prometheus",
                "operation": "resource_pressure",
                "target": target.canonical,
                "query": query.model_dump(mode="json"),
            },
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return self._provider_call(query_key, lambda: reader.query_resource_pressure(target, query))

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
        query_key = provider_query_key(
            {
                "provider": "prometheus",
                "operation": "traffic",
                "target": target.canonical,
                "query": query.model_dump(mode="json"),
            },
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return self._provider_call(query_key, lambda: reader.query_traffic(target, query))

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
        query_key = provider_query_key(
            {
                "provider": "tempo",
                "operation": "query",
                "target": target.canonical,
                "query": query.model_dump(mode="json"),
            },
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return self._provider_call(query_key, lambda: reader.query(target, query))

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
        reader = self.readers.loki
        if reader is None:
            raise RuntimeError("Loki reader is not configured")
        query_key = provider_query_key(
            {
                "provider": "loki",
                "operation": "error_logs",
                "services": sorted(services),
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "limit": limit,
            },
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        if limit is None:
            return self._provider_call(
                query_key, lambda: reader.error_logs(services, starts_at, ends_at)
            )
        return self._provider_call(
            query_key,
            lambda: reader.error_logs(services, starts_at, ends_at, limit=limit),
        )

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

    def _provider_call(self, query_key: str, call: Callable[[], _T]) -> _T:
        # M19-3.9 adds the persisted read envelope here. In M19-3.8 the key is
        # derived, but repeated calls are still invoked independently.
        del query_key
        return call()


__all__ = [
    "ProviderAdapter",
    "ProviderCallerClass",
    "ProviderReaders",
    "provider_query_key",
]
