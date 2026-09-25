"""Bounded retention for the evidence tables; off unless SRE_RETENTION_ENABLED=true.

Three guarantees hold whatever the horizons are:

* Lifecycle evidence is never kept for a shorter time than Events: the policy
  refuses ``lifecycle_horizon < events_horizon``, and a lifecycle row is never
  deleted while a retained Event of the same UID is newer than it.
* A lifecycle evidence id is never issued twice: the row holding an instance's
  highest sequence is never deleted, so the next sequence stays above it.
* Every object keeps its latest journal version, which later windows use as
  their baseline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Delete, delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from packages.storage.models import EventVersionRow, LifecycleObservationRow, ObjectVersionRow


@dataclass(frozen=True)
class RetentionPolicy:
    events_horizon: timedelta
    lifecycle_horizon: timedelta
    objects_horizon: timedelta

    def __post_init__(self) -> None:
        for name in ("events_horizon", "lifecycle_horizon", "objects_horizon"):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"retention {name} must be positive")
        if self.lifecycle_horizon < self.events_horizon:
            raise ValueError(
                "retention lifecycle_horizon must be at least events_horizon: lifecycle "
                "evidence may not be dropped before the Events it explains"
            )


@dataclass(frozen=True)
class RetentionResult:
    events_deleted: int
    lifecycle_deleted: int
    objects_deleted: int


def policy_from_environment() -> RetentionPolicy | None:
    """``None`` unless enabled; an invalid policy fails at startup."""
    if os.getenv("SRE_RETENTION_ENABLED", "").casefold() != "true":
        return None
    return RetentionPolicy(
        events_horizon=timedelta(hours=float(os.getenv("SRE_RETENTION_EVENTS_HOURS", "24"))),
        lifecycle_horizon=timedelta(hours=float(os.getenv("SRE_RETENTION_LIFECYCLE_HOURS", "48"))),
        objects_horizon=timedelta(hours=float(os.getenv("SRE_RETENTION_OBJECTS_HOURS", "168"))),
    )


def _sequence(evidence_id: str) -> int:
    tail = evidence_id.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _deletable_lifecycle(session: Session, cutoff: datetime) -> list[int]:
    candidates = session.scalars(
        select(LifecycleObservationRow).where(LifecycleObservationRow.observed_at < cutoff)
    ).all()
    if not candidates:
        return []
    instances = {(row.namespace, row.kind, row.instance_uid) for row in candidates}
    highest: dict[tuple[str, str, str], int] = {}
    for namespace, kind, uid, evidence_id in session.execute(
        select(
            LifecycleObservationRow.namespace,
            LifecycleObservationRow.kind,
            LifecycleObservationRow.instance_uid,
            LifecycleObservationRow.evidence_id,
        ).where(LifecycleObservationRow.instance_uid.in_({uid for _, _, uid in instances}))
    ):
        key = (namespace, kind, uid)
        highest[key] = max(highest.get(key, 0), _sequence(evidence_id))
    newest_event: dict[str, datetime] = {
        uid: at
        for uid, at in session.execute(
            select(EventVersionRow.involved_uid, func.max(EventVersionRow.event_at))
            .where(EventVersionRow.involved_uid.in_({uid for _, _, uid in instances}))
            .group_by(EventVersionRow.involved_uid)
        )
        if uid is not None and at is not None
    }
    deletable: list[int] = []
    for row in candidates:
        key = (row.namespace, row.kind, row.instance_uid)
        if _sequence(row.evidence_id) >= highest.get(key, 0):
            continue  # keeps the instance's sequence from ever being reissued
        event_at = newest_event.get(row.instance_uid)
        if event_at is not None and event_at > row.observed_at:
            continue  # a retained Event of this UID is newer than this row
        deletable.append(row.observation_id)
    return deletable


def _delete(session: Session, statement: Delete) -> int:
    return cast(CursorResult[Any], session.execute(statement)).rowcount or 0


def apply_retention(session: Session, policy: RetentionPolicy, now: datetime) -> RetentionResult:
    """Delete evidence older than the policy allows, in one transaction."""
    events = _delete(
        session,
        delete(EventVersionRow).where(EventVersionRow.event_at < now - policy.events_horizon),
    )
    # Evaluated after the Event pass, so only retained Events protect lifecycle rows.
    lifecycle_ids = _deletable_lifecycle(session, now - policy.lifecycle_horizon)
    lifecycle = 0
    if lifecycle_ids:
        lifecycle = _delete(
            session,
            delete(LifecycleObservationRow).where(
                LifecycleObservationRow.observation_id.in_(lifecycle_ids)
            ),
        )
    latest = select(func.max(ObjectVersionRow.version_id)).group_by(ObjectVersionRow.object_key)
    objects = _delete(
        session,
        delete(ObjectVersionRow).where(
            ObjectVersionRow.observed_at < now - policy.objects_horizon,
            ObjectVersionRow.version_id.not_in(latest),
        ),
    )
    session.commit()
    return RetentionResult(events, lifecycle, objects)


__all__ = ["RetentionPolicy", "RetentionResult", "apply_retention", "policy_from_environment"]
