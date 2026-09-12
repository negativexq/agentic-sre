"""Kafka order worker with Redis-compatible idempotency boundary."""

import os
import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from opentelemetry import propagate
from opentelemetry.trace import SpanKind

from packages.telemetry import TelemetryRuntime, create_runtime
from workload.common.adapters import (
    ProcessedStateStore,
    RedisProcessedStateStore,
)
from workload.common.contracts import OrderCreatedEvent


class OrderWorker:
    """Process each order-created event at most once per state store."""

    def __init__(
        self,
        state_store: ProcessedStateStore,
        handler: Callable[[OrderCreatedEvent], None],
    ) -> None:
        self._state_store = state_store
        self._handler = handler

    def process(self, event: OrderCreatedEvent) -> bool:
        """Process an event, returning false for an idempotent duplicate."""
        event_id = str(event.event_id)
        if self._state_store.has_processed(event_id):
            return False
        self._handler(event)
        self._state_store.mark_processed(event_id)
        return True


class KafkaOrderWorker:
    """Long-running Kafka consumer adapter."""

    def __init__(
        self,
        bootstrap_servers: str,
        group_id: str,
        worker: OrderWorker,
        *,
        runtime: TelemetryRuntime | None = None,
    ) -> None:
        from confluent_kafka import Consumer

        self._consumer: Any = Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )
        self._worker = worker
        self._runtime = runtime

    def run_forever(self, topic: str) -> None:
        """Consume and acknowledge order-created events."""
        self._consumer.subscribe([topic])
        while True:
            message = self._consumer.poll(1.0)
            if message is None:
                if self._runtime is not None and int(os.getenv("FAULT_WORKER_DELAY_MS", "0")) > 0:
                    self._runtime.metrics.kafka_lag("order-worker", topic, 101)
                continue
            if message.error():
                raise RuntimeError(str(message.error()))
            headers = {
                key: value.decode("utf-8")
                for key, value in (message.headers() or [])
                if value is not None
            }
            parent_context = propagate.extract(headers)
            span_context = (
                self._runtime.tracer.start_as_current_span(
                    "kafka consume", context=parent_context, kind=SpanKind.CONSUMER
                )
                if self._runtime is not None
                else nullcontext()
            )
            with span_context:
                if self._runtime is not None:
                    self._runtime.metrics.kafka("order-worker", topic, "consumed")
                delay_ms = int(os.getenv("FAULT_WORKER_DELAY_MS", "0"))
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
                if os.getenv("FAULT_WORKER_FAILURE", "false").lower() == "true":
                    if self._runtime is not None:
                        self._runtime.record_kafka_error("order-worker", topic)
                        self._runtime.logger.error("kafka.consumer.failure")
                    continue
                event = OrderCreatedEvent.model_validate_json(message.value())
                self._worker.process(event)
            self._consumer.commit(message=message)


def build_default_worker(handler: Callable[[OrderCreatedEvent], None]) -> KafkaOrderWorker:
    """Build the production worker with Redis-backed idempotency."""
    state_store = RedisProcessedStateStore(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    runtime = create_runtime("order-worker", otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    return KafkaOrderWorker(
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        os.getenv("KAFKA_CONSUMER_GROUP", "order-worker"),
        OrderWorker(state_store, handler),
        runtime=runtime,
    )
