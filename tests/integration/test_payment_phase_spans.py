"""Opt-in payment phases retain normal behavior and observe early failures."""

from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient
from workload.common.models import WorkloadBase
from workload.payment_service import app as payment

from packages.telemetry import create_runtime


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("early_error", [False, True])
def test_optional_phases_keep_outcomes_and_do_not_export_fault_labels(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, early_error: bool
) -> None:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    WorkloadBase.metadata.create_all(engine)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = create_runtime("payment-phase-test")
    runtime.tracer = provider.get_tracer("test")
    monkeypatch.setattr(payment, "create_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setenv("SRE_PAYMENT_PHASE_SPANS", str(enabled).lower())
    monkeypatch.setenv("FAULT_PAYMENT_ERROR", str(early_error).lower())
    monkeypatch.setenv("FAULT_PAYMENT_DELAY_MS", "0")
    monkeypatch.setenv("FAULT_PAYMENT_DB_HOLD_MS", "0")
    monkeypatch.setenv("FAULT_PAYMENT_DB_QUERY_DELAY_MS", "0")
    try:
        with TestClient(
            payment.create_app(session_factory=sessionmaker(engine)),
            raise_server_exceptions=False,
        ) as client:
            response = client.post(
                "/payments",
                json={"order_id": str(uuid4()), "amount_cents": 100, "currency": "USD"},
            )
        assert response.status_code == (500 if early_error else 201)
        spans = exporter.get_finished_spans()
        by_name = {span.name: span for span in spans}
        assert "POST /payments" in by_name
        assert ("payment authorize" in by_name) is enabled
        assert ("postgres connection acquire" in by_name) is (enabled and not early_error)
        assert ("postgres payment create" in by_name) is (not early_error)
        if enabled:
            authorize = by_name["payment authorize"]
            server = by_name["POST /payments"]
            assert authorize.parent is not None and server.context is not None
            assert authorize.parent.span_id == server.context.span_id
            if early_error:
                assert authorize.status.status_code.name == "ERROR"
        for span in spans:
            assert not any("fault" in key or "cause" in key for key in span.attributes or {})
            assert "db.statement" not in (span.attributes or {})
    finally:
        provider.shutdown()
        engine.dispose()
