"""Single durable boundary for Prometheus, Loki, and Tempo reads."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
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
    ProviderReadFailure,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)

ProviderCallerClass = Literal["CAPTURE", "ENGINE", "INVESTIGATION"]
# Every capability name `ProviderAdapter.supports` can report, in canonical order.
PROVIDER_CAPABILITIES = ("logs", "resource_pressure", "runtime_traces", "traffic")
_T = TypeVar("_T")


class ProviderIntegrityError(RuntimeError):
    """A provider read could not be recorded or reproduced faithfully.

    It is never degraded into missing evidence: tool wrappers re-raise it.
    """


class ProviderReadPersistenceError(ProviderIntegrityError):
    """A provider response or failure could not be durably taped."""


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


class AlertRuleReader(Protocol):
    def alert_rule(
        self, alert: str, labels: Mapping[str, str], at: datetime
    ) -> dict[str, object]: ...


class ExecutionRecordReader(Protocol):
    def execution_records(
        self, namespace: str, service: str, starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, str]]: ...


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


def resource_pressure_descriptor(target: EntityRef, query: InvestigationQuery) -> dict[str, Any]:
    """Canonical descriptor of one Prometheus resource-pressure read."""
    return {
        "provider": "prometheus",
        "operation": "resource_pressure",
        "target": target.canonical,
        "query": query.model_dump(mode="json"),
    }


def traffic_descriptor(target: EntityRef, query: InvestigationQuery) -> dict[str, Any]:
    """Canonical descriptor of one Prometheus traffic read."""
    return {
        "provider": "prometheus",
        "operation": "traffic",
        "target": target.canonical,
        "query": query.model_dump(mode="json"),
    }


def tempo_descriptor(target: EntityRef, query: InvestigationQuery) -> dict[str, Any]:
    """Canonical descriptor of one Tempo trace read."""
    return {
        "provider": "tempo",
        "operation": "query",
        "target": target.canonical,
        "query": query.model_dump(mode="json"),
    }


def loki_descriptor(
    services: Sequence[str], starts_at: datetime, ends_at: datetime, limit: int | None
) -> dict[str, Any]:
    """Canonical descriptor of one Loki error-log read."""
    return {
        "provider": "loki",
        "operation": "error_logs",
        "services": sorted(services),
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "limit": limit,
    }


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


def _provider_error_message(error: BaseException) -> str:
    """Reuse credential scrubbing and remove address-like object identities."""
    from packages.rca.llm import _sanitized_message

    return re.sub(r"\bat 0x[0-9a-fA-F]+\b", "at [REDACTED]", _sanitized_message(error))


class ProviderBoundary(Protocol):
    """What RCA may ask a provider boundary: the live adapter or its tape replay."""

    def for_caller(self, caller_class: ProviderCallerClass) -> ProviderBoundary: ...

    def supports(self, capability: str) -> bool: ...

    def capabilities(self) -> tuple[str, ...]: ...

    def query_resource_pressure(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[ResourcePressure, ...] | ProviderReadFailure: ...

    def query_traffic(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[TrafficObservation, ...] | ProviderReadFailure: ...

    def query_tempo(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch | ProviderReadFailure: ...

    def query_loki(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> list[LogRecord] | ProviderReadFailure: ...


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

    def fetching_traces_once(self) -> ProviderAdapter:
        """This adapter for one trace capture: an identical trace fetch reaches Tempo once (live-trace §13)."""
        fetching_once = getattr(self.readers.tempo, "fetching_once", None)
        if fetching_once is None:
            return self
        return ProviderAdapter(
            self.run_id,
            self.caller_class,
            self.session_factory,
            replace(self.readers, tempo=fetching_once()),
        )

    def supports(self, capability: str) -> bool:
        if capability in {"resource_pressure", "traffic"}:
            return self.readers.prometheus is not None
        if capability == "runtime_traces":
            return self.readers.tempo is not None
        if capability == "logs":
            return self.readers.loki is not None
        return False

    def capabilities(self) -> tuple[str, ...]:
        """The configured capability contract: sorted, unique ``supports`` names."""
        return tuple(name for name in PROVIDER_CAPABILITIES if self.supports(name))

    def query_resource_pressure(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[ResourcePressure, ...] | ProviderReadFailure:
        reader = self.readers.prometheus
        if reader is None:
            raise RuntimeError("Prometheus reader is not configured")
        descriptor = resource_pressure_descriptor(target, query)
        query_key = provider_query_key(
            descriptor,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return cast(
            tuple[ResourcePressure, ...] | ProviderReadFailure,
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
    ) -> tuple[TrafficObservation, ...] | ProviderReadFailure:
        reader = self.readers.prometheus
        if reader is None:
            raise RuntimeError("Prometheus reader is not configured")
        descriptor = traffic_descriptor(target, query)
        query_key = provider_query_key(
            descriptor,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return cast(
            tuple[TrafficObservation, ...] | ProviderReadFailure,
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
    ) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch | ProviderReadFailure:
        reader = self.readers.tempo
        if reader is None:
            raise RuntimeError("Tempo reader is not configured")
        descriptor = tempo_descriptor(target, query)
        query_key = provider_query_key(
            descriptor,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return cast(
            tuple[TraceSpanObservation, ...] | TempoTraceBatch | ProviderReadFailure,
            self._provider_call(
                capability="tempo_traces",
                query_key=query_key,
                descriptor=descriptor,
                call=lambda: reader.query(target, query),
            ),
        )

    def query_tempo_many(
        self, targets: Sequence[EntityRef], query: InvestigationQuery
    ) -> list[tuple[TraceSpanObservation, ...] | TempoTraceBatch | ProviderReadFailure]:
        """One slice's targets (live-trace-design.md §15), each recorded as its own read.

        A reader that fetches a slice's traces once answers all of them in one call, which every read's timing
        covers; any other reader is asked one target at a time.
        """
        reader = self.readers.tempo
        if reader is None:
            raise RuntimeError("Tempo reader is not configured")
        query_many = getattr(reader, "query_many", None)
        if query_many is None or len(targets) < 2:
            return [self.query_tempo(target, query) for target in targets]
        started_at = datetime.now(UTC)
        try:
            results = list(query_many(targets, query))
        except Exception as error:  # noqa: BLE001 - the call failed for every target
            results = [error] * len(targets)
        finished_at = datetime.now(UTC)
        recorded = []
        for target, result in zip(targets, results, strict=True):
            descriptor = tempo_descriptor(target, query)
            recorded.append(
                cast(
                    tuple[TraceSpanObservation, ...] | TempoTraceBatch | ProviderReadFailure,
                    self._record(
                        capability="tempo_traces",
                        query_key=provider_query_key(descriptor),
                        descriptor=descriptor,
                        started_at=started_at,
                        finished_at=finished_at,
                        result=result,
                    ),
                )
            )
        return recorded

    def query_loki(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> list[LogRecord] | ProviderReadFailure:
        result = self.query_loki_with_read_id(
            services,
            starts_at,
            ends_at,
            limit=limit,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return result if isinstance(result, ProviderReadFailure) else result[0]

    def query_loki_with_read_id(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[list[LogRecord], int] | ProviderReadFailure:
        """Return capture records with the read row committed before return."""
        reader = self.readers.loki
        if reader is None:
            raise RuntimeError("Loki reader is not configured")
        descriptor = loki_descriptor(services, starts_at, ends_at, limit)
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

        result = cast(
            tuple[list[LogRecord], int] | ProviderReadFailure,
            self._provider_call(
                capability="loki_logs",
                query_key=query_key,
                descriptor=descriptor,
                call=call,
                include_read_id=True,
            ),
        )
        return result

    def supports_symptom_capture(self) -> bool:
        """m21 §36.2: the configured Prometheus serves alert rules and Loki ExecutionRecords."""
        return callable(getattr(self.readers.prometheus, "alert_rule", None)) and callable(
            getattr(self.readers.loki, "execution_records", None)
        )

    def query_alert_rule(
        self, alert: str, labels: Mapping[str, str], at: datetime
    ) -> tuple[dict[str, object], int] | ProviderReadFailure:
        """m21 §36.2: one alert's rule as Prometheus serves it, taped with its read id."""
        reader = cast(AlertRuleReader, self.readers.prometheus)
        descriptor = {
            "provider": "prometheus",
            "operation": "alert_rule",
            "alert": alert,
            "labels": dict(sorted(labels.items())),
            "at": at.isoformat(),
        }
        return cast(
            tuple[dict[str, object], int] | ProviderReadFailure,
            self._provider_call(
                capability="prometheus_alert_rule",
                query_key=provider_query_key(descriptor),
                descriptor=descriptor,
                call=lambda: reader.alert_rule(alert, labels, at),
                include_read_id=True,
            ),
        )

    def query_execution_records(
        self, namespace: str, service: str, starts_at: datetime, ends_at: datetime
    ) -> tuple[list[dict[str, str]], int] | ProviderReadFailure:
        """m21 §36.2: one service's ExecutionRecord entries in the window, taped with its read id."""
        reader = cast(ExecutionRecordReader, self.readers.loki)
        descriptor = {
            "provider": "loki",
            "operation": "execution_records",
            "namespace": namespace,
            "service": service,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        }
        return cast(
            tuple[list[dict[str, str]], int] | ProviderReadFailure,
            self._provider_call(
                capability="loki_execution_records",
                query_key=provider_query_key(descriptor),
                descriptor=descriptor,
                call=lambda: reader.execution_records(namespace, service, starts_at, ends_at),
                include_read_id=True,
            ),
        )

    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord] | ProviderReadFailure:
        """LogReader-compatible capture method routed through this adapter."""
        return self.query_loki(services, starts_at, ends_at, limit=limit)

    def error_logs_with_read_id(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> tuple[list[LogRecord], int] | ProviderReadFailure:
        return self.query_loki_with_read_id(services, starts_at, ends_at, limit=limit)

    def _provider_call(
        self,
        *,
        capability: str,
        query_key: str,
        descriptor: dict[str, Any],
        call: Callable[[], _T],
        include_read_id: bool = False,
    ) -> _T | tuple[_T, int] | ProviderReadFailure:
        started_at = datetime.now(UTC)
        try:
            result: _T | Exception = call()
        except Exception as error:  # noqa: BLE001 - recorded as the read's failure
            result = error
        return self._record(
            capability=capability,
            query_key=query_key,
            descriptor=descriptor,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            result=result,
            include_read_id=include_read_id,
        )

    def _record(
        self,
        *,
        capability: str,
        query_key: str,
        descriptor: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
        result: _T | Exception,
        include_read_id: bool = False,
    ) -> _T | tuple[_T, int] | ProviderReadFailure:
        if isinstance(result, Exception):
            error = result
            from packages.storage.repositories import InvestigationReadRepository

            try:
                with self.session_factory() as session:
                    InvestigationReadRepository(session).append_error(
                        run_id=self.run_id,
                        caller_class=self.caller_class,
                        capability=capability,
                        query_key=query_key,
                        query_descriptor=descriptor,
                        started_at=started_at,
                        finished_at=finished_at,
                        error_type=type(error).__name__,
                        error_message=_provider_error_message(error),
                    )
            except Exception as persistence_error:
                raise ProviderReadPersistenceError(
                    f"failed {capability} read could not be persisted"
                ) from persistence_error
            return ProviderReadFailure(
                capability=capability,
                error_type=type(error).__name__,
                error_message=_provider_error_message(error),
            )
        observation = _json_value(result)
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
    "PROVIDER_CAPABILITIES",
    "ProviderAdapter",
    "ProviderBoundary",
    "ProviderIntegrityError",
    "ProviderCallerClass",
    "ProviderReaders",
    "ProviderReadPersistenceError",
    "ProviderReadFailure",
    "loki_descriptor",
    "resource_pressure_descriptor",
    "tempo_descriptor",
    "traffic_descriptor",
    "provider_query_key",
]


class DevelopmentTransportProbe:
    """Bounded, DEV-service-only transport capture at the provider boundary.

    No production adapter registration or decision authority. The measurement
    caller persists raw responses/read snapshots; it does not claim RCA tape
    replay. Kubernetes production searches keep their existing budgets.
    """

    def __init__(self, loki_url: str, tempo_url: str) -> None:
        from packages.rca.investigation.tempo import TempoConfig, TempoTraceReader
        from packages.rca.live import LokiLogReader

        self._logs = LokiLogReader(loki_url, limit=500)
        self._traces = TempoTraceReader(TempoConfig(tempo_url))

    @staticmethod
    def _service(service: str) -> None:
        if not re.fullmatch(r"binding-dev-order-[0-9a-f]{8}", service):
            raise ValueError("Transport probe is restricted to isolated binding DEV services")

    def error_logs(self, service: str, start: datetime, end: datetime) -> list[LogRecord]:
        self._service(service)
        return self._logs.error_logs([service], start, end)

    def search(
        self, service: str, start: datetime, end: datetime, *, limit: int = 500
    ) -> Mapping[str, object]:
        self._service(service)
        if not 1 <= limit <= 500:
            raise ValueError("DEV search limit must be 1..500")
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or not 0 <= (end - start).total_seconds() <= 3600
        ):
            raise ValueError("DEV transport search requires a zoned window <= 3600 seconds")
        return self._traces._get_json(
            "/api/search",
            {
                "q": '{ resource.service.name = "' + service + '" }',
                "start": int(start.timestamp()) - 1,
                "end": int(end.timestamp()),
                "limit": limit,
            },
            1_048_576,
        )

    def trace(self, trace_id: str) -> Mapping[str, object]:
        if not re.fullmatch(r"[0-9a-f]{16,32}", trace_id):
            raise ValueError("Invalid DEV trace identity")
        return self._traces._get_json(f"/api/v2/traces/{trace_id}", {}, 4_194_304)

    def trace_for_log(self, log: LogRecord, start: datetime, end: datetime) -> Mapping[str, object]:
        """DEV-only exact trace read scoped by a frozen log and bounded event window."""
        self._service(log.service)
        trace_id = log.attributes.get("trace_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", trace_id) or not int(trace_id, 16):
            raise ValueError("Explicit nonzero 32-hex log trace identity required")
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or log.at is None
            or log.at.utcoffset() is None
            or not 0 <= (end - start).total_seconds() <= 3600
            or not start <= log.at <= end
        ):
            raise ValueError("DEV log trace read requires a matching zoned window <= 3600 seconds")
        payload = self._traces._fetch_trace(
            trace_id, int(start.timestamp()) - 1, int(end.timestamp()) + 1
        )
        if payload is None:
            raise ValueError("Requested DEV log trace is unavailable")
        return payload

    def kubernetes_traces(
        self, namespace: str, deployment: str, start: datetime, end: datetime
    ) -> TempoTraceBatch:
        """Existing bounded target query, restricted to an isolated DEV deployment."""
        from packages.rca.model import InvestigationQuery

        if not re.fullmatch(r"binding-dev-[0-9a-f]{8}", namespace):
            raise ValueError("Kubernetes probe requires an isolated binding DEV namespace")
        if deployment != "binding-dev-order-" + namespace.removeprefix("binding-dev-"):
            raise ValueError("Kubernetes probe deployment must match the DEV namespace identity")
        return self._traces.query(
            EntityRef(namespace=namespace, kind="Deployment", name=deployment),
            InvestigationQuery(start=start, end=end, limit=32),
        )
