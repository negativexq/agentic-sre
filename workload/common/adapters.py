"""Deterministic workload adapters and production integration boundaries."""

from collections.abc import Callable
from typing import Any, Protocol
from urllib.request import Request, urlopen

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

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def charge(self, request: PaymentRequest) -> PaymentResponse:
        body = request.model_dump_json().encode("utf-8")
        http_request = Request(
            f"{self._base_url}/payments",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(http_request, timeout=self._timeout_seconds) as response:
            return PaymentResponse.model_validate_json(response.read())


class KafkaEventPublisher:
    """Kafka publisher adapter; the broker is never contacted at import time."""

    def __init__(self, bootstrap_servers: str) -> None:
        from confluent_kafka import Producer

        self._producer: Any = Producer({"bootstrap.servers": bootstrap_servers})

    def publish(self, topic: EventTopic, event: OrderCreatedEvent) -> None:
        self._producer.produce(
            topic.value,
            key=str(event.order_id),
            value=event.model_dump_json(),
        )
        self._producer.flush()


class LazyKafkaEventPublisher:
    """Kafka publisher that defers broker initialization until first use."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._publisher: KafkaEventPublisher | None = None

    def publish(self, topic: EventTopic, event: OrderCreatedEvent) -> None:
        if self._publisher is None:
            self._publisher = KafkaEventPublisher(self._bootstrap_servers)
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
