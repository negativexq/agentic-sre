"""FastAPI application for the deterministic incident control plane."""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from prometheus_client.registry import CollectorRegistry
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.schemas import ErrorDetail, ErrorResponse
from packages.contracts import (
    Alert,
    AlertmanagerWebhook,
    ChangeRecord,
    Evidence,
    Incident,
    IncidentEvent,
)
from packages.incident import IncidentManager, normalize_alert
from packages.storage import (
    AlertRepository,
    ChangeRecordRepository,
    EvidenceRepository,
    IncidentEventRepository,
    IncidentNotFoundError,
    IncidentRepository,
    create_database_engine,
    create_session_factory,
)
from packages.telemetry import TelemetryMiddleware, create_runtime

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/agentic_sre"


def _correlation_id(request: Request) -> str:
    """Use a caller correlation ID or generate one for the response."""
    return request.headers.get("X-Correlation-ID", str(uuid4()))


def _error(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    """Build the common error envelope."""
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, correlation_id=_correlation_id(request))
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


def get_session() -> Iterator[Session]:
    """Dependency placeholder replaced by ``create_app``."""
    raise RuntimeError("database session dependency is not configured")
    yield  # pragma: no cover


def create_app(session_factory: sessionmaker[Session] | None = None) -> FastAPI:
    """Create the control-plane application with injectable persistence."""
    if session_factory is None:
        database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        engine = create_database_engine(database_url)
        session_factory = create_session_factory(engine)

    def session_dependency() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    registry = CollectorRegistry()
    telemetry = create_runtime(
        "control-plane",
        registry=registry,
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    app = FastAPI(title="Agentic SRE Control Plane", version="0.1.1")
    app.add_middleware(TelemetryMiddleware, runtime=telemetry)
    app.mount("/metrics", make_asgi_app(registry=registry))
    app.dependency_overrides[get_session] = session_dependency

    @app.exception_handler(IncidentNotFoundError)
    async def incident_not_found_handler(
        request: Request, _: IncidentNotFoundError
    ) -> JSONResponse:
        return _error(request, "INCIDENT_NOT_FOUND", "Incident was not found.", 404)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, _: RequestValidationError) -> JSONResponse:
        return _error(request, "VALIDATION_ERROR", "Request validation failed.", 422)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return process health without requiring the database."""
        return {"status": "ok"}

    @app.get("/ready", response_model=dict[str, str], responses={503: {"model": ErrorResponse}})
    def ready(
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, str] | JSONResponse:
        """Check that the database dependency is reachable."""
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return _error(request, "DATABASE_UNAVAILABLE", "Database is unavailable.", 503)
        return {"status": "ready"}

    @app.get("/api/v1/incidents", response_model=list[Incident])
    def list_incidents(session: Session = Depends(get_session)) -> list[Incident]:  # noqa: B008
        """List incidents from materialized current state."""
        return IncidentRepository(session).list()

    @app.get(
        "/api/v1/incidents/{incident_id}",
        response_model=Incident,
        responses={404: {"model": ErrorResponse}},
    )
    def get_incident(incident_id: UUID, session: Session = Depends(get_session)) -> Incident:  # noqa: B008
        """Return one incident or the typed not-found error."""
        incident = IncidentRepository(session).get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        return incident

    @app.get("/api/v1/incidents/{incident_id}/events", response_model=list[IncidentEvent])
    def get_incident_events(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[IncidentEvent]:
        """Return the immutable incident timeline."""
        return IncidentEventRepository(session).list_for_incident(incident_id)

    @app.get("/api/v1/incidents/{incident_id}/alerts", response_model=list[Alert])
    def get_incident_alerts(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[Alert]:
        """Return normalized alerts attached to an incident."""
        return AlertRepository(session).list_for_incident(incident_id)

    @app.get("/api/v1/incidents/{incident_id}/evidence", response_model=list[Evidence])
    def get_incident_evidence(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[Evidence]:
        """Return provenance-backed evidence attached to an incident."""
        return EvidenceRepository(session).list_for_incident(incident_id)

    @app.get("/api/v1/changes", response_model=list[ChangeRecord])
    def list_changes(
        resource_name: str,
        starts_at: datetime,
        ends_at: datetime,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[ChangeRecord]:
        """Read bounded historical change facts for investigation backends."""
        return ChangeRecordRepository(session).between(
            resource_name=resource_name,
            starts_at=starts_at,
            ends_at=ends_at,
        )

    @app.post("/api/v1/changes", response_model=ChangeRecord)
    def record_change(
        record: dict[str, Any],
        session: Session = Depends(get_session),  # noqa: B008
    ) -> ChangeRecord:
        """Persist a deterministic harness change fact for later read-only inspection."""
        try:
            normalized = ChangeRecord.model_validate_json(json.dumps(record))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid change record") from error
        return ChangeRecordRepository(session).append(normalized)

    @app.post("/api/v1/webhooks/alertmanager")
    def alertmanager_webhook(
        payload: AlertmanagerWebhook,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        """Normalize and ingest an Alertmanager delivery idempotently."""
        now = datetime.now(UTC)
        manager = IncidentManager(session)
        incidents = [manager.ingest(normalize_alert(alert), now=now) for alert in payload.alerts]
        return {
            "accepted": len(incidents),
            "incident_ids": [str(incident.incident_id) for incident in incidents],
        }

    return app


app = create_app()
