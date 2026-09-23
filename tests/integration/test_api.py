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
from packages.contracts import (
    ChangeRecord,
    ChangeType,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
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


def test_security_headers_applied(client: tuple[TestClient, UUID]) -> None:
    test_client, _ = client
    response = test_client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_spa_is_served_when_built(client: tuple[TestClient, UUID]) -> None:
    from apps.control_plane.main import WEB_DIST

    if not (WEB_DIST / "index.html").exists():
        pytest.skip("operator console has not been built (apps/web/dist absent)")
    test_client, _ = client
    # Deep client routes fall back to the SPA shell.
    response = test_client.get("/app/incidents/abc")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<div id="root">' in response.text


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


def test_change_journal_accepts_and_reads_harness_facts(client: tuple[TestClient, UUID]) -> None:
    """The control-plane persistence boundary preserves historical change facts."""
    test_client, _ = client
    record = ChangeRecord(
        timestamp=NOW,
        resource_type="Deployment",
        resource_name="payment-service",
        change_type=ChangeType.UPDATED,
        before={"revision": "a"},
        after={"revision": "b"},
        revision="b",
        source="deterministic-harness",
    )
    created = test_client.post("/api/v1/changes", json=record.model_dump(mode="json"))
    assert created.status_code == 200

    queried = test_client.get(
        "/api/v1/changes",
        params={
            "resource_name": "payment-service",
            "starts_at": NOW.isoformat(),
            "ends_at": NOW.isoformat(),
        },
    )
    assert queried.status_code == 200
    assert queried.json()[0]["revision"] == "b"


def test_readiness_reports_database_unavailable() -> None:
    engine = create_engine(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:1/agentic_sre?connect_timeout=1"
    )
    with TestClient(create_app(create_session_factory(engine))) as test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    engine.dispose()


def test_write_endpoints_require_the_configured_api_token(
    client: tuple[TestClient, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, incident_id = client
    monkeypatch.setenv("SRE_API_TOKEN", "secret-token")
    record = ChangeRecord(
        timestamp=NOW,
        resource_type="Deployment",
        resource_name="payment-service",
        change_type=ChangeType.UPDATED,
        before={"revision": "a"},
        after={"revision": "b"},
        revision="b",
        source="deterministic-harness",
    ).model_dump(mode="json")

    unauthenticated = test_client.post("/api/v1/changes", json=record)
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"] == "missing or invalid bearer token"

    wrong_token = test_client.post(
        "/api/v1/changes", json=record, headers={"Authorization": "Bearer wrong"}
    )
    assert wrong_token.status_code == 401

    authenticated = test_client.post(
        "/api/v1/changes", json=record, headers={"Authorization": "Bearer secret-token"}
    )
    assert authenticated.status_code == 200

    # Read-only endpoints stay open even when a token is configured.
    assert test_client.get("/api/v1/incidents").status_code == 200
    assert test_client.get(f"/api/v1/incidents/{incident_id}").status_code == 200

    # The other write endpoints are gated the same way.
    assert test_client.post("/api/v1/cluster/snapshot").status_code == 401
    assert (
        test_client.post(
            "/api/v1/cluster/snapshot", headers={"Authorization": "Bearer secret-token"}
        ).status_code
        == 200
    )
    assert test_client.post(f"/api/v1/incidents/{incident_id}/diagnosis").status_code == 401


def test_write_endpoints_are_open_when_no_api_token_is_configured(
    client: tuple[TestClient, UUID],
) -> None:
    """The default, unconfigured state matches the offline demo and kind walkthrough."""
    test_client, _ = client
    assert test_client.post("/api/v1/cluster/snapshot").status_code == 200
