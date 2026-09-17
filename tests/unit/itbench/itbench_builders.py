"""Tiny ITBench-Lite snapshot fixtures."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchEvidenceCategory, ITBenchScenario


def _tsv(path: Path, rows: list[tuple[str, dict[str, Any]]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["Timestamp", "Body"])
        for timestamp, body in rows:
            writer.writerow([timestamp, json.dumps(body)])


def snapshot_scenario(tmp_path: Path, scenario_id: str = "Scenario-1") -> ITBenchScenario:
    root = tmp_path / scenario_id
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
        scenario_id=scenario_id,
        snapshot_path=str(root),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files={ITBenchEvidenceCategory.ALERTS: ("alerts/a.json",)},
    )
