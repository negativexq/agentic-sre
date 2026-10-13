"""Source request/span identity survives formatting, Loki and frozen storage."""

from __future__ import annotations

import io
import json
import logging
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import URLError
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send
from workload.common.adapters import HttpPaymentGateway
from workload.common.contracts import PaymentRequest

from packages.rca.live import LokiLogReader
from packages.rca.model import LogRecord
from packages.storage.models import Base, IncidentRow
from packages.storage.repositories import LogObservationRepository
from packages.telemetry import create_runtime
from packages.telemetry.correlation import current_request_id, request_context
from packages.telemetry.runtime import JsonLogFormatter, TelemetryMiddleware


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(JsonLogFormatter("order-service"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_failed_request_source_identity_survives_loki_and_storage() -> None:
    runtime = create_runtime(f"log-correlation-{uuid4()}", registry=CollectorRegistry())
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime.tracer = provider.get_tracer("correlation-test")
    capture = Capture()
    runtime.logger.handlers = [capture]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    gateway = HttpPaymentGateway(f"http://127.0.0.1:{port}", runtime=runtime)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        with pytest.raises(URLError):
            gateway.charge(PaymentRequest(order_id=uuid4(), amount_cents=100, currency="USD"))
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"failed"})

    client = TestClient(TelemetryMiddleware(app, runtime))
    for request in ("request-a", "request-b"):
        assert client.get("/orders", headers={"x-request-id": request}).status_code == 500
    lines = [json.loads(line) for line in capture.lines]
    dependency = [row for row in lines if str(row["event"]).startswith("dependency.request")]
    symptom = [row for row in lines if str(row["event"]).startswith("http.request error")]
    assert [row["request_id"] for row in dependency] == ["request-a", "request-b"]
    spans = {format(span.context.span_id, "016x"): span for span in exporter.get_finished_spans()}
    for dep, parent in zip(dependency, symptom, strict=True):
        assert dep["trace_id"] == parent["trace_id"]
        assert dep["parent_span_id"] == parent["span_id"]
        assert dep["span_id"] != parent["span_id"]
        span_parent = spans[dep["span_id"]].parent
        assert span_parent is not None
        assert format(span_parent.span_id, "016x") == dep["parent_span_id"]
        assert dep["error.type"] == "ConnectionRefusedError"
        assert dep["server.address"] == "127.0.0.1"
        assert dep["dependency.name"] == "payment-service"
        assert "target_uid" not in dep

    now = datetime.now(UTC)
    payload = {
        "data": {
            "result": [
                {
                    "stream": {"service_name": "order-service", "level": "ERROR"},
                    "values": [[str(int(now.timestamp() * 1e9)), json.dumps(dependency[0])]],
                }
            ]
        }
    }

    class Response(io.BytesIO):
        pass

    def opener(*args: Any, **kwargs: Any) -> Response:
        return Response(json.dumps(payload).encode())

    (record,) = LokiLogReader("http://unused", opener=opener).error_logs(
        ["order-service"], now - timedelta(minutes=1), now + timedelta(minutes=1)
    )
    assert record.attributes["request_id"] == "request-a"
    assert record.attributes["parent_span_id"] == symptom[0]["span_id"]
    assert record.attributes["trace_id"] == dependency[0]["trace_id"]
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    incident_id = uuid4()
    with Session(engine) as session:
        session.add(
            IncidentRow(
                incident_id=incident_id,
                status="OPEN",
                severity="HIGH",
                source="ALERT",
                title="test",
                created_at=now,
                updated_at=now,
                correlation_id=uuid4(),
            )
        )
        session.commit()
        repository = LogObservationRepository(session)
        assert repository.record(incident_id, [record], now) == 1
        ids = repository.incident_observation_ids(
            incident_id=incident_id,
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=1),
        )
        assert repository.records(ids) == [record]  # exact frozen-manifest replay read path
    engine.dispose()
    assert current_request_id() is None


def test_request_context_resets_after_nested_exception() -> None:
    assert current_request_id() is None
    with request_context("outer"):
        with pytest.raises(RuntimeError), request_context("inner"):
            assert current_request_id() == "inner"
            raise RuntimeError("failed")
        assert current_request_id() == "outer"
    assert current_request_id() is None


def test_legacy_log_serialization_and_dedup_key_stay_unchanged() -> None:
    record = LogRecord(
        service="caller", at=None, severity="ERROR", message="timeout", evidence_id="log:1"
    )
    assert record.model_dump() == {
        "service": "caller",
        "at": None,
        "severity": "ERROR",
        "message": "timeout",
        "evidence_id": "log:1",
    }
    assert LogRecord.model_validate(record.model_dump()) == record
    enriched = record.model_copy(update={"attributes": {"request_id": "req"}})
    assert LogObservationRepository._dedup_key(record) != LogObservationRepository._dedup_key(
        enriched
    )


def test_concurrent_request_contexts_do_not_share_ids() -> None:
    import asyncio

    async def run() -> None:
        entered = asyncio.Event()
        seen: list[str | None] = []

        async def first() -> None:
            with request_context("first"):
                entered.set()
                await asyncio.sleep(0)
                seen.append(current_request_id())

        async def second() -> None:
            await entered.wait()
            with request_context("second"):
                await asyncio.sleep(0)
                seen.append(current_request_id())

        await asyncio.gather(first(), second())
        assert sorted(str(value) for value in seen) == ["first", "second"]
        assert current_request_id() is None

    asyncio.run(run())
