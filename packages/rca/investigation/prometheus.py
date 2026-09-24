"""Bounded, server-owned Prometheus metric acquisition."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    ResourcePressure,
    TrafficObservation,
)

_MAX_QUERY_SECONDS = 3600
_MAX_RESPONSE_BYTES = 2_097_152
_TRAFFIC_RATE_WINDOW = "30s"
_CPU_RATE_WINDOW = "1m"
_MIN_QUERY_STEP_SECONDS = 15


class PrometheusError(RuntimeError):
    """Base error for bounded Prometheus access."""


class PrometheusHttpError(PrometheusError):
    """Prometheus returned a non-success HTTP status."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        super().__init__(f"Prometheus HTTP request failed with status {status_code}: {url}")


class PrometheusProtocolError(PrometheusError):
    """Prometheus returned a structurally invalid response."""


class PrometheusResponseTooLarge(PrometheusError):
    """Prometheus returned more bytes than the provider boundary permits."""


@dataclass(frozen=True)
class PrometheusConfig:
    base_url: str
    tenant_id: str | None = None
    bearer_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.base_url)
            _ = parsed.port
        except ValueError as error:
            raise ValueError("invalid Prometheus URL") from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Prometheus URL must use http/https and include a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Prometheus URL must not include credentials, query, or fragment")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("Prometheus timeout must be numeric")
        if not 0.1 <= float(self.timeout_seconds) <= 30.0:
            raise ValueError("Prometheus timeout must be between 0.1 and 30 seconds")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    @classmethod
    def from_environment(cls) -> PrometheusConfig | None:
        base_url = os.getenv("PROMETHEUS_URL", "").strip()
        if not base_url:
            return None
        raw_timeout = os.getenv("PROMETHEUS_TIMEOUT_SECONDS", "5.0")
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ValueError("PROMETHEUS_TIMEOUT_SECONDS must be numeric") from error
        return cls(
            base_url,
            tenant_id=os.getenv("PROMETHEUS_TENANT_ID") or None,
            bearer_token=os.getenv("PROMETHEUS_BEARER_TOKEN") or None,
            timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class PrometheusSample:
    at: datetime
    value: float


@dataclass(frozen=True)
class PrometheusSeries:
    labels: Mapping[str, str]
    samples: tuple[PrometheusSample, ...]


class _PrometheusResponse(Protocol):
    status: int

    def read(self, limit: int) -> bytes: ...

    def __enter__(self) -> _PrometheusResponse: ...

    def __exit__(self, *args: object) -> None: ...


def _default_opener(request: Request, *, timeout: float) -> _PrometheusResponse:
    return cast(_PrometheusResponse, urlopen(request, timeout=timeout))


def _escape_prometheus_label_value(value: str) -> str:
    if any(ord(char) <= 0x1F or ord(char) == 0x7F for char in value):
        raise ValueError("Prometheus label value contains a control character")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _validate_query(query: InvestigationQuery) -> tuple[datetime, datetime]:
    if query.start is None or query.end is None:
        raise ValueError("Prometheus queries require start and end")
    if (
        query.start.tzinfo is None
        or query.start.utcoffset() is None
        or query.end.tzinfo is None
        or query.end.utcoffset() is None
    ):
        raise ValueError("Prometheus query timestamps must be timezone-aware")
    if query.end < query.start:
        raise ValueError("Prometheus query end must not precede start")
    if query.end - query.start > timedelta(seconds=_MAX_QUERY_SECONDS):
        raise ValueError("Prometheus query window must be at most 3600 seconds")
    return query.start, query.end


def _epoch_seconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Prometheus query timestamps must be timezone-aware")
    return int(value.astimezone(UTC).timestamp())


def _traffic_query_step_seconds(start: datetime, end: datetime, limit: int) -> int:
    """Return the exact fixed-step interval used by the bounded traffic query."""
    point_budget = max(2, min(limit, 32))
    duration_seconds = int((end - start).total_seconds())
    return max(
        _MIN_QUERY_STEP_SECONDS,
        math.ceil(duration_seconds / (point_budget - 1)),
    )


def _resource_memory_query(target: EntityRef) -> str:
    namespace = _escape_prometheus_label_value(target.namespace)
    pod = _escape_prometheus_label_value(target.name)
    return (
        "max by (container) (container_memory_working_set_bytes{"
        f'namespace="{namespace}",pod="{pod}",container!="",container!="POD"'
        "}) / max by (container) (kube_pod_container_resource_limits{"
        f'namespace="{namespace}",pod="{pod}",container!="",resource="memory",unit="byte"'
        "})"
    )


def _resource_cpu_query(target: EntityRef) -> str:
    namespace = _escape_prometheus_label_value(target.namespace)
    pod = _escape_prometheus_label_value(target.name)
    return (
        "sum by (container) (rate(container_cpu_cfs_throttled_periods_total{"
        f'namespace="{namespace}",pod="{pod}",container!="",container!="POD"'
        f"}}[{_CPU_RATE_WINDOW}])) / sum by (container) (rate("
        "container_cpu_cfs_periods_total{"
        f'namespace="{namespace}",pod="{pod}",container!="",container!="POD"'
        f"}}[{_CPU_RATE_WINDOW}]))"
    )


def _traffic_query(target: EntityRef) -> str:
    service = _escape_prometheus_label_value(target.name)
    return (
        "sum(rate(http_requests_total{"
        f'service="{service}",route!~"/(health|metrics|__faults).*"'
        f"}}[{_TRAFFIC_RATE_WINDOW}]))"
    )


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PrometheusProtocolError(f"Prometheus {description} must be an object")
    return value


def _finite_float(value: object, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PrometheusProtocolError(f"Prometheus {description} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise PrometheusProtocolError(f"Prometheus {description} must be numeric") from error
    return parsed


def _sample_timestamp(value: object) -> datetime:
    parsed = _finite_float(value, "sample timestamp")
    if not math.isfinite(parsed) or parsed < 0:
        raise PrometheusProtocolError("Prometheus sample timestamp must not be negative")
    try:
        return datetime.fromtimestamp(parsed, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise PrometheusProtocolError("Prometheus sample timestamp is invalid") from error


def _label(value: object, key: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PrometheusProtocolError(f"Prometheus label {key} is not scalar")
    return str(value)


def _parse_series(item: object) -> PrometheusSeries:
    series = _mapping(item, "series")
    raw_metric = _mapping(series.get("metric"), "series metric")
    labels = {str(key): _label(value, str(key)) for key, value in raw_metric.items()}
    raw_values = series.get("values")
    if not isinstance(raw_values, list):
        raise PrometheusProtocolError("Prometheus series values must be a list")
    samples: dict[datetime, float] = {}
    for raw_sample in raw_values:
        if not isinstance(raw_sample, (list, tuple)) or len(raw_sample) < 2:
            raise PrometheusProtocolError(
                "Prometheus range sample must contain timestamp and value"
            )
        at = _sample_timestamp(raw_sample[0])
        value = _finite_float(raw_sample[1], "sample value")
        if not math.isfinite(value) or value < 0:
            continue
        previous = samples.get(at)
        if previous is not None and previous != value:
            raise PrometheusProtocolError("Prometheus samples conflict at one timestamp")
        samples[at] = value
    return PrometheusSeries(
        labels=labels,
        samples=tuple(PrometheusSample(at, samples[at]) for at in sorted(samples)),
    )


def _parse_response(payload: Mapping[str, object]) -> tuple[PrometheusSeries, ...]:
    if payload.get("status") != "success":
        raise PrometheusProtocolError("Prometheus response status was not success")
    data = _mapping(payload.get("data"), "response data")
    if data.get("resultType") != "matrix":
        raise PrometheusProtocolError("Prometheus response resultType must be matrix")
    raw_result = data.get("result")
    if not isinstance(raw_result, list):
        raise PrometheusProtocolError("Prometheus response result must be a list")
    return tuple(_parse_series(item) for item in raw_result)


def _evidence_digest(prefix: str, payload: Mapping[str, object]) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"prometheus:{prefix}:{sha256(material.encode()).hexdigest()[:24]}"


def _valid_samples(series: PrometheusSeries) -> tuple[PrometheusSample, ...]:
    return tuple(
        sample for sample in series.samples if math.isfinite(sample.value) and sample.value >= 0
    )


class PrometheusMetricsReader:
    def __init__(
        self,
        config: PrometheusConfig,
        *,
        opener: Callable[..., object] = _default_opener,
    ) -> None:
        self.config = config
        self._opener = opener

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.tenant_id:
            headers["X-Scope-OrgID"] = self.config.tenant_id
        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
        return headers

    def _query_range(
        self,
        promql: str,
        *,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> tuple[PrometheusSeries, ...]:
        if step_seconds < _MIN_QUERY_STEP_SECONDS:
            raise ValueError("Prometheus query step is too small")
        url = f"{self.config.base_url}/api/v1/query_range?{
            urlencode(
                {
                    'query': promql,
                    'start': _epoch_seconds(start),
                    'end': _epoch_seconds(end),
                    'step': step_seconds,
                }
            )
        }"
        request = Request(url, headers=self._headers(), method="GET")
        try:
            response = self._opener(request, timeout=self.config.timeout_seconds)
            with cast(_PrometheusResponse, response) as opened:
                if not 200 <= opened.status < 300:
                    raise PrometheusHttpError(opened.status, url)
                body = opened.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise PrometheusHttpError(error.code, url) from error
        except (URLError, TimeoutError, OSError) as error:
            raise PrometheusError("Prometheus HTTP request failed") from error
        if not isinstance(body, bytes):
            raise PrometheusProtocolError("Prometheus HTTP response was not bytes")
        if len(body) > _MAX_RESPONSE_BYTES:
            raise PrometheusResponseTooLarge(
                f"Prometheus response exceeded {_MAX_RESPONSE_BYTES} bytes"
            )
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PrometheusProtocolError("Prometheus returned invalid JSON") from error
        return _parse_response(_mapping(decoded, "response"))

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        if target.kind != "Pod":
            raise ValueError("resource pressure requires a Pod target")
        start, end = _validate_query(query)
        series_by_resource = (
            (
                "memory",
                self._query_range(
                    _resource_memory_query(target), start=start, end=end, step_seconds=15
                ),
            ),
            (
                "cpu",
                self._query_range(
                    _resource_cpu_query(target), start=start, end=end, step_seconds=15
                ),
            ),
        )
        records: list[ResourcePressure] = []
        for resource, series_items in series_by_resource:
            for series in series_items:
                container = series.labels.get("container")
                if not container:
                    if series.samples:
                        raise PrometheusProtocolError(
                            "Prometheus resource series lacks container label"
                        )
                    continue
                samples = _valid_samples(series)
                if not samples:
                    continue
                baseline = samples[0].value
                peak = max(sample.value for sample in samples)
                at = next(sample.at for sample in samples if sample.value == peak)
                evidence_id = _evidence_digest(
                    "resource",
                    {
                        "pod": target.canonical,
                        "container": container,
                        "resource": resource,
                        "baseline": baseline,
                        "peak": peak,
                        "at": at.isoformat(),
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    },
                )
                records.append(
                    ResourcePressure(
                        pod=target,
                        container=container,
                        resource=resource,
                        baseline=baseline,
                        peak=peak,
                        at=at,
                        evidence_id=evidence_id,
                        sample_count=len(samples),
                        sample_start=samples[0].at,
                        sample_end=samples[-1].at,
                    )
                )
        ordered = sorted(
            records, key=lambda item: (item.container, item.resource, item.at, item.evidence_id)
        )
        return tuple(ordered[: query.limit])

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]:
        start, end = _validate_query(query)
        step_seconds = _traffic_query_step_seconds(start, end, query.limit)
        series_items = self._query_range(
            _traffic_query(target), start=start, end=end, step_seconds=step_seconds
        )
        if len(series_items) > 1:
            raise PrometheusProtocolError("Prometheus traffic query returned multiple series")
        records: list[TrafficObservation] = []
        for series in series_items:
            for sample in _valid_samples(series):
                evidence_id = _evidence_digest(
                    "traffic",
                    {
                        "entity": target.canonical,
                        "metric": "http_requests_per_second",
                        "at": sample.at.isoformat(),
                        "value": sample.value,
                    },
                )
                records.append(
                    TrafficObservation(
                        entity=target,
                        metric="http_requests_per_second",
                        at=sample.at,
                        value=sample.value,
                        evidence_id=evidence_id,
                    )
                )
        return tuple(records[: query.limit])


__all__ = [
    "PrometheusConfig",
    "PrometheusError",
    "PrometheusHttpError",
    "PrometheusMetricsReader",
    "PrometheusProtocolError",
    "PrometheusResponseTooLarge",
    "PrometheusSample",
    "PrometheusSeries",
    "_escape_prometheus_label_value",
]
