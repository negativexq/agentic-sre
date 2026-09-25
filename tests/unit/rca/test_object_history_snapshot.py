"""M19-3.5: object history is the journal plus the run's persisted snapshot cycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.live import LiveSource
from packages.rca.model import EntityRef, JournalEntry, Lifecycle

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
WEB = EntityRef(namespace="shop", kind="Deployment", name="web")
API = EntityRef(namespace="shop", kind="Deployment", name="api")


def _deployment(name: str, image: str) -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "shop", "uid": f"uid-{name}"},
        "spec": {"template": {"spec": {"containers": [{"name": "app", "image": image}]}}},
    }


def _journal(*entries: tuple[str, str, float, Lifecycle]) -> list[JournalEntry]:
    return [
        JournalEntry(
            object_key=f"shop/Deployment/{name}",
            observed_at=T0 + timedelta(minutes=minutes),
            body=_deployment(name, image),
            version_id=index,
            lifecycle=lifecycle,
        )
        for index, (name, image, minutes, lifecycle) in enumerate(entries, start=1)
    ]


def _source(
    journal: list[JournalEntry],
    current: list[dict[str, Any]],
    *,
    cycle: int | None = 7,
    live: bool = True,
) -> LiveSource:
    return LiveSource(
        incident="i1",
        alert_items=[],
        journal=journal,
        current_objects=current,
        event_bodies=[],
        observed_at=T0 + timedelta(minutes=30),
        current_is_live=live,
        snapshot_cycle_id=cycle,
        snapshot_observed_at=T0 + timedelta(minutes=29),
    )


def test_changed_snapshot_objects_enter_with_their_persisted_snapshot_id() -> None:
    journal = _journal(("web", "app:1", 0, Lifecycle.OBSERVED))
    history = _source(journal, [_deployment("web", "app:2")]).object_history()

    first, second = history[WEB]
    assert first.evidence_id == "journal:1"
    assert (second.evidence_id, second.lifecycle) == (
        "snapshot:7:shop/Deployment/web",
        Lifecycle.UPDATED,
    )
    assert second.observed_at == T0 + timedelta(minutes=29)  # the cycle's own time
    assert second.uid == "uid-web"


def test_unchanged_snapshot_objects_add_no_version() -> None:
    journal = _journal(("web", "app:1", 0, Lifecycle.OBSERVED))
    history = _source(journal, [_deployment("web", "app:1")]).object_history()
    assert [item.evidence_id for item in history[WEB]] == ["journal:1"]


def test_absence_from_the_snapshot_is_never_a_deletion() -> None:
    journal = _journal(
        ("web", "app:1", 0, Lifecycle.OBSERVED), ("api", "app:1", 0, Lifecycle.OBSERVED)
    )
    history = _source(journal, [_deployment("web", "app:1")]).object_history()

    assert [item.lifecycle for item in history[API]] == [Lifecycle.OBSERVED]
    assert not any(
        item.evidence_id.startswith("cluster:") for items in history.values() for item in items
    )


def test_deletion_comes_only_from_the_journal_tombstone() -> None:
    journal = _journal(
        ("api", "app:1", 0, Lifecycle.OBSERVED), ("api", "app:1", 10, Lifecycle.DELETED)
    )
    history = _source(journal, []).object_history()
    assert [(item.evidence_id, item.lifecycle) for item in history[API]] == [
        ("journal:1", Lifecycle.OBSERVED),
        ("journal:2", Lifecycle.DELETED),
    ]


def test_bodies_outside_a_persisted_cycle_are_not_evidence() -> None:
    journal = _journal(("web", "app:1", 0, Lifecycle.OBSERVED))
    history = _source(journal, [_deployment("web", "app:2")], cycle=None).object_history()
    assert [item.evidence_id for item in history[WEB]] == ["journal:1"]


def test_a_resolved_incident_uses_its_frozen_journal_only() -> None:
    journal = _journal(("web", "app:1", 0, Lifecycle.OBSERVED))
    history = _source(journal, [_deployment("web", "app:2")], live=False).object_history()
    assert [item.evidence_id for item in history[WEB]] == ["journal:1"]
