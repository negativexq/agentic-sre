"""Payment service with deterministic authorization behavior."""

import os
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from packages.storage import create_database_engine, create_session_factory
from workload.common.contracts import PaymentRequest, PaymentResponse, PaymentStatus
from workload.common.persistence import PaymentRepository

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/agentic_sre"


class PaymentService:
    """Application service for payment authorization."""

    def __init__(
        self,
        repository_factory: Callable[[], Session],
        *,
        clock: Callable[[], datetime] | None = None,
        decline_above_cents: int | None = None,
    ) -> None:
        self._repository_factory = repository_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._decline_above_cents = decline_above_cents

    def charge(self, request: PaymentRequest) -> PaymentResponse:
        """Persist a deterministic approval or decline."""
        status = PaymentStatus.APPROVED
        if (
            self._decline_above_cents is not None
            and request.amount_cents > self._decline_above_cents
        ):
            status = PaymentStatus.DECLINED
        with self._repository_factory() as session:
            return PaymentRepository(session).create(request, status, self._clock())


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    *,
    service: PaymentService | None = None,
) -> FastAPI:
    """Build the payment HTTP application."""
    if session_factory is None:
        database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        session_factory = create_session_factory(create_database_engine(database_url))
    payment_service = service or PaymentService(session_factory)

    app = FastAPI(title="Payment Service", version="0.1.0")

    @app.post("/payments", response_model=PaymentResponse, status_code=201)
    def create_payment(request: PaymentRequest) -> PaymentResponse:
        return payment_service.charge(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
