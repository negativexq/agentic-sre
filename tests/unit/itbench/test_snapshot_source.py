"""Snapshot files become alerts, object versions, and events for the engine."""

from __future__ import annotations

from pathlib import Path

from itbench_builders import snapshot_scenario

from packages.evals.itbench.source import SnapshotSource
from packages.rca.engine import diagnose
from packages.rca.model import Confidence, EntityRef


def test_snapshot_source_reads_firing_alerts_versions_and_deduplicated_events(
    tmp_path: Path,
) -> None:
    source = SnapshotSource(snapshot_scenario(tmp_path))
    alerts = source.alerts()
    assert [(a.name, a.service, a.namespace) for a in alerts] == [
        ("RequestErrorRate", "checkout", "shop")
    ]
    history = source.object_history()
    assert len(history[EntityRef.parse("shop/Deployment/checkout")]) == 1
    assert len(history[EntityRef.parse("shop/ConfigMap/flags")]) == 2
    events = source.events()
    assert len(events) == 1 and events[0].count == 2 and events[0].type == "Warning"
    assert [line["body"] for line in source.logs("checkout")] == ["payment declined"]


def test_snapshot_incident_is_diagnosed_from_the_config_change(tmp_path: Path) -> None:
    diagnosis = diagnose(SnapshotSource(snapshot_scenario(tmp_path)))
    assert diagnosis.root_cause == EntityRef.parse("shop/ConfigMap/flags")
    assert diagnosis.confidence is Confidence.VERIFIED
