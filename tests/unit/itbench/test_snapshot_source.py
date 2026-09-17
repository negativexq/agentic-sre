"""Snapshot files become alerts, object versions, and events for the engine."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from itbench_builders import snapshot_scenario

from packages.evals.itbench.source import SnapshotSource, pod_pressure
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


def test_pod_pressure_compares_usage_before_and_after_the_split(tmp_path: Path) -> None:
    tags = "{'container': 'cart', 'pod': 'cart-1', 'id': '/c1'}"
    rows = ["metric_name\ttimestamp\tvalue\tpod_name\tnamespace\ttags"]

    def add(metric: str, minute: int, value: float) -> None:
        rows.append(f"{metric}\t2025-01-01 12:{minute:02d}:00.000\t{value}\tcart-1\tshop\t{tags}")

    add("cluster:namespace:pod_memory:active:kube_pod_container_resource_limits", 0, 100)
    add("container_memory_cache", 0, 99)  # ignored metric
    for minute, used, periods, throttled in [(0, 40, 0, 0), (5, 45, 200, 10), (15, 97, 400, 110)]:
        add("node_namespace_pod_container:container_memory_working_set_bytes", minute, used)
        add("container_cpu_cfs_periods_total", minute, periods)
        add("container_cpu_cfs_throttled_periods_total", minute, throttled)
    path = tmp_path / "pod_cart-1_raw.tsv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    pod = EntityRef.parse("shop/Pod/cart-1")
    split = datetime(2025, 1, 1, 12, 10, tzinfo=UTC)
    result = {item.resource: item for item in pod_pressure(pod, path, split)}
    assert result["memory"].baseline == 0.45 and result["memory"].peak == 0.97
    assert result["cpu"].baseline == 0.05 and result["cpu"].peak == 0.5
    assert result["memory"].at == datetime(2025, 1, 1, 12, 15, tzinfo=UTC)
