"""Minimal workload metrics boundary backed by Prometheus client."""

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.registry import CollectorRegistry


class WorkloadMetrics:
    """Stable HTTP, database, and Kafka metric instruments."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.http_requests = Counter(
            "http_requests_total",
            "Total HTTP requests",
            ["service", "route", "status"],
            registry=registry,
        )
        self.http_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration",
            ["service", "route"],
            registry=registry,
        )
        self.db_active_connections = Gauge(
            "db_active_connections", "Active database connections", ["service"], registry=registry
        )
        self.kafka_messages = Counter(
            "kafka_messages_total",
            "Kafka messages",
            ["service", "topic", "direction"],
            registry=registry,
        )
        self.kafka_consumer_lag = Gauge(
            "kafka_consumer_lag", "Kafka consumer lag", ["service", "topic"], registry=registry
        )
