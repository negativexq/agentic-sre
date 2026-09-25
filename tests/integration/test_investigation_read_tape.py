"""M19-3.9: PostgreSQL commit visibility and concurrent run-local ordering."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.rca.live import capture_error_logs
from packages.rca.model import EntityRef, InvestigationQuery, LogRecord, ResourcePressure
from packages.rca.provider_adapter import ProviderAdapter, ProviderReaders
from packages.storage.models import Base, InvestigationReadRow, LogObservationRow
from packages.storage.repositories import (
    IncidentRepository,
    InvestigationReadRepository,
    LogObservationRepository,
)

pytestmark = pytest.mark.postgres
AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
TARGET = EntityRef(namespace="shop", kind="Pod", name="worker")
QUERY = InvestigationQuery(start=AT, end=AT, limit=2)


class _Prometheus:
    def __init__(self, result: tuple[ResourcePressure, ...]) -> None:
        self.result = result
        self.calls = 0

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        del target, query
        self.calls += 1
        return self.result

    def query_traffic(self, target: EntityRef, query: InvestigationQuery) -> tuple[Any, ...]:
        del target, query
        return ()


class _Loki:
    def __init__(self, records: list[LogRecord] | None = None) -> None:
        self.records = (
            records
            if records is not None
            else [
                LogRecord(
                    service="worker",
                    at=AT,
                    severity="ERROR",
                    message="worker failed",
                    evidence_id="loki:worker:1",
                )
            ]
        )

    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]:
        del services, starts_at, ends_at, limit
        return self.records


def _append(factory: sessionmaker[Session], run_id: str, marker: int) -> int:
    with factory() as session:
        return InvestigationReadRepository(session).append_success(
            run_id=run_id,
            caller_class="ENGINE",
            capability="resource_pressure",
            query_key="same-query",
            query_descriptor={"provider": "prometheus", "marker": marker},
            started_at=AT,
            finished_at=AT,
            observation={"marker": marker},
            evidence_ids=[f"read:{marker}"],
        )


def test_concurrent_same_run_reads_survive_with_distinct_sequences(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    barrier = Barrier(6)

    def append(marker: int) -> int:
        barrier.wait()
        return _append(factory, "concurrent-run", marker)

    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            read_ids = list(executor.map(append, range(6)))
        assert len(set(read_ids)) == 6
        with factory() as session:
            rows = list(
                session.scalars(
                    select(InvestigationReadRow)
                    .where(InvestigationReadRow.run_id == "concurrent-run")
                    .order_by(InvestigationReadRow.sequence)
                )
            )
        assert [row.sequence for row in rows] == [1, 2, 3, 4, 5, 6]
        assert len({row.read_id for row in rows}) == 6
        assert {row.query_key for row in rows} == {"same-query"}
    finally:
        engine.dispose()


def test_committed_tape_row_is_visible_in_an_independent_session_before_return(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url)
    Base.metadata.create_all(engine)
    result = (
        ResourcePressure(
            pod=TARGET,
            container="worker",
            resource="cpu",
            baseline=0.1,
            peak=0.8,
            at=AT,
            evidence_id="pressure-visible",
        ),
    )
    reader = _Prometheus(result)
    visible_before_return: list[bool] = []
    normal_factory = sessionmaker(engine)

    def observing_session_factory() -> Session:
        session = normal_factory()
        commit = session.commit

        def commit_then_check() -> None:
            commit()
            with Session(engine) as observer:
                visible_before_return.append(
                    observer.scalar(
                        select(InvestigationReadRow.read_id).where(
                            InvestigationReadRow.run_id == "visible-run"
                        )
                    )
                    is not None
                )

        session.commit = commit_then_check  # type: ignore[method-assign]
        return session

    adapter = ProviderAdapter(
        "visible-run", "ENGINE", observing_session_factory, ProviderReaders(prometheus=reader)
    )
    long_identity = "valid-observation-identity-" + ("x" * 392)
    assert len(long_identity) == 419
    try:
        returned = adapter.query_resource_pressure(
            TARGET, QUERY, observation_identity=long_identity
        )
        assert returned is result
        assert reader.calls == 1
        assert visible_before_return == [True]
        with Session(engine) as session:
            row = session.scalar(
                select(InvestigationReadRow).where(InvestigationReadRow.run_id == "visible-run")
            )
        assert row is not None and row.query_key == long_identity
        assert len(row.query_key) == 419
    finally:
        engine.dispose()


def test_committed_error_row_is_visible_before_typed_failure_returns(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    Base.metadata.create_all(engine)

    class FailingPrometheus:
        def query_resource_pressure(
            self, target: EntityRef, query: InvestigationQuery
        ) -> tuple[ResourcePressure, ...]:
            del target, query
            raise ConnectionError("prometheus unavailable")

        def query_traffic(self, target: EntityRef, query: InvestigationQuery) -> tuple[Any, ...]:
            del target, query
            return ()

    factory = sessionmaker(engine)
    observed_commit: list[str | None] = []

    def observing_factory() -> Session:
        session = factory()
        commit = session.commit

        def commit_then_check() -> None:
            commit()
            with Session(engine) as observer:
                row = observer.scalar(
                    select(InvestigationReadRow).where(
                        InvestigationReadRow.run_id == "error-visible-run"
                    )
                )
                observed_commit.append(row.status if row else None)

        session.commit = commit_then_check  # type: ignore[method-assign]
        return session

    adapter = ProviderAdapter(
        "error-visible-run",
        "ENGINE",
        observing_factory,
        ProviderReaders(prometheus=FailingPrometheus()),
    )
    try:
        failure = adapter.query_resource_pressure(TARGET, QUERY)
        from packages.rca.model import ProviderReadFailure

        assert isinstance(failure, ProviderReadFailure)
        assert observed_commit == ["ERROR"]
        with factory() as session:
            row = session.scalar(
                select(InvestigationReadRow).where(
                    InvestigationReadRow.run_id == "error-visible-run"
                )
            )
        assert row is not None and row.status == "ERROR"
        assert row.sequence == 1
        assert row.caller_class == "ENGINE"
        assert row.query_descriptor["operation"] == "resource_pressure"
    finally:
        engine.dispose()


def test_loki_capture_provenance_points_to_committed_capture_read(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="provider capture",
        created_at=AT,
        updated_at=AT,
    )
    with factory() as session:
        IncidentRepository(session).create(incident)

    adapter = ProviderAdapter("capture-run", "CAPTURE", factory, ProviderReaders(loki=_Loki()))
    try:
        capture = capture_error_logs(adapter, ["worker"], AT - timedelta(minutes=30), AT)
        assert len(capture.records) == 1
        assert len(capture.source_read_ids) == 1
        read_id = capture.source_read_ids[0]
        assert read_id is not None

        with factory() as session:
            read = session.get(InvestigationReadRow, read_id)
            assert read is not None
            assert (read.run_id, read.caller_class, read.capability, read.status) == (
                "capture-run",
                "CAPTURE",
                "loki_logs",
                "SUCCESS",
            )
            LogObservationRepository(session).record(
                incident.incident_id,
                list(capture.records),
                AT,
                source_read_ids=capture.source_read_ids,
            )

        with factory() as session:
            log = session.scalar(select(LogObservationRow))
            assert log is not None and log.source_read_id == read_id
            historical = LogObservationRow(
                incident_id=incident.incident_id,
                service="old-worker",
                observed_at=AT,
                severity="ERROR",
                message="legacy row",
                evidence_id="legacy-log",
                dedup_key="legacy-log-key",
                source_system="legacy",
            )
            session.add(historical)
            session.commit()
            assert historical.source_read_id is None

        empty_adapter = ProviderAdapter(
            "empty-capture-run", "CAPTURE", factory, ProviderReaders(loki=_Loki([]))
        )
        empty_capture = capture_error_logs(
            empty_adapter, ["worker"], AT - timedelta(minutes=30), AT
        )
        assert empty_capture.succeeded
        assert empty_capture.records == ()
        assert empty_capture.source_read_ids == ()
        with factory() as session:
            empty_read = session.scalar(
                select(InvestigationReadRow).where(
                    InvestigationReadRow.run_id == "empty-capture-run"
                )
            )
            assert empty_read is not None
            assert empty_read.caller_class == "CAPTURE"
            assert empty_read.status == "SUCCESS"
            assert empty_read.observation == []
            assert (
                session.scalar(
                    select(LogObservationRow.observation_id).where(
                        LogObservationRow.source_read_id == empty_read.read_id
                    )
                )
                is None
            )
    finally:
        engine.dispose()


def test_loki_capture_failure_records_error_without_fabricating_logs(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    Base.metadata.create_all(engine)

    class FailingLoki(_Loki):
        def error_logs(
            self,
            services: Sequence[str],
            starts_at: datetime,
            ends_at: datetime,
            *,
            limit: int | None = None,
        ) -> list[LogRecord]:
            del services, starts_at, ends_at, limit
            raise TimeoutError("loki unavailable")

    factory = sessionmaker(engine)
    adapter = ProviderAdapter(
        "capture-error-run", "CAPTURE", factory, ProviderReaders(loki=FailingLoki())
    )
    try:
        capture = capture_error_logs(adapter, ["worker"], AT - timedelta(minutes=30), AT)
        assert not capture.succeeded
        assert capture.records == ()
        assert capture.source_read_ids == ()
        assert len(capture.failed) == 1
        with factory() as session:
            row = session.scalar(
                select(InvestigationReadRow).where(
                    InvestigationReadRow.run_id == "capture-error-run"
                )
            )
        assert row is not None
        assert (row.caller_class, row.capability, row.status) == (
            "CAPTURE",
            "loki_logs",
            "ERROR",
        )
        assert row.error_type == "TimeoutError"
        assert row.evidence_ids == []
        with factory() as session:
            logs = list(
                session.scalars(
                    select(LogObservationRow).where(LogObservationRow.source_read_id == row.read_id)
                )
            )
        assert logs == []
    finally:
        engine.dispose()
