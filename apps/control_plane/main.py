"""FastAPI application for the deterministic incident control plane."""

import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal
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
from apps.control_plane.baseline import evaluate_baseline
from apps.control_plane.connector_intake import AlertStreamConsumer
from apps.control_plane.console import create_console_router
from apps.control_plane.console.dto import SystemConnector, SystemStatus
from apps.control_plane.console.email_delivery import EmailDelivery, email_delivery_from_env
from apps.control_plane.diagnosis import DiagnosisService, service_from_environment
from apps.control_plane.schemas import (
    BaselineProbeRequest,
    DiagnosisRevisionDetail,
    DiagnosisRevisionSummary,
    ErrorDetail,
    ErrorResponse,
    RevisionDiffResponse,
)
from apps.control_plane.timeline import diagnosis_phases, newer_run_note
from packages.connector.client import ConnectorClient, ConnectorError, in_process_transport
from packages.connector.service import Connector
from packages.connector.transport import ConnectorGateway
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
from packages.rca.alert_coverage import (
    AlertCoverageConfig,
    alert_coverage_poller_from_environment,
)
from packages.rca.model import Diagnosis
from packages.rca.presentation import shown_root_canonical
from packages.rca.report import (
    Lifecycle,
    diagnosis_html,
    diagnosis_pending_html,
    incidents_html,
)
from packages.rca.revision_diff import PersistedDiagnosisRevision, diff_revisions
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


logger = logging.getLogger(__name__)


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


def build_system_status(
    session: Session,
    *,
    reader_configured: bool,
    connector_status: SystemConnector | None = None,
    connector_capabilities: frozenset[str] | None = None,
) -> SystemStatus:
    """Assemble connector health from what the control plane can truthfully tell.

    In remote mode the backends are configured on the connector, not here, so their rows come from
    the capabilities the connector reports (``None`` keeps the environment-based rows).
    """
    try:
        session.execute(text("SELECT 1"))
        database = SystemConnector(name="Database", status="connected", detail=None)
    except SQLAlchemyError:
        database = SystemConnector(name="Database", status="unavailable", detail="SELECT 1 failed")
    if connector_capabilities is not None:

        def through_connector(name: str, wanted: set[str]) -> SystemConnector:
            if not connector_capabilities:
                return SystemConnector(
                    name=name, status="unavailable", detail="no connector is connected"
                )
            if wanted & connector_capabilities:
                return SystemConnector(
                    name=name, status="connected", detail="read through the connector"
                )
            return SystemConnector(
                name=name, status="not_configured", detail="not configured on the connector"
            )

        return SystemStatus(
            connectors=[
                database,
                through_connector("Kubernetes", {"changes", "history"}),
                through_connector("Alertmanager", {"alerts"}),
                through_connector("Prometheus", {"resource_pressure", "traffic"}),
                through_connector("Loki", {"logs"}),
                through_connector("Tempo", {"runtime_traces"}),
                _env_connector("Email", "SRE_SMTP_HOST"),
                *([connector_status] if connector_status is not None else []),
            ]
        )
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
            *([connector_status] if connector_status is not None else []),
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
    # The webhook ingests alert occurrences; this read-only poller only records
    # when the alert channel was actually observed (coverage), never incidents.
    # Injected test doubles need not carry a connector; only the real service does.
    connector: Connector | None = getattr(diagnoser, "connector", None)
    # In stream mode (SRE_CONNECTOR_STREAMS=true) the connector owns every Alertmanager
    # connection: it polls, receives the webhook locally and streams both here (contract §10).
    # In remote mode (SRE_CONNECTOR_MODE=remote) a connector dials this process instead (§12).
    gateway: ConnectorGateway | None = getattr(diagnoser, "gateway", None)
    remote_client: ConnectorClient | None = getattr(diagnoser, "connector_client", None)
    stream_client: ConnectorClient | None = (
        ConnectorClient(in_process_transport(connector)) if connector is not None else remote_client
    )
    alert_coverage_poller = (
        None
        if stream_client is not None
        else alert_coverage_poller_from_environment(session_factory)
    )
    auto_diagnose = os.getenv("SRE_AUTO_DIAGNOSE", "").casefold() == "true"

    # A re-firing alert continues its episode only with a positive quiet interval; 0, the product default,
    # keeps one incident per occurrence (docs/architecture/incident-episode-contract.md, §5).
    alert_quiet = timedelta(seconds=float(os.getenv("SRE_ALERT_QUIET_SECONDS", "0")))

    def diagnose_in_background(incident_ids: list[UUID], trigger: str = "INITIAL") -> None:
        for incident_id in incident_ids:
            threading.Thread(target=diagnoser.run, args=(incident_id, trigger), daemon=True).start()

    def rediagnose_refired(incident_ids: list[UUID]) -> None:
        diagnose_in_background(incident_ids, "ALERT_REFIRED")

    def diagnose_resolved(incident_ids: list[UUID]) -> None:
        diagnose_in_background(incident_ids, "RESOLVED")

    alert_consumer = (
        AlertStreamConsumer(
            stream_client,
            session_factory,
            config=AlertCoverageConfig.from_environment(),
            on_incidents=diagnose_in_background if auto_diagnose else None,
            on_refired=rediagnose_refired if auto_diagnose else None,
            on_resolved=diagnose_resolved if auto_diagnose else None,
            quiet=alert_quiet,
        )
        if stream_client is not None
        else None
    )

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
        if gateway is not None:
            logger.info("connector gateway listening on port %d", gateway.start())
        enrollment = getattr(diagnoser, "enrollment", None)
        if enrollment is not None:
            logger.info("connector enrollment listening on port %d", enrollment.start())
        if watch_interval > 0 and (diagnoser.reader is not None or gateway is not None):
            watcher = threading.Thread(
                target=diagnoser.watch, args=(stop, watch_interval), daemon=True
            )
            watcher.start()
            # contract §15: the journal follows the change stream within about a second
            follower = getattr(diagnoser, "follow_changes", None)
            if callable(follower) and (gateway is not None or connector is not None):
                threading.Thread(target=follower, args=(stop, 1.0), daemon=True).start()
        poller = None
        if alert_coverage_poller is not None:
            poller = threading.Thread(target=alert_coverage_poller.run, args=(stop,), daemon=True)
            poller.start()
        stream_threads: list[threading.Thread] = []
        if connector is not None:
            stream_threads.append(
                threading.Thread(
                    target=connector.run,
                    args=(stop,),
                    kwargs={
                        "changes_interval": watch_interval if watch_interval > 0 else 15.0,
                        "alerts_interval": AlertCoverageConfig.from_environment().poll_interval.total_seconds(),
                    },
                    daemon=True,
                )
            )
        if alert_consumer is not None:
            stream_threads.append(
                threading.Thread(target=alert_consumer.run, args=(stop,), daemon=True)
            )
        for thread in stream_threads:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=5)
            if poller is not None:
                poller.join(timeout=5)
            for thread in stream_threads:
                thread.join(timeout=5)
            if gateway is not None:
                gateway.stop()
            if enrollment is not None:
                enrollment.stop()

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

    def connector_status() -> SystemConnector | None:
        if gateway is None:
            return None
        connected = sorted(gateway.connected())
        return (
            SystemConnector(
                name="Connector", status="connected", detail=f"connected: {', '.join(connected)}"
            )
            if connected
            else SystemConnector(
                name="Connector", status="unavailable", detail="no connector is connected"
            )
        )

    def connector_capabilities() -> frozenset[str] | None:
        if remote_client is None:
            return None
        try:
            return frozenset(remote_client.capabilities())
        except ConnectorError:
            return frozenset()

    def system_status_provider(session: Session) -> SystemStatus:
        return build_system_status(
            session,
            reader_configured=diagnoser.reader is not None,
            connector_status=connector_status(),
            connector_capabilities=connector_capabilities(),
        )

    app.include_router(
        create_console_router(
            get_session,
            system_status_provider,
            session_factory,
            email_delivery=email_delivery
            if email_delivery is not None
            else email_delivery_from_env(),
            connector_capabilities=connector_capabilities,
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
    def create_diagnosis(
        incident_id: UUID, trigger: Literal["MANUAL"] = "MANUAL"
    ) -> dict[str, Any]:
        """Diagnose the incident now and store the result."""
        return diagnoser.run(incident_id, trigger).model_dump(mode="json")

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

    @app.get(
        "/api/v1/incidents/{incident_id}/diagnoses",
        response_model=list[DiagnosisRevisionSummary],
    )
    def list_diagnoses(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[DiagnosisRevisionSummary]:
        """Return persisted revision metadata in revision-number order."""
        rows = DiagnosisRepository(session).list_revisions(incident_id)
        return [
            DiagnosisRevisionSummary(
                diagnosis_id=row.diagnosis_id,
                revision_number=row.revision_number,
                previous_diagnosis_id=row.previous_diagnosis_id,
                trigger=row.trigger,
                created_at=row.created_at,
                run_id=row.run_id,
                window_end=row.window_end,
                manifest_digest=row.manifest_digest,
                tape_digest=row.tape_digest,
                epistemic_digest=row.epistemic_digest,
                engine_version=row.engine_version,
                config_digest=row.config_digest,
                root_cause=shown_root_canonical(row.document),  # m21 §20
                confidence=row.confidence,
                mode=row.mode,
                resolution=row.document.get("resolution"),
            )
            for row in rows
        ]

    @app.get(
        "/api/v1/incidents/{incident_id}/diagnoses/{revision_number}",
        response_model=DiagnosisRevisionDetail,
        responses={404: {"model": ErrorResponse}},
    )
    def get_diagnosis_revision(
        incident_id: UUID,
        revision_number: int,
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> DiagnosisRevisionDetail | JSONResponse:
        """Return one persisted revision and its linked predecessor diff."""
        repository = DiagnosisRepository(session)
        current = repository.get_revision(incident_id, revision_number)
        if current is None:
            return _error(request, "DIAGNOSIS_NOT_FOUND", "No diagnosis yet.", 404)

        revision_diff: RevisionDiffResponse | None = None
        if current.previous_diagnosis_id is not None:
            previous = repository.get_revision_by_id(incident_id, current.previous_diagnosis_id)
            if previous is None:
                raise RuntimeError(
                    "diagnosis revision previous_diagnosis_id does not resolve within incident"
                )
            computed = diff_revisions(
                PersistedDiagnosisRevision(previous.document, previous.manifest_digest),
                PersistedDiagnosisRevision(current.document, current.manifest_digest),
            )
            diff_document = asdict(computed)
            diff_document["resolution_transition"]["changed"] = (
                computed.resolution_transition.changed
            )
            revision_diff = RevisionDiffResponse.model_validate(diff_document)

        summary = DiagnosisRevisionSummary(
            diagnosis_id=current.diagnosis_id,
            revision_number=current.revision_number,
            previous_diagnosis_id=current.previous_diagnosis_id,
            trigger=current.trigger,
            created_at=current.created_at,
            run_id=current.run_id,
            window_end=current.window_end,
            manifest_digest=current.manifest_digest,
            tape_digest=current.tape_digest,
            epistemic_digest=current.epistemic_digest,
            engine_version=current.engine_version,
            config_digest=current.config_digest,
            root_cause=shown_root_canonical(current.document),  # m21 §20
            confidence=current.confidence,
            mode=current.mode,
            resolution=current.document.get("resolution"),
        )
        return DiagnosisRevisionDetail(
            **summary.model_dump(), diagnosis=dict(current.document), diff=revision_diff
        )

    @app.post("/api/v1/baseline-probe", dependencies=[Depends(require_api_token)])
    def baseline_probe(request: BaselineProbeRequest) -> dict[str, Any]:
        """Ephemeral incident-free evaluation of the persisted baseline; writes nothing."""
        if request.namespace not in diagnoser.namespaces:
            raise HTTPException(status_code=422, detail="namespace is not watched")
        try:
            evaluation = evaluate_baseline(
                session_factory,
                namespaces=(request.namespace,),
                evidence_namespaces=diagnoser.evidence_namespaces,
                collector_started_at=request.collector_started_at,
                baseline_reference_at=request.baseline_reference_at,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return evaluation.document()

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
        if gateway is not None:
            raise HTTPException(
                status_code=410,
                detail="alerts reach the control plane through the connector; point Alertmanager at it",
            )
        if connector is not None:
            # The connector is the local receiver; incidents follow through its alert stream.
            accepted = connector.receive_webhook(payload.model_dump(mode="json", by_alias=True))
            return {"accepted": accepted, "incident_ids": []}
        now = datetime.now(UTC)
        manager = IncidentManager(session, quiet=alert_quiet)
        outcomes = [
            manager.ingest_occurrence(normalize_alert(alert), now=now) for alert in payload.alerts
        ]
        incidents = [outcome.incident for outcome in outcomes]
        if auto_diagnose:
            # only a change of an incident starts a diagnosis (diagnosis-trigger-design.md §2)
            triggers: dict[tuple[UUID, str], None] = {}
            for outcome in outcomes:
                if outcome.refired:
                    triggers[(outcome.incident.incident_id, "ALERT_REFIRED")] = None
                elif outcome.created:
                    triggers[(outcome.incident.incident_id, "INITIAL")] = None
                for resolved in outcome.resolved:
                    triggers[(resolved, "RESOLVED")] = None
            for incident_id, trigger in triggers:
                background.add_task(diagnoser.run, incident_id, trigger)
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
