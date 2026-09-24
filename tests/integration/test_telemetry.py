"""Telemetry correlation tests across service and Kafka boundaries."""

import logging
import re
import socket
from pathlib import Path
from urllib.error import URLError
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send
from workload.common.adapters import HttpPaymentGateway
from workload.common.contracts import PaymentRequest

from packages.telemetry import TelemetryContext, WorkloadMetrics, create_runtime, structured_log
from packages.telemetry.runtime import TelemetryMiddleware, TelemetryRuntime


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


def test_metrics_expose_process_start_time_for_named_service() -> None:
    registry = CollectorRegistry()
    WorkloadMetrics(registry, service_name="payment-service")

    output = generate_latest(registry).decode()

    assert 'service_process_start_time_seconds{service="payment-service"}' in output
    value = float(
        output.split('service_process_start_time_seconds{service="payment-service"} ')[1].split()[0]
    )
    assert value > 0


def test_kafka_lag_is_a_current_gauge_not_an_accumulating_sample() -> None:
    registry = CollectorRegistry()
    runtime = create_runtime("order-worker", registry=registry)

    runtime.record_kafka_lag("order-worker", "orders.created", 12)
    runtime.record_kafka_lag("order-worker", "orders.created", 4)

    output = generate_latest(registry).decode()
    assert 'kafka_consumer_lag{service="order-worker",topic="orders.created"} 4.0' in output


def test_worker_failure_alert_uses_bounded_error_burst_signal() -> None:
    rules = Path("infra/observability/prometheus-rules.yml").read_text(encoding="utf-8")
    manifest = Path("infra/kubernetes/observability.yaml").read_text(encoding="utf-8")
    expression = 'sum(increase(kafka_consumer_errors_total{service="order-worker"}[30s])) > 0'
    old_expression = 'sum(kafka_consumer_errors_total{service="order-worker"}) > 0'
    assert expression in rules
    assert expression in manifest
    assert not re.search(r"- alert: OrderWorkerConsumerErrorsHigh\n\s+expr: [^\n]+\n\s+for:", rules)
    assert not re.search(
        r"- alert: OrderWorkerConsumerErrorsHigh\n\s+expr: [^\n]+\n\s+for:", manifest
    )
    assert old_expression not in rules
    assert old_expression not in manifest


def test_worker_lag_alerts_are_counter_reset_safe() -> None:
    rules = Path("infra/observability/prometheus-rules.yml").read_text(encoding="utf-8")
    manifest = Path("infra/kubernetes/observability.yaml").read_text(encoding="utf-8")
    for text in (rules, manifest):
        assert "- alert: KafkaConsumerLag" in text
        assert "- alert: OrderWorkerLagHigh" in text
        assert text.count("increase(kafka_messages_total") >= 4
        assert text.count("expr: clamp_min(sum(kafka_messages_total") == 0


def test_configuration_latency_alert_has_distinct_bounded_signal() -> None:
    rules = Path("infra/observability/prometheus-rules.yml").read_text(encoding="utf-8")
    manifest = Path("infra/kubernetes/observability.yaml").read_text(encoding="utf-8")
    expression = (
        'sum(rate(http_request_duration_seconds_sum{service="payment-service",route="/payments"}'
        '[30s])) / sum(rate(http_request_duration_seconds_count{service="payment-service",'
        'route="/payments"}[30s])) > 8'
    )
    assert expression in rules
    assert expression in manifest
    assert not re.search(r"- alert: PaymentServiceLatencyCritical\n\s+expr: [^\n]+\n\s+for:", rules)
    assert not re.search(
        r"- alert: PaymentServiceLatencyCritical\n\s+expr: [^\n]+\n\s+for:", manifest
    )


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _captured(runtime: TelemetryRuntime) -> _Capture:
    capture = _Capture()
    runtime.logger.addHandler(capture)
    return capture


def test_failed_requests_are_logged_as_errors_with_their_status() -> None:
    runtime = create_runtime("log-test-service", registry=CollectorRegistry())
    capture = _captured(runtime)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        status = 503 if scope["path"] == "/fail" else 200
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    client = TestClient(TelemetryMiddleware(app, runtime))
    client.get("/ok")
    client.post("/fail")

    levels = [(record.levelno, record.getMessage()) for record in capture.records]
    assert levels == [
        (logging.INFO, "http.request"),
        (logging.ERROR, "http.request error: POST /fail returned 503"),
    ]


def test_failed_dependency_calls_log_the_real_error() -> None:
    runtime = create_runtime("log-test-caller", registry=CollectorRegistry())
    capture = _captured(runtime)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    gateway = HttpPaymentGateway(f"http://127.0.0.1:{port}", timeout_seconds=1, runtime=runtime)
    request = PaymentRequest(order_id=uuid4(), amount_cents=100, currency="USD")

    with pytest.raises(URLError):
        gateway.charge(request)

    (record,) = [item for item in capture.records if item.levelno == logging.ERROR]
    assert record.getMessage().startswith("dependency.request error: payment-service URLError")
    assert "refused" in record.getMessage().lower()
