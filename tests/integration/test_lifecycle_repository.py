"""M19-2.2: the append-only lifecycle ledger and the exact-instance index."""

from __future__ import annotations

import inspect as pyinspect
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from packages.storage.models import Base, EntityInstanceRow, LifecycleObservationRow
from packages.storage.repositories import (
    EntityInstanceRepository,
    LifecycleRecord,
    LifecycleRepository,
)

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _session(url: str) -> Iterator[Session]:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    yield from _session(f"sqlite:///{tmp_path}/ledger.db")


def _append(
    repo: LifecycleRepository,
    *,
    namespace: str = "shop",
    instance_uid: str = "uid-a",
    type: str = "READY_TRUE",
    observed_at: datetime = T0,
    source: str = "watch",
    payload: dict[str, object] | None = None,
    source_at: datetime | None = None,
) -> LifecycleRecord | None:
    return repo.append(
        namespace=namespace,
        kind="Pod",
        name="web-0",
        instance_uid=instance_uid,
        type=type,
        observed_at=observed_at,
        source=source,
        payload=payload or {"ready": True},
        source_at=source_at,
    )


def _id(record: LifecycleRecord | None) -> str:
    assert record is not None
    return record.evidence_id


def test_append_assigns_per_instance_evidence_ids_and_lists_in_order(session: Session) -> None:
    repo = LifecycleRepository(session)
    first = _append(repo, type="OBSERVED", observed_at=_at(10), source_at=_at(-60))
    second = _append(repo, type="READY_TRUE", observed_at=_at(5))
    other = _append(repo, instance_uid="uid-b", type="OBSERVED", observed_at=_at(7))

    assert [_id(item) for item in (first, second, other)] == [
        "lifecycle:shop:Pod:uid-a:1",
        "lifecycle:shop:Pod:uid-a:2",
        "lifecycle:shop:Pod:uid-b:1",
    ]
    records = repo.list_for("shop", "Pod", "uid-a")
    assert [(item.type, item.observed_at) for item in records] == [
        ("READY_TRUE", _at(5)),
        ("OBSERVED", _at(10)),
    ]
    assert records[1].source_at == _at(-60)
    assert records[0].ingested_at is not None


def test_same_fact_is_recorded_once(session: Session) -> None:
    repo = LifecycleRepository(session)
    assert _append(repo) is not None
    assert _append(repo, payload={"ready": True, "again": 1}) is None
    assert session.scalar(select(func.count()).select_from(LifecycleObservationRow)) == 1
    # A different source or time is a different fact.
    assert _append(repo, source="snapshot") is not None
    assert _append(repo, observed_at=_at(1)) is not None


def test_list_window_filters_namespace_and_bounds(session: Session) -> None:
    repo = LifecycleRepository(session)
    _append(repo, observed_at=_at(-1))
    _append(repo, observed_at=_at(0), type="OBSERVED")
    _append(repo, observed_at=_at(30))
    _append(repo, observed_at=_at(31), type="OBSERVED")
    _append(repo, namespace="other", observed_at=_at(10))

    window = repo.list_window({"shop"}, _at(0), _at(30))
    assert [(item.namespace, item.observed_at) for item in window] == [
        ("shop", _at(0)),
        ("shop", _at(30)),
    ]


def test_append_rejects_unknown_types_and_missing_uid(session: Session) -> None:
    repo = LifecycleRepository(session)
    with pytest.raises(ValueError, match="unknown lifecycle observation type"):
        _append(repo, type="CREATED")
    with pytest.raises(ValueError, match="exact instance uid"):
        _append(repo, instance_uid="")


def test_lifecycle_repository_has_no_update_or_delete_api() -> None:
    public = {
        name
        for name, _ in pyinspect.getmembers(LifecycleRepository, pyinspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {"append", "list_for", "list_window"}
    source = pyinspect.getsource(LifecycleRepository)
    assert not re.search(
        r"\bupdate\(|\bdelete\(|\.delete\b|merge\(|TRUNCATE|UPDATE |DELETE ", source
    )


def test_entity_instance_upsert_keeps_one_row_per_uid(session: Session) -> None:
    repo = EntityInstanceRepository(session)
    repo.upsert(namespace="shop", kind="Pod", name="web-0", uid="uid-a", observed_at=_at(10))
    repo.upsert(
        namespace="shop",
        kind="Pod",
        name="web-0",
        uid="uid-a",
        observed_at=_at(40),
        owner_kind="StatefulSet",
        owner_name="web",
        owner_uid="sts-1",
        created_at=_at(-100),
    )
    repo.upsert(
        namespace="shop", kind="Pod", name="web-0", uid="uid-a", observed_at=_at(50), deleted=True
    )
    repo.upsert(
        namespace="shop", kind="Pod", name="web-0", uid="uid-a", observed_at=_at(60), deleted=True
    )
    repo.upsert(namespace="shop", kind="Pod", name="web-0", uid="uid-b", observed_at=_at(70))

    rows = {row.uid: row for row in session.scalars(select(EntityInstanceRow)).all()}
    assert set(rows) == {"uid-a", "uid-b"}
    a = rows["uid-a"]
    assert (a.first_observed_at, a.last_observed_at) == (_at(10), _at(60))
    assert (a.owner_kind, a.owner_name, a.owner_uid) == ("StatefulSet", "web", "sts-1")
    assert a.created_at == _at(-100)
    assert a.deleted_observed_at == _at(50)
    assert rows["uid-b"].deleted_observed_at is None
    with pytest.raises(ValueError, match="exact uid"):
        repo.upsert(namespace="shop", kind="Pod", name="x", uid="", observed_at=_at(0))


@pytest.mark.postgres
def test_append_and_dedupe_on_postgres(postgres_url: str) -> None:
    for session in _session(postgres_url):
        repo = LifecycleRepository(session)
        assert _id(_append(repo)) == "lifecycle:shop:Pod:uid-a:1"
        assert _append(repo) is None
        assert _id(_append(repo, type="OBSERVED")) == "lifecycle:shop:Pod:uid-a:2"
        assert [item.type for item in repo.list_for("shop", "Pod", "uid-a")] == [
            "READY_TRUE",
            "OBSERVED",
        ]
