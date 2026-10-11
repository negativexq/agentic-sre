"""The workload's outgoing calls carry their peer as the OpenTelemetry semantic conventions name it (m21 §29.6)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client import CollectorRegistry
from workload.common.adapters import KafkaEventPublisher, RedisProcessedStateStore, peer_host
from workload.common.contracts import EventTopic, OrderCreatedEvent
from workload.payment_service.app import PaymentService

from packages.telemetry import create_runtime


def traced(service: str) -> tuple[Any, InMemorySpanExporter]:
    runtime = create_runtime(service, registry=CollectorRegistry())
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime.tracer = provider.get_tracer("test")
    return runtime, exporter


def test_the_peer_is_the_host_the_client_was_configured_with() -> None:
    assert peer_host("postgresql+psycopg://user:secret@postgres:5432/db") == "postgres"
    assert peer_host("redis://redis:6379/0") == "redis"
    assert peer_host("kafka:9092") == "kafka"
    assert peer_host("kafka-0:9092, kafka-1:9092") == "kafka-0"
    assert peer_host("http://payment-service:8000") == "payment-service"


def test_a_kafka_publish_is_a_producer_span_naming_its_broker() -> None:
    runtime, exporter = traced("order-service")

    class _Producer:
        def produce(self, *args: object, **kwargs: object) -> None:
            pass

        def flush(self) -> None:
            pass

    publisher = KafkaEventPublisher.__new__(KafkaEventPublisher)
    publisher._producer = _Producer()
    publisher._runtime = runtime
    publisher._server_address = peer_host("kafka:9092")
    event = OrderCreatedEvent(order_id=uuid4(), customer_id="c1", amount_cents=100, currency="USD")
    publisher.publish(EventTopic.ORDERS_CREATED, event)
    (span,) = exporter.get_finished_spans()
    assert span.kind is SpanKind.PRODUCER
    assert dict(span.attributes or {}) == {
        "messaging.system": "kafka",
        "messaging.destination.name": "orders.created",
        "server.address": "kafka",
    }


def test_redis_calls_are_client_spans_naming_their_server() -> None:
    runtime, exporter = traced("order-worker")

    class _Redis:
        def exists(self, key: str) -> int:
            return 0

        def set(self, key: str, value: str) -> None:
            pass

    store = RedisProcessedStateStore.__new__(RedisProcessedStateStore)
    store._key_prefix = "p:"
    store._redis = _Redis()
    store._runtime = runtime
    store._attributes = {"db.system": "redis", "server.address": peer_host("redis://redis:6379/0")}
    store.has_processed("e1")
    store.mark_processed("e1")
    spans = exporter.get_finished_spans()
    assert [(s.name, s.kind) for s in spans] == [
        ("redis exists", SpanKind.CLIENT),
        ("redis set", SpanKind.CLIENT),
    ]
    assert all(dict(s.attributes or {})["server.address"] == "redis" for s in spans)


def test_a_database_call_names_the_configured_host_only_when_one_was_configured() -> None:
    configured = PaymentService(lambda: None, db_address="postgres")  # type: ignore[arg-type, return-value]
    assert configured._db_attributes == {"db.system": "postgresql", "server.address": "postgres"}
    assert PaymentService(lambda: None)._db_attributes == {"db.system": "postgresql"}  # type: ignore[arg-type, return-value]
