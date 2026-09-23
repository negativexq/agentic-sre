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
    InvestigationResult,
    InvestigationStopReason,
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
    InvestigationRunRepository,
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


def test_report_create_fetch_and_immutability(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = app_client
    created = client.post(f"/api/v1/console/incidents/{ids['resolved']}/reports")
    assert created.status_code == 201
    report = created.json()
    assert report["diagnosis_run_id"]
    assert report["root_actor"] == "sre-demo/Deployment/payment-service"
    assert report["report_version"] == "2.0"

    fetched = client.get(f"/api/v1/console/reports/{report['report_id']}").json()
    assert fetched == report  # the stored snapshot is returned verbatim

    # A second report is a new immutable row; the first is unchanged.
    again = client.post(f"/api/v1/console/incidents/{ids['resolved']}/reports").json()
    assert again["report_id"] != report["report_id"]
    still = client.get(f"/api/v1/console/reports/{report['report_id']}").json()
    assert still == report

    listed = client.get("/api/v1/console/reports").json()
    assert {item["report_id"] for item in listed} >= {report["report_id"], again["report_id"]}


def test_report_projects_persisted_investigation_artifact() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = create_session_factory(engine)
    with Session(engine) as session:
        incident_id = _seed(
            session,
            title="persisted investigation report",
            severity=IncidentSeverity.WARNING,
            status=IncidentStatus.INVESTIGATING,
            resolution=Resolution.RESOLVED,
        )
        diagnosis = DiagnosisRepository(session).latest(incident_id)
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        assert diagnosis is not None
        assert run_id is not None
        result = InvestigationResult(
            diagnosis=Diagnosis.model_validate(diagnosis),
            initial_diagnosis=Diagnosis.model_validate(diagnosis),
            initial_resolution=Resolution.RESOLVED,
            final_resolution=Resolution.RESOLVED,
            stop_reason=InvestigationStopReason.NO_RESOLVABLE_GAP,
        )
        InvestigationRunRepository(session).save(
            diagnosis_run_id=run_id,
            incident_id=incident_id,
            artifact_version="1.0",
            created_at=T0 + timedelta(seconds=11),
            document=result.model_dump(mode="json"),
        )

    with TestClient(create_app(session_factory=factory)) as client:
        response = client.post(f"/api/v1/console/incidents/{incident_id}/reports")
        assert response.status_code == 201
        frozen = response.json()

        with factory() as session:
            latest = DiagnosisRepository(session).latest(incident_id)
            assert latest is not None
            later_diagnosis = Diagnosis.model_validate(latest).model_copy(
                update={"summary": "a later diagnosis with new wording"}
            )
            DiagnosisRepository(session).save(
                incident_id,
                later_diagnosis.model_dump(mode="json"),
                created_at=T0 + timedelta(seconds=30),
                run_id=str(uuid4()),
            )

        later_report = client.post(f"/api/v1/console/incidents/{incident_id}/reports").json()
        fetched_frozen = client.get(f"/api/v1/console/reports/{frozen['report_id']}").json()

    assert fetched_frozen == frozen
    assert later_report["summary"] == "a later diagnosis with new wording"
    assert later_report["report_id"] != frozen["report_id"]
    report = frozen
    assert report["investigation_summary"]["initial_resolution"] == "RESOLVED"
    assert report["investigation_summary"]["stop_reason"] == "NO_RESOLVABLE_GAP"
    assert report["agent_safety_audit"]["selected_actions"] == 0


def test_report_exports_markdown_json_and_pdf(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, ids = app_client
    report = client.post(f"/api/v1/console/incidents/{ids['resolved']}/reports").json()
    rid = report["report_id"]

    markdown = client.get(f"/api/v1/console/reports/{rid}/markdown")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "Incident report" in markdown.text

    body = client.get(f"/api/v1/console/reports/{rid}/json")
    assert body.headers["content-type"].startswith("application/json")
    assert body.json()["report_id"] == rid

    pdf = client.get(f"/api/v1/console/reports/{rid}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-")


def test_report_create_requires_token_when_configured(
    app_client: tuple[TestClient, dict[str, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, ids = app_client
    monkeypatch.setenv("SRE_API_TOKEN", "secret-token")
    path = f"/api/v1/console/incidents/{ids['resolved']}/reports"

    assert client.post(path).status_code == 401
    assert client.post(path, headers={"Authorization": "Bearer secret-token"}).status_code == 201
    # Read endpoints stay open even with a token configured.
    assert client.get("/api/v1/console/reports").status_code == 200


def test_report_requires_a_diagnosis(app_client: tuple[TestClient, dict[str, UUID]]) -> None:
    client, ids = app_client
    response = client.post(f"/api/v1/console/incidents/{ids['pending']}/reports")
    assert response.status_code == 409


def test_missing_report_is_not_found(app_client: tuple[TestClient, dict[str, UUID]]) -> None:
    client, _ = app_client
    assert client.get("/api/v1/console/reports/does-not-exist").status_code == 404


def test_settings_are_read_only_and_mask_secrets(
    app_client: tuple[TestClient, dict[str, UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = app_client
    monkeypatch.setenv("SRE_API_TOKEN", "supersecret-token")
    monkeypatch.setenv("SRE_WATCH_NAMESPACES", "sre-demo, other")
    monkeypatch.setenv("SRE_AUTO_DIAGNOSE", "true")
    response = client.get("/api/v1/console/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["api_token_configured"] is True
    assert body["auto_diagnose"] is True
    assert body["watched_namespaces"] == ["sre-demo", "other"]
    # The secret value itself is never returned anywhere in the payload.
    assert "supersecret-token" not in response.text


def test_legacy_incident_api_is_unchanged(
    app_client: tuple[TestClient, dict[str, UUID]],
) -> None:
    client, _ = app_client
    legacy = client.get("/api/v1/incidents")
    assert legacy.status_code == 200
    # The raw contract endpoint still returns full Incident models, not DTOs.
    assert all("correlation_id" in item for item in legacy.json())
