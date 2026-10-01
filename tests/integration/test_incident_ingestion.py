"""Alertmanager-to-incident deterministic ingestion tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
        resolved_row = session.get(IncidentRow, first.incident_id)
        assert resolved_row is not None
        assert resolved_row.status == "RESOLVED"
        assert session.scalar(select(func.count(IncidentRow.incident_id))) == 1
        assert session.scalar(select(func.count(AlertRow.alert_id))) == 1
        event_types = session.scalars(
            select(IncidentEventRow.event_type).order_by(IncidentEventRow.sequence)
        ).all()
        assert event_types[0] == IncidentEventType.INCIDENT_CREATED.value
        assert event_types[-1] == IncidentEventType.ALERT_RESOLVED.value
        assert len(event_types) == 2
    engine.dispose()


def test_resolved_fingerprint_creates_a_new_incident_episode(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'recurrence.db'}")
    Base.metadata.create_all(engine)
    later = NOW.replace(minute=20)
    with Session(engine) as session:
        manager = IncidentManager(session)
        first = manager.ingest(normalize_alert(make_alert()), now=NOW)
        assert (
            manager.ingest(normalize_alert(make_alert()), now=NOW).incident_id == first.incident_id
        )
        resolved = manager.ingest(
            normalize_alert(make_alert("resolved")), now=NOW.replace(minute=10)
        )
        assert resolved.incident_id == first.incident_id
        # A retry of the resolved delivery is idempotent.
        assert (
            manager.ingest(
                normalize_alert(make_alert("resolved")), now=NOW.replace(minute=11)
            ).incident_id
            == first.incident_id
        )

        recurring_payload = make_alert().model_copy(update={"starts_at": later})
        second = manager.ingest(normalize_alert(recurring_payload), now=later)
        assert second.incident_id != first.incident_id
        assert (
            manager.ingest(normalize_alert(recurring_payload), now=later).incident_id
            == second.incident_id
        )
        second_resolved = recurring_payload.model_copy(
            update={"status": "resolved", "ends_at": later.replace(minute=25)}
        )
        assert (
            manager.ingest(
                normalize_alert(second_resolved), now=later.replace(minute=25)
            ).incident_id
            == second.incident_id
        )
        assert (
            manager.ingest(
                normalize_alert(second_resolved), now=later.replace(minute=26)
            ).incident_id
            == second.incident_id
        )

        rows = session.scalars(select(IncidentRow).order_by(IncidentRow.created_at)).all()
        assert len(rows) == 2
        assert [row.status for row in rows] == ["RESOLVED", "RESOLVED"]
        assert session.scalar(select(func.count(AlertRow.alert_id))) == 2
        timelines = session.scalars(
            select(IncidentEventRow).order_by(
                IncidentEventRow.incident_id, IncidentEventRow.sequence
            )
        ).all()
        assert len(timelines) == 4
        assert {item.incident_id for item in timelines} == {first.incident_id, second.incident_id}
    engine.dispose()


def test_concurrent_duplicate_occurrence_is_one_incident(tmp_path: Path) -> None:
    """Database uniqueness resolves the select-then-insert race."""
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    payload = make_alert()

    def ingest_once(_: int) -> str:
        with Session(engine) as session:
            incident = IncidentManager(session).ingest(normalize_alert(payload), now=NOW)
            return str(incident.incident_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        incident_ids = list(workers.map(ingest_once, range(2)))
    with Session(engine) as session:
        assert len(set(incident_ids)) == 1
        assert session.scalar(select(func.count(IncidentRow.incident_id))) == 1
        assert session.scalar(select(func.count(AlertRow.alert_id))) == 1
    engine.dispose()


def test_concurrent_distinct_occurrences_remain_distinct(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-recurrence.db'}", connect_args={"timeout": 10}
    )
    Base.metadata.create_all(engine)
    payloads = [make_alert(), make_alert().model_copy(update={"starts_at": NOW.replace(minute=20)})]

    def ingest_once(index: int) -> str:
        with Session(engine) as session:
            incident = IncidentManager(session).ingest(
                normalize_alert(payloads[index]), now=payloads[index].starts_at
            )
            return str(incident.incident_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        incident_ids = list(workers.map(ingest_once, range(2)))
    with Session(engine) as session:
        assert len(set(incident_ids)) == 2
        assert session.scalar(select(func.count(IncidentRow.incident_id))) == 2
        assert session.scalar(select(func.count(AlertRow.alert_id))) == 2
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


# ---- alert episodes (docs/architecture/incident-episode-contract.md) ---------------------------

QUIET = timedelta(seconds=30)


def _episode_db(tmp_path: Path) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'episodes.db'}")
    Base.metadata.create_all(engine)
    return engine


def _firing(start: datetime) -> AlertmanagerAlertPayload:
    return make_alert().model_copy(update={"starts_at": start})


def _resolved(start: datetime, end: datetime) -> AlertmanagerAlertPayload:
    return make_alert().model_copy(
        update={"starts_at": start, "status": "resolved", "ends_at": end}
    )


def _refire_after(tmp_path: Path, gap: timedelta, quiet: timedelta) -> tuple[Any, Any, Any]:
    engine = _episode_db(tmp_path)
    end = NOW + timedelta(seconds=60)
    session = Session(engine)
    manager = IncidentManager(session, quiet=quiet)
    first = manager.ingest(normalize_alert(_firing(NOW)), now=NOW)
    manager.ingest(normalize_alert(_resolved(NOW, end)), now=end)
    outcome = manager.ingest_occurrence(normalize_alert(_firing(end + gap)), now=end + gap)
    return session, first, outcome


def test_a_refiring_within_the_quiet_interval_reopens_the_same_incident(tmp_path: Path) -> None:
    session, first, outcome = _refire_after(tmp_path, timedelta(seconds=6), QUIET)
    assert outcome.incident.incident_id == first.incident_id and outcome.refired
    row = session.get(IncidentRow, first.incident_id)
    assert row is not None and row.status == "OPEN"
    assert session.scalar(select(func.count(IncidentRow.incident_id))) == 1
    assert session.scalar(select(func.count(AlertRow.alert_id))) == 2  # both occurrences are kept


def test_the_timeline_keeps_the_resolution_before_the_refiring(tmp_path: Path) -> None:
    session, first, _ = _refire_after(tmp_path, timedelta(seconds=6), QUIET)
    events = session.scalars(
        select(IncidentEventRow)
        .where(IncidentEventRow.incident_id == first.incident_id)
        .order_by(IncidentEventRow.sequence)
    ).all()
    kinds = [e.event_type for e in events]
    assert kinds.index(IncidentEventType.ALERT_RESOLVED) < kinds.index(
        IncidentEventType.ALERT_REFIRED
    )
    refired = next(e for e in events if e.event_type == IncidentEventType.ALERT_REFIRED)
    assert refired.payload["gap_seconds"] == 6.0 and refired.payload["status"] == "OPEN"


def test_at_the_quiet_interval_and_beyond_a_new_incident_opens(tmp_path: Path) -> None:
    for gap in (QUIET, QUIET + timedelta(minutes=10)):
        _, first, outcome = _refire_after(tmp_path / str(gap.seconds), gap, QUIET)
        assert outcome.incident.incident_id != first.incident_id and not outcome.refired


def test_the_mechanism_is_off_when_the_quiet_interval_is_zero(tmp_path: Path) -> None:
    _, first, outcome = _refire_after(tmp_path, timedelta(seconds=6), timedelta(0))
    assert outcome.incident.incident_id != first.incident_id and not outcome.refired
    # nor is an overlapping occurrence a continuation, nor one while the earlier still fires
    engine = _episode_db(tmp_path / "off")
    with Session(engine) as session:
        manager = IncidentManager(session)  # the default is the product default: off
        first = manager.ingest(normalize_alert(_firing(NOW)), now=NOW)
        later = NOW + timedelta(seconds=5)
        second = manager.ingest(normalize_alert(_firing(later)), now=later)
        assert second.incident_id != first.incident_id


def test_a_new_occurrence_while_the_earlier_still_fires_continues_it(tmp_path: Path) -> None:
    engine = _episode_db(tmp_path)
    with Session(engine) as session:
        manager = IncidentManager(session, quiet=QUIET)
        first = manager.ingest(normalize_alert(_firing(NOW)), now=NOW)
        later = NOW + timedelta(minutes=20)  # far beyond Q: the earlier occurrence never resolved
        outcome = manager.ingest_occurrence(normalize_alert(_firing(later)), now=later)
        assert outcome.incident.incident_id == first.incident_id and outcome.refired
        # the episode resolves only when neither occurrence still fires
        manager.ingest(normalize_alert(_resolved(NOW, later)), now=later)
        row = session.get(IncidentRow, first.incident_id)
        assert row is not None and row.status == "OPEN"
        end = later + timedelta(minutes=1)
        manager.ingest(normalize_alert(_resolved(later, end)), now=end)
        session.refresh(row)
        assert row.status == "RESOLVED"


def test_another_fingerprint_never_continues_an_episode(tmp_path: Path) -> None:
    engine = _episode_db(tmp_path)
    with Session(engine) as session:
        manager = IncidentManager(session, quiet=QUIET)
        first = manager.ingest(normalize_alert(_firing(NOW)), now=NOW)
        other = make_alert().model_copy(
            update={"labels": {**make_alert().labels, "alertname": "OtherAlert"}}
        )
        outcome = manager.ingest_occurrence(normalize_alert(other), now=NOW)
        assert outcome.incident.incident_id != first.incident_id and not outcome.refired
