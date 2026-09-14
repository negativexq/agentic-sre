#!/usr/bin/env python3
"""Run the zero-model ITBench-Lite adapter qualification."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBENCH_SOURCE,
    ITBENCH_SRE_VERSION,
    ITBenchLiteDataset,
    ITBenchScenarioQualification,
    ITBenchSnapshotBackend,
    ITBenchSnapshotToolRegistry,
    atomic_json_write,
    build_observable_incident,
    entities_from_k8s_records,
)

OFFICIAL_EVALUATOR_REVISION = "14f026fc9cc348c4ecec5ab32714de954c95c1b1"
REPORT_PATH = Path("docs/benchmarks/itbench-lite-adapter-qualification.json")


def qualify(root: Path) -> dict[str, Any]:
    dataset = ITBenchLiteDataset.open(root)
    scenarios = dataset.scenarios()
    records: list[ITBenchScenarioQualification] = []
    root_kinds: Counter[str] = Counter()
    for scenario in scenarios:
        backend = ITBenchSnapshotBackend(dataset, scenario)
        data = dataset.load_investigator_data(scenario)
        registry = ITBenchSnapshotToolRegistry(backend)
        context = registry.public_context()
        if "ground_truth" in context.casefold() or "fault_mechanism" in context.casefold():
            raise ValueError(
                f"ground-truth terminology leaked into context: {scenario.scenario_id}"
            )
        incident, alerts = build_observable_incident(backend)
        if incident.incident_id is None or not alerts:
            raise ValueError(f"observable incident construction failed: {scenario.scenario_id}")
        ground_truth = dataset.load_ground_truth(scenario.scenario_id)
        for group in ground_truth.root_cause_groups:
            if group.root_cause:
                root_kinds[group.kind] += 1
        counts = {
            "alerts": len(data.alerts),
            "metrics": len(data.metrics),
            "k8s_events": len(data.k8s_events),
            "k8s_objects": len(data.k8s_objects),
            "logs": len(data.logs),
            "traces": len(data.traces),
        }
        records.append(
            ITBenchScenarioQualification(
                scenario_id=scenario.scenario_id,
                loaded=True,
                ground_truth_loaded_separately=True,
                evidence_category_counts=counts,
                generated_evidence_ids=(
                    len(data.alerts)
                    + len(data.metrics)
                    + len(data.k8s_events)
                    + len(data.k8s_objects)
                    + len(data.logs)
                    + len(data.traces)
                ),
            )
        )
        _ = registry.invoke("itbench_alerts", {"limit": 1})
        _ = registry.invoke("itbench_metrics", {"limit": 1})
        _ = registry.invoke("itbench_kubernetes_events", {"limit": 1})
        _ = registry.invoke("itbench_kubernetes_objects", {"limit": 1})
        _ = registry.invoke("itbench_logs", {"limit": 1})
        _ = registry.invoke("itbench_traces", {"limit": 1})
        entities_from_k8s_records(data.k8s_objects)
    if tuple(item.scenario_id for item in records) != ITBENCH_SCENARIO_IDS:
        raise ValueError("scenario order or count does not match pinned set")
    payload: dict[str, Any] = {
        "artifact_type": "ITBENCH_LITE_ADAPTER_QUALIFICATION",
        "status": "PASS",
        "benchmark": "ITBench-Lite",
        "source": ITBENCH_SOURCE,
        "revision": ITBENCH_DATASET_REVISION,
        "sre_version": ITBENCH_SRE_VERSION,
        "scenario_count": len(records),
        "scenario_passes": len(records),
        "scenario_failures": 0,
        "official_evaluator_revision": OFFICIAL_EVALUATOR_REVISION,
        "openai_calls": 0,
        "network_calls": 0,
        "investigator_ground_truth_access": False,
        "root_cause_entity_kind_counts": dict(sorted(root_kinds.items())),
        "scenarios": [item.model_dump(mode="json") for item in records],
        "note": "E0 adapter qualification only; no provider or official judge evaluation was run.",
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    args = parser.parse_args()
    payload = qualify(args.root)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_json_write(args.report, payload)
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    print(
        f"ITBench-Lite adapter qualification: PASS ({payload['scenario_passes']}/35, 0 OpenAI calls)"
    )
    print(f"qualification_sha256={digest}")


if __name__ == "__main__":
    main()
