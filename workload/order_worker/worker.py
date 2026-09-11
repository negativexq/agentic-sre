"""Kafka order worker with Redis-compatible idempotency boundary."""

import os
from collections.abc import Callable
from typing import Any

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

    def __init__(self, bootstrap_servers: str, group_id: str, worker: OrderWorker) -> None:
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

    def run_forever(self, topic: str) -> None:
        """Consume and acknowledge order-created events."""
        self._consumer.subscribe([topic])
        while True:
            message = self._consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                raise RuntimeError(str(message.error()))
            event = OrderCreatedEvent.model_validate_json(message.value())
            self._worker.process(event)
            self._consumer.commit(message=message)


def build_default_worker(handler: Callable[[OrderCreatedEvent], None]) -> KafkaOrderWorker:
    """Build the production worker with Redis-backed idempotency."""
    state_store = RedisProcessedStateStore(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    return KafkaOrderWorker(
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        os.getenv("KAFKA_CONSUMER_GROUP", "order-worker"),
        OrderWorker(state_store, handler),
    )
