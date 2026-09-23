"""FastAPI application for the deterministic incident control plane."""

import json
import os
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app
from prometheus_client.registry import CollectorRegistry
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.auth import require_api_token
from apps.control_plane.console import create_console_router
from apps.control_plane.console.dto import SystemConnector, SystemStatus
from apps.control_plane.console.email_delivery import EmailDelivery, email_delivery_from_env
from apps.control_plane.diagnosis import DiagnosisService, service_from_environment
from apps.control_plane.schemas import (
    ErrorDetail,
    ErrorResponse,
)
from apps.control_plane.timeline import diagnosis_phases, newer_run_note
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

# The built operator console (apps/web/dist), served under /app when present.
WEB_DIST = Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"

# The console is self-contained: same-origin scripts, styles and API/SSE. This
# keeps a strict policy while allowing the inline styles some libraries emit.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
)

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


def _env_connector(name: str, env_var: str) -> SystemConnector:
    """Report a push/pull upstream from its configuration, honestly unprobed.

    The control plane does not hold a live connection to Prometheus, Loki or
    Tempo (metrics and alerts arrive via webhook; evidence sources are read on
    demand), so the console reports whether each is configured rather than
    claiming a health it has not verified.
    """
    if os.environ.get(env_var):
        return SystemConnector(
            name=name, status="connected", detail="configured; not actively probed"
        )
    return SystemConnector(name=name, status="not_configured", detail=f"set {env_var} to enable")


def build_system_status(session: Session, *, reader_configured: bool) -> SystemStatus:
    """Assemble connector health from what the control plane can truthfully tell."""
    try:
        session.execute(text("SELECT 1"))
        database = SystemConnector(name="Database", status="connected", detail=None)
    except SQLAlchemyError:
        database = SystemConnector(name="Database", status="unavailable", detail="SELECT 1 failed")
    kubernetes = (
        SystemConnector(name="Kubernetes", status="connected", detail="cluster reader configured")
        if reader_configured
        else SystemConnector(
            name="Kubernetes", status="not_configured", detail="no in-cluster reader"
        )
    )
    alertmanager = SystemConnector(
        name="Alertmanager", status="connected", detail="webhook receiver ready"
    )
    return SystemStatus(
        connectors=[
            database,
            kubernetes,
            alertmanager,
            _env_connector("Prometheus", "PROMETHEUS_URL"),
            _env_connector("Loki", "SRE_LOKI_URL"),
            _env_connector("Tempo", "TEMPO_URL"),
            _env_connector("Email", "SRE_SMTP_HOST"),
        ]
    )


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    diagnosis_service: DiagnosisService | None = None,
    email_delivery: EmailDelivery | None = None,
) -> FastAPI:
    """Create the control-plane application with injectable persistence.

    ``email_delivery`` overrides the environment-configured backend, so tests can
    exercise the share path with a stub. When omitted, delivery is built from the
    environment and is absent (share refuses) unless SMTP is configured.
    """
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

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        """Apply conservative security headers to every response."""
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response

    reader_configured = diagnoser.reader is not None

    def system_status_provider(session: Session) -> SystemStatus:
        return build_system_status(session, reader_configured=reader_configured)

    app.include_router(
        create_console_router(
            get_session,
            system_status_provider,
            session_factory,
            email_delivery=email_delivery
            if email_delivery is not None
            else email_delivery_from_env(),
        )
    )

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
        dependencies=[Depends(require_api_token)],
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
        dependencies=[Depends(require_api_token)],
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

    @app.post("/api/v1/cluster/snapshot", dependencies=[Depends(require_api_token)])
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
                    "created_at": incident.created_at.strftime("%Y-%m-%d %H:%M"),
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
        run_id = diagnoses.latest_run_id(incident_id)
        diagnosis = Diagnosis.model_validate(document)
        alerts = AlertRepository(session).list_for_incident(incident_id)
        events = IncidentEventRepository(session).list_for_incident(incident_id)
        phases = diagnosis_phases(events, run_id)
        # A diagnosis that carries a run id but has no rendered phases means its
        # timeline events were lost; say so rather than showing another run's.
        timeline_note = (
            "Timeline unavailable or incomplete for this diagnosis run."
            if run_id and not phases
            else newer_run_note(events, phases)
        )
        lifecycle = Lifecycle(
            alert_fired=min((alert.starts_at for alert in alerts), default=None),
            incident_opened=incident.created_at,
            diagnosis_ready=diagnoses.latest_created_at(incident_id),
            reads=len(diagnosis.steps),
            evidence=len(diagnosis.evidence),
            model_calls=diagnosis.model_calls,
            phases=phases,
            run_note=timeline_note,
        )
        return diagnosis_html(diagnosis, back_link="/", lifecycle=lifecycle)

    @app.post(
        "/api/v1/webhooks/alertmanager",
        dependencies=[Depends(require_api_token)],
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

    # Serve the built operator console under /app when it has been built. In a
    # source checkout without a build, the JSON API and server-rendered pages
    # still work; only the SPA route is absent.
    if (WEB_DIST / "index.html").exists():
        assets = WEB_DIST / "assets"
        if assets.is_dir():
            app.mount("/app/assets", StaticFiles(directory=assets), name="web-assets")

        @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
        @app.get("/app/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
        def operator_console(full_path: str = "") -> FileResponse:
            """Serve the SPA shell for the console and all its client routes."""
            return FileResponse(WEB_DIST / "index.html")

    return app


app = create_app()
