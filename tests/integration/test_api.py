"""Control-plane HTTP contract tests."""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.control_plane.main import create_app
from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.storage.database import create_session_factory
from packages.storage.models import Base
from packages.storage.repositories import IncidentRepository

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path) -> Generator[tuple[TestClient, UUID], None, None]:
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.SYSTEM,
        title="API test incident",
        created_at=NOW,
        updated_at=NOW,
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident)

    with TestClient(create_app(factory)) as test_client:
        yield test_client, incident.incident_id
    engine.dispose()


def test_health_and_readiness(client: tuple[TestClient, UUID]) -> None:
    test_client, _ = client
    assert test_client.get("/health").json() == {"status": "ok"}
    assert test_client.get("/ready").json() == {"status": "ready"}


def test_incident_and_timeline_queries(client: tuple[TestClient, UUID]) -> None:
    test_client, incident_id = client

    response = test_client.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    assert response.json()["incident_id"] == str(incident_id)

    timeline = test_client.get(f"/api/v1/incidents/{incident_id}/events")
    assert timeline.status_code == 200
    assert timeline.json()[0]["event_type"] == "INCIDENT_CREATED"

    assert test_client.get("/api/v1/incidents").status_code == 200
    assert test_client.get(f"/api/v1/incidents/{incident_id}/alerts").json() == []
    assert test_client.get(f"/api/v1/incidents/{incident_id}/evidence").json() == []


def test_not_found_and_validation_errors_are_typed(client: tuple[TestClient, UUID]) -> None:
    test_client, _ = client
    correlation_id = "api-test-correlation"
    response = test_client.get(
        f"/api/v1/incidents/{uuid4()}", headers={"X-Correlation-ID": correlation_id}
    )
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "INCIDENT_NOT_FOUND",
            "message": "Incident was not found.",
            "correlation_id": correlation_id,
        }
    }

    invalid = test_client.get(
        "/api/v1/incidents/not-a-uuid", headers={"X-Correlation-ID": correlation_id}
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert invalid.json()["error"]["correlation_id"] == correlation_id


def test_readiness_reports_database_unavailable() -> None:
    engine = create_engine(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:1/agentic_sre?connect_timeout=1"
    )
    with TestClient(create_app(create_session_factory(engine))) as test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    engine.dispose()
