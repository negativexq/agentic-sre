"""ExecutionRecord: every request's start, operations and terminal (symptom explanation design §4.2)."""

from __future__ import annotations

import logging
import socket
from typing import Any
from urllib.error import URLError
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client import CollectorRegistry
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send
from workload.common.adapters import HttpPaymentGateway
from workload.common.contracts import PaymentRequest

from packages.telemetry import create_runtime
from packages.telemetry.execution import ExecutionRecordProcessor, operation_kind
from packages.telemetry.runtime import TelemetryMiddleware


class _Records(logging.Handler):
    """Each record with the trace and span ids current when it was emitted."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[logging.LogRecord, int, int]] = []

    def emit(self, record: logging.LogRecord) -> None:
        context = trace.get_current_span().get_span_context()
        self.records.append((record, context.trace_id, context.span_id))


def test_each_request_has_a_start_and_a_terminal_on_their_own_logger() -> None:
    # a logger of its own: another test's logging.config call disables loggers that already exist
    runtime = create_runtime(f"execution-{uuid4()}", registry=CollectorRegistry())
    assert runtime.execution_logger is not None
    execution, application = _Records(), _Records()
    runtime.execution_logger.addHandler(execution)
    runtime.logger.addHandler(application)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        status = 503 if scope["path"] == "/fail" else 200
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    client = TestClient(TelemetryMiddleware(app, runtime))
    client.get("/ok")
    client.post("/fail")
    client.get("/health")
    client.get("/ready")

    seen = [
        (
            r.getMessage(),
            getattr(r, "url.path"),
            getattr(r, "http.response.status_code", None),
        )
        for r, _, _ in execution.records
    ]
    assert seen == [
        ("execution.start", "/ok", None),
        ("execution.terminal", "/ok", 200),
        ("execution.start", "/fail", None),
        ("execution.terminal", "/fail", 503),
    ]
    # start and terminal of one request share its request id and its trace
    first, second = execution.records[0], execution.records[1]
    assert first[0].__dict__["request_id"] == second[0].__dict__["request_id"]
    assert first[1] == second[1] != 0
    # the application's own log stream is unchanged
    assert all(not r.getMessage().startswith("execution.") for r, _, _ in application.records)


def test_a_failed_outgoing_call_is_an_operation_record_in_its_own_span() -> None:
    runtime = create_runtime(f"execution-{uuid4()}", registry=CollectorRegistry())
    logger = logging.getLogger(f"execution-test-{uuid4()}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    records = _Records()
    logger.addHandler(records)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(ExecutionRecordProcessor(logger, pod_uid="uid-1"))
    runtime.tracer = provider.get_tracer("test")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    gateway = HttpPaymentGateway(f"http://127.0.0.1:{port}", timeout_seconds=1, runtime=runtime)

    with pytest.raises(URLError):
        gateway.charge(PaymentRequest(order_id=uuid4(), amount_cents=100, currency="USD"))

    (span,) = exporter.get_finished_spans()
    ((record, trace_id, span_id),) = records.records
    assert (trace_id, span_id) == (span.context.trace_id, span.context.span_id)
    assert record.getMessage() == "execution.operation"
    assert getattr(record, "execution.operation.kind") == "HTTP_CLIENT"
    assert getattr(record, "execution.operation.outcome") == "ERROR"
    assert getattr(record, "error.type") == "ConnectionRefusedError"
    assert getattr(record, "server.address") == "127.0.0.1"
    assert getattr(record, "k8s.pod.uid") == "uid-1"
    assert getattr(record, "execution.operation.duration_ns") >= 0


def test_a_server_span_is_not_an_operation() -> None:
    logger = logging.getLogger(f"execution-test-{uuid4()}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    records = _Records()
    logger.addHandler(records)
    provider = TracerProvider()
    provider.add_span_processor(ExecutionRecordProcessor(logger))
    with provider.get_tracer("test").start_as_current_span("GET /x", kind=SpanKind.SERVER):
        pass
    assert records.records == []


@pytest.mark.parametrize(
    ("kind", "attributes", "expected"),
    [
        (SpanKind.CLIENT, {"db.system": "postgresql"}, "DB"),
        (SpanKind.CLIENT, {"db.system": "redis"}, "DB"),
        (SpanKind.PRODUCER, {"messaging.system": "kafka"}, "MESSAGING_PUBLISH"),
        (SpanKind.CLIENT, {"http.request.method": "POST"}, "HTTP_CLIENT"),
        (SpanKind.CLIENT, {}, "OTHER"),
        (SpanKind.SERVER, {"http.request.method": "POST"}, None),
        (SpanKind.INTERNAL, {}, None),
    ],
)
def test_operation_kinds(kind: SpanKind, attributes: dict[str, Any], expected: str | None) -> None:
    assert operation_kind(kind, attributes, "span") == expected
