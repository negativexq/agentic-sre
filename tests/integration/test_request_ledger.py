"""The caller's own record of every request, and its join to the workload's records (checkpoint 1)."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from scripts.request_ledger_reconcile import TraceFacts, place, trace_facts
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from workload.common.adapters import InMemoryEventPublisher, InProcessPaymentGateway
from workload.common.contracts import OrderCreateRequest
from workload.common.models import WorkloadBase
from workload.order_service.app import OrderService
from workload.payment_service.app import PaymentService

from packages.evals.live.actions import Context
from packages.evals.live.runner import WorkloadDriver
from packages.evals.live.scenarios import Target, Workload
from packages.storage.database import create_session_factory

ORDER = "11111111-1111-4111-8111-111111111111"


def drive(handler: type[BaseHTTPRequestHandler] | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Two requests through the driver; the ledger rows and the traceparents the server saw."""
    seen: list[str] = []
    if handler is None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = None
    else:

        class _Seen(handler):  # type: ignore[valid-type,misc]
            def do_POST(self) -> None:  # noqa: N802 - http.server hook
                seen.append(self.headers.get("traceparent", ""))
                super().do_POST()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = HTTPServer(("127.0.0.1", 0), _Seen)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
    rows: list[dict[str, Any]] = []
    driver = WorkloadDriver(
        Workload(target=Target.ORDERS, count=1, concurrency=1, interval_seconds=0.01),
        Context(order_url=f"http://127.0.0.1:{port}"),
        ledger=rows.append,
    )
    driver.start()
    deadline = time.monotonic() + 5
    while len(rows) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    driver.stop()
    if server is not None:
        server.shutdown()
        server.server_close()
    return rows, seen


class _Created(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server hook
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"order_id": ORDER}).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Failed(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server hook
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_each_request_carries_the_trace_id_the_caller_recorded() -> None:
    rows, seen = drive(_Created)
    assert len(rows) >= 2
    for row, traceparent in zip(rows, seen, strict=False):
        assert traceparent.startswith(f"00-{row['trace_id']}-")
        assert row["ok"] is True and row["status"] is None and row["error"] is None
        assert row["order_id"] == ORDER
        assert row["started"] <= row["finished"]
    assert len({row["trace_id"] for row in rows}) == len(rows)


def test_a_failed_request_keeps_its_status() -> None:
    rows, _ = drive(_Failed)
    assert rows and all(
        (r["ok"], r["status"], r["error"], r["order_id"]) == (False, 500, "500", None) for r in rows
    )


def test_a_request_with_no_answer_names_its_cause() -> None:
    rows, _ = drive(None)
    assert rows and all(
        (r["ok"], r["status"], r["error"]) == (False, None, "ConnectionRefusedError") for r in rows
    )


@pytest.fixture
def workload_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(f"sqlite:///{tmp_path / 'workload.db'}")
    WorkloadBase.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def test_the_server_span_names_the_order_it_created(
    workload_factory: sessionmaker[Session],
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    service = OrderService(
        workload_factory,
        InProcessPaymentGateway(PaymentService(workload_factory).charge),
        InMemoryEventPublisher(),
        id_factory=lambda: UUID(ORDER),
    )
    with provider.get_tracer("test").start_as_current_span("POST /orders"):
        service.create(OrderCreateRequest(customer_id="c-1", amount_cents=100, currency="USD"))
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None and span.attributes["app.order.id"] == ORDER


def _attribute(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def test_trace_facts_read_the_order_and_its_failed_hops() -> None:
    document = {
        "batches": [
            {
                "resource": {"attributes": [_attribute("service.name", "order-service")]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "POST /orders",
                                "kind": "SPAN_KIND_SERVER",
                                "attributes": [_attribute("app.order.id", ORDER)],
                            },
                            {
                                "name": "POST payment-service /payments",
                                "kind": "SPAN_KIND_CLIENT",
                                "attributes": [_attribute("error.type", "TimeoutError")],
                            },
                        ]
                    }
                ],
            }
        ]
    }
    assert trace_facts(document) == TraceFacts(
        server=True, order_id=ORDER, payment_called=True, payment_error="TimeoutError"
    )


@pytest.mark.parametrize(
    ("caller_ok", "facts", "status", "consumed", "expected"),
    [
        (False, None, None, None, "UNPLACED_NO_TRACE"),
        (True, None, None, None, "OK_NO_TRACE"),
        (True, TraceFacts(server=True), "PAID", True, "OK"),
        (True, TraceFacts(server=True), "PAID", False, "OK_NOT_CONSUMED"),
        (False, TraceFacts(server=True), None, None, "FAILED_BEFORE_ORDER_ROW"),
        (False, TraceFacts(), None, None, "UNPLACED"),
        (False, TraceFacts(server=True), "PENDING", None, "FAILED_AT_PAYMENT_HOP"),
        (False, None, "PAID", None, "FAILED_AFTER_ORDER_UPDATE"),
    ],
)
def test_a_request_is_placed_only_by_its_records(
    caller_ok: bool,
    facts: TraceFacts | None,
    status: str | None,
    consumed: bool | None,
    expected: str,
) -> None:
    assert place(caller_ok, facts, status, consumed) == expected
