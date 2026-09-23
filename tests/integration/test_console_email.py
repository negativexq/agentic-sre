"""Report sharing over email: rendering, delivery, audit and idempotency (M10)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.control_plane.console.email_delivery import EmailDelivery
from apps.control_plane.main import create_app
from packages.contracts import (
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.rca.model import Confidence, Diagnosis, EntityRef, Resolution, Symptoms
from packages.report.email import EmailPayload
from packages.storage.database import create_session_factory
from packages.storage.models import Base
from packages.storage.repositories import (
    DiagnosisRepository,
    IncidentEventRepository,
    IncidentRepository,
)

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class FakeDelivery(EmailDelivery):
    """Records sends instead of talking to SMTP."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[list[str], EmailPayload]] = []
        self._fail = fail

    def send(self, recipients: list[str], payload: EmailPayload) -> None:
        if self._fail:
            raise RuntimeError("smtp unavailable")
        self.sent.append((recipients, payload))


def _seed_resolved(session: Session) -> None:
    incident = Incident(
        status=IncidentStatus.INVESTIGATING,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="OrderDependencyLatencyHigh",
        created_at=T0,
        updated_at=T0 + timedelta(seconds=24),
    )
    IncidentRepository(session).create(incident)
    actor = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
    diagnosis = Diagnosis(
        incident_id=str(incident.incident_id),
        root_cause=actor,
        confidence=Confidence.VERIFIED,
        resolution=Resolution.RESOLVED,
        summary="payment change initiated order latency",
        symptoms=Symptoms(
            onset=T0,
            last_seen=T0,
            services=("order-service",),
            namespaces=("sre-demo",),
            alert_names=("OrderDependencyLatencyHigh",),
        ),
    )
    DiagnosisRepository(session).save(
        incident.incident_id,
        diagnosis.model_dump(mode="json"),
        created_at=T0 + timedelta(seconds=24),
        run_id=str(uuid4()),
    )
    IncidentEventRepository(session).append(
        IncidentEvent(
            incident_id=incident.incident_id,
            event_type=IncidentEventType.DIAGNOSIS_COMPLETED,
            timestamp=T0 + timedelta(seconds=24),
            correlation_id=incident.correlation_id,
            payload={"run_id": "r"},
        )
    )


def _make_client(delivery: EmailDelivery | None) -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = create_session_factory(engine)
    with Session(engine) as session:
        _seed_resolved(session)
    return TestClient(create_app(factory, email_delivery=delivery)), factory


@pytest.fixture
def sent_client() -> Generator[tuple[TestClient, FakeDelivery, str], None, None]:
    delivery = FakeDelivery()
    client, _ = _make_client(delivery)
    with client:
        incident_id = client.get("/api/v1/console/incidents").json()["items"][0]["incident_id"]
        report = client.post(f"/api/v1/console/incidents/{incident_id}/reports").json()
        yield client, delivery, report["report_id"]


def test_share_is_refused_when_email_unconfigured() -> None:
    client, _ = _make_client(None)
    with client:
        incident_id = client.get("/api/v1/console/incidents").json()["items"][0]["incident_id"]
        report = client.post(f"/api/v1/console/incidents/{incident_id}/reports").json()
        response = client.post(
            f"/api/v1/console/reports/{report['report_id']}/email",
            json={"recipients": ["sre@example.com"]},
        )
        assert response.status_code == 503


def test_share_sends_and_audits(
    sent_client: tuple[TestClient, FakeDelivery, str],
) -> None:
    client, delivery, report_id = sent_client
    response = client.post(
        f"/api/v1/console/reports/{report_id}/email",
        json={"recipients": ["sre@example.com", " "], "include_pdf": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "sent"
    assert body["recipients"] == ["sre@example.com"]  # blank entry dropped
    assert len(delivery.sent) == 1

    audit = client.get(f"/api/v1/console/reports/{report_id}/deliveries").json()
    assert len(audit) == 1
    assert audit[0]["recipients"] == ["sre@example.com"]


def test_share_is_idempotent_by_key(
    sent_client: tuple[TestClient, FakeDelivery, str],
) -> None:
    client, delivery, report_id = sent_client
    payload = {"recipients": ["sre@example.com"], "idempotency_key": "abc-123"}
    first = client.post(f"/api/v1/console/reports/{report_id}/email", json=payload).json()
    second = client.post(f"/api/v1/console/reports/{report_id}/email", json=payload).json()
    assert first["delivery_id"] == second["delivery_id"]
    assert len(delivery.sent) == 1  # not sent twice


def test_share_requires_token_when_configured(
    sent_client: tuple[TestClient, FakeDelivery, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, delivery, report_id = sent_client
    monkeypatch.setenv("SRE_API_TOKEN", "secret-token")
    body = {"recipients": ["sre@example.com"]}

    unauth = client.post(f"/api/v1/console/reports/{report_id}/email", json=body)
    assert unauth.status_code == 401
    assert delivery.sent == []  # nothing sent without the token

    ok = client.post(
        f"/api/v1/console/reports/{report_id}/email",
        json=body,
        headers={"Authorization": "Bearer secret-token"},
    )
    assert ok.status_code == 200
    assert len(delivery.sent) == 1


def test_same_key_across_reports_both_send(
    sent_client: tuple[TestClient, FakeDelivery, str],
) -> None:
    client, delivery, first_report = sent_client
    incident_id = client.get("/api/v1/console/incidents").json()["items"][0]["incident_id"]
    second_report = client.post(f"/api/v1/console/incidents/{incident_id}/reports").json()[
        "report_id"
    ]
    assert second_report != first_report

    body = {"recipients": ["sre@example.com"], "idempotency_key": "shared-key"}
    one = client.post(f"/api/v1/console/reports/{first_report}/email", json=body).json()
    two = client.post(f"/api/v1/console/reports/{second_report}/email", json=body).json()

    # The key is scoped per report: both send, with distinct deliveries.
    assert one["delivery_id"] != two["delivery_id"]
    assert len(delivery.sent) == 2


def test_share_requires_a_recipient(
    sent_client: tuple[TestClient, FakeDelivery, str],
) -> None:
    client, _, report_id = sent_client
    response = client.post(f"/api/v1/console/reports/{report_id}/email", json={"recipients": []})
    assert response.status_code == 422


def test_failed_delivery_is_recorded_and_surfaced() -> None:
    delivery = FakeDelivery(fail=True)
    client, _ = _make_client(delivery)
    with client:
        incident_id = client.get("/api/v1/console/incidents").json()["items"][0]["incident_id"]
        report = client.post(f"/api/v1/console/incidents/{incident_id}/reports").json()
        response = client.post(
            f"/api/v1/console/reports/{report['report_id']}/email",
            json={"recipients": ["sre@example.com"]},
        )
        assert response.status_code == 502
        audit = client.get(f"/api/v1/console/reports/{report['report_id']}/deliveries").json()
        assert audit[0]["status"] == "failed"
        assert audit[0]["error"]
