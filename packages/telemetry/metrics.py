"""Minimal workload metrics boundary backed by Prometheus client."""

import time

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.registry import CollectorRegistry


class WorkloadMetrics:
    """Stable HTTP, database, and Kafka metric instruments."""

    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        *,
        service_name: str | None = None,
    ) -> None:
        self.process_start_time = Gauge(
            "service_process_start_time_seconds",
            "Unix timestamp when the workload process started",
            ["service"],
            registry=registry,
        )
        if service_name is not None:
            self.process_start_time.labels(service=service_name).set(time.time())
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
        self.dependency_duration = Histogram(
            "dependency_request_duration_seconds",
            "Outbound dependency request duration",
            ["service", "dependency"],
            registry=registry,
        )
        self.db_acquisition = Histogram(
            "db_connection_acquisition_seconds",
            "Database connection acquisition duration",
            ["service"],
            registry=registry,
        )
        self.db_query_duration = Histogram(
            "db_query_duration_seconds",
            "Database query duration",
            ["service", "operation"],
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
        self.kafka_consumer_errors = Counter(
            "kafka_consumer_errors_total",
            "Kafka consumer processing errors",
            ["service", "topic"],
            registry=registry,
        )
