"""Contract tests for the /api/v1/console/* operator-console API (M2)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.control_plane.main import create_app
from packages.contracts import (
    AlertSource,
    AlertStatus,
    ChangeRecord,
    ChangeScope,
    ChangeType,
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.rca.model import (
    CausalHop,
    Confidence,
    Diagnosis,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Resolution,
    Symptoms,
)
from packages.storage.database import create_session_factory
from packages.storage.models import AlertRow, Base, EvidenceRow
from packages.storage.repositories import (
    ChangeRecordRepository,
    DiagnosisRepository,
    IncidentEventRepository,
    IncidentRepository,
)

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
PHASES = [
    (IncidentEventType.DIAGNOSIS_STARTED, 0),
    (IncidentEventType.EVIDENCE_GATHERED, 4),
    (IncidentEventType.RCA_ENGINE_COMPLETED, 9),
    (IncidentEventType.DIAGNOSIS_COMPLETED, 10),
]


def _incident(title: str, severity: IncidentSeverity, status: IncidentStatus) -> Incident:
    return Incident(
        status=status,
        severity=severity,
        source=IncidentSource.ALERTMANAGER,
        title=title,
        created_at=T0,
        updated_at=T0 + timedelta(seconds=12),
    )


def _diagnosis(incident_id: UUID, resolution: Resolution) -> Diagnosis:
    actor = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
    symptom = EntityRef(kind="Deployment", name="order-service", namespace="sre-demo")
    finding = Finding(
        kind=FindingKind.SPEC_CHANGE,
        entity=actor,
        at=T0 - timedelta(seconds=30),
        summary="payment-service image changed 30s before onset",
        evidence_ids=("obj-1",),
        temporal_role=EvidenceTemporalRole.INITIATING,
        onset_delta_seconds=-30.0,
    )
    return Diagnosis(
        incident_id=str(incident_id),
        root_cause=actor,
        confidence=Confidence.VERIFIED,
        resolution=resolution,
        summary="payment-service change initiated the order latency",
        symptoms=Symptoms(
            onset=T0,
            last_seen=T0,
            services=("order-service",),
            namespaces=("sre-demo",),
            alert_names=("OrderDependencyLatencyHigh",),
        ),
        evidence=(finding,),
        causal_path=(CausalHop(source=actor, relation="dependency", target=symptom),),
        causal_explanation="LINKED",
        hypothesis=Hypothesis(
            hypothesis_id="h1",
            causal_actor=actor,
            initiating_findings=(finding,),
        ),
    )


def _seed(
    session: Session,
    *,
    title: str,
    severity: IncidentSeverity,
    status: IncidentStatus,
    resolution: Resolution | None,
    with_completed_event: bool = True,
) -> UUID:
    incident = _incident(title, severity, status)
    IncidentRepository(session).create(incident)
    incident_id = incident.incident_id
    if resolution is None:
        return incident_id

    run_id = str(uuid4())
    diagnosis = _diagnosis(incident_id, resolution)
    DiagnosisRepository(session).save(
        incident_id,
        diagnosis.model_dump(mode="json"),
        created_at=T0 + timedelta(seconds=10),
        run_id=run_id,
    )
    events = IncidentEventRepository(session)
    for event_type, offset in PHASES:
        if event_type is IncidentEventType.DIAGNOSIS_COMPLETED and not with_completed_event:
            continue
        events.append(
            IncidentEvent(
                incident_id=incident_id,
                event_type=event_type,
                timestamp=T0 + timedelta(seconds=offset),
                correlation_id=incident.correlation_id,
                payload={"run_id": run_id},
            )
        )
    session.add(
        AlertRow(
            alert_id=uuid4(),
            incident_id=incident_id,
            alert_name="OrderDependencyLatencyHigh",
            service="order-service",
            namespace="sre-demo",
            cluster="kind",
            starts_at=T0 - timedelta(seconds=2),
            ends_at=None,
            labels={},
            annotations={},
            fingerprint=f"fp-{title}",
            status=AlertStatus.FIRING.value,
            source=AlertSource.PROMETHEUS.value,
        )
    )
    session.add(
        EvidenceRow(
            evidence_id=uuid4(),
            incident_id=incident_id,
            source_type="KUBERNETES",
            source_system="cluster-reader",
            observation={"kind": "Deployment", "name": "payment-service"},
            time_window={
                "starts_at": (T0 - timedelta(minutes=2)).isoformat(),
                "ends_at": T0.isoformat(),
            },
            tool_call_id=uuid4(),
            raw_result_reference="obj-1",
            collected_at=T0 + timedelta(seconds=5),
        )
    )
    # A durable change on the leading actor 30s before onset, plus unrelated noise.
    ChangeRecordRepository(session).append(
        ChangeRecord(
            timestamp=T0 - timedelta(seconds=30),
            resource_type="Deployment",
            resource_name="payment-service",
            change_type=ChangeType.UPDATED,
            scope=ChangeScope.DEPLOYMENT,
            before={"revision": "1"},
            after={"revision": "2"},
            revision="2",
            source="harness",
        )
    )
    ChangeRecordRepository(session).append(
        ChangeRecord(
            timestamp=T0 - timedelta(minutes=7),
            resource_type="Deployment",
            resource_name="frontend",
            change_type=ChangeType.SCALED,
            scope=ChangeScope.DEPLOYMENT,
            before={"replicas": 2},
            after={"replicas": 3},
            revision="9",
            source="harness",
        )
    )
    session.commit()
    return incident_id


@pytest.fixture
def app_client() -> Generator[tuple[TestClient, dict[str, UUID]], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = create_session_factory(engine)
    ids: dict[str, UUID] = {}
    with Session(engine) as session:
        ids["resolved"] = _seed(
            session,
            title="payment dependency failure",
            severity=IncidentSeverity.CRITICAL,
            status=IncidentStatus.INVESTIGATING,
            resolution=Resolution.RESOLVED,
        )
        ids["ambiguous"] = _seed(
            session,
            title="checkout latency",
            severity=IncidentSeverity.WARNING,
            status=IncidentStatus.INVESTIGATING,
            resolution=Resolution.AMBIGUOUS,
        )
        ids["pending"] = _seed(
            session,
            title="awaiting diagnosis",
            severity=IncidentSeverity.CRITICAL,
            status=IncidentStatus.OPEN,
            resolution=None,
        )
        ids["dropped"] = _seed(
            session,
            title="lost timeline",
            severity=IncidentSeverity.INFO,
            status=IncidentStatus.INVESTIGATING,
            resolution=Resolution.RESOLVED,
            with_completed_event=False,
        )
    with TestClient(create_app(factory)) as client:
        yield client, ids
    engine.dispose()


def test_dashboard_counters_and_system(app_client: tuple[TestClient, dict[str, UUID]]) -> None:
    client, _ = app_client
    body = client.get("/api/v1/console/dashboard").json()
    counters = body["counters"]
    assert counters["active_incidents"] == 4
    assert counters["critical_incidents"] == 2
    assert counters["diagnosing"] == 1  # the pending incident has no diagnosis
    assert counters["resolved_diagnoses"] == 2
    assert counters["median_diagnosis_seconds"] is not None
    names = {c["name"]: c["status"] for c in body["system"]["connectors"]}
    assert names["Database"] == "connected"
    assert names["Alertmanager"] == "connected"
    assert names["Prometheus"] == "not_configured"


def test_incident_list_filters_and_pagination(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, _ = app_client
    everything = client.get("/api/v1/console/incidents").json()
    assert everything["total"] == 4

    critical = client.get("/api/v1/console/incidents?severity=CRITICAL").json()
    assert critical["total"] == 2
    assert {item["severity"] for item in critical["items"]} == {"CRITICAL"}

    resolved = client.get("/api/v1/console/incidents?resolution=RESOLVED").json()
    assert resolved["total"] == 2

    page = client.get("/api/v1/console/incidents?limit=1&offset=0").json()
    assert len(page["items"]) == 1
    assert page["total"] == 4

    query = client.get("/api/v1/console/incidents?q=checkout").json()
    assert query["total"] == 1
    assert query["items"][0]["title"] == "checkout latency"


def test_incident_detail_binds_timeline_to_run(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = app_client
    body = client.get(f"/api/v1/console/incidents/{ids['resolved']}").json()

    diagnosis = body["diagnosis"]
    assert diagnosis["resolution"] == "RESOLVED"
    assert diagnosis["is_resolved"] is True
    assert diagnosis["leading_root_actor"] == "sre-demo/Deployment/payment-service"
    assert diagnosis["root_cause"] == "sre-demo/Deployment/payment-service"
    assert diagnosis["causal_path"][0]["relation"] == "dependency"
    assert diagnosis["initiating_findings"][0]["kind"] == "SPEC_CHANGE"

    timeline = body["timeline"]
    assert [phase["name"] for phase in timeline["phases"]] == [
        "Diagnosis started",
        "Evidence gathered",
        "RCA engine completed",
        "Diagnosis stored",
    ]
    assert timeline["phases"][-1]["offset_seconds"] == 10.0
    assert timeline["note"] is None
    assert body["evidence_count"] == 1


def test_ambiguous_is_not_resolved(app_client: tuple[TestClient, dict[str, UUID]]) -> None:
    client, ids = app_client
    diagnosis = client.get(f"/api/v1/console/incidents/{ids['ambiguous']}/diagnosis").json()
    assert diagnosis["resolution"] == "AMBIGUOUS"
    assert diagnosis["is_resolved"] is False
    # Leading actor is still surfaced; root_cause is withheld until resolved.
    assert diagnosis["leading_root_actor"] == "sre-demo/Deployment/payment-service"
    assert diagnosis["root_cause"] is None


def test_timeline_unavailable_when_completing_event_lost(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = app_client
    timeline = client.get(f"/api/v1/console/incidents/{ids['dropped']}/timeline").json()
    assert timeline["phases"] == []
    assert timeline["note"] == "Timeline unavailable or incomplete for this diagnosis run."


def test_evidence_endpoint_returns_provenance(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = app_client
    evidence = client.get(f"/api/v1/console/incidents/{ids['resolved']}/evidence").json()
    assert len(evidence) == 1
    assert evidence[0]["raw_result_reference"] == "obj-1"
    assert evidence[0]["source_type"] == "KUBERNETES"


def test_unknown_incident_is_typed_not_found(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, _ = app_client
    missing = client.get(f"/api/v1/console/incidents/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


def test_global_changes_list_and_filter(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, _ = app_client
    everything = client.get("/api/v1/console/changes").json()
    assert len(everything) >= 2
    scaled = client.get("/api/v1/console/changes?change_type=SCALED").json()
    assert scaled and all(change["change_type"] == "SCALED" for change in scaled)
    named = client.get("/api/v1/console/changes?q=payment").json()
    assert named and all("payment" in change["resource_name"] for change in named)


def test_incident_changes_mark_leading_actor_without_claiming_cause(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = app_client
    changes = client.get(f"/api/v1/console/incidents/{ids['resolved']}/changes").json()
    by_resource = {change["resource_name"]: change for change in changes}
    # The payment-service change sits 30s before onset and is flagged as the
    # leading actor; the frontend change is present but not flagged.
    assert by_resource["payment-service"]["matches_leading_actor"] is True
    assert by_resource["payment-service"]["onset_delta_seconds"] == -30.0
    assert by_resource["frontend"]["matches_leading_actor"] is False


def test_legacy_incident_api_is_unchanged(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, _ = app_client
    legacy = client.get("/api/v1/incidents")
    assert legacy.status_code == 200
    # The raw contract endpoint still returns full Incident models, not DTOs.
    assert all("correlation_id" in item for item in legacy.json())
