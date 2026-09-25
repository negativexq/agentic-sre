"""M19-4.1: diagnosis revision metadata (0022) and legacy numbering, on SQLite and PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.storage.models import UTCDateTime
from packages.storage.repositories import DiagnosisRepository, IncidentRepository

PREVIOUS = "0021_query_key_text"
TARGET = "0022_diagnosis_revisions"
REVISION_COLUMNS = (
    "revision_number",
    "previous_diagnosis_id",
    "trigger",
    "window_end",
    "manifest_digest",
    "tape_digest",
    "epistemic_digest",
    "engine_version",
    "config_digest",
)
# Fields the plan gives no legacy source for: never fabricated.
UNBACKFILLED = tuple(
    name for name in REVISION_COLUMNS if name not in {"revision_number", "trigger"}
)
LEGACY_COLUMNS = (
    "diagnosis_id",
    "incident_id",
    "created_at",
    "root_cause",
    "confidence",
    "mode",
    "run_id",
    "document",
)
AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

TYPES: dict[str, Any] = {
    "diagnosis_id": sa.Integer(),
    "incident_id": sa.Uuid(),
    "created_at": UTCDateTime(),
    "root_cause": sa.String(),
    "confidence": sa.String(),
    "mode": sa.String(),
    "run_id": sa.String(),
    "document": sa.JSON(),
    "revision_number": sa.Integer(),
    "previous_diagnosis_id": sa.Integer(),
    "trigger": sa.String(),
    "window_end": UTCDateTime(),
    "manifest_digest": sa.String(),
    "tape_digest": sa.String(),
    "epistemic_digest": sa.String(),
    "engine_version": sa.String(),
    "config_digest": sa.String(),
}


def _table(names: tuple[str, ...]) -> sa.TableClause:
    return sa.table("diagnoses", *(sa.column(name, TYPES[name]) for name in names))


legacy = _table(LEGACY_COLUMNS)


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("diagnoses")}


def _to_pre_0022(engine: Engine) -> None:
    """Revision 0001 builds tables from live metadata; remove what 0022 adds."""
    inspector = inspect(engine)
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("diagnoses") as batch:
            if any(
                fk["name"] == "fk_diagnoses_previous_diagnosis_id"
                for fk in inspector.get_foreign_keys("diagnoses")
            ):
                batch.drop_constraint("fk_diagnoses_previous_diagnosis_id", type_="foreignkey")
            if any(
                c["name"] == "ck_diagnosis_trigger"
                for c in inspector.get_check_constraints("diagnoses")
            ):
                batch.drop_constraint("ck_diagnosis_trigger", type_="check")
            if any(
                u["name"] == "uq_diagnosis_incident_revision"
                for u in inspector.get_unique_constraints("diagnoses")
            ):
                batch.drop_constraint("uq_diagnosis_incident_revision", type_="unique")
            for name in reversed(REVISION_COLUMNS):
                if name in _columns(engine):
                    batch.drop_column(name)


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


def _seed(engine: Engine) -> tuple[UUID, UUID, list[int]]:
    """Two incidents with interleaved diagnoses, one equal-created_at tie inside A."""
    first, second = _incident(engine), _incident(engine)
    plan = [  # (incident, minutes); insertion order is not the numbering order
        (first, 30),
        (second, 5),
        (first, 10),
        (second, 20),
        (first, 10),  # same created_at as the previous first-incident row
        (first, 50),
    ]
    ids: list[int] = []
    with engine.begin() as connection:
        for index, (incident_id, minutes) in enumerate(plan):
            result = connection.execute(
                legacy.insert()
                .values(
                    incident_id=incident_id,
                    created_at=AT + timedelta(minutes=minutes),
                    root_cause=f"shop/Deployment/web-{index}",
                    confidence="LIKELY",
                    mode="deterministic",
                    run_id=f"run-{index}",
                    document={"index": index, "summary": f"diagnosis {index}"},
                )
                .returning(legacy.c.diagnosis_id)
            )
            ids.append(int(result.scalar_one()))
    return first, second, ids


def _rows(engine: Engine, names: tuple[str, ...]) -> dict[int, tuple[Any, ...]]:
    table = _table(names)
    with engine.connect() as connection:
        return {
            row[0]: tuple(row[1:])
            for row in connection.execute(sa.select(*(table.c[name] for name in names)))
        }


def _insert(engine: Engine, incident_id: UUID, **values: Any) -> bool:
    table = _table((*LEGACY_COLUMNS, *REVISION_COLUMNS))
    try:
        with engine.begin() as connection:
            connection.execute(
                table.insert().values(
                    incident_id=incident_id,
                    created_at=AT,
                    confidence="LIKELY",
                    mode="deterministic",
                    document={},
                    **values,
                )
            )
    except IntegrityError:
        return False
    return True


def _run(url: str) -> None:
    config = _config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, PREVIOUS)
        _to_pre_0022(engine)
        assert not _columns(engine) & set(REVISION_COLUMNS)
        first, second, ids = _seed(engine)
        before = _rows(engine, LEGACY_COLUMNS)

        command.upgrade(config, TARGET)

        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("diagnoses")}
        assert set(REVISION_COLUMNS) <= set(columns)
        assert all(columns[name]["nullable"] for name in REVISION_COLUMNS)
        assert {
            (item["name"], tuple(item["column_names"]))
            for item in inspector.get_unique_constraints("diagnoses")
        } == {("uq_diagnosis_incident_revision", ("incident_id", "revision_number"))}
        assert any(
            c["name"] == "ck_diagnosis_trigger"
            for c in inspector.get_check_constraints("diagnoses")
        )
        assert any(
            fk["name"] == "fk_diagnoses_previous_diagnosis_id"
            and fk["referred_table"] == "diagnoses"
            and fk["constrained_columns"] == ["previous_diagnosis_id"]
            and fk["referred_columns"] == ["diagnosis_id"]
            for fk in inspector.get_foreign_keys("diagnoses")
        )

        # Every row kept, every legacy field unchanged.
        assert _rows(engine, LEGACY_COLUMNS) == before
        revisions = _rows(engine, ("diagnosis_id", "incident_id", "revision_number", "trigger"))
        # A: minutes 10 (ids[2]), 10 (ids[4], tie broken by diagnosis_id), 30, 50. B: 5, 20.
        assert {diagnosis_id: row[1] for diagnosis_id, row in revisions.items()} == {
            ids[2]: 1,
            ids[4]: 2,
            ids[0]: 3,
            ids[5]: 4,
            ids[1]: 1,
            ids[3]: 2,
        }
        assert {row[2] for row in revisions.values()} == {"LEGACY"}
        assert {row[0] for row in revisions.values()} == {first, second}
        unbackfilled = _rows(engine, ("diagnosis_id", *UNBACKFILLED))
        assert all(value is None for row in unbackfilled.values() for value in row)

        # The revision identity is incident-scoped.
        assert not _insert(engine, first, revision_number=1, trigger="MANUAL")
        third = _incident(engine)
        assert _insert(engine, third, revision_number=1, trigger="INITIAL")
        assert not _insert(engine, third, revision_number=2, trigger="SOMETHING_ELSE")
        # No permanent default: the pre-4.2 writer still stores rows, unnumbered.
        with Session(engine) as session:
            DiagnosisRepository(session).save(third, {"confidence": "LIKELY", "mode": "x"}, AT)
            DiagnosisRepository(session).save(third, {"confidence": "LIKELY", "mode": "y"}, AT)
        unnumbered = _rows(engine, ("diagnosis_id", "incident_id", "revision_number", "trigger"))
        assert sum(1 for row in unnumbered.values() if row[0] == third and row[1] is None) == 2
        assert all(
            row[2] is None for row in unnumbered.values() if row[0] == third and row[1] is None
        )

        with engine.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM diagnoses WHERE incident_id = :i").bindparams(i=third)
            )
        command.downgrade(config, PREVIOUS)
        assert not _columns(engine) & set(REVISION_COLUMNS)
        assert not inspect(engine).get_unique_constraints("diagnoses")
        assert _rows(engine, LEGACY_COLUMNS) == before

        command.upgrade(config, TARGET)  # numbering is deterministic across a round trip
        assert (
            _rows(engine, ("diagnosis_id", "incident_id", "revision_number", "trigger"))
            == revisions
        )
    finally:
        engine.dispose()


def test_diagnosis_revision_migration_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run(f"sqlite:///{tmp_path / 'revisions.db'}")


@pytest.mark.postgres
def test_diagnosis_revision_migration_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run(postgres_url)


def test_fresh_database_reaches_the_same_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    try:
        assert set(REVISION_COLUMNS) <= _columns(engine)
        assert {item["name"] for item in inspect(engine).get_unique_constraints("diagnoses")} == {
            "uq_diagnosis_incident_revision"
        }
        incident = _incident(engine)
        assert _insert(engine, incident, revision_number=1, trigger="INITIAL")
        assert not _insert(engine, incident, revision_number=1, trigger="MANUAL")
        other = _incident(engine)
        assert _insert(engine, other, revision_number=1, trigger="EVIDENCE_DEADLINE")
    finally:
        engine.dispose()
