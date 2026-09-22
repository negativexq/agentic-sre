"""The /api/v1/console/* router: the operator console's data surface."""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.control_plane.console.dto import (
    DashboardCounters,
    DashboardSummary,
    DiagnosisView,
    EvidenceView,
    IncidentDetail,
    IncidentListItem,
    IncidentPage,
    SystemStatus,
    TimelineView,
)
from apps.control_plane.console.mappers import (
    diagnosis_view,
    evidence_view,
    incident_list_item,
    timeline_view,
)
from packages.rca.model import Diagnosis
from packages.storage import (
    AlertRepository,
    DiagnosisRepository,
    EvidenceRepository,
    IncidentEventRepository,
    IncidentNotFoundError,
    IncidentRepository,
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
) -> APIRouter:
    """Build the console router bound to the app's session dependency.

    ``system_status`` is injected so the router stays free of connector wiring;
    the app supplies a provider that probes what it can honestly determine.
    """
    router = APIRouter(prefix="/api/v1/console", tags=["console"])

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

    return router
