"""Live OpenTelemetry runtime and HTTP instrumentation for workload services."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind
from prometheus_client import CollectorRegistry
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from packages.telemetry.metrics import WorkloadMetrics


class JsonLogFormatter(logging.Formatter):
    """Serialize application logs with correlation fields."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "service": self._service_name,
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "trace_id": format(span_context.trace_id, "032x") if span_context.is_valid else None,
            "span_id": format(span_context.span_id, "016x") if span_context.is_valid else None,
        }
        return json.dumps(payload, separators=(",", ":"))


class RuntimeMetrics:
    """OTLP metric instruments for HTTP, database, and Kafka signals."""

    def __init__(self, meter: Any) -> None:
        self._http_requests = meter.create_counter(
            "http_requests_total", unit="1", description="Total HTTP requests"
        )
        self._http_duration = meter.create_histogram(
            "http_request_duration_seconds", unit="s", description="HTTP request duration"
        )
        self._dependency_duration = meter.create_histogram(
            "dependency_request_duration_seconds",
            unit="s",
            description="Outbound dependency request duration",
        )
        self._db_acquisition = meter.create_histogram(
            "db_connection_acquisition_seconds",
            unit="s",
            description="Database connection acquisition duration",
        )
        self._db_queries = meter.create_histogram(
            "db_query_duration_seconds", unit="s", description="Database query duration"
        )
        self._db_errors = meter.create_counter(
            "db_errors_total", unit="1", description="Database errors"
        )
        self._kafka_messages = meter.create_counter(
            "kafka_messages_total", unit="1", description="Kafka messages"
        )
        self._kafka_lag = meter.create_up_down_counter(
            "kafka_consumer_lag", unit="1", description="Kafka consumer lag"
        )
        self._kafka_lag_values: dict[tuple[str, str], int] = {}
        self._kafka_errors = meter.create_counter(
            "kafka_consumer_errors_total",
            unit="1",
            description="Kafka consumer processing errors",
        )

    def http(self, service: str, route: str, status: int, duration_seconds: float) -> None:
        attributes = {"service": service, "route": route, "status": str(status)}
        self._http_requests.add(1, attributes)
        self._http_duration.record(duration_seconds, {"service": service, "route": route})

    def db(
        self,
        service: str,
        duration_seconds: float,
        *,
        acquisition: bool = False,
        operation: str = "unspecified",
    ) -> None:
        instrument = self._db_acquisition if acquisition else self._db_queries
        attributes = {"service": service}
        if not acquisition:
            attributes["operation"] = operation
        instrument.record(duration_seconds, attributes)

    def db_error(self, service: str) -> None:
        self._db_errors.add(1, {"service": service})

    def kafka(self, service: str, topic: str, direction: str) -> None:
        self._kafka_messages.add(1, {"service": service, "topic": topic, "direction": direction})

    def set_kafka_lag(self, service: str, topic: str, value: int) -> None:
        """Set current lag without accumulating repeated observations."""
        key = (service, topic)
        previous = self._kafka_lag_values.get(key, 0)
        self._kafka_lag.add(value - previous, {"service": service, "topic": topic})
        self._kafka_lag_values[key] = value

    def dependency(self, service: str, dependency: str, duration_seconds: float) -> None:
        """Record one outbound dependency call, including failed calls."""
        self._dependency_duration.record(
            duration_seconds, {"service": service, "dependency": dependency}
        )

    def kafka_error(self, service: str, topic: str) -> None:
        """Record one bounded consumer processing failure."""
        self._kafka_errors.add(1, {"service": service, "topic": topic})

    def initialize_kafka_error_series(self, service: str, topic: str) -> None:
        """Publish a zero baseline so bounded error increases are observable."""
        self._kafka_errors.add(0, {"service": service, "topic": topic})


class TelemetryRuntime:
    """Service-local telemetry providers and instruments."""

    def __init__(
        self,
        service_name: str,
        tracer: trace.Tracer,
        metrics: RuntimeMetrics,
        prometheus_metrics: WorkloadMetrics,
        logger: logging.Logger,
    ) -> None:
        self.service_name = service_name
        self.tracer = tracer
        self.metrics = metrics
        self.prometheus_metrics = prometheus_metrics
        self.logger = logger

    def record_db(
        self,
        service: str,
        duration_seconds: float,
        *,
        acquisition: bool = False,
        operation: str = "unspecified",
    ) -> None:
        """Record a database observation in OTLP and the local scrape registry."""
        self.metrics.db(
            service,
            duration_seconds,
            acquisition=acquisition,
            operation=operation,
        )
        instrument = (
            self.prometheus_metrics.db_acquisition
            if acquisition
            else self.prometheus_metrics.db_query_duration
        )
        if acquisition:
            instrument.labels(service).observe(duration_seconds)
        else:
            instrument.labels(service, operation).observe(duration_seconds)

    def record_dependency(self, service: str, dependency: str, duration_seconds: float) -> None:
        """Record an outbound dependency observation in both metric paths."""
        self.metrics.dependency(service, dependency, duration_seconds)
        self.prometheus_metrics.dependency_duration.labels(service, dependency).observe(
            duration_seconds
        )

    def record_kafka_error(self, service: str, topic: str) -> None:
        """Record a consumer processing failure in both metric paths."""
        self.metrics.kafka_error(service, topic)
        self.prometheus_metrics.kafka_consumer_errors.labels(service, topic).inc()

    def initialize_kafka_error_series(self, service: str, topic: str) -> None:
        """Publish the initial zero error baseline in both telemetry paths."""
        self.metrics.initialize_kafka_error_series(service, topic)
        self.prometheus_metrics.kafka_consumer_errors.labels(service, topic).inc(0)

    def record_kafka_lag(self, service: str, topic: str, value: int) -> None:
        """Record current Kafka lag in both telemetry paths."""
        self.metrics.set_kafka_lag(service, topic, value)
        self.prometheus_metrics.kafka_consumer_lag.labels(service, topic).set(value)


def _otlp_endpoint(endpoint: str | None) -> str | None:
    return endpoint.rstrip("/") if endpoint else None


def create_runtime(
    service_name: str,
    *,
    registry: CollectorRegistry | None = None,
    otlp_endpoint: str | None = None,
) -> TelemetryRuntime:
    """Create a service runtime; exporters are enabled only when configured."""
    endpoint = _otlp_endpoint(otlp_endpoint)
    resource = Resource.create({"service.name": service_name, "service.version": "0.1.1"})

    tracer_provider = TracerProvider(resource=resource)
    logger_provider = LoggerProvider(resource=resource)
    metric_readers: list[Any] = []
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=True),
                export_interval_millis=int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "5000")),
            )
        )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
        )

    meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
    tracer = tracer_provider.get_tracer("agentic-sre", "0.1.1")
    meter = meter_provider.get_meter("agentic-sre", "0.1.1")

    logger = logging.getLogger(f"agentic-sre.{service_name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(JsonLogFormatter(service_name))
        logger.addHandler(stream_handler)
        if endpoint:
            from opentelemetry.sdk._logs import LoggingHandler

            logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))

    return TelemetryRuntime(
        service_name,
        tracer,
        RuntimeMetrics(meter),
        WorkloadMetrics(registry, service_name=service_name),
        logger,
    )


# Kubelet probes and Prometheus scrapes are not requests anyone investigates;
# tracing them would crowd real request traces out of bounded trace searches.
_UNTRACED_PATHS = frozenset({"/health", "/metrics", "/metrics/"})


class TelemetryMiddleware:
    """Create server spans, expose correlation headers, and record HTTP signals."""

    def __init__(self, app: ASGIApp, runtime: TelemetryRuntime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_id = headers.get("x-request-id", str(uuid4()))
        parent_context: Context = propagate.extract(headers)
        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        status_code = 500
        started = time.perf_counter()

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = list(message.get("headers", []))
                response_headers.extend(
                    [
                        (b"x-request-id", request_id.encode("latin-1")),
                        (b"traceparent", _traceparent_header()),
                    ]
                )
                message = {**message, "headers": response_headers}
            await send(message)

        span_context: AbstractContextManager[trace.Span] = (
            nullcontext(trace.INVALID_SPAN)
            if path in _UNTRACED_PATHS
            else self.runtime.tracer.start_as_current_span(
                f"{method} {path}", context=parent_context, kind=SpanKind.SERVER
            )
        )
        with span_context as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("url.path", path)
            span.set_attribute("http.request.header.x_request_id", request_id)
            try:
                await self.app(scope, receive, send_with_context)
            finally:
                duration = time.perf_counter() - started
                span.set_attribute("http.response.status_code", status_code)
                self.runtime.metrics.http(self.runtime.service_name, path, status_code, duration)
                self.runtime.prometheus_metrics.http_requests.labels(
                    self.runtime.service_name, path, str(status_code)
                ).inc()
                self.runtime.prometheus_metrics.http_duration.labels(
                    self.runtime.service_name, path
                ).observe(duration)
                if status_code >= 500:
                    # Failed requests carry their status in the log body so a log
                    # backend can find them without parsing structured fields.
                    self.runtime.logger.error(
                        f"http.request error: {method} {path} returned {status_code}",
                        extra={"request_id": request_id},
                    )
                else:
                    self.runtime.logger.info(
                        "http.request",
                        extra={"request_id": request_id},
                    )


def _traceparent_header() -> bytes:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent", "").encode("latin-1")
