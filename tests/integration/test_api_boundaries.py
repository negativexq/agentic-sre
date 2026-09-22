"""HTTP trust-boundary regression tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apps.control_plane.diagnosis import DiagnosisService
from apps.control_plane.main import create_app
from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.storage.database import create_session_factory
from packages.storage.models import Base, DiagnosisRow
from packages.storage.repositories import IncidentRepository


def _app(tmp_path: Path) -> tuple[Any, Any, UUID, Any]:
    engine = create_engine(f"sqlite:///{tmp_path / 'api-boundaries.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.SYSTEM,
        title="Boundary test incident",
        created_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident)
    service = DiagnosisService(session_factory=factory, namespaces=("sre-demo",))
    return engine, factory, incident.incident_id, service


def test_html_get_never_generates_or_persists_diagnosis(tmp_path: Path, monkeypatch: Any) -> None:
    engine, factory, incident_id, service = _app(tmp_path)
    calls = 0
    original = service.run

    def counted(incident: UUID) -> Any:
        nonlocal calls
        calls += 1
        return original(incident)

    service.run = counted
    monkeypatch.setenv("SRE_API_TOKEN", "boundary-token")
    with TestClient(create_app(factory, diagnosis_service=service)) as client:
        response = client.get(f"/incidents/{incident_id}")
        assert response.status_code == 200
        assert "Diagnosis running" in response.text
        assert calls == 0
    with Session(engine) as session:
        assert session.scalar(select(DiagnosisRow.diagnosis_id)) is None
    engine.dispose()


def test_diagnosis_post_is_protected_and_authorized_post_runs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    engine, factory, incident_id, service = _app(tmp_path)
    calls = 0
    original = service.run

    def counted(incident: UUID) -> Any:
        nonlocal calls
        calls += 1
        return original(incident)

    service.run = counted
    monkeypatch.setenv("SRE_API_TOKEN", "boundary-token")
    with TestClient(create_app(factory, diagnosis_service=service)) as client:
        assert client.post(f"/api/v1/incidents/{incident_id}/diagnosis").status_code == 401
        assert calls == 0
        created = client.post(
            f"/api/v1/incidents/{incident_id}/diagnosis",
            headers={"Authorization": "Bearer boundary-token"},
        )
        assert created.status_code == 200
        assert calls == 1
    with Session(engine) as session:
        assert session.scalar(select(DiagnosisRow.diagnosis_id)) is not None
    engine.dispose()


def test_alertmanager_webhook_authentication_is_optional_and_bearer_protected(
    tmp_path: Path, monkeypatch: Any
) -> None:
    engine, factory, _incident_id, _service = _app(tmp_path)
    payload = {
        "receiver": "control-plane",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "service": "payment-service",
                    "namespace": "sre-demo",
                    "cluster": "test",
                },
                "annotations": {},
                "startsAt": "2026-09-17T10:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": "boundary-webhook",
            }
        ],
    }
    monkeypatch.setenv("SRE_API_TOKEN", "boundary-token")
    with TestClient(create_app(factory)) as client:
        assert client.post("/api/v1/webhooks/alertmanager", json=payload).status_code == 401
        assert (
            client.post(
                "/api/v1/webhooks/alertmanager",
                json=payload,
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        accepted = client.post(
            "/api/v1/webhooks/alertmanager",
            json=payload,
            headers={"Authorization": "Bearer boundary-token"},
        )
        assert accepted.status_code == 200
    monkeypatch.delenv("SRE_API_TOKEN")
    with TestClient(create_app(factory)) as client:
        assert client.post("/api/v1/webhooks/alertmanager", json=payload).status_code == 200
    engine.dispose()
