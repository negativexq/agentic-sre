"""M19-4.2: numbered, immutable diagnosis revisions and their provenance."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from test_diagnosis_revision_migration import (
    LEGACY_COLUMNS,
    PREVIOUS,
    TARGET,
    _config,
    _incident,
    _insert,
    _rows,
    _seed,
    _table,
    _to_pre_0022,
)
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

import apps.control_plane.diagnosis as diagnosis_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.engine import EngineConfig
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.state import InvestigationConfig, rca_config_digest
from packages.storage.database import create_session_factory
from packages.storage.manifest import load_manifest_digest
from packages.storage.models import DiagnosisRow, IncidentEventRow
from packages.storage.repositories import (
    DiagnosisRepository,
    DiagnosisRevision,
    IncidentNotFoundError,
)
from packages.storage.tape import load_tape_digest

REQUIRED_TARGET = "0023_diagnosis_revision_required"
METADATA = (
    "window_end",
    "manifest_digest",
    "tape_digest",
    "epistemic_digest",
    "engine_version",
    "config_digest",
)


def _service(world: Any, **kwargs: Any) -> DiagnosisService:
    factory, cluster, clock, _ = world
    return DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock, **kwargs
    )


def _revisions(factory: sessionmaker[Session], incident_id: UUID) -> list[DiagnosisRow]:
    with factory() as session:
        return list(
            session.scalars(
                select(DiagnosisRow)
                .where(DiagnosisRow.incident_id == incident_id)
                .order_by(DiagnosisRow.revision_number)
            )
        )


def _window_end(factory: sessionmaker[Session], run_id: str) -> datetime:
    with factory() as session:
        payload = next(
            row.payload
            for row in session.scalars(
                select(IncidentEventRow).where(IncidentEventRow.event_type == "EVIDENCE_GATHERED")
            )
            if row.payload.get("run_id") == run_id
        )
    return datetime.fromisoformat(payload["window_end"])


def test_each_run_appends_the_next_revision_with_source_derived_provenance(
    setup: Any,  # noqa: F811
) -> None:
    factory, _, clock, incident_id = setup
    clock.now = T0 + timedelta(minutes=30)
    first = _service(setup).run(incident_id, "INITIAL")
    clock.now = T0 + timedelta(minutes=31)
    second = _service(
        setup, bounded_policy_factory=lambda: ScriptedInvestigationPolicy(actions=[])
    ).run(incident_id, "MANUAL")

    rows = _revisions(factory, incident_id)
    assert [(row.revision_number, row.trigger) for row in rows] == [(1, "INITIAL"), (2, "MANUAL")]
    assert rows[0].previous_diagnosis_id is None
    assert rows[1].previous_diagnosis_id == rows[0].diagnosis_id
    engine = EngineConfig()
    expected_config = [
        rca_config_digest(engine, None),
        rca_config_digest(engine, InvestigationConfig(engine=engine)),
    ]
    for row, diagnosis, config_digest in zip(rows, (first, second), expected_config, strict=True):
        assert all(getattr(row, name) is not None for name in METADATA)
        assert row.run_id is not None
        with factory() as session:
            assert row.manifest_digest == load_manifest_digest(session, row.run_id)
            assert row.tape_digest == load_tape_digest(session, row.run_id)
        assert row.window_end == _window_end(factory, row.run_id)
        assert row.epistemic_digest == diagnosis_epistemic_digest(diagnosis)
        assert row.engine_version == package_version("agentic-sre")
        assert row.config_digest == config_digest
    assert expected_config[0] != expected_config[1]


def test_run_requires_an_explicit_revision_trigger(setup: Any) -> None:  # noqa: F811
    factory, _, _, incident_id = setup
    service = _service(setup)
    with pytest.raises(TypeError):
        service.run(incident_id)  # type: ignore[call-arg]
    for trigger in ("LEGACY", "manual", "SOMETHING"):
        with pytest.raises(ValueError, match="not a revision trigger"):
            service.run(incident_id, trigger)
    assert _revisions(factory, incident_id) == []


def test_missing_package_metadata_fails_without_writing_a_revision(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _, _, incident_id = setup

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(diagnosis_module, "package_version", missing)
    with pytest.raises(PackageNotFoundError):
        _service(setup).run(incident_id, "MANUAL")
    assert _revisions(factory, incident_id) == []


def _write(session: Session, incident_id: UUID, **overrides: Any) -> DiagnosisRevision:
    values: dict[str, Any] = {
        "incident_id": incident_id,
        "document": {"confidence": "LIKELY", "mode": "deterministic"},
        "created_at": T0,
        "run_id": None,
        "trigger": "MANUAL",
        "window_end": T0,
        "manifest_digest": "a" * 64,
        "tape_digest": "b" * 64,
        "epistemic_digest": "c" * 64,
        "engine_version": "test-engine",
        "config_digest": "d" * 64,
    }
    return DiagnosisRepository(session).save_revision(**(values | overrides))


def test_the_writer_follows_revision_numbers_not_timestamps(setup: Any) -> None:  # noqa: F811
    factory, _, _, incident_id = setup
    with factory() as session:
        first = _write(session, incident_id, created_at=T0 + timedelta(minutes=5))
        # An older timestamp does not make a revision "earlier".
        second = _write(session, incident_id, created_at=T0)
        third = _write(session, incident_id, created_at=T0 + timedelta(minutes=1))
    rows = _revisions(factory, incident_id)
    assert [row.revision_number for row in rows] == [1, 2, 3]
    assert [row.previous_diagnosis_id for row in rows] == [
        None,
        first.diagnosis_id,
        second.diagnosis_id,
    ]
    assert third.revision_number == 3


def test_the_writer_rejects_legacy_and_unknown_incidents_and_has_no_update(
    setup: Any,  # noqa: F811
) -> None:
    factory, _, _, incident_id = setup
    with factory() as session:
        with pytest.raises(ValueError, match="not a revision trigger"):
            _write(session, incident_id, trigger="LEGACY")
        with pytest.raises(IncidentNotFoundError):
            _write(session, UUID(int=1))
    public = {name for name in dir(DiagnosisRepository) if not name.startswith("_")}
    assert not {name for name in public if name.startswith(("update", "set", "delete"))}
    assert _revisions(factory, incident_id) == []


def _racing_writer(
    factory: sessionmaker[Session], incident_id: UUID, times: int
) -> Callable[..., None]:
    """Commit a competing revision from another session, ``times`` times.

    Registered on the writer session's ``before_flush``, so it runs just before
    each insert attempt flushes.
    """
    remaining = [times]

    def before_flush(*_: Any) -> None:
        if remaining[0] == 0:
            return
        remaining[0] -= 1
        with factory() as other:
            highest = other.scalar(
                select(sa.func.max(DiagnosisRow.revision_number)).where(
                    DiagnosisRow.incident_id == incident_id
                )
            )
            other.add(
                DiagnosisRow(
                    incident_id=incident_id,
                    created_at=T0,
                    confidence="LIKELY",
                    mode="race",
                    document={},
                    revision_number=(highest or 0) + 1,
                    trigger="MANUAL",
                )
            )
            other.commit()

    return before_flush


def test_a_lost_number_race_is_retried_at_most_three_times(setup: Any) -> None:  # noqa: F811
    factory, _, _, incident_id = setup
    with factory() as session:
        event.listen(session, "before_flush", _racing_writer(factory, incident_id, 3))
        revision = _write(session, incident_id)
    assert revision.revision_number == 4  # three competitors won 1, 2, 3
    rows = _revisions(factory, incident_id)
    assert [row.revision_number for row in rows] == [1, 2, 3, 4]
    assert rows[-1].previous_diagnosis_id == rows[-2].diagnosis_id

    other = _incident(factory().get_bind().engine)
    with factory() as session, pytest.raises(IntegrityError):
        event.listen(session, "before_flush", _racing_writer(factory, other, 4))
        _write(session, other)
    assert [row.mode for row in _revisions(factory, other)] == ["race"] * 4


def _gap_insert(engine: Engine, incident_id: UUID, created_at: datetime) -> None:
    """A pre-revision-writer row: revision_number and trigger omitted."""
    with engine.begin() as connection:
        connection.execute(
            _table(LEGACY_COLUMNS)
            .insert()
            .values(
                incident_id=incident_id,
                created_at=created_at,
                confidence="LIKELY",
                mode="deterministic",
                document={},
            )
        )


def _gap_database(url: str) -> tuple[Engine, UUID, UUID, list[int]]:
    command.upgrade(_config(url), PREVIOUS)
    engine = create_engine(url)
    _to_pre_0022(engine)
    first, second, ids = _seed(engine)
    command.upgrade(_config(url), TARGET)
    # Rows the pre-revision writer stored after 0022: no number, no trigger.
    for incident_id, minutes in ((first, 1), (second, 90), (first, 60), (first, 60)):
        _gap_insert(engine, incident_id, T0 + timedelta(minutes=minutes))
    return engine, first, second, ids


def _run_required(url: str) -> None:
    engine, first, second, ids = _gap_database(url)
    try:
        before = _rows(engine, ("diagnosis_id", "incident_id", "revision_number", "trigger"))
        numbered = {key: row for key, row in before.items() if row[1] is not None}
        gap = sorted(key for key, row in before.items() if row[1] is None)
        assert len(gap) == 4

        command.upgrade(_config(url), REQUIRED_TARGET)

        after = _rows(
            engine,
            ("diagnosis_id", "incident_id", "revision_number", "trigger", "previous_diagnosis_id"),
        )
        # Numbered revisions are never renumbered.
        assert {key: after[key][:3] for key in numbered} == numbered
        # Gap rows follow each incident's highest revision, by (created_at, diagnosis_id).
        assert {key: (after[key][1], after[key][2]) for key in gap} == {
            gap[0]: (5, "LEGACY"),  # first @1min
            gap[1]: (3, "LEGACY"),  # second @90min
            gap[2]: (6, "LEGACY"),  # first @60min
            gap[3]: (7, "LEGACY"),  # first @60min, later id
        }
        assert all(after[key][3] is None for key in gap)  # no fabricated chain
        columns = {c["name"]: c for c in inspect(engine).get_columns("diagnoses")}
        assert not columns["revision_number"]["nullable"] and not columns["trigger"]["nullable"]
        assert not _insert(engine, first)  # NULL number/trigger is no longer a revision
        assert not _insert(engine, first, revision_number=99)
        assert not _insert(engine, first, trigger="MANUAL")
        assert _insert(engine, first, revision_number=99, trigger="MANUAL")

        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM diagnoses WHERE revision_number = 99"))
        command.downgrade(_config(url), TARGET)
        columns = {c["name"]: c for c in inspect(engine).get_columns("diagnoses")}
        assert columns["revision_number"]["nullable"] and columns["trigger"]["nullable"]
        assert (
            _rows(
                engine,
                (
                    "diagnosis_id",
                    "incident_id",
                    "revision_number",
                    "trigger",
                    "previous_diagnosis_id",
                ),
            )
            == after
        )
        command.upgrade(_config(url), "head")
    finally:
        engine.dispose()


def test_revision_required_migration_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run_required(f"sqlite:///{tmp_path / 'required.db'}")


@pytest.mark.postgres
def test_revision_required_migration_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _run_required(postgres_url)


@pytest.mark.postgres
def test_ten_concurrent_revisions_are_numbered_one_to_ten(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_config(postgres_url), "head")
    engine = create_engine(postgres_url, pool_size=12)
    factory = create_session_factory(engine)
    try:
        incident_id = _incident(engine)
        barrier = threading.Barrier(10)
        written: list[DiagnosisRevision] = []
        errors: list[BaseException] = []

        def request() -> None:
            try:
                with factory() as session:
                    barrier.wait()
                    written.append(_write(session, incident_id, created_at=datetime.now(UTC)))
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)

        threads = [threading.Thread(target=request) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        rows = _revisions(factory, incident_id)
        assert sorted(item.revision_number for item in written) == list(range(1, 11))
        assert [row.revision_number for row in rows] == list(range(1, 11))  # duplicates: 0
        assert [row.previous_diagnosis_id for row in rows] == [
            None,
            *(row.diagnosis_id for row in rows[:-1]),
        ]
    finally:
        engine.dispose()
