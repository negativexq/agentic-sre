"""A failed dependency call is recorded as structured evidence inside its own client span (symptom explanation §8)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.error import URLError
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from prometheus_client import CollectorRegistry, generate_latest
from workload.common.adapters import HttpPaymentGateway
from workload.common.contracts import PaymentRequest

from packages.telemetry import create_runtime


class _Errors(logging.Handler):
    """Each error record with the span id current when it was emitted."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[logging.LogRecord, int]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.records.append((record, trace.get_current_span().get_span_context().span_id))


def gateway(
    url: str, timeout: float = 2
) -> tuple[HttpPaymentGateway, InMemorySpanExporter, _Errors, Any]:
    # a logger of its own: another test's logging.config call disables loggers that already exist
    registry = CollectorRegistry()
    runtime = create_runtime(f"dependency-failure-{uuid4()}", registry=registry)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime.tracer = provider.get_tracer("test")
    errors = _Errors()
    runtime.logger.addHandler(errors)
    return (
        HttpPaymentGateway(url, timeout_seconds=timeout, runtime=runtime),
        exporter,
        errors,
        registry,
    )


def charge(client: HttpPaymentGateway) -> None:
    with pytest.raises((URLError, TimeoutError)):
        client.charge(PaymentRequest(order_id=uuid4(), amount_cents=100, currency="USD"))


def serve(handler: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    return server, thread


def assert_structured(exporter: InMemorySpanExporter, errors: _Errors, kind: str) -> None:
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["server.address"] == "127.0.0.1"
    assert span.attributes["error.type"] == kind
    assert span.status.status_code is StatusCode.ERROR
    ((record, span_id),) = errors.records
    assert span_id == span.context.span_id  # logged inside the client span, not after it
    assert getattr(record, "server.address") == "127.0.0.1"
    assert getattr(record, "error.type") == kind
    assert getattr(record, "http.request.method") == "POST"
    assert record.getMessage().startswith("dependency.request error: payment-service ")


def test_a_refused_connection_names_its_cause_and_peer() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client, exporter, errors, _ = gateway(f"http://127.0.0.1:{port}")
    charge(client)
    assert_structured(exporter, errors, "ConnectionRefusedError")


def test_an_http_error_names_its_status_code() -> None:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server hook
            self.send_response(500)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(_Handler)
    client, exporter, errors, _ = gateway(f"http://127.0.0.1:{server.server_address[1]}")
    charge(client)
    thread.join()
    server.server_close()
    assert_structured(exporter, errors, "500")


def test_a_timeout_names_its_cause() -> None:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server hook
            time.sleep(0.5)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server, thread = serve(_Handler)
    client, exporter, errors, _ = gateway(f"http://127.0.0.1:{server.server_address[1]}", 0.1)
    charge(client)
    thread.join()
    server.server_close()
    assert_structured(exporter, errors, "TimeoutError")


def test_the_duration_metric_carries_the_configured_peer() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client, _, _, registry = gateway(f"http://127.0.0.1:{port}")
    charge(client)
    output = generate_latest(registry).decode()
    assert (
        'dependency_request_duration_seconds_count{dependency="payment-service",'
        'server_address="127.0.0.1",service="' in output
    )
