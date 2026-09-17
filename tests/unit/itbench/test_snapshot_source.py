"""Snapshot files become alerts, object versions, and events for the engine."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchEvidenceCategory, ITBenchScenario
from packages.evals.itbench.source import SnapshotSource
from packages.rca.engine import diagnose
from packages.rca.model import Confidence, EntityRef


def _tsv(path: Path, rows: list[tuple[str, dict[str, Any]]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["Timestamp", "Body"])
        for timestamp, body in rows:
            writer.writerow([timestamp, json.dumps(body)])


def _scenario(tmp_path: Path) -> ITBenchScenario:
    root = tmp_path / "Scenario-1"
    (root / "alerts").mkdir(parents=True)
    (root / "alerts" / "a.json").write_text(
        json.dumps(
            {
                "data": {
                    "alerts": [
                        {
                            "state": "firing",
                            "activeAt": "2025-01-01T12:12:00.123456789Z",
                            "labels": {
                                "alertname": "RequestErrorRate",
                                "service_name": "checkout",
                                "namespace": "shop",
                            },
                        },
                        {"state": "pending", "labels": {"alertname": "Ignored"}},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    deployment: dict[str, Any] = {
        "kind": "Deployment",
        "metadata": {"name": "checkout", "namespace": "shop", "resourceVersion": "1"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "c", "envFrom": [{"configMapRef": {"name": "flags"}}]}]
                }
            }
        },
    }
    config_old: dict[str, Any] = {
        "kind": "ConfigMap",
        "metadata": {"name": "flags", "namespace": "shop"},
        "data": {"f": "off"},
    }
    config_new = {**config_old, "data": {"f": "on"}}
    status_only: dict[str, Any] = {
        **deployment,
        "metadata": {**deployment["metadata"], "resourceVersion": "2"},
        "status": {"x": 1},
    }
    _tsv(
        root / "k8s_objects_raw.tsv",
        [
            ("2025-01-01 12:00:00.000000001", deployment),
            ("2025-01-01 12:00:00.000000001", config_old),
            ("2025-01-01 12:10:00.000000001", status_only),
            ("2025-01-01 12:10:00.000000001", config_new),
        ],
    )
    event: dict[str, Any] = {
        "type": "ADDED",
        "object": {
            "metadata": {"uid": "u1"},
            "involvedObject": {"kind": "Pod", "name": "checkout-1", "namespace": "shop"},
            "reason": "BackOff",
            "type": "Warning",
            "count": 2,
            "lastTimestamp": "2025-01-01T12:11:00Z",
            "message": "back-off restarting",
        },
    }
    _tsv(
        root / "k8s_events_raw.tsv",
        [("2025-01-01 12:11:00", event), ("2025-01-01 12:11:01", event)],
    )
    (root / "otel_logs_raw.tsv").write_text(
        "Timestamp\tServiceName\tSeverityText\tBody\n"
        "2025-01-01 12:11:00\tcheckout\tERROR\tpayment declined\n"
        "2025-01-01 12:11:00\tcheckout\tINFO\tok\n",
        encoding="utf-8",
    )
    return ITBenchScenario(
        scenario_id="Scenario-1",
        snapshot_path=str(root),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files={ITBenchEvidenceCategory.ALERTS: ("alerts/a.json",)},
    )


def test_snapshot_source_reads_firing_alerts_versions_and_deduplicated_events(
    tmp_path: Path,
) -> None:
    source = SnapshotSource(_scenario(tmp_path))
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
    diagnosis = diagnose(SnapshotSource(_scenario(tmp_path)))
    assert diagnosis.root_cause == EntityRef.parse("shop/ConfigMap/flags")
    assert diagnosis.confidence is Confidence.VERIFIED
