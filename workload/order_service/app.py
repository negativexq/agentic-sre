"""Order service coordinating payment and order-created publication."""

import os
import time
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from prometheus_client.registry import CollectorRegistry
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from packages.storage import create_database_engine, create_session_factory
from packages.telemetry import TelemetryMiddleware, TelemetryRuntime, create_runtime
from workload.common.adapters import (
    EventPublisher,
    HttpPaymentGateway,
    LazyKafkaEventPublisher,
    PaymentGateway,
)
from workload.common.contracts import (
    EventTopic,
    OrderCreatedEvent,
    OrderCreateRequest,
    OrderResponse,
    PaymentRequest,
    PaymentStatus,
)
from workload.common.persistence import OrderRepository

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/agentic_sre"
DEFAULT_PAYMENT_URL = "http://payment-service:8000"
EVENT_NAMESPACE = UUID("d9e0822c-d6dc-4e7d-aea3-59f30df796fb")


class OrderFaultConfig(BaseModel):
    """Bounded local test faults; the endpoint is disabled outside test mode."""

    model_config = ConfigDict(extra="forbid", strict=True)

    delay_ms: int = Field(default=0, ge=0, le=30_000)
    error: bool = False
    db_query_delay_ms: int = Field(default=0, ge=0, le=5_000)


class OrderService:
    """Application service for the order flow."""

    def __init__(
        self,
        repository_factory: Callable[[], Session],
        payment_gateway: PaymentGateway,
        event_publisher: EventPublisher,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
        runtime: TelemetryRuntime | None = None,
        fault_config: OrderFaultConfig | None = None,
    ) -> None:
        self._repository_factory = repository_factory
        self._payment_gateway = payment_gateway
        self._event_publisher = event_publisher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4
        self._runtime = runtime
        self._fault_config = fault_config

    def create(self, request: OrderCreateRequest) -> OrderResponse:
        """Create, charge, and publish one order."""
        if self._fault_config is not None:
            if self._fault_config.delay_ms:
                time.sleep(self._fault_config.delay_ms / 1000)
            if self._fault_config.error:
                raise RuntimeError("injected order failure")
        order_id = self._id_factory()
        with self._repository_factory() as session:
            query_started = time.perf_counter()
            order_repository = OrderRepository(session)
            try:
                acquisition_started = time.perf_counter()
                session.connection()
                if self._runtime is not None:
                    self._runtime.record_db(
                        "order-service",
                        time.perf_counter() - acquisition_started,
                        acquisition=True,
                    )
                query_started = time.perf_counter()
                if self._fault_config is not None and self._fault_config.db_query_delay_ms:
                    session.execute(
                        text("SELECT pg_sleep(:delay_seconds)"),
                        {"delay_seconds": self._fault_config.db_query_delay_ms / 1000},
                    )
                span_context = (
                    self._runtime.tracer.start_as_current_span("postgres order create")
                    if self._runtime is not None
                    else nullcontext()
                )
                with span_context:
                    order_repository.create(order_id, request, self._clock())
            finally:
                if self._runtime is not None:
                    self._runtime.record_db(
                        "order-service",
                        time.perf_counter() - query_started,
                        operation="create",
                    )

        payment = self._payment_gateway.charge(
            PaymentRequest(
                order_id=order_id,
                amount_cents=request.amount_cents,
                currency=request.currency,
            )
        )
        with self._repository_factory() as session:
            query_started = time.perf_counter()
            order_repository = OrderRepository(session)
            try:
                acquisition_started = time.perf_counter()
                session.connection()
                if self._runtime is not None:
                    self._runtime.record_db(
                        "order-service",
                        time.perf_counter() - acquisition_started,
                        acquisition=True,
                    )
                query_started = time.perf_counter()
                span_context = (
                    self._runtime.tracer.start_as_current_span("postgres order update")
                    if self._runtime is not None
                    else nullcontext()
                )
                with span_context:
                    if payment.status is PaymentStatus.APPROVED:
                        response = order_repository.mark_paid(order_id, payment.payment_id)
                    else:
                        response = order_repository.mark_payment_failed(order_id)
            finally:
                if self._runtime is not None:
                    self._runtime.record_db(
                        "order-service",
                        time.perf_counter() - query_started,
                        operation="update",
                    )

        event = OrderCreatedEvent(
            event_id=uuid5(EVENT_NAMESPACE, str(order_id)),
            order_id=order_id,
            customer_id=request.customer_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
        )
        self._event_publisher.publish(EventTopic.ORDERS_CREATED, event)
        return response

    def get(
        self, order_id: UUID, repository_factory: Callable[[], Session]
    ) -> OrderResponse | None:
        """Read one order."""
        with repository_factory() as session:
            return OrderRepository(session).get(order_id)


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    *,
    payment_gateway: PaymentGateway | None = None,
    event_publisher: EventPublisher | None = None,
    service: OrderService | None = None,
) -> FastAPI:
    """Build the order HTTP application."""
    if session_factory is None:
        database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        session_factory = create_session_factory(create_database_engine(database_url))
    registry = CollectorRegistry()
    telemetry = create_runtime(
        "order-service",
        registry=registry,
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    fault_config = OrderFaultConfig(
        delay_ms=int(os.getenv("FAULT_ORDER_DELAY_MS", "0")),
        error=os.getenv("FAULT_ORDER_ERROR", "false").lower() == "true",
        db_query_delay_ms=int(os.getenv("FAULT_ORDER_DB_QUERY_DELAY_MS", "0")),
    )
    gateway = payment_gateway or HttpPaymentGateway(
        os.getenv("PAYMENT_SERVICE_URL", DEFAULT_PAYMENT_URL), runtime=telemetry
    )
    publisher = event_publisher or LazyKafkaEventPublisher(
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"), runtime=telemetry
    )
    order_service = service or OrderService(
        session_factory, gateway, publisher, runtime=telemetry, fault_config=fault_config
    )

    app = FastAPI(title="Order Service", version="0.1.1")
    app.add_middleware(TelemetryMiddleware, runtime=telemetry)
    app.mount("/metrics", make_asgi_app(registry=registry))

    @app.post("/orders", response_model=OrderResponse, status_code=201)
    def create_order(request: OrderCreateRequest) -> OrderResponse:
        return order_service.create(request)

    @app.post("/__faults", response_model=OrderFaultConfig)
    def set_faults(config: OrderFaultConfig) -> OrderFaultConfig:
        """Set bounded local test faults; never enabled in production mode."""
        if os.getenv("ENABLE_TEST_FAULTS", "false").lower() != "true":
            raise HTTPException(status_code=404, detail="test faults are disabled")
        fault_config.delay_ms = config.delay_ms
        fault_config.error = config.error
        fault_config.db_query_delay_ms = config.db_query_delay_ms
        return fault_config

    @app.get("/orders/{order_id}", response_model=OrderResponse)
    def get_order(order_id: UUID) -> OrderResponse | JSONResponse:
        result = order_service.get(order_id, session_factory)
        if result is None:
            return JSONResponse(status_code=404, content={"error": "ORDER_NOT_FOUND"})
        return result

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
