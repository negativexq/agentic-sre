"""Deterministic workload adapters and production integration boundaries."""

from collections.abc import Callable
from contextlib import nullcontext
from time import perf_counter
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from workload.common.contracts import (
    EventTopic,
    OrderCreatedEvent,
    PaymentRequest,
    PaymentResponse,
)


class PaymentGateway(Protocol):
    """Synchronous payment authorization boundary."""

    def charge(self, request: PaymentRequest) -> PaymentResponse:
        """Authorize a payment."""


class EventPublisher(Protocol):
    """Event publication boundary."""

    def publish(self, topic: EventTopic, event: OrderCreatedEvent) -> None:
        """Publish one event."""


class ProcessedStateStore(Protocol):
    """Idempotency state boundary for workers."""

    def has_processed(self, event_id: str) -> bool:
        """Return whether an event was already processed."""

    def mark_processed(self, event_id: str) -> None:
        """Record an event as processed."""


class InMemoryEventPublisher:
    """Deterministic publisher used by tests and local composition."""

    def __init__(self) -> None:
        self.events: list[tuple[EventTopic, OrderCreatedEvent]] = []

    def publish(self, topic: EventTopic, event: OrderCreatedEvent) -> None:
        self.events.append((topic, event))


class InMemoryProcessedStateStore:
    """Deterministic idempotency store used by tests."""

    def __init__(self) -> None:
        self._processed: set[str] = set()

    def has_processed(self, event_id: str) -> bool:
        return event_id in self._processed

    def mark_processed(self, event_id: str) -> None:
        self._processed.add(event_id)


class HttpPaymentGateway:
    """Small standard-library HTTP adapter for payment-service calls."""

    def __init__(
        self, base_url: str, *, timeout_seconds: float = 5.0, runtime: Any | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._runtime = runtime

    def charge(self, request: PaymentRequest) -> PaymentResponse:
        body = request.model_dump_json().encode("utf-8")
        # A CLIENT span whose context is injected into the request, so the
        # payment-service SERVER span is its direct child in the trace.
        span_context = (
            self._runtime.tracer.start_as_current_span(
                "POST payment-service /payments", kind=SpanKind.CLIENT
            )
            if self._runtime is not None
            else nullcontext(trace.INVALID_SPAN)
        )
        started = perf_counter()
        try:
            with span_context as span:
                headers = {"Content-Type": "application/json"}
                propagate.inject(headers)
                http_request = Request(
                    f"{self._base_url}/payments",
                    data=body,
                    headers=headers,
                    method="POST",
                )
                span.set_attribute("http.request.method", "POST")
                span.set_attribute("server.address", "payment-service")
                try:
                    with urlopen(http_request, timeout=self._timeout_seconds) as response:
                        span.set_attribute("http.response.status_code", response.status)
                        return PaymentResponse.model_validate_json(response.read())
                except HTTPError as exc:
                    span.set_attribute("http.response.status_code", exc.code)
                    span.set_status(Status(StatusCode.ERROR, f"HTTP {exc.code}"))
                    raise
        except Exception as exc:
            if self._runtime is not None:
                self._runtime.logger.error(
                    f"dependency.request error: payment-service {type(exc).__name__}: {exc}"
                )
            raise
        finally:
            if self._runtime is not None:
                self._runtime.record_dependency(
                    self._runtime.service_name, "payment-service", perf_counter() - started
                )


class KafkaEventPublisher:
    """Kafka publisher adapter; the broker is never contacted at import time."""

    def __init__(self, bootstrap_servers: str, *, runtime: Any | None = None) -> None:
        from confluent_kafka import Producer

        self._producer: Any = Producer({"bootstrap.servers": bootstrap_servers})
        self._runtime = runtime

    def publish(self, topic: EventTopic, event: OrderCreatedEvent) -> None:
        headers: dict[str, str] = {}
        propagate.inject(headers)
        span_context = (
            self._runtime.tracer.start_as_current_span("kafka publish")
            if self._runtime is not None
            else nullcontext()
        )
        with span_context:
            self._producer.produce(
                topic.value,
                key=str(event.order_id),
                value=event.model_dump_json(),
                headers=list(headers.items()),
            )
            self._producer.flush()
        if self._runtime is not None:
            self._runtime.metrics.kafka(self._runtime.service_name, topic.value, "produced")


class LazyKafkaEventPublisher:
    """Kafka publisher that defers broker initialization until first use."""

    def __init__(self, bootstrap_servers: str, *, runtime: Any | None = None) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._runtime = runtime
        self._publisher: KafkaEventPublisher | None = None

    def publish(self, topic: EventTopic, event: OrderCreatedEvent) -> None:
        if self._publisher is None:
            self._publisher = KafkaEventPublisher(self._bootstrap_servers, runtime=self._runtime)
        self._publisher.publish(topic, event)


class RedisProcessedStateStore:
    """Redis-backed idempotency adapter."""

    def __init__(self, redis_url: str, *, key_prefix: str = "agentic-sre:processed:") -> None:
        from redis import Redis

        self._key_prefix = key_prefix
        self._redis: Any = Redis.from_url(redis_url, decode_responses=True)

    def has_processed(self, event_id: str) -> bool:
        return bool(self._redis.exists(f"{self._key_prefix}{event_id}"))

    def mark_processed(self, event_id: str) -> None:
        self._redis.set(f"{self._key_prefix}{event_id}", "1")


class InProcessPaymentGateway:
    """Adapter that invokes PaymentService without network hops in tests."""

    def __init__(self, charge: Callable[[PaymentRequest], PaymentResponse]) -> None:
        self._charge = charge

    def charge(self, request: PaymentRequest) -> PaymentResponse:
        return self._charge(request)
