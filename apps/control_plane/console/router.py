"""The /api/v1/console/* router: the operator console's data surface."""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.console.dto import (
    ChangeView,
    DashboardCounters,
    DashboardSummary,
    DiagnosisView,
    EvidenceView,
    IncidentDetail,
    IncidentListItem,
    IncidentPage,
    ReportSummary,
    SystemStatus,
    TimelineView,
)
from apps.control_plane.console.mappers import (
    change_view,
    diagnosis_view,
    evidence_view,
    incident_list_item,
    report_summary,
    timeline_view,
)
from apps.control_plane.console.stream import global_stream, incident_stream
from apps.control_plane.timeline import diagnosis_phases
from packages.contracts import ChangeScope, ChangeType
from packages.rca.model import Diagnosis
from packages.report import ReportSnapshot, build_report, to_markdown, to_pdf
from packages.storage import (
    AlertRepository,
    ChangeRecordRepository,
    DiagnosisRepository,
    EvidenceRepository,
    IncidentEventRepository,
    IncidentNotFoundError,
    IncidentRepository,
    ReportRepository,
)

INACTIVE_STATUSES = {"RESOLVED", "CLOSED"}


def _passes(
    item: IncidentListItem,
    *,
    status: str | None,
    severity: str | None,
    service: str | None,
    confidence: str | None,
    resolution: str | None,
    query: str | None,
    active: bool | None,
) -> bool:
    if status and item.status != status:
        return False
    if severity and item.severity != severity:
        return False
    if service and item.service != service:
        return False
    if confidence and item.confidence != confidence:
        return False
    if resolution and item.resolution != resolution:
        return False
    if active is not None and (item.status not in INACTIVE_STATUSES) != active:
        return False
    if query and query.casefold() not in item.title.casefold():
        return False
    return True


def create_console_router(
    get_session: Callable[[], Iterator[Session]],
    system_status: Callable[[Session], SystemStatus],
    session_factory: sessionmaker[Session],
) -> APIRouter:
    """Build the console router bound to the app's session dependency.

    ``system_status`` is injected so the router stays free of connector wiring;
    the app supplies a provider that probes what it can honestly determine.
    ``session_factory`` backs the SSE streams, which open a short-lived session
    per poll rather than holding a request-scoped one open.
    """
    router = APIRouter(prefix="/api/v1/console", tags=["console"])

    sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    def _items(session: Session) -> list[IncidentListItem]:
        views = DiagnosisRepository(session).latest_views()
        incidents = IncidentRepository(session).list()
        return [
            incident_list_item(incident, views.get(str(incident.incident_id)))
            for incident in incidents
        ]

    @router.get("/dashboard", response_model=DashboardSummary)
    def dashboard(session: Session = Depends(get_session)) -> DashboardSummary:  # noqa: B008
        items = _items(session)
        active = [item for item in items if item.status not in INACTIVE_STATUSES]
        diagnosed = [item for item in items if item.has_diagnosis]
        durations = [
            (item.updated_at - item.created_at).total_seconds()
            for item in diagnosed
            if item.updated_at >= item.created_at
        ]
        counters = DashboardCounters(
            active_incidents=len(active),
            critical_incidents=len([i for i in active if i.severity == "CRITICAL"]),
            diagnosing=len([i for i in active if not i.has_diagnosis]),
            resolved_diagnoses=len([i for i in items if i.resolution == "RESOLVED"]),
            median_diagnosis_seconds=statistics.median(durations) if durations else None,
        )
        recent = sorted(diagnosed, key=lambda i: i.updated_at, reverse=True)[:8]
        return DashboardSummary(
            counters=counters,
            active_incidents=sorted(active, key=lambda i: i.created_at, reverse=True),
            recent_diagnoses=recent,
            system=system_status(session),
            generated_at=datetime.now(UTC),
        )

    @router.get("/system", response_model=SystemStatus)
    def system(session: Session = Depends(get_session)) -> SystemStatus:  # noqa: B008
        return system_status(session)

    @router.get("/incidents", response_model=IncidentPage)
    def incidents(  # noqa: PLR0913
        session: Session = Depends(get_session),  # noqa: B008
        status: str | None = None,
        severity: str | None = None,
        service: str | None = None,
        confidence: str | None = None,
        resolution: str | None = None,
        active: bool | None = None,
        q: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> IncidentPage:
        items = _items(session)
        items.sort(key=lambda i: i.created_at, reverse=True)
        filtered = [
            item
            for item in items
            if _passes(
                item,
                status=status,
                severity=severity,
                service=service,
                confidence=confidence,
                resolution=resolution,
                query=q,
                active=active,
            )
        ]
        window = filtered[offset : offset + limit]
        return IncidentPage(items=window, total=len(filtered), limit=limit, offset=offset)

    def _load_incident(session: Session, incident_id: UUID) -> IncidentListItem:
        incident = IncidentRepository(session).get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        view = DiagnosisRepository(session).latest_views().get(str(incident_id))
        return incident_list_item(incident, view)

    @router.get("/incidents/{incident_id}", response_model=IncidentDetail)
    def incident_detail(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> IncidentDetail:
        incidents_repo = IncidentRepository(session)
        incident = incidents_repo.get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        diagnoses = DiagnosisRepository(session)
        document = diagnoses.latest(incident_id)
        diagnosis = Diagnosis.model_validate(document) if document is not None else None
        events = IncidentEventRepository(session).list_for_incident(incident_id)
        alerts = AlertRepository(session).list_for_incident(incident_id)
        evidence = EvidenceRepository(session).list_for_incident(incident_id)
        timeline = timeline_view(
            incident=incident,
            diagnosis=diagnosis,
            run_id=diagnoses.latest_run_id(incident_id),
            events=events,
            alerts=alerts,
            diagnosis_ready=diagnoses.latest_created_at(incident_id),
        )
        return IncidentDetail(
            incident=_load_incident(session, incident_id),
            diagnosis=diagnosis_view(diagnosis) if diagnosis else None,
            timeline=timeline,
            evidence_count=len(evidence),
        )

    @router.get(
        "/incidents/{incident_id}/diagnosis",
        response_model=DiagnosisView,
        responses={404: {}},
    )
    def incident_diagnosis(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> DiagnosisView:
        document = DiagnosisRepository(session).latest(incident_id)
        if document is None:
            raise IncidentNotFoundError(str(incident_id))
        return diagnosis_view(Diagnosis.model_validate(document))

    @router.get("/incidents/{incident_id}/timeline", response_model=TimelineView)
    def incident_timeline(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> TimelineView:
        incident = IncidentRepository(session).get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        diagnoses = DiagnosisRepository(session)
        document = diagnoses.latest(incident_id)
        diagnosis = Diagnosis.model_validate(document) if document is not None else None
        return timeline_view(
            incident=incident,
            diagnosis=diagnosis,
            run_id=diagnoses.latest_run_id(incident_id),
            events=IncidentEventRepository(session).list_for_incident(incident_id),
            alerts=AlertRepository(session).list_for_incident(incident_id),
            diagnosis_ready=diagnoses.latest_created_at(incident_id),
        )

    @router.get("/incidents/{incident_id}/evidence", response_model=list[EvidenceView])
    def incident_evidence(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[EvidenceView]:
        incident = IncidentRepository(session).get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        return [
            evidence_view(item)
            for item in EvidenceRepository(session).list_for_incident(incident_id)
        ]

    @router.get("/changes", response_model=list[ChangeView])
    def changes(
        session: Session = Depends(get_session),  # noqa: B008
        scope: ChangeScope | None = None,
        change_type: ChangeType | None = None,
        q: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[ChangeView]:
        records = ChangeRecordRepository(session).recent(
            limit=limit, scope=scope, change_type=change_type, resource_query=q
        )
        return [change_view(record) for record in records]

    @router.get("/incidents/{incident_id}/changes", response_model=list[ChangeView])
    def incident_changes(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[ChangeView]:
        incident = IncidentRepository(session).get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        document = DiagnosisRepository(session).latest(incident_id)
        diagnosis = Diagnosis.model_validate(document) if document is not None else None
        onset = diagnosis.symptoms.onset if diagnosis else None
        if onset is None:
            alerts = AlertRepository(session).list_for_incident(incident_id)
            onset = min((alert.starts_at for alert in alerts), default=incident.created_at)
        leading = diagnosis.root_cause.name if diagnosis and diagnosis.root_cause else None
        records = ChangeRecordRepository(session).recent(
            starts_at=onset - timedelta(hours=2),
            ends_at=onset + timedelta(minutes=30),
            limit=200,
        )
        return [change_view(record, onset, leading) for record in records]

    def _build_report(session: Session, incident_id: UUID) -> ReportSnapshot:
        incident = IncidentRepository(session).get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        diagnoses = DiagnosisRepository(session)
        document = diagnoses.latest(incident_id)
        if document is None:
            raise HTTPException(status_code=409, detail="incident has no diagnosis to report")
        diagnosis = Diagnosis.model_validate(document)
        run_id = diagnoses.latest_run_id(incident_id)
        events = IncidentEventRepository(session).list_for_incident(incident_id)
        alerts = AlertRepository(session).list_for_incident(incident_id)
        evidence = EvidenceRepository(session).list_for_incident(incident_id)
        return build_report(
            incident_id=str(incident_id),
            title=incident.title,
            severity=incident.severity.value,
            status=incident.status.value,
            diagnosis=diagnosis,
            run_id=run_id,
            alert_fired=min((alert.starts_at for alert in alerts), default=None),
            incident_opened=incident.created_at,
            diagnosed_at=diagnoses.latest_created_at(incident_id),
            phases=diagnosis_phases(events, run_id),
            evidence_count=len(evidence),
            generated_at=datetime.now(UTC),
        )

    @router.post("/incidents/{incident_id}/reports", response_model=ReportSnapshot, status_code=201)
    def create_report(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> ReportSnapshot:
        snapshot = _build_report(session, incident_id)
        ReportRepository(session).save(
            report_id=snapshot.report_id,
            incident_id=incident_id,
            diagnosis_run_id=snapshot.diagnosis_run_id,
            report_version=snapshot.report_version,
            created_at=snapshot.generated_at,
            document=snapshot.model_dump(mode="json"),
        )
        return snapshot

    @router.get("/reports", response_model=list[ReportSummary])
    def list_reports(
        session: Session = Depends(get_session),  # noqa: B008
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[ReportSummary]:
        documents = ReportRepository(session).list_all(limit=limit)
        return [report_summary(ReportSnapshot.model_validate(doc)) for doc in documents]

    @router.get("/reports/{report_id}", response_model=ReportSnapshot, responses={404: {}})
    def get_report(
        report_id: str,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> ReportSnapshot:
        document = ReportRepository(session).get(report_id)
        if document is None:
            raise HTTPException(status_code=404, detail="report not found")
        return ReportSnapshot.model_validate(document)

    @router.get("/incidents/{incident_id}/reports", response_model=list[ReportSummary])
    def list_incident_reports(
        incident_id: UUID,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[ReportSummary]:
        documents = ReportRepository(session).list_all(incident_id=incident_id, limit=100)
        return [report_summary(ReportSnapshot.model_validate(doc)) for doc in documents]

    def _load_report(session: Session, report_id: str) -> ReportSnapshot:
        document = ReportRepository(session).get(report_id)
        if document is None:
            raise HTTPException(status_code=404, detail="report not found")
        return ReportSnapshot.model_validate(document)

    @router.get("/reports/{report_id}/markdown", response_class=PlainTextResponse)
    def report_markdown(
        report_id: str,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> PlainTextResponse:
        snapshot = _load_report(session, report_id)
        return PlainTextResponse(
            to_markdown(snapshot),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'inline; filename="report-{report_id}.md"'},
        )

    @router.get("/reports/{report_id}/json")
    def report_json(
        report_id: str,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> Response:
        snapshot = _load_report(session, report_id)
        return Response(
            snapshot.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="report-{report_id}.json"'},
        )

    @router.get("/reports/{report_id}/pdf")
    def report_pdf(
        report_id: str,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> Response:
        snapshot = _load_report(session, report_id)
        return Response(
            to_pdf(snapshot),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="report-{report_id}.pdf"'},
        )

    @router.get("/stream")
    def stream() -> StreamingResponse:
        """Emit an event whenever incidents, events or diagnoses change."""
        return StreamingResponse(
            global_stream(session_factory),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    @router.get("/incidents/{incident_id}/stream")
    def incident_stream_route(incident_id: UUID) -> StreamingResponse:
        """Emit an event whenever this incident gains an event or a diagnosis."""
        return StreamingResponse(
            incident_stream(session_factory, incident_id),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    return router
