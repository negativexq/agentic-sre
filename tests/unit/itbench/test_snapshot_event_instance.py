"""M20.5 P2a-bis: snapshot events carry the exact involved instance, like the live journal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from itbench_builders import _tsv, snapshot_scenario

from packages.evals.itbench.source import SnapshotSource
from packages.rca.model import EntityInstanceRef, EntityRef, FindingKind
from packages.rca.signals import failure_findings

POD = EntityRef.parse("shop/Pod/cart-0")


def _event(event_uid: str, involved_uid: str | None, count: int = 1) -> dict[str, Any]:
    involved: dict[str, Any] = {"kind": "Pod", "name": "cart-0", "namespace": "shop"}
    if involved_uid is not None:
        involved["uid"] = involved_uid
    return {
        "type": "ADDED",
        "object": {
            "metadata": {"uid": event_uid},
            "involvedObject": involved,
            "reason": "BackOff",
            "type": "Warning",
            "count": count,
            "lastTimestamp": "2025-01-01T12:11:00Z",
            "message": "back-off restarting",
        },
    }


def _events(tmp_path: Path, rows: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    scenario = snapshot_scenario(tmp_path)
    _tsv(Path(scenario.snapshot_path) / "k8s_events_raw.tsv", rows)
    return list(SnapshotSource(scenario).events())


def test_the_involved_uid_is_carried_when_the_source_has_it(tmp_path: Path) -> None:
    (event,) = _events(tmp_path, [("2025-01-01 12:11:00", _event("e1", "uid-A"))])
    assert event.involved_uid == "uid-A"


def test_a_missing_involved_uid_is_not_invented(tmp_path: Path) -> None:
    (event,) = _events(tmp_path, [("2025-01-01 12:11:00", _event("e1", None))])
    assert event.involved_uid is None


def test_event_deduplication_is_unchanged(tmp_path: Path) -> None:
    events = _events(
        tmp_path,
        [
            ("2025-01-01 12:11:00", _event("e1", "uid-A")),
            ("2025-01-01 12:11:01", _event("e1", "uid-A")),
            # A coalesced repeat (Kubernetes bumps count) is kept as its own record.
            ("2025-01-01 12:11:30", _event("e1", "uid-A", count=2)),
            ("2025-01-01 12:12:00", _event("e2", "uid-B")),
        ],
    )
    assert [(event.involved_uid, event.count) for event in events] == [
        ("uid-A", 1),
        ("uid-A", 2),
        ("uid-B", 1),
    ]


def test_a_failure_finding_names_the_exact_instance(tmp_path: Path) -> None:
    events = _events(
        tmp_path,
        [
            ("2025-01-01 12:11:00", _event("e1", "uid-A", count=3)),
            ("2025-01-01 12:12:00", _event("e2", None, count=3)),
        ],
    )
    findings = [
        finding for finding in failure_findings(events) if finding.kind is FindingKind.FAILURE_EVENT
    ]
    assert {finding.entity_instance for finding in findings} == {
        EntityInstanceRef(entity=POD, uid="uid-A"),
        None,
    }
