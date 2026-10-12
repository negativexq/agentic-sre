"""A Kafka publish's delivery outcome is observed, and bound to the message's own request (symptom explanation §6)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import pytest
from confluent_kafka import KafkaException
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from prometheus_client import CollectorRegistry
from workload.common.adapters import KafkaEventPublisher, peer_host
from workload.common.contracts import EventTopic, OrderCreatedEvent

from packages.telemetry import create_runtime


class _Metrics:
    def __init__(self) -> None:
        self.kafka_calls: list[str] = []

    def kafka(self, service: str, topic: str, direction: str) -> None:
        self.kafka_calls.append(direction)


class _Logs(logging.Handler):
    """Each error record with the trace id current when it was emitted."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[logging.LogRecord, int]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record, trace.get_current_span().get_span_context().trace_id))


class _KafkaError:
    def name(self) -> str:
        return "_MSG_TIMED_OUT"

    def __str__(self) -> str:
        return "Local: Message timed out"


class _Producer:
    """Holds each delivery report until a flush serves it, as the shared librdkafka queue does."""

    def __init__(self, outcome: Any = None, raises: BaseException | None = None) -> None:
        self.outcome, self.raises = outcome, raises
        self.pending: list[Any] = []

    def produce(self, *args: object, on_delivery: Any = None, **kwargs: object) -> None:
        if self.raises is not None:
            raise self.raises
        self.pending.append(on_delivery)

    def flush(self) -> int:
        while self.pending:
            self.pending.pop(0)(self.outcome, object())
        return 0


def publisher(
    producer: _Producer,
) -> tuple[KafkaEventPublisher, _Metrics, _Logs, InMemorySpanExporter, Any]:
    # a logger of its own: another test's logging.config call disables loggers that already exist
    runtime = create_runtime(f"kafka-delivery-{uuid4()}", registry=CollectorRegistry())
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime.tracer = provider.get_tracer("test")
    metrics = _Metrics()
    runtime.metrics = metrics  # type: ignore[assignment]
    logs = _Logs()
    runtime.logger.addHandler(logs)
    instance = KafkaEventPublisher.__new__(KafkaEventPublisher)
    instance._producer = producer
    instance._runtime = runtime
    instance._server_address = peer_host("kafka:9092")
    return instance, metrics, logs, exporter, runtime


def event() -> OrderCreatedEvent:
    return OrderCreatedEvent(order_id=uuid4(), customer_id="c1", amount_cents=100, currency="USD")


def test_a_delivered_message_is_counted_and_leaves_the_span_unset() -> None:
    instance, metrics, logs, exporter, _ = publisher(_Producer())
    instance.publish(EventTopic.ORDERS_CREATED, event())
    assert metrics.kafka_calls == ["delivered", "produced"]
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.UNSET
    assert not [r for r, _ in logs.records if r.levelno == logging.ERROR]


def test_a_failed_delivery_is_counted_marked_and_logged_without_failing_the_request() -> None:
    instance, metrics, logs, exporter, _ = publisher(_Producer(outcome=_KafkaError()))
    instance.publish(EventTopic.ORDERS_CREATED, event())  # the request's behaviour is unchanged
    assert metrics.kafka_calls == ["delivery_failed", "produced"]
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes is not None and span.attributes["error.type"] == "_MSG_TIMED_OUT"
    ((record, trace_id),) = [(r, t) for r, t in logs.records if r.levelno == logging.ERROR]
    assert record.getMessage().startswith(
        "dependency.request error: kafka delivery failed: _MSG_TIMED_OUT"
    )
    assert getattr(record, "server.address") == "kafka"
    assert getattr(record, "error.type") == "_MSG_TIMED_OUT"
    assert trace_id == span.context.trace_id


def test_a_report_served_by_another_requests_flush_is_bound_to_its_own_message() -> None:
    producer = _Producer(outcome=_KafkaError())
    instance, metrics, logs, exporter, runtime = publisher(producer)
    # request A produces but its report is not yet served
    flush = producer.flush
    producer.flush = lambda: 0  # type: ignore[method-assign]
    instance.publish(EventTopic.ORDERS_CREATED, event())
    producer.flush = flush  # type: ignore[method-assign]
    (span_a,) = exporter.get_finished_spans()
    # request B's flush serves A's report while B's span is the active one
    with runtime.tracer.start_as_current_span("request B") as span_b:
        producer.flush()
    ((_, trace_id),) = [(r, t) for r, t in logs.records if r.levelno == logging.ERROR]
    assert trace_id == span_a.context.trace_id
    assert trace_id != span_b.get_span_context().trace_id
    assert span_b.status.status_code is StatusCode.UNSET
    assert metrics.kafka_calls == ["produced", "delivery_failed"]


@pytest.mark.parametrize("error", [BufferError("Local: Queue full"), KafkaException("local error")])
def test_a_produce_error_is_counted_and_still_fails_the_request(error: BaseException) -> None:
    instance, metrics, _, exporter, _ = publisher(_Producer(raises=error))
    with pytest.raises(type(error)):
        instance.publish(EventTopic.ORDERS_CREATED, event())
    assert metrics.kafka_calls == ["produce_failed"]
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
