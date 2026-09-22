"""FastAPI application for the deterministic incident control plane."""

import hmac
import json
import os
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import make_asgi_app
from prometheus_client.registry import CollectorRegistry
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.diagnosis import DiagnosisService, service_from_environment
from apps.control_plane.schemas import (
    ErrorDetail,
    ErrorResponse,
)
from packages.contracts import (
    Alert,
    AlertmanagerWebhook,
    ChangeRecord,
    ChangeScope,
    Evidence,
    Incident,
    IncidentEvent,
)
from packages.incident import IncidentManager, normalize_alert
from packages.rca.model import Diagnosis
from packages.rca.report import (
    Lifecycle,
    diagnosis_html,
    diagnosis_pending_html,
    incidents_html,
)
from packages.storage import (
    AlertRepository,
    ChangeRecordRepository,
    DiagnosisRepository,
    EvidenceRepository,
    IncidentEventRepository,
    IncidentNotFoundError,
    IncidentRepository,
    create_database_engine,
    create_session_factory,
)
from packages.telemetry import TelemetryMiddleware, create_runtime

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/agentic_sre"

try:
    PROJECT_VERSION = version("agentic-sre")
except PackageNotFoundError:  # pragma: no cover - source tree without installation metadata
    PROJECT_VERSION = "unknown"


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


API_TOKEN_ENV = "SRE_API_TOKEN"


def _require_api_token(request: Request) -> None:
    """Guard state-changing endpoints with a shared-secret bearer token.

    Unset ``SRE_API_TOKEN`` keeps today's default-open behavior (the offline
    demo and kind walkthrough need no setup); setting it requires every
    caller, including Alertmanager's webhook, to send it as
    ``Authorization: Bearer <token>``. Read-only endpoints remain open in the
    built-in local/demo deployment, but they do not generate diagnoses or
    otherwise mutate state.
    """
    token = os.environ.get(API_TOKEN_ENV)
    if not token:
        return
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    presented = header[len(prefix) :] if header.startswith(prefix) else ""
    if not presented or not hmac.compare_digest(presented, token):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    diagnosis_service: DiagnosisService | None = None,
) -> FastAPI:
    """Create the control-plane application with injectable persistence."""
    if session_factory is None:
        database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        engine = create_database_engine(database_url)
        session_factory = create_session_factory(engine)
    diagnoser = diagnosis_service or service_from_environment(session_factory)
    auto_diagnose = os.getenv("SRE_AUTO_DIAGNOSE", "").casefold() == "true"

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
    watch_interval = float(os.getenv("SRE_WATCH_INTERVAL_SECONDS", "0"))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        watcher = None
        if watch_interval > 0 and diagnoser.reader is not None:
            watcher = threading.Thread(
                target=diagnoser.watch, args=(stop, watch_interval), daemon=True
            )
            watcher.start()
        try:
            yield
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=5)

    app = FastAPI(title="Agentic SRE", version=PROJECT_VERSION, lifespan=lifespan)
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
        scope: ChangeScope | None = None,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[ChangeRecord]:
        """Read bounded historical change facts for investigation backends."""
        return ChangeRecordRepository(session).between(
            resource_name=resource_name,
            starts_at=starts_at,
            ends_at=ends_at,
            scope=scope,
        )

    @app.post(
        "/api/v1/changes",
        response_model=ChangeRecord,
        dependencies=[Depends(_require_api_token)],
    )
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

    @app.post(
        "/api/v1/incidents/{incident_id}/diagnosis",
        dependencies=[Depends(_require_api_token)],
    )
    def create_diagnosis(incident_id: UUID) -> dict[str, Any]:
        """Diagnose the incident now and store the result."""
        return diagnoser.run(incident_id).model_dump(mode="json")

    @app.get("/api/v1/incidents/{incident_id}/diagnosis")
    def get_diagnosis(
        incident_id: UUID,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> Any:
        """Return the latest stored diagnosis."""
        document = DiagnosisRepository(session).latest(incident_id)
        if document is None:
            return _error(request, "DIAGNOSIS_NOT_FOUND", "No diagnosis yet.", 404)
        return document

    @app.post("/api/v1/cluster/snapshot", dependencies=[Depends(_require_api_token)])
    def snapshot_cluster() -> dict[str, int]:
        """Record changed cluster objects in the change journal."""
        return {"stored_versions": diagnoser.snapshot()}

    @app.get("/", response_class=HTMLResponse)
    def incidents_page(session: Session = Depends(get_session)) -> str:  # noqa: B008
        """Incident list with the latest diagnosis of each."""
        latest = DiagnosisRepository(session).summaries()
        rows = []
        for incident in sorted(
            IncidentRepository(session).list(), key=lambda item: item.created_at, reverse=True
        ):
            summary = latest.get(str(incident.incident_id), {})
            rows.append(
                {
                    "incident_id": str(incident.incident_id),
                    "title": incident.title,
                    "status": incident.status.value,
                    "created_at": incident.created_at.isoformat(timespec="seconds"),
                    "root_cause": summary.get("root_cause") or "",
                    "confidence": summary.get("confidence") or "",
                }
            )
        return incidents_html(rows)

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident_page(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> str:
        """Diagnosis page for one incident, with its alert-to-diagnosis lifecycle."""
        incident = IncidentRepository(session).get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        diagnoses = DiagnosisRepository(session)
        document = diagnoses.latest(incident_id)
        if document is None:
            return diagnosis_pending_html(str(incident_id), back_link="/")
        diagnosis = Diagnosis.model_validate(document)
        alerts = AlertRepository(session).list_for_incident(incident_id)
        lifecycle = Lifecycle(
            alert_fired=min((alert.starts_at for alert in alerts), default=None),
            incident_opened=incident.created_at,
            diagnosis_ready=diagnoses.latest_created_at(incident_id),
            reads=len(diagnosis.steps),
            evidence=len(diagnosis.evidence),
            model_calls=diagnosis.model_calls,
        )
        return diagnosis_html(diagnosis, back_link="/", lifecycle=lifecycle)

    @app.post(
        "/api/v1/webhooks/alertmanager",
        dependencies=[Depends(_require_api_token)],
    )
    def alertmanager_webhook(
        payload: AlertmanagerWebhook,
        background: BackgroundTasks,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        """Normalize and ingest an Alertmanager delivery idempotently."""
        now = datetime.now(UTC)
        manager = IncidentManager(session)
        incidents = [manager.ingest(normalize_alert(alert), now=now) for alert in payload.alerts]
        if auto_diagnose:
            for incident_id in dict.fromkeys(item.incident_id for item in incidents):
                background.add_task(diagnoser.run, incident_id)
        return {
            "accepted": len(incidents),
            "incident_ids": [str(incident.incident_id) for incident in incidents],
        }

    return app


app = create_app()
