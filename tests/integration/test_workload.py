"""Deterministic commerce flow tests without external infrastructure."""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from workload.common.adapters import (
    InMemoryEventPublisher,
    InMemoryProcessedStateStore,
    InProcessPaymentGateway,
)
from workload.common.contracts import (
    OrderCreatedEvent,
    OrderCreateRequest,
    OrderResponse,
    OrderStatus,
)
from workload.common.models import WorkloadBase
from workload.load_generator import LoadConfig, run_load
from workload.order_service.app import OrderService
from workload.order_service.app import create_app as create_order_app
from workload.order_worker.worker import OrderWorker
from workload.payment_service.app import PaymentService
from workload.seed import generate_orders

from packages.storage.database import create_session_factory

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def workload_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(f"sqlite:///{tmp_path / 'workload.db'}")
    WorkloadBase.metadata.create_all(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def test_order_payment_and_event_flow(workload_factory: sessionmaker[Session]) -> None:
    publisher = InMemoryEventPublisher()
    payment_service = PaymentService(workload_factory, clock=lambda: NOW)
    order_service = OrderService(
        workload_factory,
        InProcessPaymentGateway(payment_service.charge),
        publisher,
        clock=lambda: NOW,
        id_factory=lambda: UUID("11111111-1111-4111-8111-111111111111"),
    )
    request = OrderCreateRequest(customer_id="customer-0001", amount_cents=2_500, currency="USD")

    result = order_service.create(request)

    assert result.status.value == "PAID"
    assert result.payment_id is not None
    assert len(publisher.events) == 1
    assert publisher.events[0][1].order_id == result.order_id
    assert order_service.get(result.order_id, workload_factory) == result


def test_worker_is_idempotent(workload_factory: sessionmaker[Session]) -> None:
    state_store = InMemoryProcessedStateStore()
    processed: list[UUID] = []
    worker = OrderWorker(state_store, lambda event: processed.append(event.order_id))
    event = OrderCreatedEvent(
        order_id=uuid4(), customer_id="customer-0001", amount_cents=100, currency="USD"
    )

    assert worker.process(event) is True
    assert worker.process(event) is False
    assert processed == [event.order_id]


def test_seed_and_load_results_are_reproducible() -> None:
    first = generate_orders(5, seed=42)
    second = generate_orders(5, seed=42)
    assert first == second

    received: list[OrderCreateRequest] = []

    def submit(request: OrderCreateRequest) -> OrderResponse:
        received.append(request)
        return OrderResponse(
            order_id=uuid4(),
            customer_id=request.customer_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
            status=OrderStatus.PAID,
        )

    result = run_load(LoadConfig(rate=5, duration_seconds=1, seed=42), submit, sleep=lambda _: None)
    assert result.sent == 5
    assert result.success == 5
    assert result.failed == 0
    assert result.p50_ms is not None
    assert [item.customer_id for item in received] == [f"customer-{i:04d}" for i in range(5)]


def test_order_http_api(workload_factory: sessionmaker[Session]) -> None:
    publisher = InMemoryEventPublisher()
    payment_service = PaymentService(workload_factory, clock=lambda: NOW)
    service = OrderService(
        workload_factory,
        InProcessPaymentGateway(payment_service.charge),
        publisher,
        clock=lambda: NOW,
    )
    client = TestClient(create_order_app(workload_factory, service=service))

    response = client.post(
        "/orders", json={"customer_id": "customer-0001", "amount_cents": 1000, "currency": "USD"}
    )

    assert response.status_code == 201
    order_id = response.json()["order_id"]
    assert client.get(f"/orders/{order_id}").json()["status"] == "PAID"
