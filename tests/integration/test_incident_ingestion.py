"""Alertmanager-to-incident deterministic ingestion tests."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from packages.contracts import AlertmanagerAlertPayload, AlertmanagerWebhook, IncidentEventType
from packages.incident import IncidentManager, normalize_alert
from packages.storage.models import AlertRow, Base, IncidentEventRow, IncidentRow

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def make_alert(status: str = "firing") -> AlertmanagerAlertPayload:
    return AlertmanagerAlertPayload(
        status=status,
        labels={
            "alertname": "HighErrorRate",
            "service": "payment-service",
            "namespace": "sre-demo",
            "cluster": "agentic-sre",
            "severity": "critical",
        },
        annotations={"description": "Payment errors elevated"},
        startsAt=NOW,
    )


def test_fingerprint_and_normalization_are_stable() -> None:
    first = normalize_alert(make_alert())
    second = normalize_alert(make_alert())

    assert first.fingerprint == second.fingerprint
    assert first.status.value == "FIRING"
    assert first.service == "payment-service"


def test_firing_alert_sentinel_end_is_not_an_observation_boundary() -> None:
    payload = make_alert().model_copy(update={"ends_at": datetime.min.replace(tzinfo=UTC)})

    normalized = normalize_alert(payload)

    assert normalized.ends_at is None


def test_repeated_firing_and_resolution_share_one_incident(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'incidents.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        manager = IncidentManager(session)
        first = manager.ingest(normalize_alert(make_alert()), now=NOW)
        for _ in range(9):
            repeated = manager.ingest(normalize_alert(make_alert()), now=NOW)
            assert repeated.incident_id == first.incident_id
        resolved = manager.ingest(normalize_alert(make_alert("resolved")), now=NOW)

        assert resolved.incident_id == first.incident_id
        assert session.scalar(select(func.count(IncidentRow.incident_id))) == 1
        assert session.scalar(select(func.count(AlertRow.alert_id))) == 1
        event_types = session.scalars(
            select(IncidentEventRow.event_type).order_by(IncidentEventRow.sequence)
        ).all()
        assert event_types[0] == IncidentEventType.INCIDENT_CREATED.value
        assert event_types[-1] == IncidentEventType.ALERT_RESOLVED.value
        assert len(event_types) == 11
    engine.dispose()


def test_webhook_contract_accepts_alertmanager_json() -> None:
    webhook = AlertmanagerWebhook.model_validate_json(
        AlertmanagerWebhook(
            receiver="control-plane",
            status="firing",
            alerts=[make_alert()],
        ).model_dump_json(by_alias=True)
    )

    assert len(webhook.alerts) == 1
