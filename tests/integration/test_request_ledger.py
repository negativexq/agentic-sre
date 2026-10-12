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
from scripts.request_ledger_reconcile import (
    TraceFacts,
    execution_facts,
    lifecycle,
    place,
    trace_facts,
    windows,
)
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


def _entry(trace: str, record: str, **labels: str) -> dict[str, str]:
    return {"trace_id": trace, "execution_record": record, "k8s_pod_uid": "uid-1", **labels}


def test_the_execution_record_names_the_last_failed_operation_of_a_5xx() -> None:
    def operation(started: str, kind: str, outcome: str, **labels: str) -> dict[str, str]:
        return _entry(
            "t1",
            "operation",
            execution_operation_started_ns=started,
            execution_operation_kind=kind,
            execution_operation_outcome=outcome,
            **labels,
        )

    facts = execution_facts(
        [
            _entry("t1", "start"),
            operation("1", "DB", "OK", server_address="postgres"),
            operation(
                "2",
                "HTTP_CLIENT",
                "ERROR",
                server_address="payment-service",
                error_type="TimeoutError",
            ),
            _entry("t1", "terminal", http_response_status_code="500"),
            _entry("t2", "start"),
            operation("1", "DB", "ERROR"),
            _entry("t2", "terminal", http_response_status_code="201"),
            {"trace_id": "t3", "detected_level": "info"},  # not an ExecutionRecord entry
        ]
    )
    assert set(facts) == {"t1", "t2"}
    terminal = facts["t1"].terminal_operation
    assert terminal is not None
    assert (terminal.kind, terminal.server_address, terminal.error_type) == (
        "HTTP_CLIENT",
        "payment-service",
        "TimeoutError",
    )
    # a request that succeeded has no terminal operation, whatever failed inside it
    assert facts["t2"].terminal_operation is None


def test_a_start_without_a_terminal_is_an_open_lifecycle_not_a_success() -> None:
    facts = execution_facts(
        [
            _entry("a", "start"),
            _entry("a", "terminal", http_response_status_code="201"),
            _entry("b", "start"),
            _entry("c", "terminal", http_response_status_code="500"),
        ]
    )
    assert facts["b"].terminal_status is None
    assert lifecycle(facts) == {
        "uid-1": {"starts": 2, "terminals": 2, "open": 1, "terminal_without_start": 1}
    }


@pytest.mark.parametrize(
    ("start", "stop", "step", "expected"),
    [
        (0, 25, 10, [(0, 10), (11, 21), (22, 25)]),
        (5, 5, 10, [(5, 5)]),
        (6, 5, 10, []),
    ],
)
def test_loki_windows_cover_the_range_without_overlap(
    start: int, stop: int, step: int, expected: list[tuple[int, int]]
) -> None:
    assert windows(start, stop, step) == expected


def test_a_tempo_search_window_restores_trace_ids_and_reads_the_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.request_ledger_reconcile as reconcile

    found = [
        {
            "traceID": "abc",  # Tempo's search drops leading zeros
            "spanSets": [{"spans": [{"attributes": [_attribute("app.order.id", ORDER)]}]}],
        }
    ]

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server hook
            body = json.dumps({"traces": found}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert reconcile._tempo_window(url, 0, 10**10) == {
            "0" * 29 + "abc": TraceFacts(server=True, order_id=ORDER)
        }
        monkeypatch.setattr(reconcile, "TEMPO_LIMIT", 1)
        with pytest.raises(RuntimeError, match="truncated"):
            reconcile._tempo_window(url, 0, 10**10)
    finally:
        server.shutdown()
        server.server_close()
