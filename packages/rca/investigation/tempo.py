"""Bounded, provider-neutral Tempo trace acquisition."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    TraceSpanLink,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.trace_request_pairs import (
    RequestPairBatch,
    RequestPairTraceMissing,
    read_request_pairs,
)
from packages.rca.traces import SEMANTIC_ATTRIBUTE_ALLOWLIST

logger = logging.getLogger(__name__)

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
    # live-trace-design.md §14.2: joined waiters also fetch for themselves and report differences
    verify_single_flight: bool = False

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
        verify = os.getenv("TEMPO_VERIFY_SINGLE_FLIGHT", "").strip().lower() in {"1", "true"}
        return cls(
            base_url,
            tenant_id=tenant,
            bearer_token=token,
            timeout_seconds=timeout,
            verify_single_flight=verify,
        )


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
    if query.source_log_ids:
        raise ValueError("Source log IDs require the exact-pair reader")
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
        # Tempo V2's OTLP JSON endpoint emits trace/span IDs as base64, while
        # other compatible backends return their hex encoding. Normalize both
        # representations to lowercase hex before applying the same bounds.
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise TempoProtocolError(f"Tempo span has an invalid {field_name}") from error
        expected_bytes = 16 if trace else 8
        if len(decoded) != expected_bytes:
            raise TempoProtocolError(f"Tempo span has an invalid {field_name}")
        normalized = decoded.hex()
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


MAX_SPAN_LINKS = 8


def _parse_links(value: object) -> tuple[tuple[TraceSpanLink, ...], int]:
    """Bounded, deterministic span links: identical duplicates collapse, conflicts fail."""
    if value is None:
        return (), 0
    links: dict[tuple[str, str], TraceSpanLink] = {}
    for raw in _list(value, "span links"):
        item = _mapping(raw, "span link")
        attributes = _attributes(item.get("attributes"), "span link attributes")
        link = TraceSpanLink(
            trace_id=_string_id(item.get("traceId"), "link traceId", trace=True),
            span_id=_string_id(item.get("spanId"), "link spanId"),
            semantic_attributes={
                key: str(attribute)
                for key, attribute in attributes.items()
                if key in SEMANTIC_ATTRIBUTE_ALLOWLIST
            },
        )
        identity = (link.trace_id, link.span_id)
        previous = links.get(identity)
        if previous is not None and previous != link:
            raise TempoProtocolError("Tempo returned conflicting duplicate span links")
        links[identity] = link
    ordered = tuple(links[identity] for identity in sorted(links))
    return ordered[:MAX_SPAN_LINKS], max(0, len(ordered) - MAX_SPAN_LINKS)


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
    links, dropped_links = _parse_links(raw.get("links"))
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
        links=links,
        dropped_link_count=dropped_links,
        evidence_id=f"tempo:{trace_id}:{span_id}",
    )


def parse_tempo_trace_json(payload: Mapping[str, object]) -> tuple[TraceSpanObservation, ...]:
    """Parse one OTLP-compatible Tempo V2 trace response."""
    spans: dict[tuple[str, str], TraceSpanObservation] = {}
    # Tempo's V2 endpoint wraps OTLP trace data under ``trace``. Accept the
    # unwrapped OTLP form too, which remains useful for fixtures and compatible
    # backends.
    trace_value = payload.get("trace")
    trace_payload = _mapping(trace_value, "trace") if trace_value is not None else payload
    resource_spans_value = trace_payload.get("resourceSpans", [])
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


TRACE_SCOPE_IDLE_SECONDS = 120.0
TRACE_SCOPE_MAX = 64
CAPTURE_SCOPE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_FlightKey = tuple[str, str | None, str, str, int, int]


@dataclass
class TraceFlightCounts:
    """What one capture scope (or the reader in all) asked of Tempo (live-trace-design.md §14.2)."""

    requests: int = 0
    joined: int = 0
    http_fetches: int = 0
    failures: int = 0
    verify_differences: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "joined": self.joined,
            "http_fetches": self.http_fetches,
            "failures": self.failures,
            "verify_differences": self.verify_differences,
        }


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.payload: Mapping[str, object] | None = None
        self.error: BaseException | None = None


class TraceSingleFlight:
    """Identical trace fetches in flight at the same time, within one capture scope, are made once (§14.2).

    Nothing is kept once a fetch is done: the next identical request goes to Tempo. A failure reaches every
    waiter of that fetch and nothing else. With ``verify``, every joined waiter also fetches for itself, keeps
    its own fetch, and a difference from the shared one is logged and counted.
    """

    def __init__(self, *, verify: bool = False, clock: Callable[[], float] | None = None) -> None:
        import time

        self.verify = verify
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._flights: dict[_FlightKey, _Flight] = {}
        self._scopes: dict[str, tuple[TraceFlightCounts, float]] = {}
        self.totals = TraceFlightCounts()

    def _scope(self, scope: str) -> TraceFlightCounts:
        now = self._clock()
        for name, (counts, last) in list(self._scopes.items()):
            if now - last > TRACE_SCOPE_IDLE_SECONDS:
                self._end(name, counts)
        counts = self._scopes.pop(scope, (TraceFlightCounts(), now))[0]
        self._scopes[scope] = (counts, now)
        while len(self._scopes) > TRACE_SCOPE_MAX:
            oldest = next(iter(self._scopes))
            self._end(oldest, self._scopes[oldest][0])
        return counts

    def _end(self, scope: str, counts: TraceFlightCounts) -> None:
        del self._scopes[scope]
        logger.info(
            "trace capture scope %s ended: %s",
            scope,
            " ".join(f"{name}={value}" for name, value in counts.as_dict().items()),
        )

    def _count(self, counts: TraceFlightCounts, name: str) -> None:
        with self._lock:
            setattr(counts, name, getattr(counts, name) + 1)
            setattr(self.totals, name, getattr(self.totals, name) + 1)

    def fetch(
        self,
        key: _FlightKey,
        get: Callable[[], Mapping[str, object] | None],
    ) -> Mapping[str, object] | None:
        scope = key[2]
        with self._lock:
            counts = self._scope(scope)
            counts.requests += 1
            self.totals.requests += 1
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
            else:
                counts.joined += 1
                self.totals.joined += 1

        def fetched() -> Mapping[str, object] | None:
            self._count(counts, "http_fetches")
            try:
                return get()
            except BaseException:
                self._count(counts, "failures")
                raise

        if leader:
            try:
                flight.payload = fetched()
            except BaseException as error:
                flight.error = error
                raise
            finally:
                with self._lock:
                    del self._flights[key]
                flight.done.set()
            return flight.payload
        flight.done.wait()
        if flight.error is not None:
            raise flight.error
        if not self.verify:
            return flight.payload
        own = fetched()
        if _span_set(own) != _span_set(flight.payload):
            self._count(counts, "verify_differences")
            logger.warning(
                "trace single-flight verification: trace %s differs (shared %d spans, own %d spans)",
                key[3],
                len(_span_set(flight.payload)),
                len(_span_set(own)),
            )
        return own


def _span_set(payload: Mapping[str, object] | None) -> frozenset[str]:
    if payload is None:
        return frozenset()
    return frozenset(
        json.dumps(span.model_dump(mode="json"), sort_keys=True)
        for span in parse_tempo_trace_json(payload)
    )


@dataclass(frozen=True)
class TempoTraceReader:
    config: TempoConfig
    opener: _TempoOpener = field(default=_default_opener, repr=False, compare=False)
    flights: TraceSingleFlight | None = field(default=None, repr=False, compare=False)
    capture_scope: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.flights is None:
            object.__setattr__(
                self, "flights", TraceSingleFlight(verify=self.config.verify_single_flight)
            )
        if self.capture_scope is not None and not CAPTURE_SCOPE_PATTERN.match(self.capture_scope):
            raise ValueError("capture scope must be 32 lowercase hex characters")

    def fetching_once(self, capture_scope: str | None = None) -> TempoTraceReader:
        """This reader for one capture: concurrent identical trace fetches reach Tempo once (§14.2)."""
        import secrets

        return replace(self, capture_scope=capture_scope or secrets.token_hex(16))

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

    def _fetch_trace(self, trace_id: str, start: int, end: int) -> Mapping[str, object] | None:
        """One trace by ID over the range, or ``None`` when Tempo does not have it."""

        def get() -> Mapping[str, object] | None:
            try:
                return self._get_json(
                    f"/api/v2/traces/{quote(trace_id, safe='')}",
                    {"start": start, "end": end},
                    _MAX_TRACE_RESPONSE_BYTES,
                )
            except TempoHttpError as error:
                if error.status_code == 404:
                    return None
                raise

        if self.capture_scope is None or self.flights is None:
            return get()
        key = (
            self.config.base_url,
            self.config.tenant_id,
            self.capture_scope,
            trace_id,
            start,
            end,
        )
        return self.flights.fetch(key, get)

    def query_pairs(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        logs: Sequence[LogRecord],
        existing: Sequence[TraceSpanObservation],
    ) -> RequestPairBatch:
        def fetch(trace_id: str) -> tuple[TraceSpanObservation, ...]:
            assert query.start is not None and query.end is not None
            payload = self._fetch_trace(
                trace_id, _epoch_seconds(query.start), _epoch_seconds(query.end)
            )
            if payload is None:
                raise RequestPairTraceMissing("Exact log trace is unavailable")
            return parse_tempo_trace_json(payload)

        return read_request_pairs(target, query, logs, existing, fetch)

    def query(self, target: EntityRef, query: InvestigationQuery) -> TempoTraceBatch:
        (result,) = self.query_many((target,), query)
        if isinstance(result, Exception):
            raise result
        return result

    def query_many(
        self, targets: Sequence[EntityRef], query: InvestigationQuery
    ) -> list[TempoTraceBatch | Exception]:
        """Each target's own search, then every trace they selected fetched once (live-trace-design.md §15).

        Each target keeps its own search, limit, diagnostics and spans. A failed search or fetch fails only the
        targets that needed it, and a trace is not fetched once every target that selected it has failed.
        """
        start, end = _validate_query(query)
        searches: list[_Search | Exception] = []
        for target in targets:
            try:
                searches.append(self._search(target, start, end))
            except Exception as error:  # noqa: BLE001 - this target's read fails, the others go on
                searches.append(error)
        fetched: dict[str, Mapping[str, object] | Exception | None] = {}
        for search in searches:
            if isinstance(search, Exception):
                continue
            for trace_id in search.selected_ids:
                if trace_id in fetched:
                    continue
                needed = any(
                    not isinstance(other, Exception)
                    and trace_id in other.selected_ids
                    and not any(
                        isinstance(fetched.get(earlier), Exception)
                        for earlier in other.selected_ids
                    )
                    for other in searches
                )
                if not needed:
                    continue
                try:
                    fetched[trace_id] = self._fetch_trace(trace_id, start, end)
                except Exception as error:  # noqa: BLE001 - fails the targets that selected it
                    fetched[trace_id] = error
        return [
            search if isinstance(search, Exception) else _batch(search, fetched)
            for search in searches
        ]

    def _search(self, target: EntityRef, start: int, end: int) -> _Search:
        payload = self._get_json(
            "/api/search",
            {
                "q": _compile_target_traceql(target),
                "start": start,
                "end": end,
                "limit": 8,
                "spss": 1,
            },
            _MAX_SEARCH_RESPONSE_BYTES,
        )
        all_ids = _trace_ids(payload)
        inspected_traces, inspected_bytes, completed_jobs, total_jobs = _metrics(payload)
        return _Search(
            selected_ids=all_ids[:_MAX_SEARCH_TRACE_IDS],
            limit_reached=len(all_ids) >= _MAX_SEARCH_TRACE_IDS,
            inspected_traces=inspected_traces,
            inspected_bytes=inspected_bytes,
            completed_jobs=completed_jobs,
            total_jobs=total_jobs,
        )


@dataclass(frozen=True)
class _Search:
    """One target's search: the trace IDs it selected and Tempo's report of the search."""

    selected_ids: tuple[str, ...]
    limit_reached: bool
    inspected_traces: int | None
    inspected_bytes: int | None
    completed_jobs: int | None
    total_jobs: int | None


def _batch(
    search: _Search, fetched: Mapping[str, Mapping[str, object] | Exception | None]
) -> TempoTraceBatch | Exception:
    """One target's batch from the traces its own search selected."""
    found: list[str] = []
    missing: list[str] = []
    spans: dict[tuple[str, str], TraceSpanObservation] = {}
    for trace_id in search.selected_ids:
        payload = fetched.get(trace_id)
        if isinstance(payload, Exception):
            return payload
        if payload is None:
            missing.append(trace_id)
            continue
        found.append(trace_id)
        try:
            parsed = parse_tempo_trace_json(payload)
        except Exception as error:  # noqa: BLE001 - a malformed trace fails this target's read
            return error
        for span in parsed:
            identity = (span.trace_id, span.span_id)
            previous = spans.get(identity)
            if previous is not None and previous.model_dump(mode="json") != span.model_dump(
                mode="json"
            ):
                return TempoProtocolError("Tempo returned conflicting duplicate span")
            spans[identity] = span
    truncated = search.limit_reached or (
        search.completed_jobs is not None
        and search.total_jobs is not None
        and search.completed_jobs < search.total_jobs
    )
    diagnostics = TempoSearchDiagnostics(
        completeness=(
            TempoSearchCompleteness.TRUNCATED if truncated else TempoSearchCompleteness.BEST_EFFORT
        ),
        candidate_trace_ids=search.selected_ids,
        search_limit_reached=search.limit_reached,
        inspected_traces=search.inspected_traces,
        inspected_bytes=search.inspected_bytes,
        completed_jobs=search.completed_jobs,
        total_jobs=search.total_jobs,
        fetched_trace_ids=tuple(found),
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
    "TraceFlightCounts",
    "TraceSingleFlight",
    "_compile_target_traceql",
    "parse_tempo_trace_json",
]
