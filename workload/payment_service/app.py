"""Payment service with deterministic authorization behavior."""

import os
import time
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from prometheus_client import make_asgi_app
from prometheus_client.registry import CollectorRegistry
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from packages.storage import create_database_engine, create_session_factory
from packages.telemetry import TelemetryMiddleware, TelemetryRuntime, create_runtime
from workload.common.contracts import PaymentRequest, PaymentResponse, PaymentStatus
from workload.common.persistence import PaymentRepository

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/agentic_sre"


class FaultConfig(BaseModel):
    """Test-only deterministic fault knobs for live alert scenarios."""

    model_config = ConfigDict(extra="forbid", strict=True)

    delay_ms: int = Field(default=0, ge=0, le=30_000)
    error: bool = False
    db_hold_ms: int = Field(default=0, ge=0, le=5_000)


class PaymentService:
    """Application service for payment authorization."""

    def __init__(
        self,
        repository_factory: Callable[[], Session],
        *,
        clock: Callable[[], datetime] | None = None,
        decline_above_cents: int | None = None,
        runtime: TelemetryRuntime | None = None,
        fault_config: FaultConfig | None = None,
    ) -> None:
        self._repository_factory = repository_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._decline_above_cents = decline_above_cents
        self._runtime = runtime
        self._fault_config = fault_config

    def charge(self, request: PaymentRequest) -> PaymentResponse:
        """Persist a deterministic approval or decline."""
        status = PaymentStatus.APPROVED
        if (
            self._decline_above_cents is not None
            and request.amount_cents > self._decline_above_cents
        ):
            status = PaymentStatus.DECLINED
        with self._repository_factory() as session:
            query_started = time.perf_counter()
            try:
                acquisition_started = time.perf_counter()
                session.connection()
                if self._runtime is not None:
                    self._runtime.record_db(
                        "payment-service",
                        time.perf_counter() - acquisition_started,
                        acquisition=True,
                    )
                if self._fault_config is not None and self._fault_config.db_hold_ms:
                    time.sleep(self._fault_config.db_hold_ms / 1000)
                query_started = time.perf_counter()
                span_context = (
                    self._runtime.tracer.start_as_current_span("postgres payment create")
                    if self._runtime is not None
                    else nullcontext()
                )
                with span_context:
                    response = PaymentRepository(session).create(request, status, self._clock())
            except Exception:
                if self._runtime is not None:
                    self._runtime.metrics.db_error("payment-service")
                raise
            finally:
                if self._runtime is not None:
                    self._runtime.record_db(
                        "payment-service",
                        time.perf_counter() - query_started,
                        operation="create",
                    )
            return response


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    *,
    service: PaymentService | None = None,
) -> FastAPI:
    """Build the payment HTTP application."""
    if session_factory is None:
        database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        session_factory = create_session_factory(create_database_engine(database_url))
    registry = CollectorRegistry()
    telemetry = create_runtime(
        "payment-service",
        registry=registry,
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    fault_config = FaultConfig(
        delay_ms=int(os.getenv("FAULT_PAYMENT_DELAY_MS", "0")),
        error=os.getenv("FAULT_PAYMENT_ERROR", "false").lower() == "true",
        db_hold_ms=int(os.getenv("FAULT_PAYMENT_DB_HOLD_MS", "0")),
    )
    faults_enabled = os.getenv("ENABLE_TEST_FAULTS", "false").lower() == "true"
    payment_service = service or PaymentService(
        session_factory, runtime=telemetry, fault_config=fault_config
    )

    app = FastAPI(title="Payment Service", version="0.1.1")
    app.add_middleware(TelemetryMiddleware, runtime=telemetry)
    app.mount("/metrics", make_asgi_app(registry=registry))

    @app.post("/payments", response_model=PaymentResponse, status_code=201)
    def create_payment(request: PaymentRequest) -> PaymentResponse:
        if fault_config.delay_ms > 0:
            time.sleep(fault_config.delay_ms / 1000)
        if fault_config.error:
            raise RuntimeError("injected payment failure")
        return payment_service.charge(request)

    @app.post("/__faults", response_model=FaultConfig)
    def set_faults(config: FaultConfig) -> FaultConfig:
        """Set deterministic test faults; never enabled in a production deployment."""
        if not faults_enabled:
            raise HTTPException(status_code=404, detail="test faults are disabled")
        fault_config.delay_ms = config.delay_ms
        fault_config.error = config.error
        fault_config.db_hold_ms = config.db_hold_ms
        return fault_config

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
