"""M19-2.7: bounded retention that never drops lifecycle evidence before its Events."""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.diagnosis import DiagnosisService
from packages.storage.models import (
    Base,
    EventVersionRow,
    LifecycleObservationRow,
    ObjectVersionRow,
)
from packages.storage.repositories import LifecycleRepository
from packages.storage.retention import RetentionPolicy, apply_retention, policy_from_environment

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
POLICY = RetentionPolicy(
    events_horizon=10 * HOUR, lifecycle_horizon=20 * HOUR, objects_horizon=30 * HOUR
)


def _factory(url: str) -> sessionmaker[Session]:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    made = _factory(f"sqlite:///{tmp_path / 'retention.db'}")
    yield made
    made.kw["bind"].dispose()


def _event(session: Session, uid: str, at: datetime, index: int) -> None:
    session.add(
        EventVersionRow(
            namespace="shop",
            involved_kind="Pod",
            involved_name=f"pod-{uid}",
            involved_uid=uid,
            dedup_key=f"{uid}|{index}",
            event_at=at,
            observed_at=at,
            body={"reason": "Unhealthy"},
        )
    )
    session.commit()


def _lifecycle(session: Session, uid: str, at: datetime, type_: str = "STATUS_SNAPSHOT") -> str:
    record = LifecycleRepository(session).append(
        namespace="shop",
        kind="Pod",
        name=f"pod-{uid}",
        instance_uid=uid,
        type=type_,
        observed_at=at,
        source="collector",
        payload={},
    )
    assert record is not None
    return record.evidence_id


def _object(session: Session, key: str, at: datetime) -> int:
    namespace, kind, name = key.split("/")
    row = ObjectVersionRow(
        object_key=key,
        namespace=namespace,
        kind=kind,
        name=name,
        observed_at=at,
        content_hash=str(at.timestamp()),
        body={"kind": kind, "metadata": {"name": name}},
    )
    session.add(row)
    session.commit()
    return row.version_id


def test_policy_refuses_lifecycle_shorter_than_events_and_non_positive_horizons() -> None:
    with pytest.raises(ValueError, match="lifecycle_horizon must be at least events_horizon"):
        RetentionPolicy(events_horizon=2 * HOUR, lifecycle_horizon=HOUR, objects_horizon=HOUR)
    with pytest.raises(ValueError, match="must be positive"):
        RetentionPolicy(events_horizon=timedelta(0), lifecycle_horizon=HOUR, objects_horizon=HOUR)
    equal = RetentionPolicy(events_horizon=HOUR, lifecycle_horizon=HOUR, objects_horizon=HOUR)
    assert equal.lifecycle_horizon == equal.events_horizon


def test_retention_is_off_unless_enabled_and_invalid_config_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SRE_RETENTION_ENABLED", raising=False)
    assert policy_from_environment() is None
    monkeypatch.setenv("SRE_RETENTION_ENABLED", "true")
    policy = policy_from_environment()
    assert policy is not None and policy.lifecycle_horizon >= policy.events_horizon
    monkeypatch.setenv("SRE_RETENTION_EVENTS_HOURS", "48")
    monkeypatch.setenv("SRE_RETENTION_LIFECYCLE_HOURS", "24")
    with pytest.raises(ValueError, match="lifecycle_horizon"):
        policy_from_environment()


def test_retention_rules_on_a_known_world(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        # uid-a: old events only; its old lifecycle rows may go, except the highest seq.
        _event(session, "uid-a", NOW - 15 * HOUR, 1)
        a1 = _lifecycle(session, "uid-a", NOW - 25 * HOUR)
        a2 = _lifecycle(session, "uid-a", NOW - 24 * HOUR, "READY_TRUE")
        a3 = _lifecycle(session, "uid-a", NOW - 23 * HOUR)
        # uid-b: a retained Event newer than its old lifecycle rows protects them.
        _event(session, "uid-b", NOW - 5 * HOUR, 2)
        b1 = _lifecycle(session, "uid-b", NOW - 30 * HOUR)
        b2 = _lifecycle(session, "uid-b", NOW - 1 * HOUR)
        old_key = _object(session, "shop/Pod/x", NOW - 40 * HOUR)
        last_key = _object(session, "shop/Pod/x", NOW - 35 * HOUR)
        only = _object(session, "shop/Pod/y", NOW - 50 * HOUR)

        result = apply_retention(session, POLICY, NOW)

        lifecycle = set(session.scalars(select(LifecycleObservationRow.evidence_id)).all())
        events = session.scalars(select(EventVersionRow.involved_uid)).all()
        objects = set(session.scalars(select(ObjectVersionRow.version_id)).all())
    assert events == ["uid-b"]
    assert lifecycle == {a3, b1, b2}
    assert {a1, a2}.isdisjoint(lifecycle)
    assert objects == {last_key, only}
    assert old_key not in objects
    assert (result.events_deleted, result.lifecycle_deleted, result.objects_deleted) == (1, 2, 1)


def test_sequence_numbers_are_never_reissued_after_retention(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        for hours in (30, 29, 28):
            _lifecycle(session, "uid-a", NOW - hours * HOUR)
        apply_retention(session, POLICY, NOW)
        assert session.scalars(select(LifecycleObservationRow.evidence_id)).all() == [
            "lifecycle:shop:Pod:uid-a:3"
        ]
        assert _lifecycle(session, "uid-a", NOW) == "lifecycle:shop:Pod:uid-a:4"


def test_service_applies_retention_only_when_configured(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        _event(session, "uid-a", NOW - 15 * HOUR, 1)

    def service(policy: RetentionPolicy | None) -> DiagnosisService:
        return DiagnosisService(
            session_factory=factory,
            namespaces=("shop",),
            clock=lambda: NOW,
            retention_policy=policy,
        )

    service(None).apply_retention()
    with factory() as session:
        assert len(session.scalars(select(EventVersionRow)).all()) == 1
    service(POLICY).apply_retention()
    with factory() as session:
        assert session.scalars(select(EventVersionRow)).all() == []


@dataclass(frozen=True)
class _Row:
    evidence_id: str
    uid: str
    observed_at: datetime


def _sequence(evidence_id: str) -> int:
    return int(evidence_id.rsplit(":", 1)[-1])


@pytest.mark.parametrize("seed", range(60))
def test_lifecycle_evidence_outlives_its_events_on_random_worlds(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    events_h = rng.randint(1, 30)
    policy = RetentionPolicy(
        events_horizon=events_h * HOUR,
        lifecycle_horizon=(events_h + rng.randint(0, 30)) * HOUR,
        objects_horizon=rng.randint(1, 60) * HOUR,
    )
    factory = _factory(f"sqlite:///{tmp_path / f'world-{seed}.db'}")
    uids = [f"uid-{n}" for n in range(rng.randint(1, 4))]
    rows: list[_Row] = []
    events: list[tuple[str, datetime]] = []
    with factory() as session:
        for index in range(rng.randint(0, 12)):
            uid, at = rng.choice(uids), NOW - rng.randint(0, 80) * HOUR
            _event(session, uid, at, index)
            events.append((uid, at))
        for _ in range(rng.randint(1, 15)):
            uid, at = rng.choice(uids), NOW - rng.randint(0, 80) * HOUR
            if any(r.uid == uid and r.observed_at == at for r in rows):
                continue
            rows.append(_Row(_lifecycle(session, uid, at), uid, at))
        apply_retention(session, policy, NOW)
        kept = set(session.scalars(select(LifecycleObservationRow.evidence_id)).all())
        kept_events = [
            (uid, at.replace(tzinfo=UTC) if at.tzinfo is None else at)
            for uid, at in session.execute(
                select(EventVersionRow.involved_uid, EventVersionRow.event_at)
            ).all()
        ]
    factory.kw["bind"].dispose()

    for row in rows:
        if row.evidence_id in kept:
            continue
        # Only rows older than the lifecycle horizon are ever removed ...
        assert row.observed_at < NOW - policy.lifecycle_horizon
        # ... never one that a retained Event of the same UID is newer than ...
        assert not any(uid == row.uid and at > row.observed_at for uid, at in kept_events)
        # ... and never an instance's highest sequence.
        assert _sequence(row.evidence_id) < max(
            _sequence(other.evidence_id) for other in rows if other.uid == row.uid
        )
    # Events are removed exactly by their own horizon.
    expected_events = sorted((uid, at) for uid, at in events if at >= NOW - policy.events_horizon)
    assert sorted(kept_events) == expected_events
