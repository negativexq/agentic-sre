"""Persistence integration tests using SQLite as a local test double."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy import inspect as inspect_database
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.incident import TransitionResult, transition
from packages.storage.models import Base
from packages.storage.repositories import IncidentEventRepository, IncidentRepository

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session
    engine.dispose()


def make_incident() -> Incident:
    return Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.SYSTEM,
        title="Persistence test incident",
        created_at=NOW,
        updated_at=NOW,
    )


def test_incident_and_timeline_survive_session_restart(session: Session) -> None:
    repository = IncidentRepository(session)
    incident = make_incident()
    repository.create(incident)
    result = transition(
        incident,
        IncidentStatus.TRIAGING,
        "begin triage",
        timestamp=NOW + timedelta(minutes=1),
    )
    repository.save_transition(result)

    session.expunge_all()
    reloaded = repository.get(incident.incident_id)
    events = IncidentEventRepository(session).list_for_incident(incident.incident_id)

    assert reloaded == result.incident
    assert [event.event_type.value for event in events] == ["INCIDENT_CREATED", "STATE_CHANGED"]
    assert [event.payload.get("reason") for event in events] == [None, "begin triage"]


def test_duplicate_event_rolls_back_state_update(session: Session) -> None:
    repository = IncidentRepository(session)
    incident = make_incident()
    repository.create(incident)
    first_transition = transition(
        incident,
        IncidentStatus.TRIAGING,
        "begin triage",
        timestamp=NOW + timedelta(minutes=1),
    )
    repository.save_transition(first_transition)
    second_transition = transition(
        first_transition.incident,
        IncidentStatus.INVESTIGATING,
        "start investigation",
        timestamp=NOW + timedelta(minutes=2),
    )
    duplicate_event = second_transition.event.model_copy(
        update={"event_id": first_transition.event.event_id}
    )
    invalid_result = TransitionResult(incident=second_transition.incident, event=duplicate_event)

    with pytest.raises(IntegrityError):
        repository.save_transition(invalid_result)

    session.rollback()
    assert repository.get(incident.incident_id) == first_transition.incident


def test_duplicate_incident_is_rejected(session: Session) -> None:
    repository = IncidentRepository(session)
    incident = make_incident()
    repository.create(incident)

    with pytest.raises(IntegrityError):
        repository.create(incident)


def test_alembic_migration_up_and_down(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path}")
    expected_tables = {
        "action_executions",
        "alerts",
        "evidence",
        "hypotheses",
        "incident_events",
        "incidents",
        "policy_decisions",
        "remediation_proposals",
        "tool_calls",
        "verification_results",
    }
    assert set(inspect_database(engine).get_table_names()) == expected_tables | {"alembic_version"}
    engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(f"sqlite:///{database_path}")
    assert inspect_database(engine).get_table_names() == ["alembic_version"]
    engine.dispose()
