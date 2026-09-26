"""M19-5.4: the evidence requirements table (0024) and its one-OPEN-per-key contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

import packages.storage.evidence_guard  # noqa: F401 - registers the flush guard
from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.storage.evidence_guard import AUTHORITATIVE_ROWS, TRANSITIONAL_MUTABLE_ROWS
from packages.storage.models import (
    EVIDENCE_REQUIREMENT_KINDS,
    EVIDENCE_REQUIREMENT_STATUSES,
    EvidenceRequirementRow,
    UTCDateTime,
)
from packages.storage.repositories import IncidentRepository

PREVIOUS = "0023_diagnosis_revision_required"
TARGET = "0024_evidence_requirements"
TABLE = "evidence_requirements"
AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
NOT_BEFORE = AT + timedelta(minutes=15)
# Deterministic fixture keys: 5.4 defines no key formula (owner amendment).
KEY_X = "a" * 64
KEY_Y = "b" * 64
CLOSED = tuple(status for status in EVIDENCE_REQUIREMENT_STATUSES if status != "OPEN")
COLUMNS: dict[str, tuple[Any, bool]] = {
    "requirement_id": (sa.Integer, False),
    "requirement_key": (sa.String, False),
    "incident_id": ((sa.Uuid, sa.CHAR), False),  # SQLite reflects UUID as CHAR(32)
    "diagnosis_id": (sa.Integer, False),
    "hypothesis_key": (sa.String, False),
    "rule_id": (sa.String, False),
    "rule_version": (sa.String, False),
    "kind": (sa.String, False),
    "targets": (sa.JSON, False),
    "not_before": (sa.DateTime, False),
    "status": (sa.String, False),
}

requirements = sa.table(
    TABLE,
    sa.column("requirement_id", sa.Integer()),
    sa.column("requirement_key", sa.String()),
    sa.column("incident_id", sa.Uuid()),
    sa.column("diagnosis_id", sa.Integer()),
    sa.column("hypothesis_key", sa.String()),
    sa.column("rule_id", sa.String()),
    sa.column("rule_version", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("targets", sa.JSON()),
    sa.column("not_before", UTCDateTime()),
    sa.column("status", sa.String()),
)
diagnoses = sa.table(
    "diagnoses",
    sa.column("diagnosis_id", sa.Integer()),
    sa.column("incident_id", sa.Uuid()),
    sa.column("created_at", UTCDateTime()),
    sa.column("confidence", sa.String()),
    sa.column("mode", sa.String()),
    sa.column("document", sa.JSON()),
    sa.column("revision_number", sa.Integer()),
    sa.column("trigger", sa.String()),
)


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _schema(engine: Engine) -> dict[str, Any]:
    """Everything that defines the table, normalized for comparison."""
    inspector = inspect(engine)
    return {
        "columns": {
            column["name"]: (type(column["type"]).__name__, column["nullable"])
            for column in inspector.get_columns(TABLE)
        },
        "pk": inspector.get_pk_constraint(TABLE)["constrained_columns"],
        "fks": sorted(
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                (fk.get("options") or {}).get("ondelete"),
            )
            for fk in inspector.get_foreign_keys(TABLE)
        ),
        "checks": sorted(
            (item["name"], " ".join(item["sqltext"].split()))
            for item in inspector.get_check_constraints(TABLE)
        ),
        "indexes": sorted(
            (
                item["name"],
                tuple(item["column_names"]),
                bool(item["unique"]),
                " ".join(
                    str(
                        (item.get("dialect_options") or {}).get(f"{engine.dialect.name}_where", "")
                    ).split()
                ),
            )
            for item in inspector.get_indexes(TABLE)
        ),
        "uniques": inspector.get_unique_constraints(TABLE),
    }


def _drop_table(engine: Engine) -> None:
    """Revision 0001 builds tables from live metadata; remove what 0024 adds."""
    if TABLE in _tables(engine):
        with engine.begin() as connection:
            connection.execute(sa.text(f"DROP TABLE {TABLE}"))


def _incident(engine: Engine) -> UUID:
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="latency",
        created_at=AT,
        updated_at=AT,
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident)
        session.commit()
    return incident.incident_id


def _diagnosis(engine: Engine, incident_id: UUID, number: int = 1) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            diagnoses.insert()
            .values(
                incident_id=incident_id,
                created_at=AT,
                confidence="LIKELY",
                mode="deterministic",
                document={},
                revision_number=number,
                trigger="INITIAL",
            )
            .returning(diagnoses.c.diagnosis_id)
        )
        return int(result.scalar_one())


def _row(incident_id: UUID, diagnosis_id: int, **values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "requirement_key": KEY_X,
        "incident_id": incident_id,
        "diagnosis_id": diagnosis_id,
        "hypothesis_key": "hkey:0123456789abcdef",
        "rule_id": "m16.ended-manifestation-episode",
        "rule_version": "v1",
        "kind": "STATUS_CONTINUITY",
        "targets": ["shop/Pod/worker-0"],
        "not_before": NOT_BEFORE,
        "status": "OPEN",
    }
    row.update(values)
    return row


def _insert(engine: Engine, row: dict[str, Any]) -> bool:
    """Insert through Core so only the database decides; False when it refuses."""
    try:
        with engine.begin() as connection:
            connection.execute(requirements.insert().values(**row))
    except IntegrityError:
        return False
    return True


def _statuses(engine: Engine, key: str) -> list[str]:
    with engine.connect() as connection:
        return sorted(
            connection.execute(
                select(requirements.c.status).where(requirements.c.requirement_key == key)
            ).scalars()
        )


def _clear(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(requirements.delete())


def _assert_table(engine: Engine) -> None:
    columns = {column["name"]: column for column in inspect(engine).get_columns(TABLE)}
    assert set(columns) == set(COLUMNS)
    for name, (type_, nullable) in COLUMNS.items():
        assert isinstance(columns[name]["type"], type_), name
        assert columns[name]["nullable"] is nullable, name
        if name != "requirement_id":  # the serial PK is the only generated value
            assert columns[name].get("default") is None, name  # status has no DB default
    key_type = columns["requirement_key"]["type"]
    assert isinstance(key_type, sa.String) and key_type.length == 64
    schema = _schema(engine)
    assert schema["pk"] == ["requirement_id"]
    assert schema["uniques"] == []  # never a global UNIQUE(requirement_key)
    assert {name for name, *_ in schema["indexes"]} == {
        "ix_evidence_requirements_incident_id",
        "uq_evidence_requirements_open_key",
    }
    (open_key,) = [item for item in schema["indexes"] if item[0].startswith("uq_")]
    assert open_key[1:3] == (("requirement_key",), True)
    assert "OPEN" in open_key[3] and "status" in open_key[3]
    assert {name for name, _ in schema["checks"]} == {
        "ck_evidence_requirement_kind",
        "ck_evidence_requirement_status",
        "ck_evidence_requirement_targets_array",
    }


def _assert_constraints(engine: Engine) -> None:
    incident = _incident(engine)
    first, second = _diagnosis(engine, incident, 1), _diagnosis(engine, incident, 2)

    # Every value of both vocabularies is storable; nothing else is.
    for kind in EVIDENCE_REQUIREMENT_KINDS:
        for status in EVIDENCE_REQUIREMENT_STATUSES:
            key = f"{kind[:1]}{status[:2]}".ljust(64, "0")
            assert _insert(
                engine, _row(incident, first, requirement_key=key, kind=kind, status=status)
            )
    _clear(engine)
    for bad in ({"kind": "BOGUS"}, {"kind": "OTHER"}, {"status": "BOGUS"}, {"status": "open"}):
        assert not _insert(engine, _row(incident, first, **bad)), bad
    # targets is physically a JSON array.
    for bad_targets in ({"pod": "x"}, "shop/Pod/worker-0", 3):
        assert not _insert(engine, _row(incident, first, targets=bad_targets)), bad_targets
    for name in COLUMNS:
        if name != "requirement_id":
            assert not _insert(engine, {**_row(incident, first), name: None}), name
    assert _statuses(engine, KEY_X) == []

    # One OPEN per requirement_key.
    assert _insert(engine, _row(incident, first))  # X OPEN
    assert not _insert(engine, _row(incident, second))  # second X OPEN, even from a new revision
    assert _insert(
        engine,
        _row(
            incident,
            first,
            requirement_key=KEY_Y,
            kind="RESOURCE_COVERAGE",
            rule_id="m16.resource-pressure",
            targets=["shop/Deployment/payment", "shop/Pod/payment-a"],
        ),
    )  # Y OPEN
    # Closed history for X coexists with the OPEN row, repeated statuses included:
    # the index is neither UNIQUE(requirement_key) nor UNIQUE(requirement_key, status).
    for status in (*CLOSED, *CLOSED):
        assert _insert(engine, _row(incident, second, status=status)), status
    assert _statuses(engine, KEY_X) == sorted(["OPEN", *CLOSED, *CLOSED])
    assert _statuses(engine, KEY_Y) == ["OPEN"]

    # Status is mutable scheduler state: once X is closed, a new OPEN X is storable.
    with engine.begin() as connection:
        connection.execute(
            requirements.update()
            .where(requirements.c.requirement_key == KEY_X, requirements.c.status == "OPEN")
            .values(status="SUPERSEDED_BY_REVISION")
        )
    assert _insert(engine, _row(incident, second))
    assert not _insert(engine, _row(incident, second))
    assert _statuses(engine, KEY_X).count("OPEN") == 1
    _clear(engine)


def _run(url: str) -> dict[str, Any]:
    config = _config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, PREVIOUS)
        _drop_table(engine)
        assert TABLE not in _tables(engine)

        command.upgrade(config, TARGET)
        # Behaviour first, so a weakened constraint fails on the rejected insert.
        _assert_constraints(engine)
        _assert_table(engine)
        schema = _schema(engine)

        command.downgrade(config, PREVIOUS)
        assert TABLE not in _tables(engine)
        assert "diagnoses" in _tables(engine)

        command.upgrade(config, TARGET)
        assert _schema(engine) == schema
        _assert_constraints(engine)
        return schema
    finally:
        engine.dispose()


def _fresh(url: str) -> dict[str, Any]:
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    try:
        _assert_constraints(engine)
        _assert_table(engine)
        return _schema(engine)
    finally:
        engine.dispose()


def test_evidence_requirements_migration_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    migrated = _run(f"sqlite:///{tmp_path / 'requirements.db'}")
    assert _fresh(f"sqlite:///{tmp_path / 'fresh.db'}") == migrated


@pytest.mark.postgres
def test_evidence_requirements_migration_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    migrated = _run(postgres_url)
    # Fresh head on a second database of the same server has the same schema.
    engine = create_engine(postgres_url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("DROP SCHEMA public CASCADE"))
            connection.execute(sa.text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    assert _fresh(postgres_url) == migrated


@pytest.mark.postgres
def test_postgres_enforces_requirement_foreign_keys(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_config(postgres_url), "head")
    engine = create_engine(postgres_url)
    try:
        incident = _incident(engine)
        diagnosis = _diagnosis(engine, incident)
        other = _incident(engine)
        assert not _insert(engine, _row(incident, diagnosis + 1000))  # unknown diagnosis
        assert not _insert(
            engine, _row(UUID(int=1), diagnosis, requirement_key=KEY_Y)
        )  # unknown incident
        assert _insert(engine, _row(incident, diagnosis))
        assert _insert(engine, _row(other, _diagnosis(engine, other), requirement_key=KEY_Y))
        # Requirement history goes only with its incident (like its diagnoses).
        with engine.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM incidents WHERE incident_id = :i"), {"i": incident}
            )
        assert _statuses(engine, KEY_X) == []
        assert _statuses(engine, KEY_Y) == ["OPEN"]
    finally:
        engine.dispose()


def test_requirements_are_mutable_scheduler_state_not_guarded_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert not issubclass(EvidenceRequirementRow, AUTHORITATIVE_ROWS)
    assert EvidenceRequirementRow not in TRANSITIONAL_MUTABLE_ROWS
    url = f"sqlite:///{tmp_path / 'orm.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    try:
        incident = _incident(engine)
        diagnosis = _diagnosis(engine, incident)
        targets = [{"pod": "shop/Pod/worker-0", "uid": "u1"}, "shop/Pod/worker-1"]
        with Session(engine) as session:
            session.add(EvidenceRequirementRow(**_row(incident, diagnosis, targets=targets)))
            session.commit()
        with Session(engine) as session:
            row = session.scalars(select(EvidenceRequirementRow)).one()
            # Exact round trip: aware timestamp, ordered targets, explicit status.
            assert row.not_before == NOT_BEFORE
            assert row.not_before.tzinfo is not None
            assert row.targets == targets
            assert row.status == "OPEN"
            # A future lifecycle can move status through an ORM flush.
            row.status = "SATISFIED_BY_REVISION"
            session.commit()
        with Session(engine) as session:
            assert session.scalars(select(EvidenceRequirementRow.status)).one() == (
                "SATISFIED_BY_REVISION"
            )
            with pytest.raises(StatementError, match="timezone-aware"):
                session.add(
                    EvidenceRequirementRow(
                        **_row(incident, diagnosis, not_before=NOT_BEFORE.replace(tzinfo=None))
                    )
                )
                session.flush()
    finally:
        engine.dispose()
