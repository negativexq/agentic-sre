"""Telemetry correlation tests across service and Kafka boundaries."""

from prometheus_client import CollectorRegistry, generate_latest

from packages.telemetry import TelemetryContext, WorkloadMetrics, create_runtime, structured_log


def test_context_round_trips_through_kafka_style_headers() -> None:
    context = TelemetryContext(request_id="req-1", trace_id="trace-1", span_id="span-1")
    restored = TelemetryContext.from_headers(context.to_headers())

    assert restored == context
    assert (
        structured_log("payment.authorized", restored, service="payment-service")["trace_id"]
        == "trace-1"
    )


def test_required_context_headers_are_enforced() -> None:
    try:
        TelemetryContext.from_headers({"x-request-id": "req-1"})
    except ValueError as error:
        assert "trace_id" in str(error)
    else:
        raise AssertionError("missing telemetry headers were accepted")


def test_metrics_expose_http_database_and_kafka_signals() -> None:
    registry = CollectorRegistry()
    metrics = WorkloadMetrics(registry)
    metrics.http_requests.labels("order-service", "/orders", "200").inc()
    metrics.db_active_connections.labels("payment-service").set(3)
    metrics.kafka_messages.labels("order-worker", "orders.created", "consumed").inc()

    output = generate_latest(registry).decode()
    assert "http_requests_total" in output
    assert "db_active_connections" in output
    assert "kafka_messages_total" in output
    assert "db_connection_acquisition_seconds" in output
    assert "db_query_duration_seconds" in output
    assert "dependency_request_duration_seconds" in output


def test_kafka_lag_is_a_current_gauge_not_an_accumulating_sample() -> None:
    registry = CollectorRegistry()
    runtime = create_runtime("order-worker", registry=registry)

    runtime.record_kafka_lag("order-worker", "orders.created", 12)
    runtime.record_kafka_lag("order-worker", "orders.created", 4)

    output = generate_latest(registry).decode()
    assert 'kafka_consumer_lag{service="order-worker",topic="orders.created"} 4.0' in output
