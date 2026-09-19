"""Bounded, provider-neutral Tempo trace acquisition."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from packages.rca.model import EntityRef, InvestigationQuery, TraceSpanObservation, TraceSpanStatus
from packages.rca.traces import SEMANTIC_ATTRIBUTE_ALLOWLIST

_MAX_SEARCH_TRACE_IDS = 8
_MAX_SEARCH_RESPONSE_BYTES = 1_048_576
_MAX_TRACE_RESPONSE_BYTES = 4_194_304
_MAX_RUNTIME_QUERY_SECONDS = 3600
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{16,32}$")


class TempoError(RuntimeError):
    """Base error for bounded Tempo access."""


class TempoHttpError(TempoError):
    """Tempo returned a non-success HTTP status."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        super().__init__(f"Tempo HTTP request failed with status {status_code}: {url}")


class TempoProtocolError(TempoError):
    """Tempo returned a structurally invalid response."""


class TempoResponseTooLarge(TempoError):
    """Tempo returned more bytes than the provider boundary permits."""


class _TempoHttpResponse(Protocol):
    status: int

    def getcode(self) -> int: ...

    def read(self, limit: int) -> bytes: ...

    def __enter__(self) -> _TempoHttpResponse: ...

    def __exit__(self, *args: object) -> None: ...


class _TempoOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> _TempoHttpResponse: ...


def _default_opener(request: Request, *, timeout: float) -> _TempoHttpResponse:
    return cast(_TempoHttpResponse, urlopen(request, timeout=timeout))


@dataclass(frozen=True)
class TempoConfig:
    """Environment-independent configuration for one Tempo reader."""

    base_url: str
    tenant_id: str | None = None
    bearer_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.base_url)
            _ = parsed.port
        except ValueError as error:
            raise ValueError("invalid Tempo URL") from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Tempo URL must use http/https and include a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Tempo URL must not include credentials, query, or fragment")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("Tempo timeout must be numeric")
        if not 0.1 <= float(self.timeout_seconds) <= 30.0:
            raise ValueError("Tempo timeout must be between 0.1 and 30 seconds")
        normalized = self.base_url.rstrip("/")
        object.__setattr__(self, "base_url", normalized)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    @classmethod
    def from_environment(cls) -> TempoConfig | None:
        base_url = os.getenv("TEMPO_URL", "").strip()
        if not base_url:
            return None
        tenant = os.getenv("TEMPO_TENANT_ID") or None
        token = os.getenv("TEMPO_BEARER_TOKEN") or None
        raw_timeout = os.getenv("TEMPO_TIMEOUT_SECONDS", "5.0")
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ValueError("TEMPO_TIMEOUT_SECONDS must be numeric") from error
        return cls(base_url, tenant_id=tenant, bearer_token=token, timeout_seconds=timeout)


class TempoSearchCompleteness(StrEnum):
    BEST_EFFORT = "BEST_EFFORT"
    TRUNCATED = "TRUNCATED"


@dataclass(frozen=True)
class TempoSearchDiagnostics:
    completeness: TempoSearchCompleteness
    candidate_trace_ids: tuple[str, ...]
    search_limit_reached: bool
    inspected_traces: int | None
    inspected_bytes: int | None
    completed_jobs: int | None
    total_jobs: int | None
    fetched_trace_ids: tuple[str, ...]
    missing_trace_ids: tuple[str, ...]


@dataclass(frozen=True)
class TempoTraceBatch:
    spans: tuple[TraceSpanObservation, ...]
    diagnostics: TempoSearchDiagnostics


def _escape_traceql_literal(value: str) -> str:
    if any(ord(char) <= 0x1F or ord(char) == 0x7F for char in value):
        raise ValueError("Tempo target contains a control character")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _compile_target_traceql(target: EntityRef) -> str:
    attribute_by_kind = {
        "Pod": "k8s.pod.name",
        "Deployment": "k8s.deployment.name",
        "StatefulSet": "k8s.statefulset.name",
        "DaemonSet": "k8s.daemonset.name",
    }
    attribute = attribute_by_kind.get(target.kind)
    if attribute is None:
        raise ValueError(f"runtime traces do not support target kind {target.kind!r}")
    namespace = _escape_traceql_literal(target.namespace)
    name = _escape_traceql_literal(target.name)
    return (
        '{ resource."k8s.namespace.name" = "'
        + namespace
        + '" && resource."'
        + attribute
        + '" = "'
        + name
        + '" }'
    )


def _epoch_seconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Tempo query timestamps must be timezone-aware")
    return int(value.astimezone(UTC).timestamp())


def _validate_query(query: InvestigationQuery) -> tuple[int, int]:
    if query.start is None or query.end is None:
        raise ValueError("Tempo runtime trace queries require start and end")
    if (
        query.start.tzinfo is None
        or query.start.utcoffset() is None
        or query.end.tzinfo is None
        or query.end.utcoffset() is None
    ):
        raise ValueError("Tempo query timestamps must be timezone-aware")
    if query.end - query.start > timedelta(seconds=_MAX_RUNTIME_QUERY_SECONDS):
        raise ValueError("Tempo query window must be at most 3600 seconds")
    start = _epoch_seconds(query.start)
    end = _epoch_seconds(query.end)
    if end < start:
        raise ValueError("Tempo query end must not precede start")
    if query.limit > 32:
        raise ValueError("runtime_traces limit must be at most 32")
    if query.reasons or query.contains:
        raise ValueError("runtime_traces does not accept reasons/contains filters")
    return start, end


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TempoProtocolError(f"Tempo {description} must be an object")
    return value


def _list(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise TempoProtocolError(f"Tempo {description} must be a list")
    return value


def _scalar(value: object) -> object | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if key in value and isinstance(value[key], (str, bool, int, float)):
            if isinstance(value[key], bool) and key not in {"boolValue"}:
                return None
            decoded = value[key]
            if isinstance(decoded, (str, bool, int, float)):
                return decoded
    return None


def _attributes(value: object, description: str) -> dict[str, object]:
    if value is None:
        return {}
    result: dict[str, object] = {}
    for item in _list(value, description):
        attribute = _mapping(item, f"{description} item")
        key = attribute.get("key")
        if not isinstance(key, str) or not key:
            raise TempoProtocolError(f"Tempo {description} contains an invalid key")
        decoded = _scalar(attribute.get("value"))
        if decoded is not None:
            result[key] = decoded
    return result


def _string_id(value: object, field_name: str, *, trace: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise TempoProtocolError(f"Tempo span is missing {field_name}")
    normalized = value.lower()
    if not re.fullmatch(r"[0-9a-f]+", normalized):
        raise TempoProtocolError(f"Tempo span has an invalid {field_name}")
    if trace and not _TRACE_ID_RE.fullmatch(normalized):
        raise TempoProtocolError("Tempo span has an invalid traceId")
    return normalized


def _timestamp(value: object, field_name: str, *, required: bool) -> datetime | None:
    if value is None:
        if required:
            raise TempoProtocolError(f"Tempo span is missing {field_name}")
        return None
    if isinstance(value, bool):
        raise TempoProtocolError(f"Tempo span has an invalid {field_name}")
    if isinstance(value, int):
        integer = value
    elif isinstance(value, str) and value.isdigit():
        integer = int(value)
    else:
        raise TempoProtocolError(f"Tempo span has an invalid {field_name}")
    if integer < 0:
        raise TempoProtocolError(f"Tempo span has a negative {field_name}")
    try:
        return datetime.fromtimestamp(integer / 1_000_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise TempoProtocolError(f"Tempo span has an invalid {field_name}") from error


def _required_timestamp(value: object, field_name: str) -> datetime:
    timestamp = _timestamp(value, field_name, required=True)
    if timestamp is None:
        raise TempoProtocolError(f"Tempo span is missing {field_name}")
    return timestamp


def _span_kind(value: object) -> str | None:
    numeric = {
        0: "UNSPECIFIED",
        1: "INTERNAL",
        2: "SERVER",
        3: "CLIENT",
        4: "PRODUCER",
        5: "CONSUMER",
    }
    if isinstance(value, int) and not isinstance(value, bool):
        return numeric.get(value)
    if not isinstance(value, str):
        return None
    normalized = value.upper()
    if normalized.startswith("SPAN_KIND_"):
        normalized = normalized.removeprefix("SPAN_KIND_")
    return normalized if normalized in set(numeric.values()) else None


def _status(value: object) -> TraceSpanStatus:
    if isinstance(value, int) and not isinstance(value, bool):
        return {0: TraceSpanStatus.UNSET, 1: TraceSpanStatus.OK, 2: TraceSpanStatus.ERROR}.get(
            value, TraceSpanStatus.UNKNOWN
        )
    if isinstance(value, str):
        normalized = value.upper()
        if normalized.startswith("STATUS_CODE_"):
            normalized = normalized.removeprefix("STATUS_CODE_")
        return {
            "UNSET": TraceSpanStatus.UNSET,
            "OK": TraceSpanStatus.OK,
            "ERROR": TraceSpanStatus.ERROR,
        }.get(normalized, TraceSpanStatus.UNKNOWN)
    return TraceSpanStatus.UNKNOWN


def _parse_span(
    raw: Mapping[str, object], resource_attributes: Mapping[str, object]
) -> TraceSpanObservation:
    trace_id = _string_id(raw.get("traceId"), "traceId", trace=True)
    span_id = _string_id(raw.get("spanId"), "spanId")
    parent_raw = raw.get("parentSpanId")
    parent_id = (
        None
        if parent_raw is None
        or parent_raw == ""
        or (isinstance(parent_raw, str) and parent_raw in {"0" * 16, "0" * 32})
        else _string_id(parent_raw, "parentSpanId")
    )
    span_attributes = _attributes(raw.get("attributes"), "span attributes")
    semantic: dict[str, str] = {
        key: str(value)
        for key, value in resource_attributes.items()
        if key in SEMANTIC_ATTRIBUTE_ALLOWLIST
    }
    semantic.update(
        {
            key: str(value)
            for key, value in span_attributes.items()
            if key in SEMANTIC_ATTRIBUTE_ALLOWLIST
        }
    )
    service_value = resource_attributes.get("service.name")
    if service_value is None or not str(service_value).strip():
        raise TempoProtocolError("Tempo trace resource is missing service.name")
    span_name = raw.get("name")
    if not isinstance(span_name, str):
        span_name = None
    status_raw = _mapping(raw.get("status"), "span status") if raw.get("status") is not None else {}
    return TraceSpanObservation(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_id,
        service=str(service_value),
        span_name=span_name,
        span_kind=_span_kind(raw.get("kind")),
        start_at=_required_timestamp(raw.get("startTimeUnixNano"), "startTimeUnixNano"),
        end_at=_timestamp(raw.get("endTimeUnixNano"), "endTimeUnixNano", required=False),
        duration_raw=None,
        status=_status(status_raw.get("code")),
        semantic_attributes=semantic,
        evidence_id=f"tempo:{trace_id}:{span_id}",
    )


def parse_tempo_trace_json(payload: Mapping[str, object]) -> tuple[TraceSpanObservation, ...]:
    """Parse one OTLP-compatible Tempo V2 trace response."""
    spans: dict[tuple[str, str], TraceSpanObservation] = {}
    resource_spans_value = payload.get("resourceSpans", [])
    for resource_span_raw in _list(resource_spans_value, "resourceSpans"):
        resource_span = _mapping(resource_span_raw, "resourceSpans item")
        resource = _mapping(resource_span.get("resource", {}), "resource")
        resource_attributes = _attributes(resource.get("attributes"), "resource attributes")
        scope_value = resource_span.get("scopeSpans")
        if scope_value is None:
            scope_value = resource_span.get("instrumentationLibrarySpans", [])
        for scope_raw in _list(scope_value, "scope spans"):
            scope = _mapping(scope_raw, "scope spans item")
            for span_raw in _list(scope.get("spans", []), "spans"):
                span = _parse_span(_mapping(span_raw, "span"), resource_attributes)
                identity = (span.trace_id, span.span_id)
                previous = spans.get(identity)
                if previous is not None:
                    if previous.model_dump(mode="json") != span.model_dump(mode="json"):
                        raise TempoProtocolError("Tempo returned conflicting duplicate span")
                    continue
                spans[identity] = span
    return tuple(
        sorted(
            spans.values(),
            key=lambda span: (span.start_at, span.trace_id, span.span_id, span.evidence_id),
        )
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _trace_ids(payload: Mapping[str, object]) -> tuple[str, ...]:
    raw_traces = payload.get("traces", [])
    traces = _list(raw_traces, "search traces")
    normalized: set[str] = set()
    for raw in traces:
        item = _mapping(raw, "search trace")
        trace_id = _string_id(item.get("traceID"), "traceID", trace=True)
        normalized.add(trace_id)
    return tuple(sorted(normalized))


def _metrics(
    payload: Mapping[str, object],
) -> tuple[int | None, int | None, int | None, int | None]:
    raw = payload.get("metrics")
    if not isinstance(raw, Mapping):
        return None, None, None, None
    return (
        _optional_int(raw.get("inspectedTraces")),
        _optional_int(raw.get("inspectedBytes")),
        _optional_int(raw.get("completedJobs")),
        _optional_int(raw.get("totalJobs")),
    )


@dataclass(frozen=True)
class TempoTraceReader:
    config: TempoConfig
    opener: _TempoOpener = field(default=_default_opener, repr=False, compare=False)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.tenant_id:
            headers["X-Scope-OrgID"] = self.config.tenant_id
        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
        return headers

    def _get_json(
        self, path: str, params: Mapping[str, object], max_bytes: int
    ) -> Mapping[str, object]:
        url = f"{self.config.base_url}{path}?{urlencode(params)}"
        request = Request(url, headers=self._headers(), method="GET")
        try:
            with self.opener(request, timeout=self.config.timeout_seconds) as response:
                status = response.status
                if not 200 <= status < 300:
                    raise TempoHttpError(status, url)
                body = response.read(max_bytes + 1)
        except HTTPError as error:
            raise TempoHttpError(error.code, url) from error
        except (URLError, TimeoutError, OSError) as error:
            raise TempoError("Tempo HTTP request failed") from error
        if not isinstance(body, bytes):
            raise TempoProtocolError("Tempo HTTP response was not bytes")
        if len(body) > max_bytes:
            raise TempoResponseTooLarge(f"Tempo response exceeded {max_bytes} bytes")
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TempoProtocolError("Tempo returned invalid JSON") from error
        return _mapping(decoded, "response")

    def query(self, target: EntityRef, query: InvestigationQuery) -> TempoTraceBatch:
        start, end = _validate_query(query)
        traceql = _compile_target_traceql(target)
        search_payload = self._get_json(
            "/api/search",
            {"q": traceql, "start": start, "end": end, "limit": 8, "spss": 1},
            _MAX_SEARCH_RESPONSE_BYTES,
        )
        all_ids = _trace_ids(search_payload)
        selected_ids = all_ids[:_MAX_SEARCH_TRACE_IDS]
        inspected_traces, inspected_bytes, completed_jobs, total_jobs = _metrics(search_payload)
        truncated = len(all_ids) >= _MAX_SEARCH_TRACE_IDS or (
            completed_jobs is not None and total_jobs is not None and completed_jobs < total_jobs
        )
        fetched: list[str] = []
        missing: list[str] = []
        spans: dict[tuple[str, str], TraceSpanObservation] = {}
        for trace_id in selected_ids:
            try:
                payload = self._get_json(
                    f"/api/v2/traces/{quote(trace_id, safe='')}",
                    {"start": start, "end": end},
                    _MAX_TRACE_RESPONSE_BYTES,
                )
            except TempoHttpError as error:
                if error.status_code == 404:
                    missing.append(trace_id)
                    continue
                raise
            fetched.append(trace_id)
            for span in parse_tempo_trace_json(payload):
                identity = (span.trace_id, span.span_id)
                previous = spans.get(identity)
                if previous is not None and previous.model_dump(mode="json") != span.model_dump(
                    mode="json"
                ):
                    raise TempoProtocolError("Tempo returned conflicting duplicate span")
                spans[identity] = span
        diagnostics = TempoSearchDiagnostics(
            completeness=(
                TempoSearchCompleteness.TRUNCATED
                if truncated
                else TempoSearchCompleteness.BEST_EFFORT
            ),
            candidate_trace_ids=selected_ids,
            search_limit_reached=len(all_ids) >= _MAX_SEARCH_TRACE_IDS,
            inspected_traces=inspected_traces,
            inspected_bytes=inspected_bytes,
            completed_jobs=completed_jobs,
            total_jobs=total_jobs,
            fetched_trace_ids=tuple(fetched),
            missing_trace_ids=tuple(missing),
        )
        return TempoTraceBatch(
            spans=tuple(
                sorted(
                    spans.values(),
                    key=lambda span: (span.start_at, span.trace_id, span.span_id, span.evidence_id),
                )
            ),
            diagnostics=diagnostics,
        )


__all__ = [
    "TempoConfig",
    "TempoError",
    "TempoHttpError",
    "TempoProtocolError",
    "TempoResponseTooLarge",
    "TempoSearchCompleteness",
    "TempoSearchDiagnostics",
    "TempoTraceBatch",
    "TempoTraceReader",
    "_compile_target_traceql",
    "parse_tempo_trace_json",
]
